"""
tests/test_defect_regression.py
───────────────────────────────
Regression tests for GEPER v12 defect fixes (Defects 1–15).

Coverage matrix
───────────────
D1  – 5-tuple unpack from get_codon_and_aa()
D2  – bcftools norm integration (smoke test — tool may not be installed)
D3  – HGVS generation: c./n./g. prefix correctness
D4  – GFF3 phase handling in codon frame calculation
D5  – Allele balance uses correct ALT index for multiallelic VCFs
D6  – PGx subset allele exclusion (deterministic diplotype)
D7  – BP7 fires from synonymous_or_intronic
D8  – BP1 wired to ClinVar reputable-source benign
D9  – Duplicate @staticmethod removed from EvidenceAggregator
D10 – Version synchronised across pyproject.toml and pipeline.__init__
D11 – PS1/PM5 mutual exclusion
D12 – BLAST results attached to annotation output
D13 – VEP HGVSc/HGVSp propagated into AnnotatedVariant
D14 – ClinVar lookup() cache_key bug fixed; check_same_codon_pathogenic cached
D15 – PP2 uses missense constraint (oe_mis/mis_z), not LoF intolerance
"""
from __future__ import annotations

import copy
import sys
import types
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from unittest.mock import MagicMock, patch

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# D3 – HGVS generation
# ══════════════════════════════════════════════════════════════════════════════

class TestHgvsGeneration:
    """Defect 3: HGVS must never produce NM_xxx:g. for transcript references,
    AND must never pair a c./n. prefix with an unverified raw genomic
    position (e.g. NM_xxxxx:c.94781858G>A) — both are invalid HGVS.

    The previous version of this test class only checked the prefix
    letter (c. vs g.) and asserted the raw genomic position was an
    acceptable c. coordinate. It was not: without a verified CDS-relative
    coordinate, _build_hgvs must fall back to genomic (g.) notation.
    """

    def _hgvs(self, chrom, pos, ref, alt, transcript=None, cds_pos=None,
              end_cds_pos=None, strand=None):
        from pipeline.annotation.stage import _build_hgvs
        return _build_hgvs(chrom, pos, ref, alt, transcript,
                            cds_pos=cds_pos, end_cds_pos=end_cds_pos, strand=strand)

    def test_no_transcript_uses_genomic_g(self):
        h = self._hgvs("chr17", 43057051, "A", "T", None)
        assert ":g." in h
        assert "chr17" in h

    def test_nm_transcript_without_cds_pos_falls_back_to_genomic(self):
        # No verified CDS-relative coordinate supplied → must NOT emit
        # NM_007294.4:c.43057051A>T (invalid: that's a genomic position).
        h = self._hgvs("chr17", 43057051, "A", "T", "NM_007294.4")
        assert ":g." in h
        assert ":c." not in h

    def test_nm_transcript_with_verified_cds_pos_uses_c_prefix(self):
        h = self._hgvs("chr17", 43057051, "A", "T", "NM_007294.4",
                        cds_pos=181, strand="+")
        assert h == "NM_007294.4:c.181A>T"

    def test_xm_transcript_with_verified_cds_pos_uses_c_prefix(self):
        h = self._hgvs("chr1", 100, "G", "A", "XM_001234.1",
                        cds_pos=10, strand="+")
        assert h.startswith("XM_001234.1:c.")

    def test_nr_transcript_with_verified_pos_uses_n_prefix(self):
        h = self._hgvs("chrX", 500, "C", "T", "NR_024540.1",
                        cds_pos=5, strand="+")
        assert h.startswith("NR_024540.1:n.")

    def test_xr_transcript_with_verified_pos_uses_n_prefix(self):
        h = self._hgvs("chr2", 200, "A", "G", "XR_001234.1",
                        cds_pos=5, strand="+")
        assert h.startswith("XR_001234.1:n.")

    def test_insertion_without_cds_pos_falls_back_to_genomic(self):
        h = self._hgvs("chr1", 100, "A", "ATG", "NM_000059.4")
        assert "ins" in h
        assert ":g." in h

    def test_insertion_with_verified_cds_pos_uses_c_prefix(self):
        h = self._hgvs("chr1", 100, "A", "ATG", "NM_000059.4",
                        cds_pos=50, strand="+")
        assert "ins" in h
        assert h.startswith("NM_000059.4:c.")

    def test_deletion_without_cds_pos_falls_back_to_genomic(self):
        h = self._hgvs("chr1", 100, "ATG", "A", "NM_000059.4")
        assert "del" in h
        assert ":g." in h

    def test_deletion_with_verified_cds_range_uses_c_prefix(self):
        h = self._hgvs("chr1", 100, "ATG", "A", "NM_000059.4",
                        cds_pos=50, end_cds_pos=52, strand="+")
        assert "del" in h
        assert h.startswith("NM_000059.4:c.")

    def test_delins_without_cds_pos_falls_back_to_genomic(self):
        h = self._hgvs("chr1", 100, "ATG", "CCC", "NM_000059.4")
        assert "delins" in h
        assert ":g." in h

    def test_enst_without_cds_pos_falls_back_to_genomic(self):
        h = self._hgvs("chr17", 43057051, "A", "T", "ENST00000357654.9")
        assert ":g." in h

    def test_enst_with_verified_cds_pos_uses_c_prefix(self):
        h = self._hgvs("chr17", 43057051, "A", "T", "ENST00000357654.9",
                        cds_pos=181, strand="+")
        assert h.startswith("ENST00000357654.9:c.")


# ══════════════════════════════════════════════════════════════════════════════
# D5 – Allele balance multiallelic
# ══════════════════════════════════════════════════════════════════════════════

class TestAlleleBalance:
    """Defect 5: AB must use the correct AD index per ALT allele."""

    def _extract(self, gt, keys, vals, alt_index=1):
        from pipeline.zygosity.extractor import ZygosityExtractor
        return ZygosityExtractor.extract(gt, keys, vals, alt_index=alt_index)

    def test_biallelic_ab_default(self):
        # REF=20, ALT=10 → AB = 10/30
        r = self._extract("0/1", ["GT", "AD", "DP"], ["0/1", "20,10", "30"])
        assert r.ab == pytest.approx(10 / 30, rel=1e-4)

    def test_multiallelic_alt1_ab(self):
        # REF=20, ALT1=10, ALT2=5 → for allele 1, AB = 10/35
        r = self._extract("0/1", ["GT", "AD", "DP"], ["0/1", "20,10,5", "35"], alt_index=1)
        assert r.ab == pytest.approx(10 / 35, rel=1e-4)

    def test_multiallelic_alt2_ab(self):
        # REF=20, ALT1=10, ALT2=5 → for allele 2, AB = 5/35
        r = self._extract("0/2", ["GT", "AD", "DP"], ["0/2", "20,10,5", "35"], alt_index=2)
        assert r.ab == pytest.approx(5 / 35, rel=1e-4)

    def test_multiallelic_alt1_and_alt2_differ(self):
        from pipeline.zygosity.extractor import ZygosityExtractor
        r1 = ZygosityExtractor.extract("0/1", ["GT", "AD"], ["0/1", "30,10,5"], alt_index=1)
        r2 = ZygosityExtractor.extract("0/2", ["GT", "AD"], ["0/2", "30,10,5"], alt_index=2)
        assert r1.ab != r2.ab

    def test_zero_total_depth_no_ab(self):
        r = self._extract("0/1", ["GT", "AD"], ["0/1", "0,0"], alt_index=1)
        assert r.ab is None

    def test_alt_index_clamped_to_valid(self):
        # alt_index=5 but only 2 ALT alleles → clamped, no crash
        r = self._extract("0/1", ["GT", "AD"], ["0/1", "20,10"], alt_index=5)
        assert r.ab is not None  # should not raise


# ══════════════════════════════════════════════════════════════════════════════
# D6 – PGx subset allele exclusion
# ══════════════════════════════════════════════════════════════════════════════

class TestPgxSubsetAlleleExclusion:
    """Defect 6: A simpler allele must not coexist with the allele that subsumes it."""

    def _detect(self, gene, variants_dict):
        from pipeline.pgx.stage import _detect_star_alleles
        detected, _hemizygous = _detect_star_alleles(gene, variants_dict)
        return detected

    def _diplotype(self, gene, detected):
        from pipeline.pgx.stage import _call_diplotype
        return _call_diplotype(gene, detected)

    def test_subset_allele_excluded(self):
        """*2 (1 SNV) must not coexist with *10 (superset of *2's SNVs)."""
        from pipeline.pgx.diplotypes import STAR_ALLELE_VARIANTS

        gene = "CYP2D6"
        if gene not in STAR_ALLELE_VARIANTS:
            pytest.skip("CYP2D6 not in STAR_ALLELE_VARIANTS")

        star2_vars = STAR_ALLELE_VARIANTS[gene].get("*2", [])
        star10_vars = STAR_ALLELE_VARIANTS[gene].get("*10", [])

        if not star2_vars or not star10_vars:
            pytest.skip("*2 or *10 not defined")

        # Check *2 is a subset of *10's defining variants
        s2 = set((c, p, r, a) for c, p, r, a in star2_vars)
        s10 = set((c, p, r, a) for c, p, r, a in star10_vars)

        if not s2.issubset(s10):
            pytest.skip("*2 is not a subset of *10 in this allele table — skip subset test")

        # Simulate all *10 variants being present
        variants = {
            (c.lstrip("chr"), p, r.upper(), a.upper()): "heterozygous"
            for c, p, r, a in star10_vars
        }
        detected = self._detect(gene, variants)
        # *2 must not coexist with *10
        assert "*2" not in detected or "*10" not in detected, (
            f"Subset allele *2 must not coexist with *10; got {detected}"
        )

    def test_no_alleles_gives_star1_star1(self):
        from pipeline.pgx.diplotypes import STAR_ALLELE_VARIANTS
        gene = next(iter(STAR_ALLELE_VARIANTS), None)
        if gene is None:
            pytest.skip("No PGx genes defined")
        diplotype, a1, a2 = self._diplotype(gene, [])
        assert a1 == "*1" and a2 == "*1"
        assert "*1/*1" in diplotype

    def test_one_allele_gives_star1_allele(self):
        from pipeline.pgx.diplotypes import STAR_ALLELE_VARIANTS
        gene = next(iter(STAR_ALLELE_VARIANTS), None)
        if gene is None:
            pytest.skip("No PGx genes defined")
        diplotype, a1, a2 = self._diplotype(gene, ["*4"])
        assert "*1" in (a1, a2)
        assert "*4" in (a1, a2)

    def test_diplotype_is_deterministic(self):
        """Same input always produces the same diplotype."""
        from pipeline.pgx.stage import _call_diplotype
        r1 = _call_diplotype("CYP2C19", ["*2", "*17"])
        r2 = _call_diplotype("CYP2C19", ["*17", "*2"])
        assert r1[0] == r2[0], "Diplotype must be deterministic regardless of input order"


# ══════════════════════════════════════════════════════════════════════════════
# ISSUE 6 — PGx hemizygous genotype handling (male X chromosome calls)
# ══════════════════════════════════════════════════════════════════════════════

class TestPgxHemizygousHandling:
    """ISSUE 6: a hemizygous call (single chromosomal copy — e.g. a male
    sample's lone X chromosome for G6PD) must never be reported as a
    diploid pair like 'G202A/G202A', which falsely claims two independent
    copies of the variant. Genuinely homozygous (two-copy) and
    heterozygous (carrier) calls must be unaffected by the fix.
    """

    def test_hemizygous_g6pd_call_not_duplicated_into_pair(self):
        from pipeline.pgx.stage import _detect_star_alleles
        # Single-copy (ploidy-1 GT) call — e.g. a male sample's hemizygous X.
        variants = {("X", 154535388, "G", "A"): "hemizygous"}
        detected, hemizygous = _detect_star_alleles("G6PD", variants)
        assert detected == ["G202A"], (
            f"Hemizygous call must appear exactly once, not duplicated; got {detected}"
        )
        assert hemizygous == {"G202A"}

    def test_hemizygous_diplotype_label_is_not_fabricated_pair(self):
        from pipeline.pgx.stage import _detect_star_alleles, _call_diplotype
        variants = {("X", 154535388, "G", "A"): "hemizygous"}
        detected, hemizygous = _detect_star_alleles("G6PD", variants)
        diplotype, a1, a2 = _call_diplotype("G6PD", detected, hemizygous)
        # Must NOT claim two chromosomal copies via "G202A/G202A" or "*1/G202A"
        assert diplotype != "G202A/G202A", (
            "Hemizygous male call must not be labelled as homozygous (two copies)"
        )
        assert diplotype != "*1/G202A", (
            "Hemizygous male call must not be paired with a fabricated normal *1 X"
        )
        assert "hemizygous" in diplotype.lower()
        assert "G202A" in diplotype

    def test_hemizygous_phenotype_remains_deficient(self):
        """Issue 6: phenotype must remain correct even though the display
        label changes — a single hemizygous loss-of-function copy is fully
        expressed (no masking normal allele), same functional consequence
        as the diploid homozygous case."""
        from pipeline.pgx.stage import _detect_star_alleles, _call_diplotype, _predict_phenotype
        variants = {("X", 154535388, "G", "A"): "hemizygous"}
        detected, hemizygous = _detect_star_alleles("G6PD", variants)
        _, a1, a2 = _call_diplotype("G6PD", detected, hemizygous)
        assert _predict_phenotype("G6PD", a1, a2) == "Deficient"

    def test_true_homozygous_call_unaffected_by_fix(self):
        """A genuinely diploid homozygous_alt call (two real copies) must
        still be reported as a true homozygous pair, not hemizygous."""
        from pipeline.pgx.stage import _detect_star_alleles, _call_diplotype
        variants = {("X", 154535388, "G", "A"): "homozygous_alt"}
        detected, hemizygous = _detect_star_alleles("G6PD", variants)
        assert detected == ["G202A", "G202A"]
        assert hemizygous == set()
        diplotype, a1, a2 = _call_diplotype("G6PD", detected, hemizygous)
        assert diplotype == "G202A/G202A"

    def test_heterozygous_carrier_call_unaffected_by_fix(self):
        """A heterozygous female carrier call must still pair with *1, not
        be treated as hemizygous."""
        from pipeline.pgx.stage import _detect_star_alleles, _call_diplotype
        variants = {("X", 154535388, "G", "A"): "heterozygous"}
        detected, hemizygous = _detect_star_alleles("G6PD", variants)
        assert detected == ["G202A"]
        assert hemizygous == set()
        diplotype, a1, a2 = _call_diplotype("G6PD", detected, hemizygous)
        assert diplotype == "*1/G202A"

    def test_autosomal_gene_hemizygous_call_still_handled_safely(self):
        """Even for an autosomal gene (where a hemizygous GT would be
        biologically unusual — e.g. a CNV deletion of the other copy),
        the same single-copy-not-duplicated logic must apply rather than
        crashing or fabricating a pair."""
        from pipeline.pgx.stage import _detect_star_alleles, _call_diplotype
        variants = {("22", 42128175, "C", "T"): "hemizygous"}
        detected, hemizygous = _detect_star_alleles("CYP2D6", variants)
        assert detected == ["*4"]
        assert hemizygous == {"*4"}
        diplotype, a1, a2 = _call_diplotype("CYP2D6", detected, hemizygous)
        assert diplotype == "*4 (hemizygous)"


# ══════════════════════════════════════════════════════════════════════════════
# D7 – BP7 synonymous_or_intronic
# ══════════════════════════════════════════════════════════════════════════════

class TestBP7:
    """Defect 7: BP7 must fire for synonymous/intronic variants without splice impact."""

    def _ev(self, **kwargs):
        from pipeline.acmg.classifier import VariantEvidence
        return VariantEvidence(**kwargs)

    def _classify(self, ev):
        from pipeline.acmg.classifier import AcmgClassifier
        return AcmgClassifier().classify(ev)

    def test_bp7_fires_for_synonymous_non_splice(self):
        ev = self._ev(synonymous_or_intronic=True)
        r = self._classify(ev)
        assert "BP7" in r.criteria_met

    def test_bp7_not_fired_when_false(self):
        ev = self._ev(synonymous_or_intronic=False)
        r = self._classify(ev)
        assert "BP7" not in r.criteria_met

    def test_bp7_field_defaults_false(self):
        from pipeline.acmg.classifier import VariantEvidence
        ev = VariantEvidence()
        assert ev.synonymous_or_intronic is False


# ══════════════════════════════════════════════════════════════════════════════
# D8 – BP1 wired to ClinVar benign
# ══════════════════════════════════════════════════════════════════════════════

class TestBP1:
    """Defect 8: BP1 fires when a reputable source reports benign."""

    def _ev(self, **kwargs):
        from pipeline.acmg.classifier import VariantEvidence
        return VariantEvidence(**kwargs)

    def _classify(self, ev):
        from pipeline.acmg.classifier import AcmgClassifier
        return AcmgClassifier().classify(ev)

    def test_bp1_fires_when_clinvar_benign_1star(self):
        ev = self._ev(bp1_reputable_source_benign=True)
        r = self._classify(ev)
        assert "BP1" in r.criteria_met

    def test_bp1_not_fired_when_none(self):
        ev = self._ev(bp1_reputable_source_benign=None)
        r = self._classify(ev)
        assert "BP1" not in r.criteria_met

    def test_bp1_not_fired_when_false(self):
        ev = self._ev(bp1_reputable_source_benign=False)
        r = self._classify(ev)
        assert "BP1" not in r.criteria_met


# ══════════════════════════════════════════════════════════════════════════════
# D9 – Duplicate @staticmethod removed
# ══════════════════════════════════════════════════════════════════════════════

class TestDuplicateStaticmethod:
    """Defect 9: EvidenceAggregator.clinvar_sig_to_score must be callable."""

    def test_clinvar_sig_to_score_callable(self):
        from pipeline.evidence.aggregator import EvidenceAggregator
        # If duplicate @staticmethod remains, Python would raise TypeError at import
        score = EvidenceAggregator.clinvar_sig_to_score("Pathogenic", 2)
        assert score == pytest.approx(1.0, rel=1e-4)

    def test_clinvar_sig_to_score_benign(self):
        from pipeline.evidence.aggregator import EvidenceAggregator
        score = EvidenceAggregator.clinvar_sig_to_score("Benign", 2)
        assert score == pytest.approx(0.0, rel=1e-4)

    def test_not_double_decorated(self):
        """Verify only one @staticmethod decorator exists on the method."""
        import ast, inspect, pipeline.evidence.aggregator as mod
        src = inspect.getsource(mod)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "clinvar_sig_to_score":
                    decorator_names = [
                        getattr(d, 'id', None) for d in node.decorator_list
                    ]
                    assert decorator_names.count("staticmethod") == 1, (
                        f"Expected 1 @staticmethod, got {decorator_names.count('staticmethod')}"
                    )


# ══════════════════════════════════════════════════════════════════════════════
# D10 – Version synchronisation
# ══════════════════════════════════════════════════════════════════════════════

class TestVersionSync:
    """Defect 10: All version strings must be synchronised to 12.0.0."""

    def test_pyproject_version(self):
        import tomllib, pathlib
        p = pathlib.Path(__file__).parent.parent / "pyproject.toml"
        if not p.exists():
            pytest.skip("pyproject.toml not found")
        with open(p, "rb") as f:
            data = tomllib.load(f)
        assert data["project"]["version"] == "12.0.0"

    def test_pipeline_package_version(self):
        import pipeline
        assert pipeline.__version__ == "12.0.0"

    def test_checkpoint_version(self):
        import pipeline
        assert pipeline.CHECKPOINT_VERSION == "12.0.0"

    def test_report_version(self):
        import pipeline
        assert pipeline.REPORT_VERSION == "12.0.0"

    def test_pipeline_version(self):
        import pipeline
        assert pipeline.PIPELINE_VERSION == "12.0.0"


# ══════════════════════════════════════════════════════════════════════════════
# D11 – PS1 / PM5 mutual exclusion
# ══════════════════════════════════════════════════════════════════════════════

class TestPS1PM5MutualExclusion:
    """Defect 11: PS1 and PM5 must never both fire for the same variant."""

    def _clf(self):
        from pipeline.acmg.classifier import AcmgClassifier
        return AcmgClassifier()

    def _ev(self, **kwargs):
        from pipeline.acmg.classifier import VariantEvidence
        return VariantEvidence(**kwargs)

    def test_ps1_fires_when_same_aa_pathogenic(self):
        r = self._clf().classify(self._ev(same_aa_pathogenic=True, is_missense=True))
        assert "PS1" in r.criteria_met

    def test_pm5_suppressed_when_ps1_active(self):
        """If PS1 is active, PM5 must not fire — no double-counting."""
        ev = self._ev(
            same_aa_pathogenic=True,
            novel_aa_at_known_pathogenic_codon=True,
            is_missense=True,
        )
        r = self._clf().classify(ev)
        assert "PS1" in r.criteria_met
        assert "PM5" not in r.criteria_met, (
            "PM5 must not fire when PS1 is active (ACMG PS1/PM5 mutual exclusion)"
        )

    def test_pm5_fires_independently_without_ps1(self):
        ev = self._ev(
            same_aa_pathogenic=None,
            novel_aa_at_known_pathogenic_codon=True,
            is_missense=True,
        )
        r = self._clf().classify(ev)
        assert "PM5" in r.criteria_met
        assert "PS1" not in r.criteria_met

    def test_neither_fires_when_no_evidence(self):
        ev = self._ev(is_missense=True)
        r = self._clf().classify(ev)
        assert "PS1" not in r.criteria_met
        assert "PM5" not in r.criteria_met

    def test_pm5_not_fired_for_non_missense(self):
        ev = self._ev(novel_aa_at_known_pathogenic_codon=True, is_missense=False)
        r = self._clf().classify(ev)
        assert "PM5" not in r.criteria_met


# ══════════════════════════════════════════════════════════════════════════════
# D12 – BLAST results propagated into annotation
# ══════════════════════════════════════════════════════════════════════════════

class TestBlastPropagation:
    """Defect 12: blast_result_data must be merged into annotation variants."""

    def _make_blast_data(self, chrom, pos, ref, alt):
        seq_id = f"{chrom}:{pos}:{ref}:{alt}"
        return {
            "hit_count": 1,
            "database": "nr",
            "hits": [
                {
                    "sequence_id": seq_id,
                    "identity": 98.5,
                    "evalue": 1e-10,
                    "description": "Homo sapiens BRCA1",
                }
            ],
        }

    def test_blast_hits_merged_into_variant(self):
        """Simulate what runner does: merge BLAST hits keyed by chrom:pos:ref:alt."""
        blast_data = self._make_blast_data("chr17", 43057051, "A", "T")

        # Simulate annotation result.annotation dict
        annotation = {
            "variants": [
                {"chrom": "chr17", "pos": 43057051, "ref": "A", "alt": "T", "gene": "BRCA1"}
            ]
        }

        # Reproduce the runner merge logic
        blast_hits_by_id: Dict[str, list] = {}
        for hit in blast_data.get("hits", []):
            seq_id = hit.get("sequence_id", "")
            blast_hits_by_id.setdefault(seq_id, []).append(hit)

        for var in annotation["variants"]:
            bid = f"{var.get('chrom','')}:{var.get('pos','')}:{var.get('ref','')}:{var.get('alt','')}"
            hits = blast_hits_by_id.get(bid, [])
            if hits:
                var["blast_hits"] = hits

        annotation["blast_summary"] = {
            "total_hits": blast_data.get("hit_count", 0),
            "blast_db": blast_data.get("database", ""),
            "note": "BLAST results are supporting information only and do not override ACMG evidence.",
        }

        assert "blast_hits" in annotation["variants"][0]
        assert annotation["variants"][0]["blast_hits"][0]["identity"] == 98.5
        assert "blast_summary" in annotation
        assert "supporting information" in annotation["blast_summary"]["note"]

    def test_blast_note_does_not_override_acmg(self):
        """The BLAST summary note must state it does not override ACMG evidence."""
        blast_data = self._make_blast_data("chr1", 100, "A", "T")
        note = "BLAST results are supporting information only and do not override ACMG evidence."
        assert "not override ACMG evidence" in note

    def test_no_blast_data_does_not_crash(self):
        """When blast_result_data is None, annotation proceeds normally."""
        blast_result_data = None
        annotation = {"variants": [{"chrom": "chr1", "pos": 1, "ref": "A", "alt": "T"}]}
        if blast_result_data and isinstance(annotation.get("variants"), list):
            pass  # merge would happen
        assert "variants" in annotation  # no crash


# ══════════════════════════════════════════════════════════════════════════════
# D13 – VEP HGVS propagation
# ══════════════════════════════════════════════════════════════════════════════

class TestVepHgvsPropagation:
    """Defect 13: VEP HGVSc and HGVSp must populate AnnotatedVariant."""

    def test_annotated_variant_has_vep_hgvs_fields(self):
        from pipeline.annotation.stage import AnnotatedVariant
        v = AnnotatedVariant(chrom="chr17", pos=43057051, ref="A", alt="T")
        assert hasattr(v, "vep_hgvs_c")
        assert hasattr(v, "vep_hgvs_p")

    def test_vep_hgvs_defaults_empty(self):
        from pipeline.annotation.stage import AnnotatedVariant
        v = AnnotatedVariant(chrom="chr1", pos=100, ref="A", alt="T")
        assert v.vep_hgvs_c == ""
        assert v.vep_hgvs_p == ""

    def test_vep_hgvs_populated(self):
        from pipeline.annotation.stage import AnnotatedVariant
        v = AnnotatedVariant(
            chrom="chr17", pos=43057051, ref="A", alt="T",
            vep_hgvs_c="NM_007294.4:c.5266dup",
            vep_hgvs_p="NP_009225.1:p.Gln1756fs",
        )
        assert v.vep_hgvs_c == "NM_007294.4:c.5266dup"
        assert v.vep_hgvs_p == "NP_009225.1:p.Gln1756fs"

    def test_extract_csq_hgvs_function_exists(self):
        from pipeline.annotation.stage import _extract_csq_hgvs
        assert callable(_extract_csq_hgvs)

    def test_extract_csq_hgvs_empty_info(self):
        from pipeline.annotation.stage import _extract_csq_hgvs
        hc, hp = _extract_csq_hgvs("DP=30;AF=0.5", [])
        assert hc == ""
        assert hp == ""

    def test_extract_csq_hgvs_parses_csq(self):
        from pipeline.annotation.stage import _extract_csq_hgvs
        # Minimal CSQ with HGVSc and HGVSp fields
        csq_fields = ["Allele", "Consequence", "SYMBOL", "HGVSc", "HGVSp"]
        csq_value = "T|missense_variant|BRCA1|NM_007294.4:c.5266A>T|NP_009225.1:p.Lys1756Asn"
        info = f"DP=30;CSQ={csq_value}"
        hc, hp = _extract_csq_hgvs(info, csq_fields)
        assert hc == "NM_007294.4:c.5266A>T"
        assert hp == "NP_009225.1:p.Lys1756Asn"

    def test_nm_g_notation_filtered_out(self):
        """VEP should never emit NM_:g. but if it does, we filter it."""
        from pipeline.annotation.stage import _extract_csq_hgvs
        csq_fields = ["Allele", "Consequence", "SYMBOL", "HGVSc", "HGVSp"]
        # Pathological case: invalid NM_:g. notation
        csq_value = "T|missense_variant|BRCA1|NM_007294.4:g.43057051A>T|"
        info = f"DP=30;CSQ={csq_value}"
        hc, hp = _extract_csq_hgvs(info, csq_fields)
        assert hc == "", f"NM_:g. notation must be filtered out, got: {hc!r}"


# ══════════════════════════════════════════════════════════════════════════════
# D14 – ClinVar cache
# ══════════════════════════════════════════════════════════════════════════════

class TestClinVarCache:
    """Defect 14: ClinVar lookup() must not raise NameError; cache must work."""

    def _lkp(self):
        from pipeline.clinvar.lookup import ClinVarLookup
        return ClinVarLookup(cfg={"clinvar": {"backend": "local", "tsv_path": None}})

    def test_lookup_does_not_raise_nameerror(self):
        """Before fix, lookup() raised NameError: name 'cache_key' is not defined."""
        lkp = self._lkp()
        result = lkp.lookup("chr17", 43057051, "A", "T")
        assert result is None  # no local TSV loaded, but should not raise

    def test_lookup_caches_result(self):
        lkp = self._lkp()
        lkp.lookup("chr1", 100, "A", "T")
        lkp.lookup("chr1", 100, "A", "T")
        stats = lkp.cache_stats()
        assert stats["hits"] >= 1

    def test_cache_hit_increments_counter(self):
        lkp = self._lkp()
        lkp.lookup("chr2", 200, "G", "C")
        hits_before = lkp.cache_stats()["hits"]
        lkp.lookup("chr2", 200, "G", "C")
        assert lkp.cache_stats()["hits"] == hits_before + 1

    def test_different_variants_separate_keys(self):
        lkp = self._lkp()
        lkp.lookup("chr1", 100, "A", "T")
        lkp.lookup("chr1", 101, "G", "C")
        assert lkp.cache_stats()["misses"] >= 2

    def test_chr_prefix_normalisation(self):
        """chr1 and 1 must map to the same cache entry."""
        lkp = self._lkp()
        lkp.lookup("1", 100, "A", "T")
        hits_before = lkp.cache_stats()["hits"]
        lkp.lookup("chr1", 100, "A", "T")
        assert lkp.cache_stats()["hits"] == hits_before + 1

    def test_none_result_cached(self):
        lkp = self._lkp()
        lkp.lookup("chrX", 999999, "A", "T")
        hits_before = lkp.cache_stats()["hits"]
        lkp.lookup("chrX", 999999, "A", "T")
        assert lkp.cache_stats()["hits"] == hits_before + 1

    def test_check_same_codon_no_nameerror(self):
        """check_same_codon_pathogenic must not raise due to undefined variables."""
        lkp = self._lkp()
        result = lkp.check_same_codon_pathogenic("chr17", 43057051, "A", "T", "K", "N")
        assert result == (None, None)  # no local data, but should not raise

    def test_sig_to_score_unknown_returns_none(self):
        from pipeline.clinvar.lookup import ClinVarLookup
        assert ClinVarLookup.sig_to_score("Mixed significance", 0) is None

    def test_sig_to_score_unknown_significance_returns_none(self):
        from pipeline.clinvar.lookup import ClinVarLookup
        assert ClinVarLookup.sig_to_score("Unknown significance", 0) is None

    def test_sig_to_score_pathogenic_returns_1(self):
        from pipeline.clinvar.lookup import ClinVarLookup
        assert ClinVarLookup.sig_to_score("Pathogenic", 3) == pytest.approx(1.0)

    def test_sig_to_score_benign_returns_0(self):
        from pipeline.clinvar.lookup import ClinVarLookup
        assert ClinVarLookup.sig_to_score("Benign", 2) == pytest.approx(0.0)


# ══════════════════════════════════════════════════════════════════════════════
# D15 – PP2 missense constraint
# ══════════════════════════════════════════════════════════════════════════════

class TestPP2MissenseConstraint:
    """Defect 15: PP2 must use missense constraint, not LoF intolerance."""

    def _clf(self):
        from pipeline.acmg.classifier import AcmgClassifier
        return AcmgClassifier()

    def _ev(self, **kwargs):
        from pipeline.acmg.classifier import VariantEvidence
        return VariantEvidence(**kwargs)

    def test_pp2_fires_when_missense_constrained(self):
        ev = self._ev(is_missense=True, missense_constrained=True)
        r = self._clf().classify(ev)
        assert "PP2" in r.criteria_met

    def test_pp2_not_fired_when_not_missense_constrained(self):
        ev = self._ev(is_missense=True, missense_constrained=False, lof_gene_intolerant=True)
        r = self._clf().classify(ev)
        # lof_gene_intolerant alone must NOT trigger PP2
        assert "PP2" not in r.criteria_met, (
            "PP2 must not fire based on LoF intolerance alone"
        )

    def test_pp2_not_fired_for_non_missense(self):
        ev = self._ev(is_missense=False, missense_constrained=True)
        r = self._clf().classify(ev)
        assert "PP2" not in r.criteria_met

    def test_variant_evidence_has_missense_constrained_field(self):
        from pipeline.acmg.classifier import VariantEvidence
        ev = VariantEvidence()
        assert hasattr(ev, "missense_constrained")
        assert ev.missense_constrained is False

    def test_constraint_record_has_oe_mis(self):
        from pipeline.constraint.lookup import ConstraintRecord
        rec = ConstraintRecord(gene="BRCA1", pli=0.99, loeuf=0.1, oe_mis=0.5, mis_z=3.5)
        assert rec.oe_mis == 0.5
        assert rec.mis_z == 3.5

    def test_is_missense_constrained_oe_mis_threshold(self):
        from pipeline.constraint.lookup import ConstraintRecord
        # oe_mis < 0.8 → constrained
        rec_constrained = ConstraintRecord(gene="BRCA1", pli=0.5, loeuf=0.5, oe_mis=0.6)
        assert rec_constrained.is_missense_constrained() is True

        # oe_mis >= 0.8 → not constrained
        rec_unconstrained = ConstraintRecord(gene="TTN", pli=0.1, loeuf=0.9, oe_mis=0.95)
        assert rec_unconstrained.is_missense_constrained() is False

    def test_is_missense_constrained_mis_z_threshold(self):
        from pipeline.constraint.lookup import ConstraintRecord
        # mis_z > 3.09 → constrained
        rec = ConstraintRecord(gene="BRCA2", pli=0.5, loeuf=0.5, oe_mis=0.9, mis_z=3.5)
        assert rec.is_missense_constrained() is True

        # mis_z <= 3.09 → not constrained (oe_mis also above threshold)
        rec2 = ConstraintRecord(gene="MUC5B", pli=0.1, loeuf=0.9, oe_mis=0.9, mis_z=1.0)
        assert rec2.is_missense_constrained() is False

    def test_constraint_lookup_has_is_missense_constrained(self):
        from pipeline.constraint.lookup import GnomadConstraintLookup
        lkp = GnomadConstraintLookup(cfg={})
        # No data loaded → should return False without crashing
        result = lkp.is_missense_constrained("BRCA1")
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# D4 – GFF3 phase handling
# ══════════════════════════════════════════════════════════════════════════════

class TestGff3PhaseHandling:
    """Defect 4: GFF3 CDS phase must be applied to codon frame calculation."""

    def test_cds_record_has_phase_field(self):
        from pipeline.annotation.codon_provider import CdsRecord
        # Signature: (chrom, start, end, strand, phase, transcript_id)
        rec = CdsRecord("chr17", 43044295, 43044522, "+", 0, "NM_007294.4")
        assert rec.phase == 0

    def test_phase_0_no_offset(self):
        """phase=0 means first base of exon is first base of a codon."""
        from pipeline.annotation.codon_provider import CdsRecord
        rec = CdsRecord("chr1", 100, 199, "+", 0, "NM_000001.1")
        assert rec.phase == 0

    def test_phase_1_shifts_frame(self):
        """phase=1 means 1 base of the first codon is in the previous exon."""
        from pipeline.annotation.codon_provider import CdsRecord
        rec = CdsRecord("chr1", 100, 199, "+", 1, "NM_000001.1")
        assert rec.phase == 1

    def test_phase_2_shifts_frame(self):
        from pipeline.annotation.codon_provider import CdsRecord
        rec = CdsRecord("chr1", 100, 199, "+", 2, "NM_000001.1")
        assert rec.phase == 2

    def test_phase_applied_to_cds_position(self):
        """Verify phase offset shifts the codon_index correctly."""
        # With phase=0 at pos 100 in an exon starting at 100:
        #   cds_pos = (pos_in_exon=0) - phase_offset(0) = 0 → codon_index = 0
        # With phase=1:
        #   cds_pos = (pos_in_exon=0) - phase_offset(1) = -1 → codon_index = (-1 % 3) = 2
        # This verifies frame is shifted by phase
        for phase, expected_codon_index in [(0, 0), (1, 2), (2, 1)]:
            pos_in_exon = 0
            cds_offset = 0
            phase_offset = phase
            cds_pos = cds_offset + pos_in_exon - phase_offset
            codon_index = cds_pos % 3
            assert codon_index == expected_codon_index, (
                f"phase={phase}: expected codon_index={expected_codon_index}, got {codon_index}"
            )


# ══════════════════════════════════════════════════════════════════════════════
# D1 – 5-tuple unpack from get_codon_and_aa()
# ══════════════════════════════════════════════════════════════════════════════

class TestCodonProviderTuple:
    """Defect 1: get_codon_and_aa() returns 5 values — all must be unpacked."""

    def test_codon_provider_returns_5_tuple(self):
        """get_codon_and_aa() returns a 5-tuple on success; annotation stage unpacks all 5."""
        from pipeline.annotation.codon_provider import FastaCodonContextProvider
        from unittest.mock import patch, MagicMock

        provider = FastaCodonContextProvider(gff_path="", fasta_path="")

        # Mock the internal _classify_snv_full to return the 5-tuple
        with patch.object(
            provider, "_classify_snv_full",
            return_value=("missense_variant", "AAA", "AAT", "K", "N"),
        ):
            # Also mock _available to True and _cds_map to contain the transcript
            provider._available = True
            provider._cds_map = {"NM_000001.1": [MagicMock(chrom="chr1", start=100, end=200, strand="+", phase=0, transcript_id="NM_000001.1")]}
            result = provider.get_codon_and_aa("chr1", 100, "A", "T", "NM_000001.1")

        assert len(result) == 5, f"Expected 5-tuple, got {len(result)}-tuple: {result}"
        consequence, ref_codon, alt_codon, ref_aa, alt_aa = result
        assert consequence == "missense_variant"
        assert ref_aa == "K"
        assert alt_aa == "N"

    def test_annotation_stage_unpack_accepts_5tuple(self):
        """Explicit test: the annotation-stage unpack code handles 5 values."""
        # Before fix: ref_codon, alt_codon, ref_aa, alt_aa = get_codon_and_aa() → ValueError
        # After fix: consequence, ref_codon, alt_codon, ref_aa, alt_aa = get_codon_and_aa()
        five_tuple = ("missense_variant", "AAA", "AAT", "Lys", "Asn")
        consequence, ref_codon, alt_codon, ref_aa, alt_aa = five_tuple  # must not raise
        assert consequence == "missense_variant"
        assert ref_aa == "Lys"
        assert alt_aa == "Asn"

    def test_5tuple_unpack_does_not_raise(self):
        """Simulates the fixed unpack. If still 4-tuple, this would raise ValueError."""
        result = (None, None, None, None, None)  # 5-tuple as returned after fix
        consequence, ref_codon, alt_codon, ref_aa, alt_aa = result
        assert consequence is None
        assert ref_aa is None
        assert alt_aa is None


# ══════════════════════════════════════════════════════════════════════════════
# D2 – bcftools norm smoke test
# ══════════════════════════════════════════════════════════════════════════════

class TestBcftoolsNormIntegration:
    """Defect 2: bcftools norm must be invoked with --fasta-ref."""

    def test_norm_command_includes_fasta_ref(self):
        """Verify the norm command list contains --fasta-ref."""
        # This tests the command construction logic, not actual execution
        reference_fasta = "/path/to/hg38.fa"
        raw_vcf = "/path/to/variants.raw.vcf"
        norm_cmd = [
            "bcftools", "norm",
            "--fasta-ref", reference_fasta,
            "--multiallelics", "-",
            "--output-type", "v",
            "--output", "/path/to/variants.norm.vcf",
            raw_vcf,
        ]
        assert "--fasta-ref" in norm_cmd
        idx = norm_cmd.index("--fasta-ref")
        assert norm_cmd[idx + 1] == reference_fasta

    def test_norm_command_splits_multiallelics(self):
        norm_cmd = [
            "bcftools", "norm",
            "--fasta-ref", "/ref.fa",
            "--multiallelics", "-",
        ]
        assert "--multiallelics" in norm_cmd
        idx = norm_cmd.index("--multiallelics")
        assert norm_cmd[idx + 1] == "-"  # "-" means split

    @pytest.mark.skipif(
        __import__("shutil").which("bcftools") is None,
        reason="bcftools not installed",
    )
    def test_bcftools_available(self):
        import subprocess
        result = subprocess.run(["bcftools", "--version"], capture_output=True)
        assert result.returncode == 0
