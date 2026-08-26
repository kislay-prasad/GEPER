"""
Unit tests for pipeline/models/spliceformer_plugin.py.

Mirrors tests/test_enformer_plugin.py's own structure and mocking
boundary exactly: the official Spliceformer model source is vendored
(pipeline/models/spliceformer/vendor/), but building it requires
`einops` and loading a real checkpoint requires reaching GitHub, which
this test environment cannot always do -- so every test here mocks at
that exact boundary (`spliceformer_loader.build_model`/
`download_checkpoint`/`load_checkpoint_into`, or `self.model`
directly), never the surrounding GEPER logic. This means the plugin's
own code (sequence preparation, one-hot encoding, delta/classification
computation, error sanitization) runs for real and is what's actually
under test.
"""

import unittest
from unittest import mock

import torch

from pipeline.models.manager import ModelManager, PluginUnavailableError
from pipeline.models.registry import ModelRegistry
from pipeline.models.spliceformer_plugin import (
    SPLICEFORMER_TOTAL_INPUT_LENGTH,
    SpliceFormerPlugin,
)
from utils.exceptions import ModelLoadError
from utils.auto_install import PackageCheckStatus


class _FakeSpliceFormerModel:
    """Mimics the vendored SpliceFormer module's callable interface
    just enough to drive SpliceFormerPlugin's own logic: callable with
    a one-hot-encoded input tensor of shape (2, 4, total_length)
    already on the expected device, returning the same 5-tuple the
    real `SpliceFormer.forward()` returns when `returnFmap=False`
    (out, acceptor_actions, donor_actions, acceptor_log_probs,
    donor_log_probs) -- SpliceFormerPlugin only ever reads the first
    element.
    """

    def __init__(self, ref_value: float, alt_value: float, scored_len: int = 8, expected_device: str = "cpu"):
        self.ref_value = ref_value
        self.alt_value = alt_value
        self.scored_len = scored_len
        self.expected_device = expected_device
        self.to_calls = []
        self.eval_called = False

    def to(self, device):
        self.to_calls.append(device)
        return self

    def eval(self):
        self.eval_called = True
        return self

    def __call__(self, features):
        assert isinstance(features, torch.Tensor)
        assert features.shape[0] == 2
        assert features.shape[1] == 4
        assert features.shape[2] == SPLICEFORMER_TOTAL_INPUT_LENGTH
        assert str(features.device) == self.expected_device
        ref_out = torch.full((3, self.scored_len), self.ref_value)
        alt_out = torch.full((3, self.scored_len), self.alt_value)
        out = torch.stack([ref_out, alt_out], dim=0)
        return out, None, None, None, None


class TestPrepareSequence(unittest.TestCase):
    def test_short_sequence_is_centered_and_padded_with_n(self):
        prepared = SpliceFormerPlugin._prepare_sequence("ACGT", target_length=10)
        self.assertEqual(len(prepared), 10)
        self.assertEqual(prepared, "NNNACGTNNN")

    def test_exact_length_sequence_is_unchanged(self):
        seq = "A" * 20
        self.assertEqual(SpliceFormerPlugin._prepare_sequence(seq, target_length=20), seq)

    def test_long_sequence_is_truncated_symmetrically(self):
        seq = "N" * 5 + "ACGTACGTAC" + "N" * 5  # length 20
        prepared = SpliceFormerPlugin._prepare_sequence(seq, target_length=10)
        self.assertEqual(len(prepared), 10)
        self.assertEqual(prepared, "ACGTACGTAC")

    def test_default_target_length_matches_official_configuration(self):
        prepared = SpliceFormerPlugin._prepare_sequence("A")
        self.assertEqual(len(prepared), SPLICEFORMER_TOTAL_INPUT_LENGTH)
        # SL (5,000) + CL_max (40,000), per the official repo's own
        # inference notebook -- see pipeline/models/spliceformer/loader.py.
        self.assertEqual(SPLICEFORMER_TOTAL_INPUT_LENGTH, 45_000)

    def test_lowercase_input_is_uppercased(self):
        prepared = SpliceFormerPlugin._prepare_sequence("acgt", target_length=4)
        self.assertEqual(prepared, "ACGT")


class TestOneHotEncode(unittest.TestCase):
    def test_known_bases_encode_correctly_on_plus_strand(self):
        tensor = SpliceFormerPlugin._one_hot_encode("ACGT", strand="+")
        self.assertEqual(tuple(tensor.shape), (4, 4))
        # Column order is [A, C, G, T] (official IN_MAP convention).
        expected = torch.tensor(
            [
                [1, 0, 0, 0],  # A
                [0, 1, 0, 0],  # C
                [0, 0, 1, 0],  # G
                [0, 0, 0, 1],  # T
            ],
            dtype=torch.float32,
        ).T
        self.assertTrue(torch.equal(tensor, expected))

    def test_n_and_unknown_bases_are_all_zero_column(self):
        tensor = SpliceFormerPlugin._one_hot_encode("ANXG", strand="+")
        self.assertTrue(torch.equal(tensor[:, 1], torch.zeros(4)))  # N
        self.assertTrue(torch.equal(tensor[:, 2], torch.zeros(4)))  # X (unknown)

    def test_minus_strand_reverse_complements(self):
        # Official convention: reverse the sequence, then complement
        # (A<->T, C<->G) via the (5-code)%5 trick -- see _one_hot_encode's
        # docstring. "AACG" reverse-complemented is "CGTT".
        plus = SpliceFormerPlugin._one_hot_encode("CGTT", strand="+")
        minus = SpliceFormerPlugin._one_hot_encode("AACG", strand="-")
        self.assertTrue(torch.equal(plus, minus))

    def test_shape_and_dtype(self):
        tensor = SpliceFormerPlugin._one_hot_encode("A" * 100, strand="+")
        self.assertEqual(tuple(tensor.shape), (4, 100))
        self.assertEqual(tensor.dtype, torch.float32)


class TestSpliceFormerInference(unittest.TestCase):
    def _make_instance(self, fake_model):
        instance = SpliceFormerPlugin.__new__(SpliceFormerPlugin)
        instance.logger = mock.Mock()
        instance.device = "cpu"
        instance.model = fake_model
        return instance

    def test_identical_ref_and_alt_yields_no_significant_effect(self):
        instance = self._make_instance(_FakeSpliceFormerModel(ref_value=0.2, alt_value=0.2))
        result = instance._infer_impl("A" * 10, "A" * 10)
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["classification"], "no_significant_effect")
        self.assertEqual(result["confidence"], 0.0)

    def test_large_delta_yields_large_effect_classification(self):
        instance = self._make_instance(_FakeSpliceFormerModel(ref_value=0.0, alt_value=0.9))
        result = instance._infer_impl("A" * 10, "T" * 10)
        self.assertAlmostEqual(result["score"], 0.9, places=5)
        self.assertEqual(result["classification"], "large_effect")

    def test_moderate_delta_yields_moderate_effect_classification(self):
        instance = self._make_instance(_FakeSpliceFormerModel(ref_value=0.0, alt_value=0.3))
        result = instance._infer_impl("A" * 10, "T" * 10)
        self.assertEqual(result["classification"], "moderate_effect")

    def test_details_report_creation_and_disruption_deltas_and_calibration_status(self):
        instance = self._make_instance(_FakeSpliceFormerModel(ref_value=0.0, alt_value=0.6))
        result = instance._infer_impl("A" * 10, "T" * 10)
        details = result["details"]
        self.assertIn("uncalibrated", details["calibration_status"])
        self.assertAlmostEqual(details["acceptor_creation_delta"], 0.6, places=5)
        self.assertAlmostEqual(details["donor_creation_delta"], 0.6, places=5)
        self.assertEqual(details["strand"], "+")

    def test_predict_via_base_class_tags_meta_correctly(self):
        instance = self._make_instance(_FakeSpliceFormerModel(ref_value=0.0, alt_value=0.6))
        instance._loaded = True  # bypass load() since we set self.model manually
        result = instance.predict("A" * 10, "T" * 10)
        self.assertEqual(result["meta"]["model"], "spliceformer")
        self.assertIn("device", result["meta"])

    def test_minus_strand_kwarg_is_forwarded_and_recorded(self):
        instance = self._make_instance(_FakeSpliceFormerModel(ref_value=0.0, alt_value=0.6))
        result = instance._infer_impl("A" * 10, "T" * 10, strand="-")
        self.assertEqual(result["details"]["strand"], "-")


class TestSpliceFormerLoadImpl(unittest.TestCase):
    def setUp(self):
        self.instance = SpliceFormerPlugin.__new__(SpliceFormerPlugin)
        self.instance.logger = mock.Mock()
        self.instance.device = "cpu"
        self.instance._loaded = False
        self.instance.model = None
        self.instance._weight_cache = mock.Mock()
        fake_cache_dir = mock.Mock()
        fake_cache_dir.__truediv__ = lambda self_, name: mock.Mock(is_file=mock.Mock(return_value=False))
        self.instance._weight_cache.ensure_dir.return_value = fake_cache_dir

    def test_successful_load_downloads_checkpoint_and_calls_to_and_eval(self):
        fake_model = _FakeSpliceFormerModel(ref_value=0.0, alt_value=1.0)
        with (
            mock.patch(
                "pipeline.models.spliceformer_plugin.check_pip_package_availability",
                return_value=PackageCheckStatus.PRESENT,
            ),
            mock.patch("pipeline.models.spliceformer_plugin.spliceformer_loader.build_model", return_value=fake_model),
            mock.patch("pipeline.models.spliceformer_plugin.spliceformer_loader.download_checkpoint") as mock_download,
            mock.patch(
                "pipeline.models.spliceformer_plugin.spliceformer_loader.load_checkpoint_into"
            ) as mock_load_state,
        ):
            self.instance._load_impl()

        mock_download.assert_called_once()
        mock_load_state.assert_called_once()
        self.assertIs(self.instance.model, fake_model)
        self.assertEqual(fake_model.to_calls, ["cpu"])
        self.assertTrue(fake_model.eval_called)

    def test_skips_download_when_checkpoint_already_cached(self):
        fake_model = _FakeSpliceFormerModel(ref_value=0.0, alt_value=1.0)
        cached_path = mock.Mock(is_file=mock.Mock(return_value=True))
        self.instance._weight_cache.ensure_dir.return_value.__truediv__ = lambda self_, name: cached_path

        with (
            mock.patch(
                "pipeline.models.spliceformer_plugin.check_pip_package_availability",
                return_value=PackageCheckStatus.PRESENT,
            ),
            mock.patch("pipeline.models.spliceformer_plugin.spliceformer_loader.build_model", return_value=fake_model),
            mock.patch("pipeline.models.spliceformer_plugin.spliceformer_loader.download_checkpoint") as mock_download,
            mock.patch(
                "pipeline.models.spliceformer_plugin.spliceformer_loader.load_checkpoint_into"
            ) as mock_load_state,
        ):
            self.instance._load_impl()

        mock_download.assert_not_called()
        mock_load_state.assert_called_once_with(fake_model, cached_path, "cpu")

    def test_network_failure_is_sanitized(self):
        with (
            mock.patch(
                "pipeline.models.spliceformer_plugin.check_pip_package_availability",
                return_value=PackageCheckStatus.PRESENT,
            ),
            mock.patch(
                "pipeline.models.spliceformer_plugin.spliceformer_loader.build_model",
                return_value=_FakeSpliceFormerModel(0.0, 1.0),
            ),
            mock.patch(
                "pipeline.models.spliceformer_plugin.spliceformer_loader.download_checkpoint",
                side_effect=ConnectionError("could not reach raw.githubusercontent.com"),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.instance._load_impl()

        self.assertEqual(str(ctx.exception), "SpliceFormer model unavailable")
        self.assertNotIn("raw.githubusercontent.com", str(ctx.exception))

    def test_missing_einops_raises_clear_error(self):
        with mock.patch(
            "pipeline.models.spliceformer_plugin.check_pip_package_availability", return_value=PackageCheckStatus.ABSENT
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.instance._load_impl()
        self.assertIn("einops", str(ctx.exception))

    def test_full_load_via_public_api_wraps_in_model_load_error_on_failure(self):
        # Exercise through PluginModel.load() (not _load_impl directly)
        # to confirm the base class's own wrapping still applies.
        with (
            mock.patch(
                "pipeline.models.spliceformer_plugin.check_pip_package_availability",
                return_value=PackageCheckStatus.PRESENT,
            ),
            mock.patch(
                "pipeline.models.spliceformer_plugin.spliceformer_loader.build_model",
                return_value=_FakeSpliceFormerModel(0.0, 1.0),
            ),
            mock.patch(
                "pipeline.models.spliceformer_plugin.spliceformer_loader.download_checkpoint",
                side_effect=OSError("network unreachable"),
            ),
        ):
            with self.assertRaises(ModelLoadError):
                self.instance.load()


class TestSpliceFormerMetadataAndAvailability(unittest.TestCase):
    def test_metadata_reports_commercial_use_allowed(self):
        meta = SpliceFormerPlugin.metadata()
        self.assertTrue(meta.commercial_use_allowed)
        self.assertEqual(meta.license_name, "MIT")
        self.assertIn("benniatli/Spliceformer", meta.source)

    def test_disabled_by_default_flag_off(self):
        with mock.patch("pipeline.models.spliceformer_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_SPLICEFORMER = False
            self.assertFalse(SpliceFormerPlugin.is_available())
            self.assertIn("ENABLE_SPLICEFORMER", SpliceFormerPlugin.unavailability_reason())

    def test_available_when_flag_on_and_einops_installed(self):
        with (
            mock.patch("pipeline.models.spliceformer_plugin.CONFIG") as mock_config,
            mock.patch("pipeline.models.spliceformer_plugin.ensure_pip_package_available", return_value=True),
        ):
            mock_config.splicing.ENABLE_SPLICEFORMER = True
            self.assertTrue(SpliceFormerPlugin.is_available())

    def test_unavailable_when_flag_on_but_einops_missing(self):
        with (
            mock.patch("pipeline.models.spliceformer_plugin.CONFIG") as mock_config,
            mock.patch("pipeline.models.spliceformer_plugin.ensure_pip_package_available", return_value=False),
        ):
            mock_config.splicing.ENABLE_SPLICEFORMER = True
            self.assertFalse(SpliceFormerPlugin.is_available())
            self.assertIn("einops", SpliceFormerPlugin.unavailability_reason())


class TestSpliceFormerThroughModelManager(unittest.TestCase):
    """Confirms the plugin behaves correctly when driven through
    ModelManager (not called directly) -- lazy loading, caching,
    graceful failure -- using the real manager, a real registry, and
    only the network/package-install boundary mocked."""

    def test_predict_returns_none_when_disabled(self):
        registry = ModelRegistry()
        registry.register("spliceformer", SpliceFormerPlugin)
        manager = ModelManager(registry=registry)
        with mock.patch("pipeline.models.spliceformer_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_SPLICEFORMER = False
            result = manager.predict("spliceformer", "A" * 10, "T" * 10)
        self.assertIsNone(result)

    def test_get_raises_plugin_unavailable_when_disabled(self):
        registry = ModelRegistry()
        registry.register("spliceformer", SpliceFormerPlugin)
        manager = ModelManager(registry=registry)
        with mock.patch("pipeline.models.spliceformer_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_SPLICEFORMER = False
            with self.assertRaises(PluginUnavailableError):
                manager.get("spliceformer")


if __name__ == "__main__":
    unittest.main()
