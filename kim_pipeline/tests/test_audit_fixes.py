"""
tests/test_audit_fixes.py
──────────────────────────
Regression tests for all bugs found during the Phase 2 audit:

  BUG 1 — BP4 missing AlphaMissense vote (asymmetric with PP3)
  BUG 2 — PM5 double-counted PS1 evidence (same_aa_pathogenic used for both)
  BUG 3 — result.errors not declared on PipelineResult dataclass
  BUG 4 — PP1 docstring falsely claimed PP4 uses segregates_with_disease
           (PP4 uses phenotype_specific_for_gene — logic was actually correct,
            just the documentation was wrong; verified here)
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence
from pipeline.orchestration.runner import PipelineResult


# ─── BUG 1: BP4 missing AlphaMissense ────────────────────────────────────────


class TestBp4AlphaMissense:
    """BP4 must include AlphaMissense in its benign vote (symmetric with PP3)."""

    def _clf(self):
        return AcmgClassifier()

    def test_bp4_fires_with_low_alphamissense_alone(self):
        """Low AlphaMissense alone should trigger BP4 (no other scores needed).

        FIX (Issue 4 audit): AlphaMissense is a missense-specific
        predictor — is_missense=True is required for its score to be
        counted at all (see AcmgClassifier._pp3/_bp4 docstrings).
        """
        ev = VariantEvidence(alphamissense_score=0.1, is_missense=True)  # well below 0.34 threshold
        r = self._clf().classify(ev)
        assert "BP4" in r.criteria_met, (
            "BP4 should fire when AlphaMissense < bp4_alphamissense threshold"
        )

    def test_bp4_not_fired_with_high_alphamissense(self):
        """High AlphaMissense alone should NOT trigger BP4."""
        ev = VariantEvidence(alphamissense_score=0.9, is_missense=True)
        r = self._clf().classify(ev)
        assert "BP4" not in r.criteria_met

    def test_bp4_alphamissense_joins_majority_vote(self):
        """AlphaMissense participates in the majority-vote — two benign, one damaging."""
        # CADD=5 (benign), AM=0.1 (benign), REVEL=0.9 (damaging)
        # 2/3 benign → majority benign → BP4 met
        ev = VariantEvidence(
            cadd_phred=5.0, alphamissense_score=0.1, revel_score=0.9, is_missense=True
        )
        r = self._clf().classify(ev)
        assert "BP4" in r.criteria_met

    def test_bp4_not_fired_when_alphamissense_damaging_breaks_majority(self):
        """AlphaMissense damaging + benign CADD → not majority benign."""
        # Only 1 score: CADD benign=True, AM benign=False → 1/2 = not majority
        ev = VariantEvidence(cadd_phred=5.0, alphamissense_score=0.9, is_missense=True)
        r = self._clf().classify(ev)
        assert "BP4" not in r.criteria_met

    def test_pp3_and_bp4_symmetric_with_alphamissense(self):
        """PP3 and BP4 cannot both fire for the same AlphaMissense score."""
        ev_high = VariantEvidence(alphamissense_score=0.9, is_missense=True)
        r_high = self._clf().classify(ev_high)
        assert "PP3" in r_high.criteria_met
        assert "BP4" not in r_high.criteria_met

        ev_low = VariantEvidence(alphamissense_score=0.1, is_missense=True)
        r_low = self._clf().classify(ev_low)
        assert "BP4" in r_low.criteria_met
        assert "PP3" not in r_low.criteria_met


# ─── BUG 2: PM5 / PS1 double-counting ────────────────────────────────────────


class TestPm5Ps1NoDuplication:
    """PM5 and PS1 must not both fire on the same evidence field."""

    def _clf(self):
        return AcmgClassifier()

    def test_ps1_fires_on_same_aa_pathogenic(self):
        """PS1: same amino-acid change as known pathogenic."""
        ev = VariantEvidence(same_aa_pathogenic=True, is_missense=True)
        r = self._clf().classify(ev)
        assert "PS1" in r.criteria_met

    def test_pm5_does_not_fire_on_same_aa_pathogenic(self):
        """PM5 must NOT fire when same_aa_pathogenic=True (that is PS1's evidence)."""
        ev = VariantEvidence(same_aa_pathogenic=True, is_missense=True)
        r = self._clf().classify(ev)
        assert "PM5" not in r.criteria_met, (
            "PM5 and PS1 must not double-count the same evidence field"
        )

    def test_pm5_fires_on_novel_aa_at_known_pathogenic_codon(self):
        """PM5: novel amino-acid change at codon with known pathogenic missense."""
        ev = VariantEvidence(novel_aa_at_known_pathogenic_codon=True, is_missense=True)
        r = self._clf().classify(ev)
        assert "PM5" in r.criteria_met
        assert "PS1" not in r.criteria_met

    def test_pm5_not_fired_when_not_missense(self):
        ev = VariantEvidence(novel_aa_at_known_pathogenic_codon=True, is_missense=False)
        r = self._clf().classify(ev)
        assert "PM5" not in r.criteria_met

    def test_pm5_not_fired_when_none(self):
        ev = VariantEvidence(is_missense=True)
        r = self._clf().classify(ev)
        assert "PM5" not in r.criteria_met

    def test_ps2_and_pm6_mutually_exclusive(self):
        """ISSUE 7 FIX: When confirmed_de_novo=True (PS2), PM6 must NOT fire.

        Confirmed de novo (PS2, Strong) and assumed de novo (PM6, Moderate)
        describe the same underlying observation at two confidence levels,
        not two independent pieces of evidence. If upstream pedigree data
        ever sets both flags for the same variant, PS2 (the stronger,
        confirmed observation) must take precedence and PM6 must be
        suppressed — otherwise a single de novo event is double-counted.
        This mirrors the existing PS1/PM5 mutual-exclusion fix (FIX 11)
        for the identical double-counting pattern.
        """
        ev = VariantEvidence(confirmed_de_novo=True, assumed_de_novo=True)
        r = self._clf().classify(ev)
        assert "PS2" in r.criteria_met, "PS2 should fire when confirmed_de_novo=True"
        assert "PM6" not in r.criteria_met, (
            "PM6 must not fire when PS2 is active (ACMG PS2/PM6 mutual exclusion)"
        )

    def test_pm6_fires_without_ps2(self):
        """PM6 fires when assumed_de_novo=True and PS2 is NOT active."""
        ev = VariantEvidence(assumed_de_novo=True, confirmed_de_novo=False)
        r = self._clf().classify(ev)
        assert "PM6" in r.criteria_met
        assert "PS2" not in r.criteria_met

    def test_both_ps1_and_pm5_can_fire_independently(self):
        """FIX 11: When same_aa_pathogenic=True (PS1), PM5 must NOT fire.

        ACMG guidance: PS1 and PM5 are mutually exclusive evidence.
        - PS1: same amino-acid change at same codon = established pathogenic.
        - PM5: DIFFERENT amino-acid change at same codon = novel missense.
        A single variant cannot simultaneously produce both the same AND a
        different amino-acid substitution. When both fields are set, PS1 takes
        precedence and PM5 is suppressed to prevent double-counting.
        """
        ev = VariantEvidence(
            same_aa_pathogenic=True,
            novel_aa_at_known_pathogenic_codon=True,
            is_missense=True,
        )
        r = self._clf().classify(ev)
        # PS1 fires; PM5 must NOT fire (mutual exclusion)
        assert "PS1" in r.criteria_met, "PS1 should fire when same_aa_pathogenic=True"
        assert "PM5" not in r.criteria_met, (
            "PM5 must not fire when PS1 is active (ACMG PS1/PM5 mutual exclusion)"
        )

    def test_pm5_fires_without_ps1(self):
        """PM5 fires when novel_aa_at_known_pathogenic_codon=True and PS1 is NOT active."""
        ev = VariantEvidence(
            same_aa_pathogenic=None,  # PS1 NOT active
            novel_aa_at_known_pathogenic_codon=True,
            is_missense=True,
        )
        r = self._clf().classify(ev)
        assert "PM5" in r.criteria_met, "PM5 should fire when PS1 is not active"
        assert "PS1" not in r.criteria_met


# ─── BUG 3: PipelineResult.errors field ──────────────────────────────────────


class TestPipelineResultErrorsField:
    """errors must be a declared dataclass field, not dynamically set."""

    def test_errors_field_declared_on_dataclass(self):
        fields = {f.name for f in dataclasses.fields(PipelineResult)}
        assert "errors" in fields, (
            "PipelineResult.errors must be a declared dataclass field, "
            "not set dynamically with setattr/getattr"
        )

    def test_errors_defaults_to_empty_list(self):
        r = PipelineResult()
        assert r.errors == []
        assert isinstance(r.errors, list)

    def test_errors_can_be_appended(self):
        r = PipelineResult()
        r.errors.append("Stage 4b setup error: test")
        assert len(r.errors) == 1
        assert "Stage 4b" in r.errors[0]

    def test_errors_included_in_to_dict(self):
        r = PipelineResult(errors=["some error"])
        d = r.to_dict()
        assert "errors" in d
        assert d["errors"] == ["some error"]

    def test_errors_list_not_shared_between_instances(self):
        """Mutable default must use field(default_factory=list), not [] directly."""
        r1 = PipelineResult()
        r2 = PipelineResult()
        r1.errors.append("err")
        assert r2.errors == [], (
            "errors lists must not be shared between instances — use field(default_factory=list)"
        )


# ─── BUG 4: PP1 / PP4 field independence ─────────────────────────────────────


class TestPp1Pp4Independence:
    """PP1 uses segregates_with_disease; PP4 uses phenotype_specific_for_gene."""

    def _clf(self):
        return AcmgClassifier()

    def test_pp1_and_pp4_use_independent_fields(self):
        """PP1 and PP4 cannot both fire from a single field."""
        # Only segregates_with_disease → PP1 met, PP4 not met
        ev1 = VariantEvidence(segregates_with_disease=True, phenotype_specific_for_gene=False)
        r1 = self._clf().classify(ev1)
        assert "PP1" in r1.criteria_met
        assert "PP4" not in r1.criteria_met

        # Only phenotype_specific_for_gene → PP4 met, PP1 not met
        ev2 = VariantEvidence(segregates_with_disease=False, phenotype_specific_for_gene=True)
        r2 = self._clf().classify(ev2)
        assert "PP4" in r2.criteria_met
        assert "PP1" not in r2.criteria_met

    def test_both_pp1_and_pp4_can_fire_together(self):
        """When both fields are True, both criteria fire — that is correct."""
        ev = VariantEvidence(segregates_with_disease=True, phenotype_specific_for_gene=True)
        r = self._clf().classify(ev)
        assert "PP1" in r.criteria_met
        assert "PP4" in r.criteria_met

    def test_neither_fires_when_both_false(self):
        ev = VariantEvidence(segregates_with_disease=False, phenotype_specific_for_gene=False)
        r = self._clf().classify(ev)
        assert "PP1" not in r.criteria_met
        assert "PP4" not in r.criteria_met


# ─── Additional: novel_aa field in VariantEvidence ────────────────────────────


class TestNovelAaField:
    """novel_aa_at_known_pathogenic_codon field behaves correctly."""

    def test_field_defaults_to_none(self):
        ev = VariantEvidence()
        assert ev.novel_aa_at_known_pathogenic_codon is None

    def test_field_included_in_to_dict(self):
        ev = VariantEvidence(novel_aa_at_known_pathogenic_codon=True)
        d = ev.to_dict()
        assert "novel_aa_at_known_pathogenic_codon" in d
        assert d["novel_aa_at_known_pathogenic_codon"] is True
