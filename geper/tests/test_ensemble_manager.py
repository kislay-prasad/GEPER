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

import pytest

from pipeline.models.ensemble import EnsembleManager


def _result(score, classification, confidence=0.5):
    return {"score": score, "classification": classification, "confidence": confidence, "details": {}}


class TestZeroModelsAvailable(unittest.TestCase):
    def test_no_models_returns_empty_models_used(self):
        # `last_inference_errors()` must return {} here, matching the
        # real `ModelManager`'s contract for "both plugins cleanly
        # unavailable, neither was ever attempted" -- see
        # TestZeroModelsBecauseBothCrashed below for the crash case
        # this same call site now also has to distinguish (2026-09-11).
        manager = mock.Mock()
        manager.predict.return_value = None
        manager.last_inference_errors.return_value = {}
        ensemble = EnsembleManager(manager=manager)

        result = ensemble.evaluate("A" * 10, "T" * 10)

        self.assertEqual(result["models_used"], [])
        self.assertIsNone(result["consensus_score"])
        self.assertIsNone(result["confidence"])
        self.assertIsNone(result["agreement_percentage"])
        self.assertIsNone(result["classification"])
        self.assertEqual(result["basis"], "no_models")
        self.assertIsNone(result["error"])
        self.assertIn("No splicing/regulatory AI model was available", result["reasoning"])

    def test_no_models_never_raises(self):
        manager = mock.Mock()
        manager.predict.side_effect = lambda *a, **k: None
        manager.last_inference_errors.return_value = {}
        ensemble = EnsembleManager(manager=manager)
        # Must not raise even when called repeatedly / with odd inputs.
        for _ in range(5):
            result = ensemble.evaluate("", "")
            self.assertEqual(result["models_used"], [])


class TestZeroModelsBecauseBothCrashed(unittest.TestCase):
    """
    Sweep finding 3 (2026-09-11): `EnsembleManager.evaluate()` used to
    never call `ModelManager.last_inference_errors()`, so a genuine
    double crash (both Enformer and Borzoi raised `ModelInferenceError`
    during inference, not "cleanly unavailable") was indistinguishable
    from a clean double-disable -- both produced `models_used=[]` and
    the same generic "disabled, not installed, or failed to load/run"
    reasoning. `report/report_generator.py::_render_ai_splicing_ensemble`
    then rendered NOTHING AT ALL for either case: not a wrong line, an
    absent section.

    THE CONTROL THAT MATTERS: `test_no_models_returns_empty_models_used`
    above pins the OTHER direction -- a genuinely clean disable (no
    entries in `last_inference_errors()`) must keep reading exactly as
    it did before this fix, with `error=None` and the unchanged
    reasoning text. Fixing the crash case must not turn a disabled
    model into a false crash report.
    """

    def test_double_crash_is_distinguished_from_clean_disable(self):
        manager = mock.Mock()
        manager.predict.return_value = None
        manager.last_inference_errors.return_value = {
            "enformer": "CUDA out of memory",
            "borzoi": "weights checksum mismatch",
        }
        ensemble = EnsembleManager(manager=manager)

        result = ensemble.evaluate("A" * 10, "T" * 10)

        self.assertEqual(result["models_used"], [])
        self.assertIsNone(result["consensus_score"])
        self.assertEqual(result["basis"], "no_models")
        self.assertIsNotNone(result["error"])
        self.assertIn("enformer", result["error"])
        self.assertIn("CUDA out of memory", result["error"])
        self.assertIn("borzoi", result["error"])
        self.assertIn("weights checksum mismatch", result["error"])
        self.assertNotIn(
            "No splicing/regulatory AI model was available",
            result["reasoning"],
            "a genuine crash must not be reported with the clean-disable reasoning text",
        )
        self.assertIn("failed", result["reasoning"].lower())

    def test_single_crash_alongside_a_cleanly_disabled_sibling_still_reported(self):
        """One model crashed, the other was cleanly disabled (never
        even attempted) -- still n==0 overall, and the error must name
        only the one that actually crashed."""
        manager = mock.Mock()
        manager.predict.return_value = None
        manager.last_inference_errors.return_value = {"enformer": "timed out after 30s"}
        ensemble = EnsembleManager(manager=manager)

        result = ensemble.evaluate("A" * 10, "T" * 10)

        self.assertEqual(result["models_used"], [])
        self.assertIsNotNone(result["error"])
        self.assertIn("enformer", result["error"])
        self.assertIn("timed out after 30s", result["error"])
        self.assertNotIn("borzoi", result["error"])


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
    @pytest.mark.real_pip
    def test_default_manager_uses_real_registry_and_never_crashes(self):
        """
        No mocking at all -- exercises the real default construction
        path: EnsembleManager() -> real EnformerPlugin -> real
        is_available() -> real
        utils.auto_install.ensure_pip_package_available() -> a genuine
        `sys.executable -m pip install` subprocess call. This is the
        ONLY test in the suite that confirms that real chain never
        crashes end-to-end, for whatever it resolves to (see below for
        why the result is deliberately not pinned).

        OPT-IN, NOT PART OF THE DEFAULT SUITE (see pytest.ini's
        `real_pip` marker / `addopts`). A bare `pytest` or
        `pytest tests/` run DOES NOT COLLECT this test -- run it
        explicitly with `pytest -m real_pip`, and only against a
        disposable/isolated interpreter (a throwaway venv), never the
        shared one every agent on this floor uses.

        WHY: this test shells out to a REAL pip install against
        whatever interpreter runs it, every single time it runs. Doing
        that against a SHARED global interpreter has a demonstrated
        collision/data-loss risk -- a WinError 32 from two concurrent
        pip processes fighting over the same site-packages file
        (2026-08-20), and a live hypothesis (2026-08-21) that a
        harness governor kill landing between pip's uninstall-old and
        install-new steps is how a package can vanish from the shared
        environment with no trace of an interrupted install. Mocking
        the pip call instead was considered and rejected -- it would
        destroy exactly the thing this test uniquely verifies (that
        the REAL subprocess call, with all its real failure modes,
        does not crash the manager).

        THE HONEST TRADE, stated plainly per the ruling that added this
        marker: because this test is excluded from default runs, THE
        DEFAULT SUITE DOES NOT COVER THE REAL AUTO-INSTALL SUBPROCESS
        PATH. If `utils.auto_install.ensure_pip_package_available()`
        or the real pip subprocess plumbing around it breaks, a
        default `pytest tests/` run will not catch it -- only an
        explicit, deliberate `pytest -m real_pip` run (in a disposable
        environment) will. This is a real, accepted coverage gap, not
        a hidden one.

        Deliberately does NOT pin models_used to a specific value:
        enformer/borzoi are enabled BY CONFIG DEFAULT (see
        test_new_plugins_integration.py::
        TestBothPluginsDisabledByDefault::
        test_enformer_borzoi_dependency_gated) and only gated by their
        own optional pip package / model-cache availability, which is
        genuine machine state, not policy -- pinning an empty list here
        was only ever true because the packages happened to be
        uninstalled/broken on whichever box ran it, not because of any
        "disabled by default" guarantee. The `[]` case itself is
        already covered deterministically by
        TestZeroModelsAvailable::test_no_models_returns_empty_models_used
        above, via a mocked manager -- this test's unique value is the
        real, unmocked registry construction path staying crash-free
        and internally consistent for WHATEVER it resolves to.
        """
        ensemble = EnsembleManager()
        result = ensemble.evaluate("A" * 10, "T" * 10)
        self.assertIsInstance(result["models_used"], list)
        self.assertLessEqual(set(result["models_used"]), {"enformer", "borzoi"})
        if result["models_used"]:
            self.assertNotEqual(result["basis"], "no_models")
        else:
            self.assertEqual(result["basis"], "no_models")


if __name__ == "__main__":
    unittest.main()
