"""
Unit tests for pipeline/models/enformer_plugin.py.

`enformer_pytorch` is genuinely pip-installed in this environment
(confirmed available for import), but its `from_pretrained` call
requires reaching huggingface.co, which this test environment cannot
do -- so every test here mocks at that exact boundary (the
`enformer_pytorch.from_pretrained` call itself, or `self.model`
directly), never the surrounding GEPER logic. This means the plugin's
own code (sequence preparation, error sanitization, delta/
classification computation) runs for real and is what's actually
under test.
"""

import unittest
from unittest import mock

import torch

from pipeline.models.enformer_plugin import (
    ENFORMER_SEQUENCE_LENGTH,
    EnformerPlugin,
)
from pipeline.models.manager import ModelManager, PluginUnavailableError
from pipeline.models.registry import ModelRegistry
from utils.exceptions import ModelLoadError


class _FakeEnformerModel:
    """Mimics enformer_pytorch's model interface just enough to drive
    EnformerPlugin's own logic: callable with a pre-encoded one-hot
    input tensor (shape [2, seq_len, 4]) already on the expected
    device, and a `head` kwarg -- returning a dict with a tensor per
    head of shape [batch, target_length, num_tracks].

    The tensor-input (rather than raw-string) interface reflects the
    device-mismatch fix: EnformerPlugin now one-hot encodes the
    sequences itself and moves the tensor to `self.device` *before*
    calling the model, instead of relying on
    enformer_pytorch.Enformer.forward()'s own (buggy, no-op)
    `x.to(self.device)` call.
    """

    def __init__(self, ref_value: float, alt_value: float, num_tracks: int = 8, expected_device: str = "cpu"):
        self.ref_value = ref_value
        self.alt_value = alt_value
        self.num_tracks = num_tracks
        self.expected_device = expected_device
        self.to_calls = []
        self.eval_called = False

    def to(self, device):
        self.to_calls.append(device)
        return self

    def eval(self):
        self.eval_called = True
        return self

    def __call__(self, seqs, head="human"):
        assert isinstance(seqs, torch.Tensor)
        assert seqs.shape[0] == 2
        assert seqs.shape[1] == ENFORMER_SEQUENCE_LENGTH
        assert str(seqs.device) == self.expected_device
        ref_track = torch.full((4, self.num_tracks), self.ref_value)
        alt_track = torch.full((4, self.num_tracks), self.alt_value)
        return torch.stack([ref_track, alt_track], dim=0)


class TestPrepareSequence(unittest.TestCase):
    def test_short_sequence_is_centered_and_padded_with_n(self):
        prepared = EnformerPlugin._prepare_sequence("ACGT", target_length=10)
        self.assertEqual(len(prepared), 10)
        self.assertEqual(prepared, "NNNACGTNNN")

    def test_exact_length_sequence_is_unchanged(self):
        seq = "A" * 20
        self.assertEqual(EnformerPlugin._prepare_sequence(seq, target_length=20), seq)

    def test_long_sequence_is_truncated_symmetrically(self):
        seq = "N" * 5 + "ACGTACGTAC" + "N" * 5  # length 20
        prepared = EnformerPlugin._prepare_sequence(seq, target_length=10)
        self.assertEqual(len(prepared), 10)
        self.assertEqual(prepared, "ACGTACGTAC")

    def test_default_target_length_matches_official_constant(self):
        prepared = EnformerPlugin._prepare_sequence("A")
        self.assertEqual(len(prepared), ENFORMER_SEQUENCE_LENGTH)
        self.assertEqual(ENFORMER_SEQUENCE_LENGTH, 196_608)

    def test_lowercase_input_is_uppercased(self):
        prepared = EnformerPlugin._prepare_sequence("acgt", target_length=4)
        self.assertEqual(prepared, "ACGT")


class TestEnformerInference(unittest.TestCase):
    def _make_instance(self, fake_model):
        instance = EnformerPlugin.__new__(EnformerPlugin)
        instance.logger = mock.Mock()
        instance.device = "cpu"
        instance.model = fake_model
        return instance

    def test_identical_ref_and_alt_yields_no_significant_effect(self):
        instance = self._make_instance(_FakeEnformerModel(ref_value=1.0, alt_value=1.0))
        result = instance._infer_impl("A" * 10, "A" * 10)
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["classification"], "no_significant_effect")
        self.assertEqual(result["confidence"], 0.0)

    def test_large_delta_yields_large_effect_classification(self):
        instance = self._make_instance(_FakeEnformerModel(ref_value=0.0, alt_value=2.0))
        result = instance._infer_impl("A" * 10, "T" * 10)
        self.assertEqual(result["score"], 2.0)
        self.assertEqual(result["classification"], "large_effect")
        self.assertEqual(result["confidence"], 1.0)  # capped at 1.0

    def test_moderate_delta_yields_moderate_effect_classification(self):
        instance = self._make_instance(_FakeEnformerModel(ref_value=0.0, alt_value=0.3))
        result = instance._infer_impl("A" * 10, "T" * 10)
        self.assertEqual(result["classification"], "moderate_effect")

    def test_details_report_calibration_status_honestly(self):
        instance = self._make_instance(_FakeEnformerModel(ref_value=0.0, alt_value=1.0))
        result = instance._infer_impl("A" * 10, "T" * 10)
        self.assertIn("uncalibrated", result["details"]["calibration_status"])
        self.assertEqual(result["details"]["num_tracks"], 8)

    def test_predict_via_base_class_tags_meta_correctly(self):
        instance = self._make_instance(_FakeEnformerModel(ref_value=0.0, alt_value=1.0))
        instance._loaded = True  # bypass load() since we set self.model manually
        result = instance.predict("A" * 10, "T" * 10)
        self.assertEqual(result["meta"]["model"], "enformer")
        self.assertIn("device", result["meta"])


class TestEnformerDeviceMismatchFix(unittest.TestCase):
    """Regression coverage for the device-mismatch bug: the plugin
    must one-hot encode ref/alt sequences itself and move the
    resulting tensor to `self.device` before ever calling
    `self.model(...)`, rather than relying on enformer_pytorch's own
    forward() to do it (which it doesn't, due to a no-op `.to()` call
    in that third-party library)."""

    def _make_instance(self, fake_model, device):
        instance = EnformerPlugin.__new__(EnformerPlugin)
        instance.logger = mock.Mock()
        instance.device = device
        instance.model = fake_model
        return instance

    def test_input_tensor_matches_model_device_on_cpu(self):
        fake_model = _FakeEnformerModel(ref_value=0.0, alt_value=1.0, expected_device="cpu")
        instance = self._make_instance(fake_model, device="cpu")
        # No assertion error raised inside the fake model's __call__
        # means the tensor really was on "cpu" as expected.
        result = instance._infer_impl("A" * 10, "T" * 10)
        self.assertEqual(result["classification"], "large_effect")

    def test_diagnostic_logging_reports_model_device_input_device_and_shape(self):
        fake_model = _FakeEnformerModel(ref_value=0.0, alt_value=1.0, expected_device="cpu")
        instance = self._make_instance(fake_model, device="cpu")
        instance._infer_impl("A" * 10, "T" * 10)

        logged_messages = [call_args[0][0] for call_args in instance.logger.debug.call_args_list]
        combined = " ".join(logged_messages)
        self.assertIn("model device", combined)
        self.assertIn("input tensor device", combined)
        self.assertIn("input tensor shape", combined)
        self.assertIn(str(ENFORMER_SEQUENCE_LENGTH), combined)


class TestEnformerLoadImpl(unittest.TestCase):
    def setUp(self):
        self.instance = EnformerPlugin.__new__(EnformerPlugin)
        self.instance.logger = mock.Mock()
        self.instance.device = "cpu"
        self.instance._loaded = False
        self.instance.model = None
        self.instance._weight_cache = mock.Mock()
        self.instance._weight_cache.ensure_dir.return_value = "/tmp/fake_enformer_cache"

    def test_successful_load_calls_to_and_eval(self):
        fake_model = _FakeEnformerModel(ref_value=0.0, alt_value=1.0)
        with mock.patch(
            "pipeline.models.enformer_plugin.ensure_pip_package_available", return_value=True
        ), mock.patch("enformer_pytorch.from_pretrained", return_value=fake_model):
            self.instance._load_impl()

        self.assertIs(self.instance.model, fake_model)
        self.assertEqual(fake_model.to_calls, ["cpu"])
        self.assertTrue(fake_model.eval_called)

    def test_network_failure_is_sanitized(self):
        with mock.patch(
            "pipeline.models.enformer_plugin.ensure_pip_package_available", return_value=True
        ), mock.patch(
            "enformer_pytorch.from_pretrained", side_effect=ConnectionError("could not reach huggingface.co")
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.instance._load_impl()

        self.assertEqual(str(ctx.exception), "Enformer model unavailable")
        self.assertNotIn("huggingface.co", str(ctx.exception))

    def test_missing_package_raises_clear_error(self):
        with mock.patch(
            "pipeline.models.enformer_plugin.ensure_pip_package_available", return_value=False
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.instance._load_impl()
        self.assertIn("enformer-pytorch", str(ctx.exception))

    def test_full_load_via_public_api_wraps_in_model_load_error_on_failure(self):
        # Exercise through PluginModel.load() (not _load_impl directly)
        # to confirm the base class's own wrapping still applies.
        with mock.patch(
            "pipeline.models.enformer_plugin.ensure_pip_package_available", return_value=True
        ), mock.patch(
            "enformer_pytorch.from_pretrained", side_effect=OSError("network unreachable")
        ):
            with self.assertRaises(ModelLoadError):
                self.instance.load()


class TestEnformerMetadataAndAvailability(unittest.TestCase):
    def test_metadata_reports_commercial_use_allowed(self):
        meta = EnformerPlugin.metadata()
        self.assertTrue(meta.commercial_use_allowed)
        self.assertIn("MIT", meta.license_name)
        self.assertIn("Apache-2.0", meta.license_name)

    def test_disabled_by_default(self):
        with mock.patch("pipeline.models.enformer_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_ENFORMER = False
            self.assertFalse(EnformerPlugin.is_available())
            self.assertIn("ENABLE_ENFORMER", EnformerPlugin.unavailability_reason())

    def test_available_when_flag_on_and_package_installed(self):
        with mock.patch("pipeline.models.enformer_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_ENFORMER = True
            self.assertTrue(EnformerPlugin.is_available())  # enformer_pytorch really is installed


class TestEnformerThroughModelManager(unittest.TestCase):
    """Confirms the plugin behaves correctly when driven through
    ModelManager (not called directly) -- lazy loading, caching,
    graceful failure -- using the real manager, a real registry, and
    only the network boundary mocked."""

    def test_predict_returns_none_when_disabled(self):
        registry = ModelRegistry()
        registry.register("enformer", EnformerPlugin)
        manager = ModelManager(registry=registry)
        with mock.patch("pipeline.models.enformer_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_ENFORMER = False
            result = manager.predict("enformer", "A" * 10, "T" * 10)
        self.assertIsNone(result)

    def test_get_raises_plugin_unavailable_when_disabled(self):
        registry = ModelRegistry()
        registry.register("enformer", EnformerPlugin)
        manager = ModelManager(registry=registry)
        with mock.patch("pipeline.models.enformer_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_ENFORMER = False
            with self.assertRaises(PluginUnavailableError):
                manager.get("enformer")


if __name__ == "__main__":
    unittest.main()
