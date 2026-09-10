"""
Tests for the gnomAD -> ACMG (BA1/BS1/PM2) evidence contribution added
to `pipeline/interpretation.py::InterpretationEngine`.

Verifies requirement #6 ("Automatically contribute evidence for BA1,
BS1, PM2. Use configurable thresholds. Never overwrite existing
evidence.") directly against the real, non-mocked interpretation
engine -- only `CONFIG.gnomad`'s thresholds are patched, to test the
"configurable thresholds" behavior deterministically without depending
on whatever the shipped defaults happen to be.

WHAT THESE TESTS ASSERT ON, AND WHY IT CHANGED (2026-09-11, card
DEFECT-t3f1-deletion-leaves-eight-tests-pinning-a-string-with-no-consumer).
`_gnomad_acmg_evidence` returns (evidence_text, score_weight) pairs. Since
the HIGH 3 Q4-B/T3-F1 ruling its sole production caller
(`interpretation.py::interpret`) reads `for _text, weight in ...` -- THE
TEXT IS DISCARDED. Eight assertions in this file and one in
tests/test_gnomad_lookup_failure_not_absent.py were still pinning that
text, and they still passed, which is the problem: they could only ever go
red for something no reader can see.

MEASURED, NOT ASSUMED (both directions):
  * rewording every branch's sentence turned SEVEN tests red and left the
    rendered report BYTE-IDENTICAL;
  * changing BA1's weight from -3.0 to -2.0 left ALL of them GREEN and
    changed what a clinician reads -- `_build_summary` turns the score into
    the "Summary"/"Confidence (legacy)" lines that
    `report_generator.py::_render_interpretation` renders, so that variant
    went from "uncertain clinical significance"/low to "some evidence
    suggestive of clinical relevance"/moderate.
So the suite was pinning the half nobody reads and leaving the half a
clinician does read unguarded. Each text assertion below is now an
assertion on the EXACT weight for its branch. That is the value that
survived the deletion, it is the one with a reader-visible consequence,
and unlike the old `assertLess(weight, 0)` it tells BA1 (-3.0) apart from
BS1 (-2.0) -- which previously only the discarded text did.
"""

import unittest
from unittest import mock

from pipeline.interpretation import InterpretationEngine


class _FakeGnomadConfig:
    BA1_AF_THRESHOLD = 0.05
    BS1_AF_THRESHOLD = 0.01
    PM2_AF_THRESHOLD = 0.0001
    USE_POPMAX_FOR_BA1_BS1 = True


class TestGnomadAcmgEvidence(unittest.TestCase):
    def setUp(self):
        self.engine = InterpretationEngine()
        self.patcher = mock.patch("pipeline.interpretation.CONFIG")
        fake_config = self.patcher.start()
        fake_config.gnomad = _FakeGnomadConfig()
        self.addCleanup(self.patcher.stop)

    def test_ba1_triggers_above_threshold(self):
        result = self.engine._gnomad_acmg_evidence(
            {"skipped": False, "found": True, "global_af": 0.2, "population_breakdown": {}}
        )
        self.assertEqual(len(result), 1)
        _text, weight = result[0]
        # BA1's own weight, not merely "negative": -2.0 would be BS1's.
        self.assertEqual(weight, -3.0)

    def test_bs1_triggers_between_thresholds(self):
        result = self.engine._gnomad_acmg_evidence(
            {"skipped": False, "found": True, "global_af": 0.02, "population_breakdown": {}}
        )
        _text, weight = result[0]
        self.assertEqual(weight, -2.0)

    def test_pm2_triggers_below_threshold(self):
        result = self.engine._gnomad_acmg_evidence(
            {"skipped": False, "found": True, "global_af": 0.00001, "population_breakdown": {}}
        )
        _text, weight = result[0]
        self.assertEqual(weight, 1.0)

    def test_pm2_triggers_when_absent_from_gnomad(self):
        result = self.engine._gnomad_acmg_evidence({"skipped": False, "found": False})
        self.assertEqual(len(result), 1)
        _text, weight = result[0]
        # Absence IS PM2's own criterion, so it must carry PM2's weight and
        # not merely produce an entry. This test asserted only on the
        # sentence before; the sentence has no reader.
        self.assertEqual(weight, 1.0)

    def test_no_criterion_in_the_gap_between_pm2_and_bs1(self):
        result = self.engine._gnomad_acmg_evidence(
            {"skipped": False, "found": True, "global_af": 0.002, "population_breakdown": {}}
        )
        # The real decision here is that the gap still produces an ENTRY,
        # carrying no score either way -- not that it produces nothing.
        self.assertEqual(len(result), 1)
        _text, weight = result[0]
        self.assertEqual(weight, 0.0)

    def test_skipped_result_contributes_no_evidence(self):
        self.assertEqual(self.engine._gnomad_acmg_evidence({"skipped": True}), [])

    def test_errored_result_contributes_no_evidence(self):
        self.assertEqual(self.engine._gnomad_acmg_evidence({"skipped": False, "error": "timeout"}), [])

    def test_none_result_contributes_no_evidence(self):
        self.assertEqual(self.engine._gnomad_acmg_evidence(None), [])

    def test_popmax_used_over_global_when_configured(self):
        # global_af is below BS1, but one population's AF alone
        # exceeds BA1 -- popmax mode must catch this (requirement:
        # ACMG/AMP's own recommended approach for BA1/BS1 is the
        # highest single population frequency, not the pooled global one).
        result = self.engine._gnomad_acmg_evidence(
            {
                "skipped": False,
                "found": True,
                "global_af": 0.005,
                "population_breakdown": {"fin": {"af": 0.15}},
            }
        )
        _text, weight = result[0]
        # -3.0 is BA1. Global AF alone (0.005) falls in the no-criterion gap
        # and would weigh 0.0, so this weight is what separates popmax mode
        # from global mode -- previously only the discarded text did.
        self.assertEqual(weight, -3.0)

    def test_evidence_is_additive_not_replacing(self):
        """Full interpret() call: ClinVar evidence is never overwritten by the gnomAD block running
        alongside it (requirement #6's actual guarantee -- this used to also check that gnomAD's own
        text was ADDED to `supporting_evidence`; per the HIGH 3 Q4-B/T3-F1 ruling that text is no
        longer appended there at all, see `test_legacy_gnomad_text_no_longer_reaches_supporting_evidence`
        below for that half)."""
        variant_dict = {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"}
        primary = {"clinical_significance": "Pathogenic", "review_status": "criteria provided", "variant_match": True}
        clinvar_result = {"records": [primary], "match_status": "matched", "primary_record": primary}
        gnomad_result = {"skipped": False, "found": True, "global_af": 0.2, "population_breakdown": {}}

        result = self.engine.interpret(
            variant_dict,
            [],
            clinvar_result,
            {},
            {},
            {},
            gnomad_result=gnomad_result,
        )
        evidence_text = " ".join(result["supporting_evidence"])
        self.assertIn("Pathogenic", evidence_text)  # ClinVar evidence preserved

    def test_legacy_gnomad_text_no_longer_reaches_supporting_evidence(self):
        """HIGH 3 Q4-B/T3-F1 (2026-08-28): `_gnomad_acmg_evidence`'s own sentence must never reach
        `supporting_evidence` any more -- it has no population-priority-AF awareness (unlike the real
        ACMGRuleEngine's `_ba1_bs1`/`_pm2`) and could be flatly wrong under a supported config while
        sitting beside the real engine's own correct sentence for the same variant. The score
        contribution is intentionally unchanged (provably unread by every renderer) -- only the text
        is removed. This variant would have produced a BA1 sentence before the fix."""
        variant_dict = {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"}
        gnomad_result = {"skipped": False, "found": True, "global_af": 0.2, "population_breakdown": {}}

        result = self.engine.interpret(
            variant_dict,
            [],
            {},
            {},
            {},
            {},
            gnomad_result=gnomad_result,
        )
        self.assertFalse(any("gnomAD" in e for e in result["supporting_evidence"]))
        # The internal pre-ACMG score still moves (unchanged, deliberately) -- confirms this is a
        # text-only removal, not silently disabling the whole gnomAD block.
        self.assertLess(result["legacy_pre_acmg_significance_score"], 0)


if __name__ == "__main__":
    unittest.main()
