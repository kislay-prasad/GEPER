"""
Tests for the ClinGen -> ACMG evidence contribution added to
`pipeline/interpretation.py::InterpretationEngine._clingen_acmg_evidence`
(requirement #5: "ClinGen must contribute evidence ... Never overwrite
existing ACMG evidence. Only add evidence. All thresholds/configuration
must remain configurable.").

Mirrors `tests/test_gnomad_acmg.py`'s approach: exercises the real,
non-mocked static method directly with plain evidence dicts, only
patching `CONFIG.clingen`'s thresholds.
"""

import unittest
from unittest import mock

from pipeline.interpretation import InterpretationEngine


class _FakeClinGenConfig:
    DOSAGE_SUFFICIENT_EVIDENCE_SCORE = 3
    DOSAGE_UNLIKELY_SCORE = 40


def _clingen_result(**overrides):
    base = {
        "skipped": False,
        "found": True,
        "error": None,
        "gene_symbol": "BRCA1",
        "gene_disease_validity": [],
        "clinical_validity_summary": None,
        "expert_panel": None,
        "dosage_sensitivity": None,
    }
    base.update(overrides)
    return base


class TestClinGenAcmgEvidence(unittest.TestCase):
    def setUp(self):
        self.engine = InterpretationEngine()
        self.patcher = mock.patch("pipeline.interpretation.CONFIG")
        fake_config = self.patcher.start()
        fake_config.clingen = _FakeClinGenConfig()
        self.addCleanup(self.patcher.stop)

    # -- gating -----------------------------------------------------

    def test_none_result_contributes_no_evidence(self):
        self.assertEqual(self.engine._clingen_acmg_evidence(None, is_predicted_lof=True), [])

    def test_skipped_result_contributes_no_evidence(self):
        self.assertEqual(self.engine._clingen_acmg_evidence({"skipped": True}, is_predicted_lof=True), [])

    def test_errored_result_contributes_no_evidence(self):
        result = _clingen_result(error="ClinGen API unreachable")
        self.assertEqual(self.engine._clingen_acmg_evidence(result, is_predicted_lof=True), [])

    def test_not_found_contributes_no_evidence(self):
        result = _clingen_result(found=False)
        self.assertEqual(self.engine._clingen_acmg_evidence(result, is_predicted_lof=True), [])

    def test_found_but_no_curation_data_contributes_nothing(self):
        result = _clingen_result(found=True)
        self.assertEqual(self.engine._clingen_acmg_evidence(result, is_predicted_lof=True), [])

    # -- PVS1 / dosage sensitivity ------------------------------------

    def test_sufficient_haploinsufficiency_supports_pvs1_for_lof_variant(self):
        result = _clingen_result(
            dosage_sensitivity={"haploinsufficiency_score": 3, "haploinsufficiency_label": "Sufficient evidence for dosage pathogenicity"}
        )
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=True)
        self.assertEqual(len(evidence), 1)
        text, weight = evidence[0]
        self.assertIn("PVS1", text)
        self.assertGreater(weight, 0)

    def test_sufficient_haploinsufficiency_does_not_fire_for_non_lof_variant(self):
        result = _clingen_result(
            dosage_sensitivity={"haploinsufficiency_score": 3, "haploinsufficiency_label": "Sufficient evidence"}
        )
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=False)
        self.assertEqual(evidence, [])

    def test_dosage_unlikely_cautions_against_pvs1_for_lof_variant(self):
        result = _clingen_result(
            dosage_sensitivity={"haploinsufficiency_score": 40, "haploinsufficiency_label": "Dosage sensitivity unlikely"}
        )
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=True)
        self.assertEqual(len(evidence), 1)
        text, weight = evidence[0]
        self.assertIn("caution", text.lower())
        self.assertLess(weight, 0)

    def test_low_haploinsufficiency_score_below_threshold_contributes_nothing(self):
        result = _clingen_result(
            dosage_sensitivity={"haploinsufficiency_score": 1, "haploinsufficiency_label": "Little evidence"}
        )
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=True)
        self.assertEqual(evidence, [])

    def test_threshold_is_configurable(self):
        self.engine  # keep reference; reconfigure threshold to something stricter than the score provided
        with mock.patch("pipeline.interpretation.CONFIG") as fake_config:
            fake_config.clingen = _FakeClinGenConfig()
            fake_config.clingen.DOSAGE_SUFFICIENT_EVIDENCE_SCORE = 99  # unreachable threshold
            result = _clingen_result(dosage_sensitivity={"haploinsufficiency_score": 3, "haploinsufficiency_label": "x"})
            evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=True)
        self.assertEqual(evidence, [])

    # -- gene-disease clinical validity (PP5/BP6-style) -----------------

    def test_definitive_classification_supports(self):
        result = _clingen_result(clinical_validity_summary="Definitive", expert_panel="Hereditary Cancer GCEP")
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=False)
        self.assertEqual(len(evidence), 1)
        text, weight = evidence[0]
        self.assertIn("Definitive", text)
        self.assertGreater(weight, 0)

    def test_strong_classification_supports(self):
        result = _clingen_result(clinical_validity_summary="Strong")
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=False)
        text, weight = evidence[0]
        self.assertGreater(weight, 0)

    def test_disputed_classification_argues_against(self):
        result = _clingen_result(clinical_validity_summary="Disputed")
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=False)
        text, weight = evidence[0]
        self.assertIn("Disputed", text)
        self.assertLess(weight, 0)

    def test_refuted_classification_argues_against(self):
        result = _clingen_result(clinical_validity_summary="Refuted")
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=False)
        text, weight = evidence[0]
        self.assertLess(weight, 0)

    def test_limited_classification_is_neutral(self):
        result = _clingen_result(clinical_validity_summary="Limited")
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=False)
        text, weight = evidence[0]
        self.assertEqual(weight, 0.0)

    # -- both contributions can fire together --------------------------

    def test_pvs1_and_clinical_validity_both_contribute(self):
        result = _clingen_result(
            clinical_validity_summary="Definitive",
            dosage_sensitivity={"haploinsufficiency_score": 3, "haploinsufficiency_label": "Sufficient evidence"},
        )
        evidence = self.engine._clingen_acmg_evidence(result, is_predicted_lof=True)
        self.assertEqual(len(evidence), 2)
        total_weight = sum(w for _, w in evidence)
        self.assertGreater(total_weight, 0)


if __name__ == "__main__":
    unittest.main()
