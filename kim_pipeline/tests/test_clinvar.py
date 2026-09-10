"""
tests/test_clinvar.py
─────────────────────
Unit tests for pipeline.clinvar.lookup.ClinVarLookup.
"""

import gzip
from pathlib import Path

import pytest

from pipeline.clinvar.lookup import ClinVarLookup, ClinVarHit, _review_stars
from pipeline.evidence.aggregator import EvidenceAggregator


# ── Helpers ───────────────────────────────────────────────────────────────────

TSV_HEADER = (
    "#AlleleID\tType\tGeneSymbol\tClinicalSignificance\tReviewStatus\t"
    "NumberSubmitters\tChromosome\tStart\tReferenceAllele\tAlternateAllele\n"
)

TSV_ROW_PATHOGENIC = (
    "12345\tsnv\tBRCA1\tPathogenic\t"
    "criteria provided, multiple submitters, no conflicts\t3\t17\t43057051\tA\tT\n"
)

TSV_ROW_BENIGN = (
    "99999\tsnv\tTP53\tBenign\tcriteria provided, single submitter\t1\t17\t7577121\tC\tG\n"
)

TSV_ROW_CHR_PREFIX = (
    "11111\tsnv\tSCN5A\tPathogenic\treviewed by expert panel\t5\tchr3\t38674422\tG\tA\n"
)

TSV_ROW_CONFLICTING = (
    "22222\tsnv\tMLH1\tPathogenic/Likely pathogenic\t"
    "criteria provided, conflicting interpretations\t4\t3\t37034481\tT\tC\n"
)


def _write_tsv_gz(tmp_path: Path, rows: list[str]) -> Path:
    """Write a minimal variant_summary.txt.gz file and return its path."""
    gz_path = tmp_path / "variant_summary.txt.gz"
    content = TSV_HEADER + "".join(rows)
    with gzip.open(gz_path, "wt", encoding="utf-8") as fh:
        fh.write(content)
    return gz_path


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_local_tsv_hit(tmp_path):
    """Lookup a known variant returns a ClinVarHit with correct significance."""
    gz = _write_tsv_gz(tmp_path, [TSV_ROW_PATHOGENIC])
    lookup = ClinVarLookup(cfg={"clinvar": {"tsv_gz_path": str(gz)}})
    hit = lookup.lookup("17", 43057051, "A", "T")
    assert hit is not None
    assert hit.significance == "Pathogenic"
    assert hit.gene == "BRCA1"
    assert hit.allele_id == "12345"


def test_local_tsv_miss(tmp_path):
    """Lookup of an unknown variant returns None."""
    gz = _write_tsv_gz(tmp_path, [TSV_ROW_PATHOGENIC])
    lookup = ClinVarLookup(cfg={"clinvar": {"tsv_gz_path": str(gz)}})
    assert lookup.lookup("17", 1, "A", "T") is None


def test_local_tsv_chr_prefix_normalised(tmp_path):
    """Variants stored with 'chr' prefix are found when queried without it."""
    gz = _write_tsv_gz(tmp_path, [TSV_ROW_CHR_PREFIX])
    lookup = ClinVarLookup(cfg={"clinvar": {"tsv_gz_path": str(gz)}})
    hit = lookup.lookup("3", 38674422, "G", "A")
    assert hit is not None
    assert hit.significance == "Pathogenic"


def test_local_tsv_conflicting(tmp_path):
    """Conflicting ReviewStatus sets conflicting=True on the hit."""
    gz = _write_tsv_gz(tmp_path, [TSV_ROW_CONFLICTING])
    lookup = ClinVarLookup(cfg={"clinvar": {"tsv_gz_path": str(gz)}})
    hit = lookup.lookup("3", 37034481, "T", "C")
    assert hit is not None
    assert hit.conflicting is True


def test_sig_to_score_pathogenic():
    """Pathogenic with stars ≥ 1 → 1.0."""
    assert ClinVarLookup.sig_to_score("Pathogenic", 3) == 1.0


def test_sig_to_score_benign():
    """Benign with stars ≥ 1 → 0.0."""
    assert ClinVarLookup.sig_to_score("Benign", 2) == 0.0


def test_sig_to_score_zero_stars_dampens():
    """stars=0 dampens score 10% toward 0.5."""
    score = ClinVarLookup.sig_to_score("Pathogenic", 0)
    assert score < 1.0
    assert score > 0.5


def test_sig_to_score_vus():
    """VUS / Uncertain significance → 0.5 regardless of stars."""
    assert ClinVarLookup.sig_to_score("Uncertain significance", 0) == 0.5
    assert ClinVarLookup.sig_to_score("Uncertain significance", 4) == 0.5


def test_sig_to_score_likely_pathogenic():
    """Likely pathogenic → 0.75 (stars ≥ 1)."""
    assert ClinVarLookup.sig_to_score("Likely pathogenic", 1) == 0.75


def test_sig_to_score_likely_benign():
    """Likely benign → 0.25 (stars ≥ 1)."""
    assert ClinVarLookup.sig_to_score("Likely benign", 1) == 0.25


def test_review_stars_all_tiers():
    """Each known ReviewStatus maps to the correct star count."""
    assert _review_stars("practice guideline") == 4
    assert _review_stars("reviewed by expert panel") == 3
    assert _review_stars("criteria provided, multiple submitters, no conflicts") == 2
    assert _review_stars("criteria provided, single submitter") == 1
    assert _review_stars("no assertion criteria provided") == 0
    assert _review_stars("") == 0


def test_local_multiple_variants_both_found(tmp_path):
    """Multiple variants in the file are all indexed and retrievable."""
    gz = _write_tsv_gz(tmp_path, [TSV_ROW_PATHOGENIC, TSV_ROW_BENIGN])
    lookup = ClinVarLookup(cfg={"clinvar": {"tsv_gz_path": str(gz)}})
    assert lookup.lookup("17", 43057051, "A", "T") is not None
    assert lookup.lookup("17", 7577121, "C", "G") is not None


# ─────────────────────────────────────────────────────────────────────────────
# FIX 2 regression — ClinVar score must call EvidenceAggregator.clinvar_sig_to_score,
# not the non-existent ClinVarLookup.sig_to_score
# ─────────────────────────────────────────────────────────────────────────────


class TestFix2ClinVarScoreMethod:
    """Regression tests for: ClinVar score method called on wrong class."""

    def test_clinvar_sig_to_score_returns_float_for_pathogenic(self):
        """clinvar_sig_to_score('Pathogenic', stars=2) must return a float, not None."""
        cv_score = EvidenceAggregator.clinvar_sig_to_score("Pathogenic", 2)
        assert cv_score is not None, "cv_score must not be None for Pathogenic"
        assert isinstance(cv_score, float), f"cv_score must be float, got {type(cv_score).__name__}"

    def test_runner_calls_evidence_aggregator_not_clinvar_lookup(self):
        """The ClinVar scoring call must reference EvidenceAggregator.clinvar_sig_to_score,
        not ClinVarLookup.sig_to_score. This logic now lives in
        pipeline/orchestration/shared.run_acmg_evidence_batch (extracted from
        runner.py so `analyze` and `vcf` share the same implementation), so
        check both modules."""
        import inspect
        from pipeline.orchestration import runner as runner_mod
        from pipeline.orchestration import shared as shared_mod

        combined_src = inspect.getsource(runner_mod) + inspect.getsource(shared_mod)
        assert "EvidenceAggregator.clinvar_sig_to_score" in combined_src, (
            "ACMG/evidence orchestration must call EvidenceAggregator.clinvar_sig_to_score"
        )
        assert "ClinVarLookup.sig_to_score" not in combined_src, (
            "orchestration code must not call ClinVarLookup.sig_to_score"
        )

    def test_no_attribute_error_when_computing_cv_score(self):
        """AttributeError must not be raised when computing cv_score via the fixed call."""
        hit = ClinVarHit(
            significance="Pathogenic",
            review_stars=2,
            submitter_count=3,
            allele_id="12345",
            conflicting=False,
            gene="BRCA1",
        )
        # Must not raise AttributeError
        try:
            cv_score = EvidenceAggregator.clinvar_sig_to_score(hit.significance, hit.review_stars)
        except AttributeError as exc:
            pytest.fail(f"AttributeError raised: {exc}")
        assert cv_score is not None

    def test_clinvar_stream_in_evidence_result_when_hit_present(self):
        """When a ClinVar hit is present, 'clinvar' must appear in streams_used."""
        from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence

        ev = VariantEvidence(chrom="17", pos=43057051, ref="A", alt="T")
        acmg_result = AcmgClassifier(cfg={}).classify(ev)

        cv_score = EvidenceAggregator.clinvar_sig_to_score("Pathogenic", 2)
        agg = EvidenceAggregator(cfg={})
        ev_result = agg.aggregate_from_acmg(
            acmg_result=acmg_result,
            clinvar_score=cv_score,
        )
        assert "clinvar" in ev_result.streams_used, (
            f"'clinvar' must be in streams_used when cv_score is present; "
            f"got {ev_result.streams_used}"
        )
