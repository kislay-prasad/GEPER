"""
Phase 1 Genome Analysis: reference metadata, contig normalisation, the pure
variant-to-chromosome aggregation and the patient allow-list projection. No
database, no network.
"""

import ast
import json
import pathlib

import pytest

from clinical.genome_analysis import (
    CANONICAL_CHROMOSOMES,
    LIMITATIONS,
    PATIENT_WITHHELD_NOTICE,
    patient_projection,
    REFERENCE_SOURCES,
    SUPPORTED_ASSEMBLIES,
    canonical_assembly,
    chromosomes_for,
    get_chromosome,
    normalize_contig,
    summarise_run_document,
)
from clinical.genome_analysis import summary as summary_module


def _doc(variants, assembly="GRCh38", complete=True, **extra):
    return {
        "assembly": assembly,
        "run_complete": complete,
        "geper_version": "1.0.0",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "input_vcf": "/srv/secret/path/sample.vcf",
        "variants": [
            {
                "variant": {"chrom": c, "pos": p, "ref": r, "alt": a, "variant_type": "SNV"},
                "acmg": {"class": "Pathogenic"},
            }
            for c, p, r, a in variants
        ],
        **extra,
    }


class TestReference:
    @pytest.mark.parametrize("assembly", SUPPORTED_ASSEMBLIES)
    def test_25_primary_sequences_in_order(self, assembly):
        names = [c.name for c in chromosomes_for(assembly)]
        assert names == list(CANONICAL_CHROMOSOMES)
        assert len(names) == 25 and names[:22] == [str(i) for i in range(1, 23)] and names[22:] == ["X", "Y", "MT"]

    @pytest.mark.parametrize(
        "assembly,name,length",
        [
            ("GRCh37", "1", 249250621),
            ("GRCh38", "1", 248956422),
            ("GRCh37", "X", 155270560),
            ("GRCh38", "X", 156040895),
            ("GRCh37", "Y", 59373566),
            ("GRCh38", "Y", 57227415),
            ("GRCh37", "MT", 16569),
            ("GRCh38", "MT", 16569),
        ],
    )
    def test_known_lengths(self, assembly, name, length):
        assert get_chromosome(assembly, name).length_bp == length

    def test_builds_differ_and_carry_their_own_accessions(self):
        assert get_chromosome("GRCh37", "1").refseq_accession == "NC_000001.10"
        assert get_chromosome("GRCh38", "1").refseq_accession == "NC_000001.11"
        for name in CANONICAL_CHROMOSOMES:
            if name != "MT":
                assert get_chromosome("GRCh37", name).length_bp != get_chromosome("GRCh38", name).length_bp

    def test_kinds(self):
        assert get_chromosome("GRCh38", "7").kind == "autosome"
        assert get_chromosome("GRCh38", "X").kind == "sex_chromosome"
        assert get_chromosome("GRCh38", "MT").kind == "mitochondrial"

    def test_unsupported_assembly_raises_rather_than_defaulting(self):
        with pytest.raises(ValueError):
            chromosomes_for("T2T-CHM13")
        with pytest.raises(ValueError):
            get_chromosome("hg38", "1")  # non-canonical spelling must be canonicalised first

    def test_source_provenance_is_recorded_for_both_builds(self):
        for assembly in SUPPORTED_ASSEMBLIES:
            src = REFERENCE_SOURCES[assembly]
            assert src["refseq_assembly"].startswith("GCF_") and len(src["report_sha256"]) == 64

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("GRCh37", "GRCh37"),
            ("hg19", "GRCh37"),
            ("b37", "GRCh37"),
            ("GRCh38", "GRCh38"),
            ("hg38", "GRCh38"),
            (" GRCh-38 ", "GRCh38"),
            ("grch38", "GRCh38"),
        ],
    )
    def test_assembly_aliases(self, value, expected):
        assert canonical_assembly(value) == expected

    @pytest.mark.parametrize(
        "value", [None, "", "37", "GRCh39", "T2T-CHM13", "hg38_decoy_37", "build 38", 38, ["GRCh38"]]
    )
    def test_unrecognised_assembly_is_none_not_a_guess(self, value):
        assert canonical_assembly(value) is None


class TestNormalizeContig:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1", "1"),
            ("chr1", "1"),
            ("CHR22", "22"),
            ("chrX", "X"),
            ("x", "X"),
            ("chrY", "Y"),
            ("M", "MT"),
            ("MT", "MT"),
            ("chrM", "MT"),
            ("chrMT", "MT"),
            ("mt", "MT"),
            (" chr7 ", "7"),
        ],
    )
    def test_canonical(self, raw, expected):
        assert normalize_contig(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "chr",
            "0",
            "chr0",
            "23",
            "chr23",
            "01",
            "chr01",
            "+1",
            "1.0",
            "chrUn_gl000220",
            "GL000220.1",
            "hs37d5",
            "chr1_KI270706v1_random",
            "chr6_GL000250v2_alt",
            "NC_000001.11",
            "MTX",
            "chrchr1",
            "X Y",
            None,
            1,
            1.5,
            b"1",
            ["1"],
        ],
    )
    def test_everything_else_is_none(self, raw):
        assert normalize_contig(raw) is None

    def test_idempotent(self):
        for name in CANONICAL_CHROMOSOMES:
            assert normalize_contig(name) == name == normalize_contig(normalize_contig("chr" + name))


class TestAggregation:
    def test_maps_variants_to_chromosomes_with_build_and_sorted(self):
        s = summarise_run_document(
            _doc([("chr17", 7674220, "C", "T"), ("17", 100, "A", "G"), ("chrX", 5000, "G", "A")])
        )
        assert s.status == "ok" and s.assembly == "GRCh38"
        c17 = s.chromosome("17")
        assert c17.status == "variants_present" and c17.variant_count == 2
        assert [v["pos"] for v in c17.variants] == [100, 7674220]
        assert c17.variants[0]["variant_key"] == "17:100:A>G"
        assert s.chromosome("X").variant_count == 1
        assert len(s.chromosomes) == 25

    def test_not_assessed_is_distinct_from_no_findings(self):
        complete = summarise_run_document(_doc([("1", 10, "A", "T")], complete=True))
        partial = summarise_run_document(_doc([("1", 10, "A", "T")], complete=False))
        assert complete.chromosome("2").status == "no_variants_reported"
        assert partial.chromosome("2").status == "not_assessed"
        assert partial.chromosome("1").status == "variants_present"
        assert any("did not complete" in w for w in partial.warnings)

    def test_missing_run_complete_means_not_assessed(self):
        doc = _doc([])
        del doc["run_complete"]
        assert summarise_run_document(doc).chromosome("3").status == "not_assessed"

    def test_empty_complete_run_is_no_findings_everywhere_but_carries_the_caveat(self):
        s = summarise_run_document(_doc([]))
        assert {c.status for c in s.chromosomes} == {"no_variants_reported"}
        assert any("not evidence that it is normal" in text for text in s.to_dict()["limitations"])

    @pytest.mark.parametrize("assembly", [None, "", "GRCh39", "T2T", 38, "hg38_or_37"])
    def test_build_is_mandatory(self, assembly):
        s = summarise_run_document(_doc([("1", 10, "A", "T")], assembly=assembly))
        assert s.status == "assembly_unavailable" and s.assembly is None and s.chromosomes == ()
        assert s.to_dict()["evidence"]["small_variants"]["status"] == "not_assessed"

    def test_same_position_not_mixed_across_builds(self):
        a = summarise_run_document(_doc([("1", 249_000_000, "A", "T")], assembly="GRCh37"))
        b = summarise_run_document(_doc([("1", 249_000_000, "A", "T")], assembly="GRCh38"))
        assert a.chromosome("1").variant_count == 1 and a.assembly == "GRCh37"
        assert b.chromosome("1").variant_count == 0 and b.invalid_variant_count == 1  # beyond chr1 end in GRCh38
        assert b.warnings and "GRCh38" in b.warnings[-1]

    def test_non_primary_contigs_are_counted_not_dropped(self):
        s = summarise_run_document(_doc([("GL000220.1", 5, "A", "T"), ("chrUn_x", 5, "A", "T"), ("2", 5, "A", "T")]))
        assert s.unmapped_variant_count == 2 and s.invalid_variant_count == 0
        assert any("2 variant(s) lie on contigs" in w for w in s.warnings)
        assert sum(c.variant_count for c in s.chromosomes) == 1

    @pytest.mark.parametrize(
        "variant",
        [
            {"chrom": "1", "pos": 0, "ref": "A", "alt": "T"},
            {"chrom": "1", "pos": -4, "ref": "A", "alt": "T"},
            {"chrom": "1", "pos": "12", "ref": "A", "alt": "T"},
            {"chrom": "1", "pos": True, "ref": "A", "alt": "T"},
            {"chrom": "1", "pos": 1.0, "ref": "A", "alt": "T"},
            {"chrom": "1", "pos": 5, "ref": "", "alt": "T"},
            {"chrom": "1", "pos": 5, "ref": "A", "alt": None},
            {"chrom": "", "pos": 5, "ref": "A", "alt": "T"},
            {"chrom": None, "pos": 5, "ref": "A", "alt": "T"},
            {"pos": 5, "ref": "A", "alt": "T"},
            "nonsense",
            None,
        ],
    )
    def test_malformed_variants_are_counted_invalid(self, variant):
        doc = _doc([])
        doc["variants"] = [{"variant": variant}]
        s = summarise_run_document(doc)
        assert s.invalid_variant_count == 1 and sum(c.variant_count for c in s.chromosomes) == 0

    def test_entry_without_variant_mapping_is_invalid(self):
        doc = _doc([])
        doc["variants"] = [{}, "x", 3, None]
        assert summarise_run_document(doc).invalid_variant_count == 4

    def test_position_bounds_use_the_chromosome_length(self):
        end = get_chromosome("GRCh38", "MT").length_bp
        s = summarise_run_document(_doc([("chrM", end, "A", "T"), ("chrM", end + 1, "A", "T")]))
        assert s.chromosome("MT").variant_count == 1 and s.invalid_variant_count == 1

    @pytest.mark.parametrize(
        "document",
        [
            None,
            [],
            "x",
            5,
            {"assembly": "GRCh38"},
            {"assembly": "GRCh38", "variants": {}},
            {"assembly": "GRCh38", "variants": None},
        ],
    )
    def test_malformed_documents_never_raise(self, document):
        s = summarise_run_document(document)
        assert s.status in ("document_unreadable", "assembly_unavailable") and s.chromosomes == ()

    def test_purged_empty_document_is_unusable_not_clean(self):
        s = summarise_run_document({})
        assert s.status == "assembly_unavailable" and s.to_dict()["chromosomes"] == []

    def test_variant_type_is_whitelisted(self):
        doc = _doc([("1", 5, "A", "T")])
        doc["variants"][0]["variant"]["variant_type"] = "<script>"
        assert summarise_run_document(doc).chromosome("1").variants[0]["variant_type"] is None


class TestScientificGuards:
    def test_karyotype_cnv_sv_are_always_not_assessed(self):
        for doc in (_doc([("X", 100, "A", "T"), ("Y", 100, "A", "T")]), _doc([]), {}):
            ev = summarise_run_document(doc).to_dict()["evidence"]
            for key in ("copy_number_variants", "structural_variants", "karyotype"):
                assert ev[key]["status"] == "not_assessed"

    def test_output_never_contains_karyotype_or_sex_inference(self):
        s = summarise_run_document(_doc([("X", 100 + i, "A", "T") for i in range(50)] + [("Y", 7, "A", "T")]))
        text = json.dumps(s.to_dict()) + json.dumps(s.chromosome_to_dict("X"))
        for forbidden in ("46,XX", "46,XY", "XX", "XY", "male", "female", "karyotype_result"):
            assert forbidden not in text.replace("Y ", "")  # 'XY'/'XX' absent; limitation prose says "X or Y"
        assert any("karyotype" in lim.lower() and "cannot be derived" in lim for lim in LIMITATIONS)
        assert any("sex" in lim.lower() and "not inferred" in lim for lim in LIMITATIONS)

    def test_x_and_y_counts_do_not_change_any_other_field(self):
        base = summarise_run_document(_doc([])).to_dict()
        skewed = summarise_run_document(_doc([("X", 100, "A", "T"), ("Y", 100, "A", "T")])).to_dict()
        assert set(base) == set(skewed)
        assert base["evidence"] == {**skewed["evidence"], "small_variants": base["evidence"]["small_variants"]}

    def test_clinical_content_and_paths_are_not_surfaced(self):
        s = summarise_run_document(_doc([("1", 10, "A", "T")], hgvs_c="c.1A>T"))
        text = json.dumps(s.to_dict()) + json.dumps(s.chromosome_to_dict("1"))
        assert "Pathogenic" not in text and "/srv/secret" not in text and "acmg" not in text.lower()

    def test_provenance_preserved(self):
        p = summarise_run_document(_doc([], assembly="hg19")).to_dict()["provenance"]
        assert p["assembly_as_recorded"] == "hg19"
        assert p["reference"]["refseq_assembly"] == "GCF_000001405.25"
        assert p["geper_version"] == "1.0.0"


class TestModuleHygiene:
    def test_no_imports_outside_the_package_or_stdlib(self):
        """The clinical service deploys without the rest of geper/, so this package must stand alone."""
        stdlib_ok = {"__future__", "dataclasses", "typing"}
        pkg = pathlib.Path(summary_module.__file__).parent
        for path in pkg.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    assert all(a.name.split(".")[0] in stdlib_ok for a in node.names), (path.name, node.names)
                elif isinstance(node, ast.ImportFrom):
                    assert node.level == 1 or (node.module or "").split(".")[0] in stdlib_ok, (path.name, node.module)


class TestPatientProjection:
    """The only view a patient may be given: reference metadata, findings withheld."""

    def _full(self):
        doc = _doc(
            [("chr17", 7674220, "C", "T"), ("chrX", 5000, "G", "A"), ("chrY", 77, "A", "T"), ("chrM", 73, "A", "G")]
        )
        doc["variants"][0]["gene"] = "TP53-GENE-MARKER"
        return summarise_run_document(doc)

    def test_shape_is_exactly_the_allow_list(self):
        view = patient_projection(self._full())
        assert set(view) == {"assembly", "findings", "reference_chromosomes", "limitations"}
        assert view["assembly"] == "GRCh38"
        assert view["findings"] == {"status": "withheld", "reason": PATIENT_WITHHELD_NOTICE}
        assert set(view["reference_chromosomes"][0]) == {
            "name",
            "kind",
            "length_bp",
            "refseq_accession",
            "genbank_accession",
        }

    def test_sex_chromosomes_are_absent_and_the_rest_are_present(self):
        names = [c["name"] for c in patient_projection(self._full())["reference_chromosomes"]]
        assert names == [str(n) for n in range(1, 23)] + ["MT"]
        assert "X" not in names and "Y" not in names

    def test_nothing_derived_from_the_variants_survives(self):
        text = json.dumps(patient_projection(self._full()))
        for forbidden in (
            "7674220",
            "5000",
            "variant",
            "TP53-GENE-MARKER",
            "Pathogenic",
            "count",
            "variants_present",
            "no_variants_reported",
            "unmapped",
            "invalid",
            "provenance",
            "/srv/secret",
            "17:",
        ):
            assert forbidden not in text, forbidden

    def test_the_projection_is_identical_whatever_the_variants_are(self):
        many = summarise_run_document(_doc([("chr1", 10 + i, "A", "T") for i in range(40)] + [("chrX", 9, "A", "T")]))
        none = summarise_run_document(_doc([]))
        assert patient_projection(many) == patient_projection(none)

    def test_an_unusable_summary_gives_no_reference_and_no_build(self):
        for doc in ({}, {"assembly": "T2T", "variants": []}, None, {"assembly": "GRCh38"}):
            view = patient_projection(summarise_run_document(doc))
            assert view["assembly"] is None and view["reference_chromosomes"] == []
            assert view["findings"]["status"] == "withheld"

    def test_the_build_is_the_canonical_constant_never_the_raw_string(self):
        view = patient_projection(summarise_run_document(_doc([], assembly="  hg19 ")))
        assert view["assembly"] == "GRCh37"
        view = patient_projection(summarise_run_document(_doc([], assembly="GRCh38<script>")))
        assert view["assembly"] is None
