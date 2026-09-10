"""
Unit tests for pipeline/models/mmsplice/utils.py.

These are all pure-function tests (SeqSplitter, one-hot encoding, the
delta-logit-PSI combination formula, eligibility/region classification,
interpretation text generation) -- no model weights, no network, no
mocking required.
"""

import unittest

import numpy as np

from pipeline.models.mmsplice.models import ExonAnnotation, ModularScores
from pipeline.models.mmsplice.utils import (
    SeqSplitter,
    classify_region,
    distances_to_exon_boundaries,
    encode_batch,
    evaluate_eligibility,
    generate_interpretation,
    logit,
    one_hot_encode,
    predict_delta_logit_psi,
)


class TestOneHotEncoding(unittest.TestCase):
    def test_encodes_acgt_correctly(self):
        encoded = one_hot_encode("ACGT")
        expected = np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=np.float32)
        np.testing.assert_array_equal(encoded, expected)

    def test_n_and_unknown_bases_are_all_zero(self):
        encoded = one_hot_encode("ANx")
        self.assertTrue(np.array_equal(encoded[0], [1, 0, 0, 0]))
        self.assertTrue(np.array_equal(encoded[1], [0, 0, 0, 0]))
        self.assertTrue(np.array_equal(encoded[2], [0, 0, 0, 0]))

    def test_lowercase_is_handled(self):
        encoded = one_hot_encode("acgt")
        self.assertEqual(encoded.shape, (4, 4))
        self.assertEqual(encoded.sum(), 4)

    def test_batch_left_pads_shorter_sequences(self):
        batch = encode_batch(["ACGT", "AC"])
        self.assertEqual(batch.shape, (2, 4, 4))
        # Shorter sequence's real content should be right-aligned (the
        # first 2 rows padded with all-zero 'N' rows).
        self.assertTrue(np.array_equal(batch[1, 0], [0, 0, 0, 0]))
        self.assertTrue(np.array_equal(batch[1, 1], [0, 0, 0, 0]))
        self.assertTrue(np.array_equal(batch[1, 2], [1, 0, 0, 0]))  # 'A'

    def test_empty_batch(self):
        batch = encode_batch([])
        self.assertEqual(batch.shape[0], 0)


class TestSeqSplitter(unittest.TestCase):
    def setUp(self):
        self.splitter = SeqSplitter()

    def test_split_lengths_match_model_input_shapes(self):
        # 100bp intron + 80bp exon + 100bp intron, matching each
        # submodel's expected input length exactly (see
        # models.py::MODEL_FILENAMES / the loader.py module docstring
        # for how these were verified against the real .h5 files:
        # Acceptor=53, Donor=18).
        seq = "N" * 100 + "A" * 80 + "N" * 100
        splits = self.splitter.split(seq, overhang=(100, 100))
        self.assertEqual(len(splits["acceptor"]), 53)
        self.assertEqual(len(splits["donor"]), 18)
        self.assertEqual(len(splits["exon"]), 80)
        self.assertEqual(len(splits["acceptor_intron"]), 94)  # 100 - 6 (acceptor_intron_cut)
        self.assertEqual(len(splits["donor_intron"]), 94)

    def test_short_overhang_is_padded_with_n(self):
        # Overhang shorter than the model requires (e.g. a variant near
        # a contig edge, or a terminal exon) must be zero-padded, not
        # raise.
        seq = "N" * 10 + "A" * 20 + "N" * 10
        splits = self.splitter.split(seq, overhang=(10, 10))
        self.assertEqual(len(splits["acceptor"]), 53)
        self.assertEqual(len(splits["donor"]), 18)
        self.assertTrue(set(splits["acceptor"][:20]).issubset({"N"}))

    def test_exon_never_empty(self):
        seq = "N" * 100 + "N" * 100  # zero-length exon
        splits = self.splitter.split(seq, overhang=(100, 100))
        self.assertEqual(splits["exon"], "N")

    def test_rejects_overhang_larger_than_sequence(self):
        with self.assertRaises(ValueError):
            self.splitter.split("ACGT", overhang=(100, 100))


class TestDeltaLogitPsi(unittest.TestCase):
    def test_identical_ref_alt_gives_near_zero_delta(self):
        # Not exactly zero: the official published linear model has a
        # small nonzero intercept term (~6.5e-4), applied regardless of
        # input -- this asserts the *only* deviation from zero is that
        # intercept, not a bug in the feature computation.
        scores = ModularScores(0.1, 0.2, 0.3, 0.4, 0.5)
        delta = predict_delta_logit_psi(scores, scores)
        self.assertAlmostEqual(delta, 0.0006480262366686865, places=9)

    def test_donor_loss_yields_negative_delta(self):
        ref = ModularScores(0.0, 0.0, 0.0, 5.0, 0.0)
        alt = ModularScores(0.0, 0.0, 0.0, -5.0, 0.0)  # donor logit score collapses
        delta = predict_delta_logit_psi(ref, alt)
        self.assertLess(delta, 0.0)

    def test_is_deterministic(self):
        ref = ModularScores(0.1, 0.2, 0.3, 0.4, 0.5)
        alt = ModularScores(0.15, -0.9, 0.1, -2.0, 0.05)
        self.assertEqual(predict_delta_logit_psi(ref, alt), predict_delta_logit_psi(ref, alt))


class TestLogit(unittest.TestCase):
    def test_clips_extreme_values(self):
        # p=0 or p=1 would be -inf/+inf without clipping.
        result = logit(np.array([0.0, 1.0]))
        self.assertTrue(np.all(np.isfinite(result)))

    def test_logit_of_half_is_zero(self):
        self.assertAlmostEqual(float(logit(np.array([0.5]))[0]), 0.0, places=6)


def _exon(start=1000, end=1100, strand=1):
    return ExonAnnotation(chrom="1", start=start, end=end, strand=strand, exon_id="ENSE1", transcript_id="ENST1")


class TestDistancesAndEligibility(unittest.TestCase):
    def test_plus_strand_intronic_near_acceptor(self):
        exon = _exon(start=1000, end=1100, strand=1)
        dist_acc, dist_don = distances_to_exon_boundaries(990, exon)
        self.assertEqual(dist_acc, 10)  # 10bp upstream of the acceptor (exon start)
        self.assertEqual(dist_don, -110)  # far from the donor side, negative = not near

    def test_plus_strand_intronic_near_donor(self):
        exon = _exon(start=1000, end=1100, strand=1)
        dist_acc, dist_don = distances_to_exon_boundaries(1110, exon)
        self.assertEqual(dist_don, 10)

    def test_minus_strand_swaps_acceptor_and_donor_sides(self):
        exon = _exon(start=1000, end=1100, strand=-1)
        # On the minus strand, the transcript's acceptor side is the
        # genomically *larger*-coordinate boundary (exon.end).
        dist_acc, _ = distances_to_exon_boundaries(1110, exon)
        self.assertEqual(dist_acc, 10)

    def test_classify_region_intronic_near_acceptor(self):
        region = classify_region(10, -110, exon_length=100, intron_window=100, exon_near_splice_window=50)
        self.assertEqual(region, "intronic_near_acceptor")

    def test_classify_region_deep_intronic(self):
        region = classify_region(500, -600, exon_length=100, intron_window=100, exon_near_splice_window=50)
        self.assertEqual(region, "deep_intronic")

    def test_classify_region_exonic_near_splice(self):
        # 5bp inside the exon from the acceptor boundary.
        region = classify_region(-5, -95, exon_length=100, intron_window=100, exon_near_splice_window=50)
        self.assertEqual(region, "exonic_near_splice")

    def test_classify_region_deep_exonic_in_long_exon(self):
        region = classify_region(-500, -500, exon_length=1000, intron_window=100, exon_near_splice_window=50)
        self.assertEqual(region, "deep_exonic")

    def test_classify_region_short_exon_always_near_splice(self):
        # An exon shorter than 2x the near-splice window is always
        # "near splice" from either side.
        region = classify_region(-40, -40, exon_length=80, intron_window=100, exon_near_splice_window=50)
        self.assertEqual(region, "exonic_near_splice")

    def test_eligibility_rejects_unsupported_variant_type(self):
        result = evaluate_eligibility(
            variant_type="MNV",
            supported_variant_types=("SNV", "insertion", "deletion"),
            dist_to_acceptor=10,
            dist_to_donor=-90,
            exon_length=100,
            intron_window=100,
            exon_near_splice_window=50,
        )
        self.assertFalse(result.eligible)
        self.assertIn("MNV", result.reason)

    def test_eligibility_accepts_intronic_snv(self):
        result = evaluate_eligibility(
            variant_type="SNV",
            supported_variant_types=("SNV", "insertion", "deletion"),
            dist_to_acceptor=10,
            dist_to_donor=-90,
            exon_length=100,
            intron_window=100,
            exon_near_splice_window=50,
        )
        self.assertTrue(result.eligible)
        self.assertEqual(result.region, "intronic_near_acceptor")

    def test_eligibility_rejects_deep_intronic_snv(self):
        result = evaluate_eligibility(
            variant_type="SNV",
            supported_variant_types=("SNV", "insertion", "deletion"),
            dist_to_acceptor=500,
            dist_to_donor=-600,
            exon_length=100,
            intron_window=100,
            exon_near_splice_window=50,
        )
        self.assertFalse(result.eligible)
        self.assertIn("outside", result.reason)


class TestInterpretationText(unittest.TestCase):
    def _thresholds(self):
        return dict(
            moderate_threshold=2.0,
            strong_threshold=5.0,
            site_loss_threshold=2.5,
            exon_skipping_threshold=2.0,
            intron_retention_threshold=2.0,
        )

    def test_no_disruption(self):
        text, category, confidence = generate_interpretation(
            delta_logit_psi=0.1,
            donor_delta=0.0,
            acceptor_delta=0.0,
            exon_skipping_score=0.0,
            intron_retention_score=0.0,
            **self._thresholds(),
        )
        self.assertEqual(text, "No predicted splice disruption.")
        self.assertEqual(category, "none")

    def test_moderate_disruption(self):
        text, category, _ = generate_interpretation(
            delta_logit_psi=3.0,
            donor_delta=-1.0,
            acceptor_delta=-1.0,
            exon_skipping_score=0.0,
            intron_retention_score=0.0,
            **self._thresholds(),
        )
        self.assertEqual(text, "Moderate splice disruption.")
        self.assertEqual(category, "moderate")

    def test_strong_donor_loss(self):
        text, category, _ = generate_interpretation(
            delta_logit_psi=-6.0,
            donor_delta=-6.0,
            acceptor_delta=-0.1,
            exon_skipping_score=0.0,
            intron_retention_score=0.0,
            **self._thresholds(),
        )
        self.assertEqual(text, "Strong donor site loss.")
        self.assertEqual(category, "strong_donor_loss")

    def test_strong_acceptor_loss(self):
        text, category, _ = generate_interpretation(
            delta_logit_psi=-6.0,
            donor_delta=-0.1,
            acceptor_delta=-6.0,
            exon_skipping_score=0.0,
            intron_retention_score=0.0,
            **self._thresholds(),
        )
        self.assertEqual(text, "Strong acceptor site loss.")
        self.assertEqual(category, "strong_acceptor_loss")

    def test_exon_skipping_takes_priority(self):
        text, category, _ = generate_interpretation(
            delta_logit_psi=-6.0,
            donor_delta=-6.0,
            acceptor_delta=-6.0,
            exon_skipping_score=3.0,
            intron_retention_score=0.0,
            **self._thresholds(),
        )
        self.assertEqual(text, "Likely exon skipping.")
        self.assertEqual(category, "exon_skipping")

    def test_intron_retention(self):
        text, category, _ = generate_interpretation(
            delta_logit_psi=3.0,
            donor_delta=0.5,
            acceptor_delta=0.5,
            exon_skipping_score=0.0,
            intron_retention_score=3.0,
            **self._thresholds(),
        )
        self.assertEqual(text, "Predicted intron retention.")
        self.assertEqual(category, "intron_retention")


if __name__ == "__main__":
    unittest.main()
