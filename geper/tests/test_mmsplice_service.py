"""
Unit tests for pipeline/models/mmsplice/service.py.

Ensembl HTTP calls are mocked at the `requests.Session` level (the
same session `SequenceContextGenerator` uses); `MMSpliceModel` is
mocked so no TensorFlow / real weight files are needed.
"""

import unittest
from unittest import mock

from pipeline.models.mmsplice.service import MMSpliceService, _null_result
from pipeline.sequence_context import SequenceContextGenerator
from pipeline.vcf_parser import Variant


def _variant(chrom="1", pos=1000, ref="A", alt="G"):
    return Variant(chrom=chrom, pos=pos, variant_id=".", ref=ref, alt=alt, qual=None, filter_status=None)


def _exon_feature(start=900, end=1100, strand=1, exon_id="ENSE1", parent="ENST1"):
    return {"start": start, "end": end, "strand": strand, "exon_id": exon_id, "Parent": parent}


class TestMMSpliceServiceEligibility(unittest.TestCase):
    def setUp(self):
        self.model = mock.Mock()
        self.seq_ctx = SequenceContextGenerator(species="human", assembly=None)
        self.service = MMSpliceService(model=self.model, sequence_context_generator=self.seq_ctx, species="human")

    def test_unsupported_variant_type_is_skipped_gracefully(self):
        variant = Variant(chrom="1", pos=1000, variant_id=".", ref="AT", alt="AC", qual=None, filter_status=None)
        # ref='AT', alt='AC' -> same length, not SNV/insertion/deletion -> MNV
        self.assertEqual(variant.variant_type, "MNV")
        result = self.service.predict(variant)
        self.assertFalse(result["supported"])
        self.assertFalse(result["predicted"])
        self.assertIn("MNV", result["skip_reason"])

    def test_no_overlapping_exon_returns_graceful_skip(self):
        variant = _variant()
        with mock.patch.object(self.service, "_fetch_overlapping_exons", return_value=[]):
            result = self.service.predict(variant)
        self.assertFalse(result["supported"])
        self.assertFalse(result["predicted"])
        self.assertIn("no exon annotation", result["skip_reason"])

    def test_deep_intronic_variant_is_ineligible(self):
        variant = _variant(pos=100)  # far from exon at 900-1100
        exon_feature = _exon_feature(start=900, end=1100)
        with mock.patch.object(
            self.service,
            "_fetch_overlapping_exons",
            return_value=[self.service._parse_exon_feature("1", exon_feature)],
        ):
            result = self.service.predict(variant)
        self.assertFalse(result["supported"])
        self.assertFalse(result["predicted"])

    def test_never_raises_on_internal_error(self):
        variant = _variant()
        with mock.patch.object(self.service, "_fetch_overlapping_exons", side_effect=RuntimeError("boom")):
            result = self.service.predict(variant)  # must not raise
        self.assertFalse(result["supported"])
        self.assertFalse(result["predicted"])
        self.assertIn("boom", result["skip_reason"])


class TestMMSpliceServiceEndToEndWithMocks(unittest.TestCase):
    def setUp(self):
        self.model = mock.Mock()
        self.seq_ctx = SequenceContextGenerator(species="human", assembly=None)
        self.service = MMSpliceService(model=self.model, sequence_context_generator=self.seq_ctx, species="human")

    def test_eligible_variant_calls_predictor_and_returns_full_schema(self):
        variant = _variant(pos=905)  # 5bp inside a 900-1100 exon, near acceptor boundary
        exon = self.service._parse_exon_feature("1", _exon_feature(start=900, end=1100))

        fake_raw = mock.Mock(
            delta_logit_psi=-3.0,
            donor_delta=-1.0,
            acceptor_delta=-3.0,
            exon_delta=-0.5,
            acceptor_intron_delta=0.0,
            donor_intron_delta=0.0,
            alt_donor_score=0.1,
            alt_acceptor_score=0.2,
            exon_skipping_score=0.0,
            intron_retention_score=0.0,
            model_version="mmsplice-2.4.0-test",
            runtime_ms=12.3,
        )

        with (
            mock.patch.object(self.service, "_fetch_overlapping_exons", return_value=[exon]),
            mock.patch.object(self.service, "_build_windows", return_value=("N" * 300, "N" * 300, (100, 100))),
            mock.patch.object(self.service.predictor, "predict", return_value=fake_raw) as mock_predict,
        ):
            result = self.service.predict(variant)

        mock_predict.assert_called_once()
        self.assertTrue(result["supported"])
        self.assertTrue(result["predicted"])
        self.assertEqual(result["donor_score"], -1.0)
        self.assertEqual(result["acceptor_score"], -3.0)
        self.assertIn("interpretation", result)
        self.assertIn("confidence", result)
        self.assertEqual(result["model_version"], "mmsplice-2.4.0-test")

    def test_cache_hit_skips_predictor(self):
        variant = _variant(pos=905)
        exon = self.service._parse_exon_feature("1", _exon_feature(start=900, end=1100))
        fake_raw = mock.Mock(
            delta_logit_psi=-3.0,
            donor_delta=-1.0,
            acceptor_delta=-3.0,
            exon_delta=-0.5,
            acceptor_intron_delta=0.0,
            donor_intron_delta=0.0,
            alt_donor_score=0.1,
            alt_acceptor_score=0.2,
            exon_skipping_score=0.0,
            intron_retention_score=0.0,
            model_version="mmsplice-2.4.0-test",
            runtime_ms=12.3,
        )
        with (
            mock.patch.object(self.service, "_fetch_overlapping_exons", return_value=[exon]),
            mock.patch.object(self.service, "_build_windows", return_value=("N" * 300, "N" * 300, (100, 100))),
            mock.patch.object(self.service.predictor, "predict", return_value=fake_raw) as mock_predict,
        ):
            self.service.predict(variant)
            self.service.predict(variant)  # second call: should hit the cache

        self.assertEqual(mock_predict.call_count, 1)


class TestNullResultSchema(unittest.TestCase):
    def test_null_result_contains_every_required_key(self):
        required_keys = {
            "supported",
            "predicted",
            "delta_logit_psi",
            "donor_score",
            "acceptor_score",
            "exon_skipping",
            "intron_retention",
            "alt_donor",
            "alt_acceptor",
            "confidence",
            "interpretation",
            "runtime_ms",
            "model_version",
        }
        result = _null_result(supported=False, predicted=False, reason="test")
        self.assertTrue(required_keys.issubset(result.keys()))


if __name__ == "__main__":
    unittest.main()
