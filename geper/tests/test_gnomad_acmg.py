"""
Tests for the gnomAD -> ACMG (BA1/BS1/PM2) evidence contribution added
to `pipeline/interpretation.py::InterpretationEngine`.

Verifies requirement #6 ("Automatically contribute evidence for BA1,
BS1, PM2. Use configurable thresholds. Never overwrite existing
evidence.") directly against the real, non-mocked interpretation
engine -- only `CONFIG.gnomad`'s thresholds are patched, to test the
"configurable thresholds" behavior deterministically without depending
on whatever the shipped defaults happen to be.
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
        text, weight = result[0]
        self.assertIn("BA1", text)
        self.assertLess(weight, 0)

    def test_bs1_triggers_between_thresholds(self):
        result = self.engine._gnomad_acmg_evidence(
            {"skipped": False, "found": True, "global_af": 0.02, "population_breakdown": {}}
        )
        text, weight = result[0]
        self.assertIn("BS1", text)
        self.assertLess(weight, 0)

    def test_pm2_triggers_below_threshold(self):
        result = self.engine._gnomad_acmg_evidence(
            {"skipped": False, "found": True, "global_af": 0.00001, "population_breakdown": {}}
        )
        text, weight = result[0]
        self.assertIn("PM2", text)
        self.assertGreater(weight, 0)

    def test_pm2_triggers_when_absent_from_gnomad(self):
        result = self.engine._gnomad_acmg_evidence({"skipped": False, "found": False})
        text, weight = result[0]
        self.assertIn("PM2", text)
        self.assertIn("absent from gnomAD", text)

    def test_no_criterion_in_the_gap_between_pm2_and_bs1(self):
        result = self.engine._gnomad_acmg_evidence(
            {"skipped": False, "found": True, "global_af": 0.002, "population_breakdown": {}}
        )
        text, weight = result[0]
        self.assertIn("no BA1/BS1/PM2 threshold met", text)
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
                "skipped": False, "found": True, "global_af": 0.005,
                "population_breakdown": {"fin": {"af": 0.15}},
            }
        )
        text, weight = result[0]
        self.assertIn("BA1", text)

    def test_evidence_is_additive_not_replacing(self):
        """Full interpret() call: gnomAD evidence appended alongside pre-existing ClinVar evidence, never overwriting it."""
        variant_dict = {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"}
        clinvar_result = {"records": [{"clinical_significance": "Pathogenic", "review_status": "criteria provided"}]}
        gnomad_result = {"skipped": False, "found": True, "global_af": 0.2, "population_breakdown": {}}

        result = self.engine.interpret(
            variant_dict, [], clinvar_result, {}, {}, {}, gnomad_result=gnomad_result,
        )
        evidence_text = " ".join(result["supporting_evidence"])
        self.assertIn("Pathogenic", evidence_text)  # ClinVar evidence preserved
        self.assertTrue(any("gnomAD" in e and "BA1" in e for e in result["supporting_evidence"]))  # gnomAD evidence added


if __name__ == "__main__":
    unittest.main()
