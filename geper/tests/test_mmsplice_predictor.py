"""
Unit tests for pipeline/models/mmsplice/predictor.py.

MMSpliceModel is mocked out entirely (no TensorFlow, no real weight
files needed) -- these tests verify MMSplicePredictor's own
combination/derivation logic given known modular scores.
"""

import unittest
from unittest import mock

from pipeline.models.mmsplice.models import ModularScores
from pipeline.models.mmsplice.predictor import MMSplicePredictor


def _make_mock_model(ref_scores: ModularScores, alt_scores: ModularScores, cryptic_score: float = 0.0):
    model = mock.Mock()
    model.model_version = "mmsplice-2.4.0-test"
    model.score_modular.side_effect = [ref_scores, alt_scores]
    model.score_single_module_batch.return_value = [cryptic_score] * 41  # scan_range default 20 -> 41 candidates
    return model


class TestMMSplicePredictor(unittest.TestCase):
    def test_predict_returns_all_required_fields(self):
        ref = ModularScores(0.0, 0.0, 0.0, 0.0, 0.0)
        alt = ModularScores(0.0, 0.0, 0.0, -3.0, 0.0)
        model = _make_mock_model(ref, alt)
        predictor = MMSplicePredictor(model)

        result = predictor.predict("N" * 300, "N" * 300, overhang=(100, 100))

        self.assertEqual(result.ref_scores, ref)
        self.assertEqual(result.alt_scores, alt)
        self.assertIsInstance(result.delta_logit_psi, float)
        self.assertAlmostEqual(result.donor_delta, -3.0)
        self.assertEqual(result.model_version, "mmsplice-2.4.0-test")
        self.assertGreaterEqual(result.runtime_ms, 0.0)

    def test_exon_skipping_score_requires_coordinated_loss(self):
        ref = ModularScores(0.0, 2.0, 1.0, 2.0, 0.0)
        alt = ModularScores(0.0, -2.0, -1.0, -2.0, 0.0)  # both acceptor and donor collapse
        model = _make_mock_model(ref, alt)
        predictor = MMSplicePredictor(model)

        result = predictor.predict("N" * 300, "N" * 300, overhang=(100, 100))
        self.assertGreater(result.exon_skipping_score, 0.0)

    def test_exon_skipping_score_zero_when_only_one_site_weakens(self):
        ref = ModularScores(0.0, 2.0, 1.0, 2.0, 0.0)
        alt = ModularScores(0.0, -2.0, 1.0, 2.0, 0.0)  # only acceptor weakens, donor unchanged
        model = _make_mock_model(ref, alt)
        predictor = MMSplicePredictor(model)

        result = predictor.predict("N" * 300, "N" * 300, overhang=(100, 100))
        self.assertEqual(result.exon_skipping_score, 0.0)

    def test_intron_retention_score_requires_coordinated_gain(self):
        ref = ModularScores(0.0, 0.0, 0.0, 0.0, 0.0)
        alt = ModularScores(3.0, 0.0, 0.0, 0.0, 3.0)  # both intron modules gain signal
        model = _make_mock_model(ref, alt)
        predictor = MMSplicePredictor(model)

        result = predictor.predict("N" * 300, "N" * 300, overhang=(100, 100))
        self.assertAlmostEqual(result.intron_retention_score, 3.0)

    def test_cryptic_scan_failure_is_non_fatal(self):
        ref = ModularScores(0.0, 0.0, 0.0, 0.0, 0.0)
        alt = ModularScores(0.0, 0.0, 0.0, -1.0, 0.0)
        model = mock.Mock()
        model.model_version = "mmsplice-2.4.0-test"
        model.score_modular.side_effect = [ref, alt]
        model.score_single_module_batch.side_effect = RuntimeError("TF session died")
        predictor = MMSplicePredictor(model)

        result = predictor.predict("N" * 300, "N" * 300, overhang=(100, 100))
        self.assertEqual(result.alt_donor_score, 0.0)
        self.assertEqual(result.alt_acceptor_score, 0.0)

    def test_predict_batch_matches_predict_per_item(self):
        ref = ModularScores(0.1, 0.1, 0.1, 0.1, 0.1)
        alt = ModularScores(0.1, 0.1, 0.1, -2.0, 0.1)
        model = mock.Mock()
        model.model_version = "mmsplice-2.4.0-test"
        model.score_modular_batch.side_effect = [[ref, ref], [alt, alt]]
        model.score_single_module_batch.return_value = [0.0] * 41
        predictor = MMSplicePredictor(model)

        results = predictor.predict_batch(["N" * 300, "N" * 300], ["N" * 300, "N" * 300], overhang=(100, 100))
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertAlmostEqual(r.donor_delta, -2.1, places=6)

    def test_predict_batch_empty_input(self):
        model = mock.Mock()
        predictor = MMSplicePredictor(model)
        self.assertEqual(predictor.predict_batch([], [], overhang=(100, 100)), [])


if __name__ == "__main__":
    unittest.main()
