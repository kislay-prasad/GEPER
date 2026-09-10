"""
Tests for the ensemble (Enformer/Borzoi) integration into
pipeline/acmg_rules.py's PP3/BP4 evaluation.

Covers the exact routing rules requested:
    0 models -> ensemble contributes nothing (PP3/BP4 fall back to
                whatever AlphaMissense/MMSplice evidence exists, or
                "not_evaluated" if there's none either -- unchanged
                from pre-ensemble behavior).
    1 model  -> that single model's prediction is used directly.
    2 models -> the two-model consensus is used.

Every existing call site that doesn't pass `ensemble_result` at all
(the default is None) must behave exactly as before this integration
-- verified explicitly below.
"""

import unittest

from pipeline.acmg_rules import ACMGRuleEngine


def _ensemble(
    models_used,
    classification,
    consensus_score,
    agreement_percentage=None,
    basis=None,
    calibration_statuses=None,
):
    if not models_used:
        return {
            "models_used": [],
            "individual_scores": {},
            "consensus_score": None,
            "confidence": None,
            "agreement_percentage": None,
            "classification": None,
            "basis": "no_models",
            "reasoning": "No splicing/regulatory AI model was available.",
        }
    individual = {}
    for m in models_used:
        entry = {"score": consensus_score, "classification": classification}
        # Matches the real shape `status.py::_ensemble_model_status` reads
        # (individual_scores[key]["details"]["calibration_status"]) -- only
        # populated when a test asks for it, so existing tests that never
        # cared about calibration disclosure are unaffected.
        if calibration_statuses and m in calibration_statuses:
            entry["details"] = {"calibration_status": calibration_statuses[m]}
        individual[m] = entry
    return {
        "models_used": models_used,
        "individual_scores": individual,
        "consensus_score": consensus_score,
        "confidence": 0.5,
        "agreement_percentage": agreement_percentage,
        "classification": classification,
        "basis": basis or ("single_model" if len(models_used) == 1 else "two_model_consensus"),
        "reasoning": f"test ensemble reasoning for {models_used}",
    }


class TestPP3WithoutEnsemble(unittest.TestCase):
    """Confirms zero behavior change for existing callers that never
    pass ensemble_result."""

    def test_no_ensemble_arg_behaves_exactly_as_before(self):
        result = ACMGRuleEngine._pp3(None, None)
        self.assertEqual(result.status, "not_evaluated")

    def test_no_ensemble_arg_with_alphamissense_still_triggers_normally(self):
        am = {"skipped": False, "found": True, "am_class": "likely_pathogenic", "am_pathogenicity": 0.9}
        result = ACMGRuleEngine._pp3(am, None)
        self.assertEqual(result.status, "triggered")
        self.assertEqual(result.evidence_sources, ["AlphaMissense"])


class TestPP3EnsembleRouting(unittest.TestCase):
    def test_zero_models_contributes_nothing_and_falls_back_to_not_evaluated(self):
        ensemble = _ensemble([], None, None)
        result = ACMGRuleEngine._pp3(None, None, ensemble)
        self.assertEqual(result.status, "not_evaluated")
        self.assertNotIn("AI-ensemble", " ".join(result.evidence_sources))

    def test_zero_models_does_not_block_other_evidence(self):
        ensemble = _ensemble([], None, None)
        am = {"skipped": False, "found": True, "am_class": "likely_pathogenic", "am_pathogenicity": 0.9}
        result = ACMGRuleEngine._pp3(am, None, ensemble)
        self.assertEqual(result.status, "triggered")
        self.assertNotIn("AI-ensemble", " ".join(result.evidence_sources))

    def test_single_model_damaging_triggers_pp3(self):
        ensemble = _ensemble(["enformer"], "large_effect", 0.8)
        result = ACMGRuleEngine._pp3(None, None, ensemble)
        self.assertEqual(result.status, "triggered")
        self.assertIn("AI-ensemble(enformer)", result.evidence_sources)
        self.assertTrue(any("single-model prediction" in s for s in result.supporting_evidence))

    def test_single_model_non_damaging_does_not_trigger_pp3(self):
        ensemble = _ensemble(["borzoi"], "no_significant_effect", 0.02)
        result = ACMGRuleEngine._pp3(None, None, ensemble)
        self.assertEqual(result.status, "not_triggered")
        self.assertIn("AI-ensemble(borzoi)", result.evidence_sources)

    def test_two_model_consensus_damaging_triggers_pp3(self):
        ensemble = _ensemble(["enformer", "borzoi"], "moderate_effect", 0.3, agreement_percentage=95.0)
        result = ACMGRuleEngine._pp3(None, None, ensemble)
        self.assertEqual(result.status, "triggered")
        self.assertTrue(any("consensus" in s for s in result.supporting_evidence))
        self.assertTrue(any("95.0%" in s for s in result.supporting_evidence))

    def test_two_model_consensus_non_damaging_does_not_trigger_pp3(self):
        ensemble = _ensemble(["enformer", "borzoi"], "no_significant_effect", 0.03, agreement_percentage=88.0)
        result = ACMGRuleEngine._pp3(None, None, ensemble)
        self.assertEqual(result.status, "not_triggered")

    def test_ensemble_combines_with_alphamissense_in_supporting_evidence(self):
        ensemble = _ensemble(["enformer", "borzoi"], "large_effect", 0.9, agreement_percentage=100.0)
        am = {"skipped": False, "found": True, "am_class": "likely_pathogenic", "am_pathogenicity": 0.95}
        result = ACMGRuleEngine._pp3(am, None, ensemble)
        self.assertEqual(result.status, "triggered")
        self.assertEqual(sorted(result.evidence_sources), ["AI-ensemble(enformer+borzoi)", "AlphaMissense"])
        self.assertEqual(len(result.supporting_evidence), 2)
        self.assertEqual(result.confidence, "Moderate")  # >1 supporting evidence


class TestEnsembleCalibrationDisclosure(unittest.TestCase):
    """PP3/BP4's ensemble evidence must disclose each fired model's own
    `individual_scores[key]["details"]["calibration_status"]` inline --
    the exact read-and-disclose pattern `_bp7` already applies to
    SpliceFormer/SpliceBERT (acmg_rules.py:2570-2579): read, never
    hand-typed; an honest "not reported" fallback when the key is absent,
    never invented; and a plain-language caveat that this is a raw model
    score, not a validated clinical measure.

    THE RED-FIRST CASES. Enformer/Borzoi already carry this same
    calibration_status field (see status.py's own read of it), but
    _pp3_bp4's ensemble branch (acmg_rules.py:2131-2163) only ever reads
    models_used/classification/consensus_score/basis/agreement_percentage/
    reasoning -- calibration_status is silently dropped on the floor for
    the one PP3/BP4-feeding source that never discloses it.
    """

    def test_single_model_calibration_status_disclosed_in_pp3_evidence(self):
        ensemble = _ensemble(
            ["enformer"],
            "large_effect",
            0.8,
            calibration_statuses={"enformer": "uncalibrated -- raw model output, no clinical validation"},
        )
        result = ACMGRuleEngine._pp3(None, None, ensemble)
        self.assertEqual(result.status, "triggered")
        text = " ".join(result.supporting_evidence)
        self.assertIn(
            "enformer (uncalibrated -- raw model output, no clinical validation)",
            text,
            f"calibration status not disclosed in PP3 evidence: {result.supporting_evidence!r}",
        )

    def test_missing_calibration_status_reports_honest_absence_not_a_guess(self):
        ensemble = _ensemble(["borzoi"], "large_effect", 0.9)  # no calibration_statuses supplied at all
        result = ACMGRuleEngine._pp3(None, None, ensemble)
        text = " ".join(result.supporting_evidence)
        self.assertIn(
            "borzoi (calibration status not reported by this model)",
            text,
            f"a missing calibration_status must use the same honest-absence text _bp7 uses, "
            f"not be silently omitted: {result.supporting_evidence!r}",
        )

    def test_two_model_consensus_discloses_both_models_calibration_independently(self):
        ensemble = _ensemble(
            ["enformer", "borzoi"],
            "moderate_effect",
            0.3,
            agreement_percentage=95.0,
            calibration_statuses={"enformer": "uncalibrated -- A", "borzoi": "uncalibrated -- B"},
        )
        result = ACMGRuleEngine._pp3(None, None, ensemble)
        text = " ".join(result.supporting_evidence)
        self.assertIn("enformer (uncalibrated -- A)", text)
        self.assertIn("borzoi (uncalibrated -- B)", text)

    def test_benign_direction_also_discloses_calibration(self):
        ensemble = _ensemble(
            ["enformer"],
            "no_significant_effect",
            0.02,
            calibration_statuses={"enformer": "uncalibrated -- raw model output, no clinical validation"},
        )
        result = ACMGRuleEngine._bp4(None, None, ensemble)
        self.assertEqual(result.status, "triggered")
        text = " ".join(result.supporting_evidence)
        self.assertIn(
            "enformer (uncalibrated -- raw model output, no clinical validation)",
            text,
            f"BP4's benign-direction ensemble evidence must disclose calibration too, "
            f"same as _bp7's benign branch: {result.supporting_evidence!r}",
        )

    def test_raw_score_caveat_present(self):
        ensemble = _ensemble(["enformer"], "large_effect", 0.8, calibration_statuses={"enformer": "uncalibrated"})
        result = ACMGRuleEngine._pp3(None, None, ensemble)
        text = " ".join(result.supporting_evidence)
        self.assertIn(
            "not a validated clinical",
            text,
            f"missing the plain-language caveat _bp7 attaches to every calibration-qualified "
            f"score: {result.supporting_evidence!r}",
        )


class TestBP4WithoutEnsemble(unittest.TestCase):
    def test_no_ensemble_arg_behaves_exactly_as_before(self):
        result = ACMGRuleEngine._bp4(None, None)
        self.assertEqual(result.status, "not_evaluated")


class TestBP4EnsembleRouting(unittest.TestCase):
    def test_zero_models_falls_back_to_not_evaluated(self):
        ensemble = _ensemble([], None, None)
        result = ACMGRuleEngine._bp4(None, None, ensemble)
        self.assertEqual(result.status, "not_evaluated")

    def test_single_model_benign_triggers_bp4(self):
        ensemble = _ensemble(["borzoi"], "no_significant_effect", 0.01)
        result = ACMGRuleEngine._bp4(None, None, ensemble)
        self.assertEqual(result.status, "triggered")
        self.assertIn("AI-ensemble(borzoi)", result.evidence_sources)

    def test_single_model_damaging_does_not_trigger_bp4(self):
        ensemble = _ensemble(["enformer"], "large_effect", 0.85)
        result = ACMGRuleEngine._bp4(None, None, ensemble)
        self.assertEqual(result.status, "not_triggered")

    def test_two_model_consensus_benign_triggers_bp4(self):
        ensemble = _ensemble(["enformer", "borzoi"], "no_significant_effect", 0.02, agreement_percentage=97.5)
        result = ACMGRuleEngine._bp4(None, None, ensemble)
        self.assertEqual(result.status, "triggered")
        self.assertTrue(any("97.5%" in s for s in result.supporting_evidence))

    def test_two_model_consensus_damaging_does_not_trigger_bp4(self):
        ensemble = _ensemble(["enformer", "borzoi"], "moderate_effect", 0.3, agreement_percentage=90.0)
        result = ACMGRuleEngine._bp4(None, None, ensemble)
        self.assertEqual(result.status, "not_triggered")

    def test_disagreeing_models_do_not_trigger_bp4_when_consensus_is_damaging(self):
        # consensus classification is what governs triggering; even
        # with low agreement, a "moderate_effect" consensus must not
        # trigger the benign criterion.
        ensemble = _ensemble(["enformer", "borzoi"], "moderate_effect", 0.35, agreement_percentage=40.0)
        result = ACMGRuleEngine._bp4(None, None, ensemble)
        self.assertEqual(result.status, "not_triggered")


class TestEvaluateThreadsEnsembleResultThrough(unittest.TestCase):
    """Confirms ACMGRuleEngine.evaluate() (the public entry point) wires
    ensemble_result to both PP3 and BP4 correctly. `evaluate()` returns
    `all_criteria` as {code: CriterionResult.to_dict()} -- see
    pipeline/acmg_rules.py::ACMGRuleEngine.evaluate."""

    def test_evaluate_wires_ensemble_into_pp3_and_bp4(self):
        engine = ACMGRuleEngine()
        ensemble = _ensemble(["enformer", "borzoi"], "large_effect", 0.9, agreement_percentage=100.0)
        result = engine.evaluate(ensemble_result=ensemble)

        pp3 = result["all_criteria"]["PP3"]
        self.assertEqual(pp3["status"], "triggered")
        self.assertIn("AI-ensemble(enformer+borzoi)", pp3["evidence_sources"])

        bp4 = result["all_criteria"]["BP4"]
        self.assertEqual(bp4["status"], "not_triggered")  # damaging consensus, not benign

    def test_evaluate_without_ensemble_result_is_unaffected(self):
        engine = ACMGRuleEngine()
        result = engine.evaluate()
        self.assertEqual(result["all_criteria"]["PP3"]["status"], "not_evaluated")
        self.assertEqual(result["all_criteria"]["BP4"]["status"], "not_evaluated")
        # Every one of the 28 ACMG/AMP criteria must still be present,
        # exactly as before this integration -- nothing was removed.
        self.assertEqual(len(result["all_criteria"]), 28)

    def test_evaluate_with_benign_two_model_consensus_triggers_bp4_not_pp3(self):
        engine = ACMGRuleEngine()
        ensemble = _ensemble(["enformer", "borzoi"], "no_significant_effect", 0.02, agreement_percentage=98.0)
        result = engine.evaluate(ensemble_result=ensemble)
        self.assertEqual(result["all_criteria"]["BP4"]["status"], "triggered")
        self.assertEqual(result["all_criteria"]["PP3"]["status"], "not_triggered")

    def test_evaluate_with_high_confidence_single_model_reflects_in_pp3_confidence_field(self):
        engine = ACMGRuleEngine()
        ensemble = _ensemble(["enformer"], "large_effect", 0.9)
        result = engine.evaluate(ensemble_result=ensemble)
        pp3 = result["all_criteria"]["PP3"]
        self.assertEqual(pp3["status"], "triggered")
        # Single evidence source -> "Low" per the existing confidence
        # rule (unchanged: `"Moderate" if len(supporting) > 1 else "Low"`).
        self.assertEqual(pp3["confidence"], "Low")

    def test_evaluate_with_missing_confidence_in_ensemble_result_does_not_crash(self):
        engine = ACMGRuleEngine()
        ensemble = _ensemble(["borzoi"], "no_significant_effect", 0.01)
        ensemble["confidence"] = None  # simulate a missing/None confidence value
        result = engine.evaluate(ensemble_result=ensemble)
        self.assertEqual(result["all_criteria"]["BP4"]["status"], "triggered")

    def test_evaluate_with_malformed_ensemble_result_never_crashes(self):
        engine = ACMGRuleEngine()
        malformed_cases = [
            {},  # empty dict -- no "models_used" key at all
            {"models_used": None},  # None instead of a list
            {"models_used": ["enformer"]},  # missing score/classification/etc entirely
            {"models_used": ["enformer", "borzoi"], "classification": "unexpected_value", "consensus_score": None},
        ]
        for malformed in malformed_cases:
            with self.subTest(malformed=malformed):
                try:
                    result = engine.evaluate(ensemble_result=malformed)
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"evaluate() raised for malformed ensemble_result={malformed}: {exc!r}")
                self.assertIn("PP3", result["all_criteria"])
                self.assertIn("BP4", result["all_criteria"])

    def test_evaluate_with_low_confidence_single_model_still_triggers_but_stays_low_confidence(self):
        # Note: CriterionResult.confidence is derived from the number of
        # supporting evidence sources (existing, unchanged rule:
        # "Moderate" if >1 source else "Low"), not from the ensemble's
        # own numeric `confidence` field -- this test documents that
        # fact rather than asserting a behavior this integration never
        # added.
        engine = ACMGRuleEngine()
        ensemble = _ensemble(["borzoi"], "large_effect", 0.55)
        ensemble["confidence"] = 0.05  # low numeric confidence from the ensemble itself
        result = engine.evaluate(ensemble_result=ensemble)
        pp3 = result["all_criteria"]["PP3"]
        self.assertEqual(pp3["status"], "triggered")
        self.assertEqual(pp3["confidence"], "Low")

    def test_evaluate_with_high_agreement_two_model_consensus(self):
        engine = ACMGRuleEngine()
        ensemble = _ensemble(["enformer", "borzoi"], "large_effect", 0.85, agreement_percentage=99.0)
        result = engine.evaluate(ensemble_result=ensemble)
        pp3 = result["all_criteria"]["PP3"]
        self.assertEqual(pp3["status"], "triggered")
        self.assertTrue(any("99.0%" in s for s in pp3["supporting_evidence"]))

    def test_evaluate_with_low_agreement_two_model_consensus_still_routes_on_classification(self):
        engine = ACMGRuleEngine()
        ensemble = _ensemble(["enformer", "borzoi"], "large_effect", 0.6, agreement_percentage=12.0)
        result = engine.evaluate(ensemble_result=ensemble)
        pp3 = result["all_criteria"]["PP3"]
        # Low agreement doesn't suppress triggering -- the consensus
        # *classification* is what routes PP3/BP4 (agreement is
        # reported for auditability, not used as a gate); this matches
        # the routing rules as specified (2 models -> use consensus).
        self.assertEqual(pp3["status"], "triggered")
        self.assertTrue(any("12.0%" in s for s in pp3["supporting_evidence"]))

        """Simulates a pre-existing call site that only knows about the
        original kwargs (no ensemble_result at all) -- must produce
        identical output to what this engine returned before the
        ensemble integration existed."""
        engine = ACMGRuleEngine()
        am = {"skipped": False, "found": True, "am_class": "likely_pathogenic", "am_pathogenicity": 0.9}
        legacy_result = engine.evaluate(
            clinvar_result=None,
            dbsnp_result=None,
            protein_result=None,
            alphamissense_result=am,
            mmsplice_result=None,
            gnomad_result=None,
            clingen_result=None,
            interpro_result=None,
        )
        self.assertEqual(legacy_result["all_criteria"]["PP3"]["status"], "triggered")
        self.assertEqual(legacy_result["all_criteria"]["PP3"]["evidence_sources"], ["AlphaMissense"])


if __name__ == "__main__":
    unittest.main()
