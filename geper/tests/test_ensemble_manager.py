"""
Unit tests for pipeline/models/ensemble.py::EnsembleManager.

`ModelManager.predict()` is mocked directly (returning canned
per-model result dicts, exactly the shape EnformerPlugin/BorzoiPlugin
produce) -- these tests exercise the ensemble's own combination logic
(consensus math, agreement percentage, 0/1/2-model routing), not the
real models or any network access.
"""

import unittest
from unittest import mock

from pipeline.models.ensemble import EnsembleManager


def _result(score, classification, confidence=0.5):
    return {"score": score, "classification": classification, "confidence": confidence, "details": {}}


class TestZeroModelsAvailable(unittest.TestCase):
    def test_no_models_returns_empty_models_used(self):
        manager = mock.Mock()
        manager.predict.return_value = None
        ensemble = EnsembleManager(manager=manager)

        result = ensemble.evaluate("A" * 10, "T" * 10)

        self.assertEqual(result["models_used"], [])
        self.assertIsNone(result["consensus_score"])
        self.assertIsNone(result["confidence"])
        self.assertIsNone(result["agreement_percentage"])
        self.assertIsNone(result["classification"])
        self.assertEqual(result["basis"], "no_models")
        self.assertIn("No splicing/regulatory AI model was available", result["reasoning"])

    def test_no_models_never_raises(self):
        manager = mock.Mock()
        manager.predict.side_effect = lambda *a, **k: None
        ensemble = EnsembleManager(manager=manager)
        # Must not raise even when called repeatedly / with odd inputs.
        for _ in range(5):
            result = ensemble.evaluate("", "")
            self.assertEqual(result["models_used"], [])


class TestSingleModelAvailable(unittest.TestCase):
    def test_only_enformer_available_uses_its_prediction_directly(self):
        def fake_predict(key, ref, alt, **kwargs):
            if key == "enformer":
                return _result(0.7, "large_effect", confidence=0.8)
            return None

        manager = mock.Mock()
        manager.predict.side_effect = fake_predict
        ensemble = EnsembleManager(manager=manager)

        result = ensemble.evaluate("A" * 10, "T" * 10)

        self.assertEqual(result["models_used"], ["enformer"])
        self.assertAlmostEqual(result["consensus_score"], 0.7)
        self.assertEqual(result["classification"], "large_effect")
        self.assertIsNone(result["agreement_percentage"])
        self.assertEqual(result["basis"], "single_model")
        self.assertIn("Only one splicing/regulatory AI model", result["reasoning"])
        self.assertIn("enformer", result["individual_scores"])

    def test_only_borzoi_available_uses_its_prediction_directly(self):
        def fake_predict(key, ref, alt, **kwargs):
            if key == "borzoi":
                return _result(0.05, "no_significant_effect", confidence=0.2)
            return None

        manager = mock.Mock()
        manager.predict.side_effect = fake_predict
        ensemble = EnsembleManager(manager=manager)

        result = ensemble.evaluate("A" * 10, "T" * 10)

        self.assertEqual(result["models_used"], ["borzoi"])
        self.assertEqual(result["classification"], "no_significant_effect")
        self.assertIsNone(result["agreement_percentage"])
        self.assertEqual(result["basis"], "single_model")


class TestTwoModelsAgreement(unittest.TestCase):
    def test_identical_scores_and_classifications_yield_full_agreement(self):
        def fake_predict(key, ref, alt, **kwargs):
            return _result(0.3, "moderate_effect", confidence=0.5)

        manager = mock.Mock()
        manager.predict.side_effect = fake_predict
        ensemble = EnsembleManager(manager=manager)

        result = ensemble.evaluate("A" * 10, "T" * 10)

        self.assertEqual(sorted(result["models_used"]), ["borzoi", "enformer"])
        self.assertAlmostEqual(result["consensus_score"], 0.3)
        self.assertEqual(result["classification"], "moderate_effect")
        self.assertEqual(result["agreement_percentage"], 100.0)
        self.assertEqual(result["basis"], "two_model_consensus")

    def test_close_scores_same_classification_yield_high_agreement(self):
        def fake_predict(key, ref, alt, **kwargs):
            if key == "enformer":
                return _result(0.60, "moderate_effect")
            return _result(0.65, "moderate_effect")

        manager = mock.Mock()
        manager.predict.side_effect = fake_predict
        ensemble = EnsembleManager(manager=manager)

        result = ensemble.evaluate("A" * 10, "T" * 10)

        self.assertAlmostEqual(result["consensus_score"], 0.625)
        self.assertGreater(result["agreement_percentage"], 90.0)
        self.assertLessEqual(result["agreement_percentage"], 100.0)


class TestTwoModelsDisagreement(unittest.TestCase):
    def test_far_apart_scores_yield_low_agreement(self):
        def fake_predict(key, ref, alt, **kwargs):
            if key == "enformer":
                return _result(0.05, "no_significant_effect")
            return _result(0.95, "large_effect")

        manager = mock.Mock()
        manager.predict.side_effect = fake_predict
        ensemble = EnsembleManager(manager=manager)

        result = ensemble.evaluate("A" * 10, "T" * 10)

        self.assertLess(result["agreement_percentage"], 20.0)
        self.assertEqual(result["basis"], "two_model_consensus")

    def test_different_classifications_cap_agreement_even_if_scores_close(self):
        def fake_predict(key, ref, alt, **kwargs):
            if key == "enformer":
                return _result(0.095, "no_significant_effect")
            return _result(0.105, "moderate_effect")

        manager = mock.Mock()
        manager.predict.side_effect = fake_predict
        ensemble = EnsembleManager(manager=manager)

        result = ensemble.evaluate("A" * 10, "T" * 10)

        # Scores are nearly identical (spread=0.01) but classifications
        # straddle the no_significant_effect/moderate_effect boundary --
        # agreement must be capped, not reported as ~99%.
        self.assertLessEqual(result["agreement_percentage"], 50.0)

    def test_disagreement_reasoning_lists_both_models(self):
        def fake_predict(key, ref, alt, **kwargs):
            if key == "enformer":
                return _result(0.1, "no_significant_effect")
            return _result(0.9, "large_effect")

        manager = mock.Mock()
        manager.predict.side_effect = fake_predict
        ensemble = EnsembleManager(manager=manager)

        result = ensemble.evaluate("A" * 10, "T" * 10)

        self.assertIn("enformer", result["reasoning"])
        self.assertIn("borzoi", result["reasoning"])
        self.assertIn("agreement=", result["reasoning"])


class TestConsensusConfidence(unittest.TestCase):
    def test_confidence_is_averaged_across_models(self):
        def fake_predict(key, ref, alt, **kwargs):
            if key == "enformer":
                return _result(0.5, "moderate_effect", confidence=0.4)
            return _result(0.5, "moderate_effect", confidence=0.8)

        manager = mock.Mock()
        manager.predict.side_effect = fake_predict
        ensemble = EnsembleManager(manager=manager)

        result = ensemble.evaluate("A" * 10, "T" * 10)
        self.assertAlmostEqual(result["confidence"], 0.6)

    def test_missing_confidence_values_are_skipped_not_treated_as_zero(self):
        def fake_predict(key, ref, alt, **kwargs):
            if key == "enformer":
                return {"score": 0.5, "classification": "moderate_effect", "confidence": None, "details": {}}
            return _result(0.5, "moderate_effect", confidence=0.8)

        manager = mock.Mock()
        manager.predict.side_effect = fake_predict
        ensemble = EnsembleManager(manager=manager)

        result = ensemble.evaluate("A" * 10, "T" * 10)
        self.assertAlmostEqual(result["confidence"], 0.8)  # average of just [0.8], not [0.8, 0]


class TestEnsembleManagerDefaultConstruction(unittest.TestCase):
    def test_default_manager_uses_real_registry_and_stays_disabled(self):
        # No mocking at all -- exercises the real default construction
        # path, confirming it never crashes even with both plugins
        # disabled by default.
        ensemble = EnsembleManager()
        result = ensemble.evaluate("A" * 10, "T" * 10)
        self.assertEqual(result["models_used"], [])
        self.assertEqual(result["basis"], "no_models")


if __name__ == "__main__":
    unittest.main()
