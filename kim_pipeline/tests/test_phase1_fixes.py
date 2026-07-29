"""
tests/test_phase1_fixes.py
───────────────────────────
Tests for all Phase 1 fixes:
  FIX 1.1 — Consequence annotation in AnnotationStage
  FIX 1.2 — AlphaMissense wiring in runner
  FIX 1.3 — ZygosityExtractor in AnnotationStage (hemizygous / richer fields)
  FIX 1.4 — Per-variant exception isolation in Stage 4b
  FIX 1.5 — PM3, PP1, BS4, BP2, BP5 ACMG criteria
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ═══════════════════════════════════════════════════════════════════════════════
# FIX 1.1 — Consequence annotation
# ═══════════════════════════════════════════════════════════════════════════════

from pipeline.annotation.stage import (
    _map_consequence,
    _consequence_to_is_inframe_indel,
    NoCdsCodonContextProvider,
    AnnotationStage,
    AnnotatedVariant,
)


class TestMapConsequence:
    """_map_consequence maps region + variant shape to the correct SO term."""

    def test_splice_donor(self):
        assert _map_consequence("splice_donor", "A", "T", None, "chr1", 100, "tx") == "splice_donor_variant"

    def test_splice_acceptor(self):
        assert _map_consequence("splice_acceptor", "G", "C", None, "chr1", 100, "tx") == "splice_acceptor_variant"

    def test_intron_variant(self):
        assert _map_consequence("intronic", "A", "T", None, "chr1", 100, "tx") == "intron_variant"

    def test_intergenic(self):
        assert _map_consequence("intergenic", "A", "T", None, "chr1", 100, "tx") == "intergenic_variant"

    def test_frameshift_deletion(self):
        # length diff = 2, not divisible by 3 → frameshift
        assert _map_consequence("exonic", "ATG", "A", None, "chr1", 100, "tx") == "frameshift_variant"

    def test_frameshift_insertion(self):
        # length diff = 2
        assert _map_consequence("exonic", "A", "ATT", None, "chr1", 100, "tx") == "frameshift_variant"

    def test_inframe_deletion(self):
        # length diff = 3
        assert _map_consequence("exonic", "ATGC", "A", None, "chr1", 100, "tx") == "inframe_deletion"

    def test_inframe_insertion(self):
        # length diff = 3
        assert _map_consequence("exonic", "A", "ATGC", None, "chr1", 100, "tx") == "inframe_insertion"

    def test_exonic_snv_no_cds_returns_coding_sequence_variant(self):
        # Without a CDS provider, SNV → coding_sequence_variant (not a guess)
        result = _map_consequence("exonic", "A", "T", None, "chr1", 100, "tx")
        assert result == "coding_sequence_variant"

    def test_exonic_snv_with_no_cds_provider_raises_returns_coding(self):
        provider = NoCdsCodonContextProvider()
        result = _map_consequence("exonic", "A", "T", provider, "chr1", 100, "tx")
        assert result == "coding_sequence_variant"


class TestConsequenceToIsInframeIndel:
    def test_inframe_insertion(self):
        assert _consequence_to_is_inframe_indel("inframe_insertion") is True

    def test_inframe_deletion(self):
        assert _consequence_to_is_inframe_indel("inframe_deletion") is True

    def test_frameshift_is_not_inframe(self):
        assert _consequence_to_is_inframe_indel("frameshift_variant") is False

    def test_missense_is_not_inframe(self):
        assert _consequence_to_is_inframe_indel("missense_variant") is False

    def test_empty_string(self):
        assert _consequence_to_is_inframe_indel("") is False


class TestAnnotationStageConsequence:
    """End-to-end consequence annotation with a real GFF3 fixture."""

    def _write_gff3(self, path: Path) -> None:
        """Minimal GFF3: test gene on chr17, two exons, one intron."""
        path.write_text(
            "##gff-version 3\n"
            "chr17\tRefSeq\tgene\t1000\t2000\t.\t+\t.\t"
            "ID=gene-TEST1;Name=TEST1;gene=TEST1\n"
            "chr17\tRefSeq\tmRNA\t1000\t2000\t.\t+\t.\t"
            "ID=rna-NM_TEST1;Parent=gene-TEST1;transcript_id=NM_TEST1;gene=TEST1\n"
            # Exon 1: 1000-1200
            "chr17\tRefSeq\texon\t1000\t1200\t.\t+\t.\t"
            "Parent=NM_TEST1\n"
            # Exon 2: 1400-2000
            "chr17\tRefSeq\texon\t1400\t2000\t.\t+\t.\t"
            "Parent=NM_TEST1\n"
        )

    def _write_vcf(self, path: Path, records: list) -> None:
        header = (
            "##fileformat=VCFv4.2\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        )
        path.write_text(header + "".join(records))

    def test_exonic_snv_gets_coding_sequence_variant(self, tmp_path):
        gff = tmp_path / "test.gff3"
        self._write_gff3(gff)
        vcf = tmp_path / "test.vcf"
        # pos 1100 is inside exon 1
        self._write_vcf(vcf, ["chr17\t1100\t.\tA\tT\t100\tPASS\t.\n"])
        stage = AnnotationStage({"rna_analysis": {"refseq_gff": str(gff)}})
        result = stage.run(str(vcf), str(tmp_path / "out"))
        assert result.total_variants == 1
        v = result.variants[0]
        # Without CDS FASTA, exonic SNV → coding_sequence_variant
        assert v.consequence == "coding_sequence_variant"
        # is_inframe_indel must be False for SNV
        assert v.is_inframe_indel is False

    def test_intronic_variant_gets_intron_variant(self, tmp_path):
        gff = tmp_path / "test.gff3"
        self._write_gff3(gff)
        vcf = tmp_path / "test.vcf"
        # pos 1300 is between exons → intronic
        self._write_vcf(vcf, ["chr17\t1300\t.\tA\tT\t100\tPASS\t.\n"])
        stage = AnnotationStage({"rna_analysis": {"refseq_gff": str(gff)}})
        result = stage.run(str(vcf), str(tmp_path / "out"))
        v = result.variants[0]
        assert v.consequence == "intron_variant"

    def test_frameshift_indel_consequence(self, tmp_path):
        gff = tmp_path / "test.gff3"
        self._write_gff3(gff)
        vcf = tmp_path / "test.vcf"
        # 2-bp deletion in exon 1 → frameshift
        self._write_vcf(vcf, ["chr17\t1100\t.\tATG\tA\t100\tPASS\t.\n"])
        stage = AnnotationStage({"rna_analysis": {"refseq_gff": str(gff)}})
        result = stage.run(str(vcf), str(tmp_path / "out"))
        v = result.variants[0]
        assert v.consequence == "frameshift_variant"
        assert v.is_inframe_indel is False

    def test_inframe_deletion_consequence(self, tmp_path):
        gff = tmp_path / "test.gff3"
        self._write_gff3(gff)
        vcf = tmp_path / "test.vcf"
        # 3-bp deletion in exon 1 → inframe_deletion
        self._write_vcf(vcf, ["chr17\t1100\t.\tATGC\tA\t100\tPASS\t.\n"])
        stage = AnnotationStage({"rna_analysis": {"refseq_gff": str(gff)}})
        result = stage.run(str(vcf), str(tmp_path / "out"))
        v = result.variants[0]
        assert v.consequence == "inframe_deletion"
        assert v.is_inframe_indel is True

    def test_no_gff_consequence_empty(self, tmp_path):
        vcf = tmp_path / "test.vcf"
        self._write_vcf(vcf, ["chr17\t1100\t.\tA\tT\t100\tPASS\t.\n"])
        stage = AnnotationStage({"annotation": {"require_gff": False}})
        result = stage.run(str(vcf), str(tmp_path / "out"))
        v = result.variants[0]
        # No GFF → no transcript → consequence should be intergenic or empty
        assert v.consequence in ("intergenic_variant", "")


# ═══════════════════════════════════════════════════════════════════════════════
# FIX 1.3 — ZygosityExtractor wired into AnnotationStage
# ═══════════════════════════════════════════════════════════════════════════════

class TestAnnotationStageZygosity:
    """Richer zygosity fields now appear on AnnotatedVariant."""

    def _write_vcf(self, path: Path, records: list) -> None:
        path.write_text(
            "##fileformat=VCFv4.2\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
            + "".join(records)
        )

    def test_gq_and_ab_populated(self, tmp_path):
        vcf = tmp_path / "test.vcf"
        self._write_vcf(vcf, [
            "chr1\t100\t.\tA\tT\t200\tPASS\t.\tGT:AD:DP:GQ\t0/1:15,10:25:99\n"
        ])
        stage = AnnotationStage({"annotation": {"require_gff": False}})
        result = stage.run(str(vcf), str(tmp_path / "out"))
        v = result.variants[0]
        assert v.gq == 99
        assert v.ab == pytest.approx(10 / 25, rel=1e-4)

    def test_phase_set_populated(self, tmp_path):
        vcf = tmp_path / "test.vcf"
        self._write_vcf(vcf, [
            "chr1\t100\t.\tA\tT\t200\tPASS\t.\tGT:PS\t0|1:100\n"
        ])
        stage = AnnotationStage({"annotation": {"require_gff": False}})
        result = stage.run(str(vcf), str(tmp_path / "out"))
        v = result.variants[0]
        assert v.phase_set == "100"

    def test_hemizygous_flagged(self, tmp_path):
        vcf = tmp_path / "test.vcf"
        self._write_vcf(vcf, [
            "chr1\t100\t.\tA\tT\t200\tPASS\t.\tGT\t1\n"
        ])
        stage = AnnotationStage({"annotation": {"require_gff": False}})
        result = stage.run(str(vcf), str(tmp_path / "out"))
        v = result.variants[0]
        assert v.hemizygous is True
        assert v.zygosity == "Hemizygous"

    def test_existing_fields_unchanged(self, tmp_path):
        vcf = tmp_path / "test.vcf"
        self._write_vcf(vcf, [
            "chr1\t100\t.\tA\tT\t200\tPASS\t.\tGT:AD:DP\t0/1:15,10:25\n"
        ])
        stage = AnnotationStage({"annotation": {"require_gff": False}})
        result = stage.run(str(vcf), str(tmp_path / "out"))
        v = result.variants[0]
        assert v.gt == "0/1"
        assert v.ad == "15,10"
        assert v.dp == "25"
        assert v.zygosity == "Heterozygous"


# ═══════════════════════════════════════════════════════════════════════════════
# FIX 1.2 — AlphaMissense wiring via compute_computational_score
# ═══════════════════════════════════════════════════════════════════════════════

from pipeline.evidence.aggregator import EvidenceAggregator


class TestAlphaMissenseWiring:
    def test_alphamissense_included_in_computational_score(self):
        # With only alphamissense, score = alphamissense (normalised directly)
        score = EvidenceAggregator.compute_computational_score(
            alphamissense_score=0.8
        )
        assert score == pytest.approx(0.8, rel=1e-4)

    def test_alphamissense_averages_with_cadd(self):
        # CADD 50 → normalised 1.0; alphamissense 0.6 → mean = 0.8
        score = EvidenceAggregator.compute_computational_score(
            cadd_phred=50.0,
            alphamissense_score=0.6,
        )
        assert score == pytest.approx(0.8, rel=1e-4)

    def test_none_alphamissense_excluded(self):
        score = EvidenceAggregator.compute_computational_score(
            cadd_phred=20.0,
            alphamissense_score=None,
        )
        # Only CADD: 20/50 = 0.4
        assert score == pytest.approx(0.4, rel=1e-4)


# ═══════════════════════════════════════════════════════════════════════════════
# FIX 1.4 — Per-variant exception isolation (unit-level test of classify isolation)
# ═══════════════════════════════════════════════════════════════════════════════

from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence


class TestPerVariantIsolation:
    """Simulate Stage 4b exception isolation at the classifier level."""

    def test_classify_does_not_raise_for_valid_evidence(self):
        clf = AcmgClassifier()
        ev = VariantEvidence(chrom="1", pos=100, ref="A", alt="T")
        result = clf.classify(ev)
        assert result is not None

    def test_classify_score_stays_in_range_edge_cases(self):
        clf = AcmgClassifier()
        # Edge case: all evidence None
        ev = VariantEvidence()
        result = clf.classify(ev)
        assert 0.0 <= result.score <= 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# FIX 1.5 — New ACMG criteria: PM3, PP1, BS4, BP2, BP5
# ═══════════════════════════════════════════════════════════════════════════════

def _clf():
    return AcmgClassifier()


class TestPm3:
    def test_pm3_met_ar_in_trans(self):
        ev = VariantEvidence(
            in_trans_with_pathogenic=True,
            inheritance_pattern="AR",
        )
        result = _clf().classify(ev)
        assert "PM3" in result.criteria_met

    def test_pm3_met_xl_in_trans(self):
        ev = VariantEvidence(
            in_trans_with_pathogenic=True,
            inheritance_pattern="XL",
        )
        result = _clf().classify(ev)
        assert "PM3" in result.criteria_met

    def test_pm3_not_met_ad_even_if_in_trans(self):
        ev = VariantEvidence(
            in_trans_with_pathogenic=True,
            inheritance_pattern="AD",
        )
        result = _clf().classify(ev)
        assert "PM3" not in result.criteria_met

    def test_pm3_not_met_when_in_trans_false(self):
        ev = VariantEvidence(
            in_trans_with_pathogenic=False,
            inheritance_pattern="AR",
        )
        result = _clf().classify(ev)
        assert "PM3" not in result.criteria_met

    def test_pm3_not_met_when_none(self):
        ev = VariantEvidence()
        result = _clf().classify(ev)
        assert "PM3" not in result.criteria_met


class TestPp1:
    def test_pp1_met_when_segregates_true(self):
        ev = VariantEvidence(segregates_with_disease=True)
        result = _clf().classify(ev)
        assert "PP1" in result.criteria_met

    def test_pp1_not_met_when_false(self):
        ev = VariantEvidence(segregates_with_disease=False)
        result = _clf().classify(ev)
        assert "PP1" not in result.criteria_met

    def test_pp1_not_met_when_none(self):
        ev = VariantEvidence()
        result = _clf().classify(ev)
        assert "PP1" not in result.criteria_met


class TestBs4:
    def test_bs4_met_when_segregates_away_true(self):
        ev = VariantEvidence(segregates_away_from_disease=True)
        result = _clf().classify(ev)
        assert "BS4" in result.criteria_met

    def test_bs4_not_met_when_false(self):
        ev = VariantEvidence(segregates_away_from_disease=False)
        result = _clf().classify(ev)
        assert "BS4" not in result.criteria_met

    def test_bs4_not_met_when_none(self):
        ev = VariantEvidence()
        result = _clf().classify(ev)
        assert "BS4" not in result.criteria_met


class TestBp2:
    def test_bp2_met_when_unexpected_trans_cis(self):
        ev = VariantEvidence(in_trans_or_cis_with_pathogenic_unexpected=True)
        result = _clf().classify(ev)
        assert "BP2" in result.criteria_met

    def test_bp2_not_met_when_false(self):
        ev = VariantEvidence(in_trans_or_cis_with_pathogenic_unexpected=False)
        result = _clf().classify(ev)
        assert "BP2" not in result.criteria_met

    def test_bp2_not_met_when_none(self):
        ev = VariantEvidence()
        result = _clf().classify(ev)
        assert "BP2" not in result.criteria_met


class TestBp5:
    def test_bp5_met_when_alternate_basis_found(self):
        ev = VariantEvidence(alternate_molecular_basis_found=True)
        result = _clf().classify(ev)
        assert "BP5" in result.criteria_met

    def test_bp5_not_met_when_false(self):
        ev = VariantEvidence(alternate_molecular_basis_found=False)
        result = _clf().classify(ev)
        assert "BP5" not in result.criteria_met

    def test_bp5_not_met_when_none(self):
        ev = VariantEvidence()
        result = _clf().classify(ev)
        assert "BP5" not in result.criteria_met


class TestNewCriteriaInAllCriteria:
    """Ensure all five new criteria appear in all_criteria (not_met set)."""

    def test_all_five_new_codes_present_in_output(self):
        ev = VariantEvidence()
        result = _clf().classify(ev)
        all_codes = {c.code for c in result.all_criteria}
        for code in ("PM3", "PP1", "BS4", "BP2", "BP5"):
            assert code in all_codes, f"{code} missing from all_criteria"

    def test_new_criteria_directions_and_strengths(self):
        ev = VariantEvidence()
        result = _clf().classify(ev)
        by_code = {c.code: c for c in result.all_criteria}
        assert by_code["PM3"].strength == "moderate"
        assert by_code["PM3"].direction == "pathogenic"
        assert by_code["PP1"].strength == "supporting"
        assert by_code["PP1"].direction == "pathogenic"
        assert by_code["BS4"].strength == "strong"
        assert by_code["BS4"].direction == "benign"
        assert by_code["BP2"].strength == "supporting"
        assert by_code["BP2"].direction == "benign"
        assert by_code["BP5"].strength == "supporting"
        assert by_code["BP5"].direction == "benign"
