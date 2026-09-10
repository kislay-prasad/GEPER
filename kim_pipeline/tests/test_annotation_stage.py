"""
tests/test_annotation_stage.py
────────────────────────────────
Tests for pipeline/annotation/stage.py and pipeline/annotation/gff_index.py.

These tests do not require an external GFF3 file.  The annotation index
tests build a tiny in-memory-equivalent GFF3 file; the stage tests use
the ``require_gff=False`` path so they run in any environment.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.annotation.stage import (
    AnnotationStage,
    _extract_genotype,
    _parse_zygosity,
    _build_hgvs,
    _parse_vcf,
)
from pipeline.annotation.gff_index import GffIndex, _normalise_chrom


# ─── Zygosity helpers (Task 5) ────────────────────────────────────────────────


class TestZygosity:
    def test_heterozygous(self):
        assert _parse_zygosity("0/1") == "Heterozygous"

    def test_homozygous_alt(self):
        assert _parse_zygosity("1/1") == "Homozygous_alt"

    def test_homozygous_ref(self):
        assert _parse_zygosity("0/0") == "Homozygous_ref"

    def test_no_call(self):
        assert _parse_zygosity("./.") == "No_call"
        assert _parse_zygosity(".") == "No_call"

    def test_phased_het(self):
        assert _parse_zygosity("0|1") == "Heterozygous"

    def test_multi_allelic(self):
        assert _parse_zygosity("1/2") == "Multi_allelic"


class TestExtractGenotype:
    def test_extracts_gt_ad_dp(self):
        geno = _extract_genotype("GT:AD:DP:GQ", "0/1:15,10:25:99")
        assert geno["GT"] == "0/1"
        assert geno["AD"] == "15,10"
        assert geno["DP"] == "25"
        assert geno["Zygosity"] == "Heterozygous"

    def test_missing_ad_returns_none(self):
        geno = _extract_genotype("GT:DP", "1/1:30")
        assert geno["GT"] == "1/1"
        assert geno["AD"] is None
        assert geno["DP"] == "30"
        assert geno["Zygosity"] == "Homozygous_alt"

    def test_minimal_gt_only(self):
        geno = _extract_genotype("GT", "./.")
        assert geno["GT"] == "./."
        assert geno["Zygosity"] == "No_call"


# ── DEFECT-zygosity-collapses-absent-and-malformed: fallback pair (Fix B) ──────
# The pair above is unreachable in production (measured: 1493 adversarial
# trials against ZygosityExtractor.extract(), 0 exceptions -- see the card),
# but is directly importable and still has the identical unvalidated
# collapse ZygosityExtractor had before pipeline/zygosity/extractor.py's own
# fix. `_parse_zygosity` itself is untouched -- its bare-str return is
# load-bearing in TestZygosity and TestExtractGenotype above. The
# discriminator lives only in `_extract_genotype`'s returned dict.


class TestExtractGenotypeMalformedDiscriminator:
    def test_valid_gt_not_flagged_malformed(self):
        # NOTE: this legacy fallback has no "hemizygous" category at all
        # (_parse_zygosity predates ZygosityExtractor) -- a haploid "1"
        # lands on "Homozygous_alt" here, untouched by this fix.
        geno = _extract_genotype("GT", "1")
        assert geno["Zygosity"] == "Homozygous_alt"
        assert geno["ZygosityMalformed"] is False

    def test_no_call_not_flagged_malformed(self):
        geno = _extract_genotype("GT", "./.")
        assert geno["Zygosity"] == "No_call"
        assert geno["ZygosityMalformed"] is False

    def test_malformed_gt_flagged_and_still_produces_a_category(self):
        """Documents the residual, same as the primary extractor: `Zygosity`
        itself is unchanged by this fix (out of scope to redefine it) --
        `ZygosityMalformed` is the only discriminator. A garbage GT token
        still yields a real-looking category (the collapse this whole
        defect family is about), but now a caller can tell."""
        geno = _extract_genotype("GT", "BAD")
        assert geno["Zygosity"] in (
            "Homozygous_ref",
            "Homozygous_alt",
            "Heterozygous",
            "Multi_allelic",
            "No_call",
        )
        assert geno["ZygosityMalformed"] is True

    def test_empty_gt_flagged_malformed(self):
        geno = _extract_genotype("GT", "")
        assert geno["ZygosityMalformed"] is True


# ─── HGVS notation ────────────────────────────────────────────────────────────


class TestHgvs:
    def test_snv_no_transcript(self):
        h = _build_hgvs("chr17", 43057051, "A", "T", None)
        assert h == "chr17:g.43057051A>T"

    def test_snv_with_transcript_but_no_cds_pos_falls_back_to_genomic(self):
        """ISSUE 1 REGRESSION: without a verified CDS-relative coordinate,
        _build_hgvs must NEVER pair a c. prefix with the raw genomic
        position (e.g. NM_xxxxx:c.94781858G>A) — that is invalid HGVS.
        It must fall back to genomic (g.) notation instead.
        """
        h = _build_hgvs("chr17", 43057051, "A", "T", "NM_007294.4")
        assert h == "chr17:g.43057051A>T"
        assert ":c." not in h

    def test_snv_with_verified_cds_pos_uses_c_notation(self):
        # When a true CDS-relative coordinate is supplied, c. notation
        # is used with that coordinate — not the genomic position.
        h = _build_hgvs(
            "chr17",
            43057051,
            "A",
            "T",
            "NM_007294.4",
            cds_pos=181,
            strand="+",
        )
        assert h == "NM_007294.4:c.181A>T"

    def test_snv_minus_strand_reverse_complements_bases(self):
        # On a minus-strand transcript, c. notation is on the sense
        # (transcript) strand, so ref/alt must be complemented.
        h = _build_hgvs(
            "chr17",
            43057051,
            "A",
            "T",
            "NM_999999.1",
            cds_pos=50,
            strand="-",
        )
        assert h == "NM_999999.1:c.50T>A"

    def test_noncoding_transcript_uses_n_notation(self):
        h = _build_hgvs(
            "chr1",
            1000,
            "G",
            "C",
            "NR_123456.1",
            cds_pos=42,
            strand="+",
        )
        assert h == "NR_123456.1:n.42G>C"

    def test_insertion(self):
        h = _build_hgvs("chr1", 100, "A", "AGG", None)
        assert "ins" in h
        assert "GG" in h

    def test_deletion(self):
        h = _build_hgvs("chr1", 100, "ATG", "A", None)
        assert "del" in h

    def test_indel_without_cds_pos_falls_back_to_genomic(self):
        # Indels also must never get a c. prefix without a verified,
        # contiguous CDS-relative range.
        h = _build_hgvs("chr1", 100, "ATG", "A", "NM_111111.1")
        assert h.startswith("chr1:g.")

    def test_indel_minus_strand_falls_back_to_genomic(self):
        # Minus-strand indel coordinate conversion is intentionally not
        # attempted (ambiguous without sequence context) — safe fallback.
        h = _build_hgvs(
            "chr1",
            100,
            "ATG",
            "A",
            "NM_111111.1",
            cds_pos=50,
            end_cds_pos=48,
            strand="-",
        )
        assert h.startswith("chr1:g.")

    def test_indel_plus_strand_with_verified_range_uses_c_notation(self):
        h = _build_hgvs(
            "chr1",
            100,
            "ATG",
            "A",
            "NM_111111.1",
            cds_pos=50,
            end_cds_pos=52,
            strand="+",
        )
        assert h == "NM_111111.1:c.51_52del"


# ─── GFF3 index ───────────────────────────────────────────────────────────────


class TestGffIndex:
    def _write_mini_gff3(self, path: Path) -> None:
        """Write a minimal GFF3 with one gene covering chr17:43044295-43125364 (BRCA1)."""
        path.write_text(
            "##gff-version 3\n"
            "NC_000017.11\tRefSeq\tgene\t43044295\t43125364\t.\t-\t.\t"
            "ID=gene-BRCA1;Name=BRCA1;gene=BRCA1;Dbxref=GeneID:672\n"
            "NC_000017.11\tRefSeq\tmRNA\t43044295\t43125364\t.\t-\t.\t"
            "ID=rna-NM_007294.4;Parent=gene-BRCA1;transcript_id=NM_007294.4;gene=BRCA1\n"
        )

    def test_lookup_known_position(self, tmp_path):
        gff = tmp_path / "mini.gff3"
        self._write_mini_gff3(gff)
        idx = GffIndex.from_file(str(gff))
        gene, tx = idx.lookup("chr17", 43057051)
        assert gene == "BRCA1"
        assert tx == "NM_007294.4"

    def test_lookup_intergenic_returns_none(self, tmp_path):
        gff = tmp_path / "mini.gff3"
        self._write_mini_gff3(gff)
        idx = GffIndex.from_file(str(gff))
        gene, tx = idx.lookup("chr17", 1)  # far outside BRCA1
        assert gene is None

    def test_missing_gff_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError) as exc_info:
            GffIndex.from_file(str(tmp_path / "missing.gff3"))
        assert "not found" in str(exc_info.value).lower()

    def test_chrom_aliases_resolved(self, tmp_path):
        gff = tmp_path / "mini.gff3"
        self._write_mini_gff3(gff)
        idx = GffIndex.from_file(str(gff))
        # Query using bare number "17" — should alias to chr17
        gene, _ = idx.lookup("17", 43057051)
        assert gene == "BRCA1"

    def test_lookup_position_one_single_gene(self, tmp_path):
        """Regression: lookup at pos=1 on a chromosome with one gene at start=1
        must return the gene, not crash (off-by-one i=-1 bug) or return None."""
        gff = tmp_path / "pos1.gff3"
        gff.write_text(
            "##gff-version 3\n"
            "chr1\tRefSeq\tgene\t1\t500\t.\t+\t.\t"
            "ID=gene-EARLY1;Name=EARLY1;gene=EARLY1\n"
        )
        idx = GffIndex.from_file(str(gff))
        gene, tx = idx.lookup("chr1", 1)
        assert gene == "EARLY1"

    def test_normalise_chrom(self):
        assert _normalise_chrom("NC_000017.11") == "chr17"
        assert _normalise_chrom("17") == "chr17"
        assert _normalise_chrom("chr17") == "chr17"
        assert _normalise_chrom("chrX") == "chrX"
        assert _normalise_chrom("MT") == "chrMT"


# ─── VCF parser ───────────────────────────────────────────────────────────────


class TestParseVcf:
    def _write_vcf(self, path: Path, body: str) -> None:
        path.write_text(
            "##fileformat=VCFv4.2\n"
            '##FILTER=<ID=PASS,Description="All filters passed">\n'
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n" + body
        )

    def test_parses_snv_with_genotype(self, tmp_path):
        vcf = tmp_path / "test.vcf"
        self._write_vcf(vcf, "chr17\t43057051\t.\tA\tT\t200\tPASS\t.\tGT:AD:DP\t0/1:15,10:25\n")
        variants = _parse_vcf(str(vcf))
        assert len(variants) == 1
        v = variants[0]
        assert v.chrom == "chr17"
        assert v.pos == 43057051
        assert v.ref == "A"
        assert v.alt == "T"
        assert v.zygosity == "Heterozygous"
        assert v.gt == "0/1"
        assert v.dp == "25"

    def test_missing_input_vcf_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _parse_vcf(str(tmp_path / "nope.vcf"))

    def test_empty_vcf_returns_empty_list(self, tmp_path):
        vcf = tmp_path / "empty.vcf"
        self._write_vcf(vcf, "")
        variants = _parse_vcf(str(vcf))
        assert variants == []


# ─── Annotation stage (end-to-end without GFF3) ───────────────────────────────


class TestAnnotationStageNoGff:
    def _write_vcf(self, path: Path) -> None:
        path.write_text(
            "##fileformat=VCFv4.2\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
            "chr17\t43057051\t.\tA\tT\t200\tPASS\t.\tGT:AD:DP\t0/1:15,10:25\n"
            "chr1\t925952\t.\tG\tA\t150\tPASS\t.\tGT:DP\t1/1:40\n"
        )

    def test_no_gff_require_false_runs_without_annotation(self, tmp_path):
        vcf = tmp_path / "filtered.vcf"
        self._write_vcf(vcf)
        stage = AnnotationStage({"annotation": {"require_gff": False}})
        result = stage.run(str(vcf), str(tmp_path / "ann_out"), sample_id="TEST")
        assert result.total_variants == 2
        assert result.annotated_count == 0  # no GFF3 → nothing annotated
        assert result.unannotated_count == 2
        # HGVS should still be generated even without a transcript
        for v in result.variants:
            assert v.hgvs  # non-empty

    def test_no_gff_require_true_raises(self, tmp_path):
        vcf = tmp_path / "filtered.vcf"
        self._write_vcf(vcf)
        stage = AnnotationStage({"annotation": {"require_gff": True}})
        with pytest.raises(FileNotFoundError) as exc_info:
            stage.run(str(vcf), str(tmp_path / "ann_out"))
        assert "GFF3" in str(exc_info.value) or "gff" in str(exc_info.value).lower()

    def test_json_output_written(self, tmp_path):
        vcf = tmp_path / "filtered.vcf"
        self._write_vcf(vcf)
        stage = AnnotationStage({"annotation": {"require_gff": False}})
        result = stage.run(str(vcf), str(tmp_path / "ann_out"), sample_id="S01")
        assert Path(result.annotation_json_path).exists()
        payload = json.loads(Path(result.annotation_json_path).read_text())
        assert payload["sample_id"] == "S01"
        assert "variants" in payload
        assert len(payload["variants"]) == 2

    def test_zygosity_in_output(self, tmp_path):
        vcf = tmp_path / "filtered.vcf"
        self._write_vcf(vcf)
        stage = AnnotationStage({"annotation": {"require_gff": False}})
        result = stage.run(str(vcf), str(tmp_path / "ann_out"))
        v0 = result.variants[0]
        assert v0.zygosity == "Heterozygous"
        v1 = result.variants[1]
        assert v1.zygosity == "Homozygous_alt"
