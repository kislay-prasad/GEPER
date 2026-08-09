"""
Tests for PP3/BP4 mutual exclusivity
(`pipeline/acmg_rules.py::ACMGRuleEngine._pp3_bp4`).

Background: `_pp3` ("computational evidence supports a deleterious
effect") and `_bp4` ("computational evidence suggests no deleterious
effect") used to be two fully independent scans of the same four
computational evidence sources (AlphaMissense, MMSplice, the
Enformer/Borzoi AI ensemble, and PhyloP/PhastCons/GERP++
conservation). Nothing stopped PP3 finding a damaging signal from one
source while BP4 simultaneously found a benign signal from another --
both reported "triggered" on the same variant, which is a
contradiction (PP3 and BP4 are logical opposites). This was observed
in a real GEPER report: multiple variants had both PP3 and BP4
triggered with overlapping predictor lists.

`_pp3_bp4` now computes one shared set of damaging/benign signals and
guarantees at most one of {PP3 triggered, BP4 triggered} for a given
variant: if both directions are present, computational evidence is
self-contradictory and *neither* triggers (see that method's own
docstring for the full reasoning on why "neither wins" was chosen over
a priority order or majority vote).
"""

import unittest
from unittest import mock

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.pvs1.utils import ProteinEffectFlags


def _cfg():
    cfg = mock.Mock()
    cfg.conservation.PHYLOP_CONSERVED_THRESHOLD = 2.0
    cfg.conservation.PHYLOP_NOT_CONSERVED_THRESHOLD = 0.0
    cfg.conservation.PHASTCONS_CONSERVED_THRESHOLD = 0.8
    cfg.conservation.PHASTCONS_NOT_CONSERVED_THRESHOLD = 0.2
    cfg.conservation.GERP_CONSERVED_THRESHOLD = 2.0
    cfg.conservation.GERP_NOT_CONSERVED_THRESHOLD = 0.0
    return cfg


def _am(am_class, score=0.9):
    return {"skipped": False, "found": True, "am_class": am_class, "am_pathogenicity": score}


def _mmsplice(damaging):
    return {
        "predicted": True,
        "interpretation_category": "exon_skipping" if damaging else "no_significant_effect",
        "interpretation": "predicted exon skipping" if damaging else "no significant splice effect",
    }


def _ensemble(damaging):
    return {
        "models_used": ["enformer", "borzoi"],
        "classification": "large_effect" if damaging else "no_significant_effect",
        "consensus_score": 0.9 if damaging else 0.02,
        "agreement_percentage": 95.0,
        "basis": "two_model_consensus",
        "reasoning": "test",
    }


def _conservation(phylop=None, phastcons=None):
    result = {"found": True}
    if phylop is not None:
        result["phylop_score"] = phylop
    if phastcons is not None:
        result["phastcons_score"] = phastcons
    return result


class TestMutualExclusivity(unittest.TestCase):
    """Core invariant: PP3 and BP4 must never both be 'triggered' for
    the same evaluation, across every pairwise combination of the four
    evidence sources."""

    def test_alphamissense_vs_mmsplice_conflict(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3, bp4 = ACMGRuleEngine._pp3_bp4(_am("likely_pathogenic"), _mmsplice(damaging=False))
        self.assertEqual(pp3.status, "not_triggered")
        self.assertEqual(bp4.status, "not_triggered")

    def test_alphamissense_vs_ensemble_conflict(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3, bp4 = ACMGRuleEngine._pp3_bp4(_am("likely_benign"), None, _ensemble(damaging=True))
        self.assertEqual(pp3.status, "not_triggered")
        self.assertEqual(bp4.status, "not_triggered")

    def test_mmsplice_vs_conservation_conflict(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3, bp4 = ACMGRuleEngine._pp3_bp4(None, _mmsplice(damaging=True), None, _conservation(phylop=-2.67))
        self.assertEqual(pp3.status, "not_triggered")
        self.assertEqual(bp4.status, "not_triggered")

    def test_phylop_vs_phastcons_conflict(self):
        """The exact within-conservation disagreement case: PhyloP and
        PhastCons are independently thresholded and can point opposite
        directions for the same variant."""
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3, bp4 = ACMGRuleEngine._pp3_bp4(None, None, None, _conservation(phylop=7.76, phastcons=0.0))
        self.assertEqual(pp3.status, "not_triggered")
        self.assertEqual(bp4.status, "not_triggered")

    def test_conflicting_evidence_surfaced_on_both_sides(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3, bp4 = ACMGRuleEngine._pp3_bp4(_am("likely_pathogenic"), _mmsplice(damaging=False))
        self.assertTrue(pp3.conflicting_evidence)
        self.assertTrue(bp4.conflicting_evidence)
        self.assertTrue(any("MMSplice" in c for c in pp3.conflicting_evidence))
        self.assertTrue(any("AlphaMissense" in c for c in bp4.conflicting_evidence))
        self.assertIn("disagree", pp3.rationale.lower())
        self.assertIn("disagree", bp4.rationale.lower())

    def test_all_four_sources_all_agreeing_damaging_triggers_pp3_only(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3, bp4 = ACMGRuleEngine._pp3_bp4(
                _am("likely_pathogenic"),
                _mmsplice(damaging=True),
                _ensemble(damaging=True),
                _conservation(phylop=7.76, phastcons=1.0),
            )
        self.assertEqual(pp3.status, "triggered")
        self.assertEqual(bp4.status, "not_triggered")

    def test_all_four_sources_all_agreeing_benign_triggers_bp4_only(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3, bp4 = ACMGRuleEngine._pp3_bp4(
                _am("likely_benign"),
                _mmsplice(damaging=False),
                _ensemble(damaging=False),
                _conservation(phylop=-2.67, phastcons=0.0),
            )
        self.assertEqual(pp3.status, "not_triggered")
        self.assertEqual(bp4.status, "triggered")

    def test_no_sources_both_not_evaluated(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3, bp4 = ACMGRuleEngine._pp3_bp4(None, None, None, None)
        self.assertEqual(pp3.status, "not_evaluated")
        self.assertEqual(bp4.status, "not_evaluated")

    def test_ambiguous_only_both_not_triggered_not_conflicting(self):
        """Scores that fall between the conserved/not-conserved
        thresholds are real evidence (checked) but argue neither
        direction -- not a conflict, just inconclusive."""
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3, bp4 = ACMGRuleEngine._pp3_bp4(None, None, None, _conservation(phylop=1.0, phastcons=0.5))
        self.assertEqual(pp3.status, "not_triggered")
        self.assertEqual(bp4.status, "not_triggered")
        self.assertFalse(pp3.conflicting_evidence)
        self.assertFalse(bp4.conflicting_evidence)


_MISSENSE = ProteinEffectFlags(
    is_lof=False, is_inframe_indel=False, is_synonymous=False, is_missense=True, determined=True
)
_SYNONYMOUS = ProteinEffectFlags(
    is_lof=False, is_inframe_indel=False, is_synonymous=True, is_missense=False, determined=True
)


class TestMmspliceGatedByConsequence(unittest.TestCase):
    """
    Regression tests for I5 (report review round 4): MMSplice must not
    participate in the PP3/BP4 agree/disagree vote for a confirmed
    missense variant -- that "no splice disruption" reading answers a
    different question than "is this missense substitution damaging".

    Real, live-verified scenario: `test_data/conflict_tiers.vcf`
    Finding 2, VHL W88C -- AlphaMissense am_pathogenicity 0.997
    ("likely_pathogenic") vs. MMSplice's unrelated "no significant
    splice disruption" reading previously withheld both PP3 and BP4 as
    self-contradictory. Finding 1 (MLH1 p.Gly181=, synonymous) is the
    control case: MMSplice *is* the relevant signal there, and BP7
    already depends on it, so this fix must leave it unchanged.
    """

    def test_missense_alphamissense_damaging_now_triggers_pp3_ignoring_mmsplice(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3, bp4 = ACMGRuleEngine._pp3_bp4(
                _am("likely_pathogenic", score=0.997),
                _mmsplice(damaging=False),
                protein_flags=_MISSENSE,
            )
        self.assertEqual(pp3.status, "triggered")
        self.assertEqual(bp4.status, "not_triggered")
        self.assertNotIn("MMSplice", pp3.evidence_sources)
        self.assertFalse(any("MMSplice" in c for c in pp3.conflicting_evidence))

    def test_missense_mmsplice_damaging_also_excluded_not_just_benign_direction(self):
        # Symmetric: a "damaging" MMSplice reading on a confirmed
        # missense variant is excluded too, not only the misleading
        # "benign" direction that motivated this fix -- MMSplice is
        # simply not consulted for this consequence class.
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3, bp4 = ACMGRuleEngine._pp3_bp4(
                _am("likely_benign"),
                _mmsplice(damaging=True),
                protein_flags=_MISSENSE,
            )
        self.assertEqual(bp4.status, "triggered")
        self.assertEqual(pp3.status, "not_triggered")
        self.assertNotIn("MMSplice", bp4.evidence_sources)

    def test_synonymous_finding1_style_case_is_unchanged(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3, bp4 = ACMGRuleEngine._pp3_bp4(None, _mmsplice(damaging=False), protein_flags=_SYNONYMOUS)
        self.assertEqual(bp4.status, "triggered")
        self.assertIn("MMSplice", bp4.evidence_sources)

    def test_undetermined_protein_flags_stays_permissive_unchanged_behavior(self):
        # No positive confirmation this is missense -> MMSplice still
        # participates, same as before this fix (protein_flags=None is
        # the default every existing caller/test already exercises).
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3, bp4 = ACMGRuleEngine._pp3_bp4(_am("likely_pathogenic"), _mmsplice(damaging=False))
        self.assertEqual(pp3.status, "not_triggered")
        self.assertEqual(bp4.status, "not_triggered")
        self.assertTrue(any("MMSplice" in c for c in pp3.conflicting_evidence))


class TestMMSpliceNeutralNowFeedsBp4(unittest.TestCase):
    """Secondary fix found while resolving the mutual-exclusivity bug:
    the old `_bp4` computed `mm_neutral_only`/`checked_mm` but never
    actually used them to trigger BP4 (dead code -- see
    `_pp3_bp4`'s docstring). MMSplice 'no significant splice
    disruption' now counts as genuine BP4 evidence, symmetric with how
    MMSplice damaging already counted for PP3."""

    def test_mmsplice_alone_no_effect_now_triggers_bp4(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3, bp4 = ACMGRuleEngine._pp3_bp4(None, _mmsplice(damaging=False))
        self.assertEqual(bp4.status, "triggered")
        self.assertEqual(pp3.status, "not_triggered")
        self.assertIn("MMSplice", bp4.evidence_sources)

    def test_mmsplice_alone_damaging_still_only_triggers_pp3(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3, bp4 = ACMGRuleEngine._pp3_bp4(None, _mmsplice(damaging=True))
        self.assertEqual(pp3.status, "triggered")
        self.assertEqual(bp4.status, "not_triggered")


class TestPP3Bp4WrappersDelegateToSharedCore(unittest.TestCase):
    """`_pp3`/`_bp4` remain as thin wrappers (back-compat for existing
    call sites/tests) but must return results identical to calling the
    shared `_pp3_bp4` core directly."""

    def test_pp3_wrapper_matches_shared_core(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            pp3_direct, _ = ACMGRuleEngine._pp3_bp4(_am("likely_pathogenic"), None)
            pp3_wrapper = ACMGRuleEngine._pp3(_am("likely_pathogenic"), None)
        self.assertEqual(pp3_direct.status, pp3_wrapper.status)
        self.assertEqual(pp3_direct.rationale, pp3_wrapper.rationale)

    def test_bp4_wrapper_matches_shared_core(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            _, bp4_direct = ACMGRuleEngine._pp3_bp4(_am("likely_benign"), None)
            bp4_wrapper = ACMGRuleEngine._bp4(_am("likely_benign"), None)
        self.assertEqual(bp4_direct.status, bp4_wrapper.status)
        self.assertEqual(bp4_direct.rationale, bp4_wrapper.rationale)


class TestEvaluateNeverProducesBothTriggered(unittest.TestCase):
    """End-to-end check through the public `evaluate()` entry point,
    matching the exact real-report scenario (AlphaMissense damaging +
    conservation benign on the same variant)."""

    def test_evaluate_with_conflicting_computational_evidence(self):
        engine = ACMGRuleEngine()
        with mock.patch("pipeline.acmg_rules.CONFIG", _cfg()):
            result = engine.evaluate(
                alphamissense_result=_am("likely_pathogenic"),
                conservation_result=_conservation(phylop=-2.67),
            )
        pp3 = result["all_criteria"]["PP3"]
        bp4 = result["all_criteria"]["BP4"]
        self.assertFalse(pp3["status"] == "triggered" and bp4["status"] == "triggered")
        self.assertEqual(pp3["status"], "not_triggered")
        self.assertEqual(bp4["status"], "not_triggered")


if __name__ == "__main__":
    unittest.main()
