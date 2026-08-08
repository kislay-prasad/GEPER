"""
Unit tests for pipeline/models/borzoi_plugin.py.

Same mocking philosophy as test_enformer_plugin.py: `borzoi_pytorch`
is genuinely installed, but `Borzoi.from_pretrained` needs
huggingface.co, which this environment can't reach -- so tests mock
exactly that call (or `self.model` directly), leaving GEPER's own
code (one-hot encoding, the license namespace guard, error
sanitization, delta/classification computation) running for real.
"""

import unittest
from unittest import mock

import torch

from pipeline.models.borzoi_plugin import (
    BORZOI_SEQUENCE_LENGTH,
    BorzoiLicenseGuardError,
    BorzoiPlugin,
)
from pipeline.models.manager import ModelManager, PluginUnavailableError
from pipeline.models.registry import ModelRegistry
from utils.exceptions import ModelLoadError


class _FakeBorzoiModel:
    """Mimics borzoi_pytorch's Borzoi.forward interface: takes a
    (N, 4, L) one-hot tensor, returns a (N, C, L) tensor."""

    def __init__(self, value: float, num_tracks: int = 6, out_length: int = 4):
        self.value = value
        self.num_tracks = num_tracks
        self.out_length = out_length
        self.to_calls = []
        self.eval_called = False

    def to(self, device):
        self.to_calls.append(device)
        return self

    def eval(self):
        self.eval_called = True
        return self

    def __call__(self, x, is_human=True):
        assert x.ndim == 3 and x.shape[1] == 4
        batch = x.shape[0]
        return torch.full((batch, self.num_tracks, self.out_length), self.value)


class TestOneHotEncode(unittest.TestCase):
    def test_shape_and_dtype(self):
        encoded = BorzoiPlugin._one_hot_encode("ACGT", target_length=10)
        self.assertEqual(tuple(encoded.shape), (4, 10))
        self.assertEqual(encoded.dtype, torch.float32)

    def test_known_bases_encode_correctly(self):
        encoded = BorzoiPlugin._one_hot_encode("ACGT", target_length=4)
        # A -> row 0, C -> row 1, G -> row 2, T -> row 3, one per position.
        expected = torch.tensor(
            [
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ]
        )
        self.assertTrue(torch.equal(encoded, expected))

    def test_n_and_unknown_bases_are_all_zero_column(self):
        encoded = BorzoiPlugin._one_hot_encode("ANX", target_length=3)
        self.assertTrue(torch.equal(encoded[:, 1], torch.zeros(4)))  # 'N'
        self.assertTrue(torch.equal(encoded[:, 2], torch.zeros(4)))  # 'X' (unknown)

    def test_short_sequence_padded_with_n_on_both_sides(self):
        encoded = BorzoiPlugin._one_hot_encode("AC", target_length=6)
        self.assertTrue(torch.equal(encoded[:, 0], torch.zeros(4)))
        self.assertTrue(torch.equal(encoded[:, 1], torch.zeros(4)))
        self.assertEqual(encoded[0, 2].item(), 1.0)  # A
        self.assertEqual(encoded[1, 3].item(), 1.0)  # C

    def test_long_sequence_truncated_symmetrically(self):
        # length 8 sequence, target 4 -> keep middle 4 (positions 2:6)
        encoded = BorzoiPlugin._one_hot_encode("AACCGGTT", target_length=4)
        self.assertEqual(tuple(encoded.shape), (4, 4))

    def test_default_target_length_matches_official_constant(self):
        encoded = BorzoiPlugin._one_hot_encode("A")
        self.assertEqual(encoded.shape[1], BORZOI_SEQUENCE_LENGTH)
        self.assertEqual(BORZOI_SEQUENCE_LENGTH, 524_288)


class TestBorzoiInference(unittest.TestCase):
    def _make_instance(self, fake_model):
        instance = BorzoiPlugin.__new__(BorzoiPlugin)
        instance.logger = mock.Mock()
        instance.device = "cpu"
        instance.model = fake_model
        return instance

    def test_identical_ref_and_alt_yields_no_significant_effect(self):
        instance = self._make_instance(_FakeBorzoiModel(value=1.0))
        result = instance._infer_impl("A" * 10, "A" * 10)
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["classification"], "no_significant_effect")

    def test_large_delta_yields_large_effect(self):
        instance = self._make_instance(_FakeBorzoiModel(value=0.0))
        # Monkeypatch __call__ per-invocation to differ between ref/alt.
        calls = {"n": 0}
        values = [0.0, 2.0]

        def fake_forward(x, is_human=True):
            v = values[calls["n"]]
            calls["n"] += 1
            return torch.full((x.shape[0], 6, 4), v)

        instance.model = mock.Mock(side_effect=fake_forward)
        result = instance._infer_impl("A" * 10, "T" * 10)
        self.assertEqual(result["score"], 2.0)
        self.assertEqual(result["classification"], "large_effect")

    def test_details_report_calibration_status_honestly(self):
        instance = self._make_instance(_FakeBorzoiModel(value=1.0))
        result = instance._infer_impl("A" * 10, "A" * 10)
        self.assertIn("uncalibrated", result["details"]["calibration_status"])
        self.assertEqual(result["details"]["num_tracks"], 6)

    def test_predict_via_base_class_tags_meta_correctly(self):
        instance = self._make_instance(_FakeBorzoiModel(value=1.0))
        instance._loaded = True
        result = instance.predict("A" * 10, "A" * 10)
        self.assertEqual(result["meta"]["model"], "borzoi")


class TestBorzoiLoadImpl(unittest.TestCase):
    def setUp(self):
        self.instance = BorzoiPlugin.__new__(BorzoiPlugin)
        self.instance.logger = mock.Mock()
        self.instance.device = "cpu"
        self.instance._loaded = False
        self.instance.model = None
        self.instance._weight_cache = mock.Mock()
        self.instance._weight_cache.ensure_dir.return_value = "/tmp/fake_borzoi_cache"

    def test_successful_load_calls_to_and_eval(self):
        fake_model = _FakeBorzoiModel(value=1.0)
        with (
            mock.patch("pipeline.models.borzoi_plugin.ensure_pip_package_available", return_value=True),
            mock.patch("borzoi_pytorch.Borzoi.from_pretrained", return_value=fake_model),
        ):
            self.instance._load_impl()

        self.assertIs(self.instance.model, fake_model)
        self.assertEqual(fake_model.to_calls, ["cpu"])
        self.assertTrue(fake_model.eval_called)

    def test_network_failure_is_sanitized(self):
        with (
            mock.patch("pipeline.models.borzoi_plugin.ensure_pip_package_available", return_value=True),
            mock.patch(
                "borzoi_pytorch.Borzoi.from_pretrained",
                side_effect=ConnectionError("could not reach huggingface.co"),
            ),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.instance._load_impl()
        self.assertEqual(str(ctx.exception), "Borzoi model unavailable")
        self.assertNotIn("huggingface.co", str(ctx.exception))

    def test_missing_package_raises_clear_error(self):
        with mock.patch("pipeline.models.borzoi_plugin.ensure_pip_package_available", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                self.instance._load_impl()
        self.assertIn("borzoi-pytorch", str(ctx.exception))

    def test_license_guard_blocks_non_johahi_repo(self):
        with mock.patch("pipeline.models.borzoi_plugin.CONFIG") as mock_config:
            mock_config.splicing.BORZOI_HF_REPO = "calico/borzoi-original-weights"
            with self.assertRaises(BorzoiLicenseGuardError) as ctx:
                self.instance._load_impl()
        self.assertIn("johahi", str(ctx.exception))
        self.assertIn("calico/borzoi-original-weights", str(ctx.exception))

    def test_license_guard_allows_johahi_repo(self):
        fake_model = _FakeBorzoiModel(value=1.0)
        with mock.patch("pipeline.models.borzoi_plugin.CONFIG") as mock_config:
            mock_config.splicing.BORZOI_HF_REPO = "johahi/borzoi-replicate-2"
            with (
                mock.patch("pipeline.models.borzoi_plugin.ensure_pip_package_available", return_value=True),
                mock.patch("borzoi_pytorch.Borzoi.from_pretrained", return_value=fake_model),
            ):
                self.instance._load_impl()
        self.assertIs(self.instance.model, fake_model)

    def test_full_load_via_public_api_wraps_license_guard_in_model_load_error(self):
        with mock.patch("pipeline.models.borzoi_plugin.CONFIG") as mock_config:
            mock_config.splicing.BORZOI_HF_REPO = "some/other-repo"
            with self.assertRaises(ModelLoadError):
                self.instance.load()


class TestBorzoiMetadataAndAvailability(unittest.TestCase):
    def test_metadata_reports_commercial_use_allowed(self):
        meta = BorzoiPlugin.metadata()
        self.assertTrue(meta.commercial_use_allowed)
        self.assertIn("Apache-2.0", meta.license_name)
        self.assertIn("CC-BY-4.0", meta.license_name)

    def test_metadata_does_not_leak_the_old_incorrect_mit_label(self):
        """Regression: both the code and weights license labels were
        corrected from MIT to Apache-2.0 (code) / CC-BY-4.0 (weights) on
        2026-08-08 (the previous MIT claim, cited to wording in the
        Flashzoi paper, did not match either primary source directly --
        see LICENSE_AUDIT.md). Guards against that incorrect label
        silently creeping back in."""
        meta = BorzoiPlugin.metadata()
        self.assertNotIn("MIT", meta.license_name)

    def test_disabled_by_default(self):
        with mock.patch("pipeline.models.borzoi_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_BORZOI = False
            self.assertFalse(BorzoiPlugin.is_available())
            self.assertIn("ENABLE_BORZOI", BorzoiPlugin.unavailability_reason())

    def test_available_when_flag_on_and_package_installed(self):
        with mock.patch("pipeline.models.borzoi_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_BORZOI = True
            self.assertTrue(BorzoiPlugin.is_available())  # borzoi_pytorch really is installed


class TestBorzoiThroughModelManager(unittest.TestCase):
    def test_predict_returns_none_when_disabled(self):
        registry = ModelRegistry()
        registry.register("borzoi", BorzoiPlugin)
        manager = ModelManager(registry=registry)
        with mock.patch("pipeline.models.borzoi_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_BORZOI = False
            result = manager.predict("borzoi", "A" * 10, "T" * 10)
        self.assertIsNone(result)

    def test_get_raises_plugin_unavailable_when_disabled(self):
        registry = ModelRegistry()
        registry.register("borzoi", BorzoiPlugin)
        manager = ModelManager(registry=registry)
        with mock.patch("pipeline.models.borzoi_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_BORZOI = False
            with self.assertRaises(PluginUnavailableError):
                manager.get("borzoi")


if __name__ == "__main__":
    unittest.main()
