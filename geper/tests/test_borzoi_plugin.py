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
from utils.auto_install import PackageCheckStatus

try:
    import borzoi_pytorch  # noqa: F401

    _HAS_BORZOI_PYTORCH = True
except ImportError:
    _HAS_BORZOI_PYTORCH = False

_BORZOI_SKIP_REASON = (
    "borzoi-pytorch is not installed in this environment -- Borzoi is an "
    "optional, config-gated integration (CONFIG.splicing.ENABLE_BORZOI; see "
    "requirements.txt's own header comment for the rationale), not a broken "
    "environment."
)


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

    @unittest.skipUnless(_HAS_BORZOI_PYTORCH, _BORZOI_SKIP_REASON)
    def test_successful_load_calls_to_and_eval(self):
        fake_model = _FakeBorzoiModel(value=1.0)
        with (
            mock.patch(
                "pipeline.models.borzoi_plugin.check_pip_package_availability", return_value=PackageCheckStatus.PRESENT
            ),
            mock.patch("borzoi_pytorch.Borzoi.from_pretrained", return_value=fake_model),
        ):
            self.instance._load_impl()

        self.assertIs(self.instance.model, fake_model)
        self.assertEqual(fake_model.to_calls, ["cpu"])
        self.assertTrue(fake_model.eval_called)

    @unittest.skipUnless(_HAS_BORZOI_PYTORCH, _BORZOI_SKIP_REASON)
    def test_network_failure_is_sanitized(self):
        with (
            mock.patch(
                "pipeline.models.borzoi_plugin.check_pip_package_availability", return_value=PackageCheckStatus.PRESENT
            ),
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
        with mock.patch(
            "pipeline.models.borzoi_plugin.check_pip_package_availability", return_value=PackageCheckStatus.ABSENT
        ):
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

    @unittest.skipUnless(_HAS_BORZOI_PYTORCH, _BORZOI_SKIP_REASON)
    def test_license_guard_allows_johahi_repo(self):
        fake_model = _FakeBorzoiModel(value=1.0)
        with mock.patch("pipeline.models.borzoi_plugin.CONFIG") as mock_config:
            mock_config.splicing.BORZOI_HF_REPO = "johahi/borzoi-replicate-2"
            with (
                mock.patch(
                    "pipeline.models.borzoi_plugin.check_pip_package_availability",
                    return_value=PackageCheckStatus.PRESENT,
                ),
                mock.patch("borzoi_pytorch.Borzoi.from_pretrained", return_value=fake_model),
            ):
                self.instance._load_impl()
        self.assertIs(self.instance.model, fake_model)

    def test_full_load_via_public_api_wraps_license_guard_in_model_load_error(self):
        with mock.patch("pipeline.models.borzoi_plugin.CONFIG") as mock_config:
            mock_config.splicing.BORZOI_HF_REPO = "some/other-repo"
            with self.assertRaises(ModelLoadError):
                self.instance.load()


@unittest.skipUnless(_HAS_BORZOI_PYTORCH, _BORZOI_SKIP_REASON)
class TestBorzoiAllTiedWeightsKeysShim(unittest.TestCase):
    """
    Regression test for the upstream compatibility bug confirmed live
    in a real Colab GPU run (2026-08-17, transformers>=5 installed):
    `borzoi_pytorch.Borzoi` subclasses `transformers.PreTrainedModel`
    but never calls `self.post_init()`, so
    `PreTrainedModel.from_pretrained`'s `_finalize_model_loading` ->
    `_move_missing_keys_from_meta_to_device` unconditionally reads
    `self.all_tied_weights_keys.keys()`, which Borzoi never has set --
    raising `AttributeError: 'Borzoi' object has no attribute
    'all_tied_weights_keys'` before any real weight loads. Same class
    of bug `pipeline/models/enformer_plugin.py` already shims for
    Enformer (see that file's `_load_impl` for the full upstream
    explanation) -- `BorzoiPlugin._load_impl` now applies the
    identical fix.

    This sandbox's own installed `transformers` predates the version
    that actually enforces this attribute at load time (confirmed
    below), so the original crash cannot be reproduced end-to-end here
    via a real `from_pretrained()` call -- there is also no network
    access to huggingface.co in this environment either way. Instead,
    this proves the shim's mechanics directly against the REAL
    `borzoi_pytorch.Borzoi` class (not a fake/mock class), since that
    real-class behavior is what silently regresses if this code is
    ever "cleaned up" by someone who doesn't know why it's there.
    """

    def setUp(self):
        import borzoi_pytorch

        self.Borzoi = borzoi_pytorch.Borzoi
        # Undo the shim if an earlier test in this process already
        # applied it (BorzoiPlugin._load_impl mutates the class
        # itself, not an instance -- see the shim's own comment for
        # why), so each test here starts from the same "unshimmed"
        # state a fresh process would.
        if "all_tied_weights_keys" in vars(self.Borzoi):
            del self.Borzoi.all_tied_weights_keys

    def tearDown(self):
        # Leave the real, shared borzoi_pytorch.Borzoi class clean for
        # any other test module that imports it in the same process.
        if "all_tied_weights_keys" in vars(self.Borzoi):
            del self.Borzoi.all_tied_weights_keys
        if "_tied_weights_keys" in vars(self.Borzoi) and self.Borzoi._tied_weights_keys == {
            "decoder.weight": "encoder.weight"
        }:
            del self.Borzoi._tied_weights_keys

    def _make_instance(self):
        instance = BorzoiPlugin.__new__(BorzoiPlugin)
        instance.logger = mock.Mock()
        instance.device = "cpu"
        instance._loaded = False
        instance.model = None
        instance._weight_cache = mock.Mock()
        instance._weight_cache.ensure_dir.return_value = "/tmp/fake_borzoi_cache"
        return instance

    def test_real_borzoi_class_lacks_the_attribute_before_the_shim(self):
        # Confirms the bug's precondition against the REAL upstream
        # class, not an assumption: this is exactly what
        # `_finalize_model_loading` reads and what was missing in the
        # live Colab crash.
        self.assertFalse(hasattr(self.Borzoi, "all_tied_weights_keys"))

    def test_load_impl_shim_sets_the_attribute_on_the_real_class(self):
        instance = self._make_instance()
        fake_model = _FakeBorzoiModel(value=1.0)
        with (
            mock.patch(
                "pipeline.models.borzoi_plugin.check_pip_package_availability", return_value=PackageCheckStatus.PRESENT
            ),
            mock.patch("borzoi_pytorch.Borzoi.from_pretrained", return_value=fake_model),
        ):
            instance._load_impl()

        # The exact attribute access that crashed in the live traceback
        # (`self.all_tied_weights_keys.keys()`) now succeeds on the
        # real class.
        self.assertTrue(hasattr(self.Borzoi, "all_tied_weights_keys"))
        self.assertEqual(dict(self.Borzoi.all_tied_weights_keys), {})

    def test_shim_does_not_override_a_real_tied_weights_keys(self):
        # If a future borzoi_pytorch release ever DOES set a real,
        # non-empty _tied_weights_keys, the shim must use that value,
        # not silently clobber it with an empty dict -- exactly what
        # `getattr(Borzoi, "_tied_weights_keys", None) or {}` already
        # guarantees; pinned here so that behavior can't regress.
        self.Borzoi._tied_weights_keys = {"decoder.weight": "encoder.weight"}
        instance = self._make_instance()
        fake_model = _FakeBorzoiModel(value=1.0)
        with (
            mock.patch(
                "pipeline.models.borzoi_plugin.check_pip_package_availability", return_value=PackageCheckStatus.PRESENT
            ),
            mock.patch("borzoi_pytorch.Borzoi.from_pretrained", return_value=fake_model),
        ):
            instance._load_impl()
        self.assertEqual(self.Borzoi.all_tied_weights_keys, {"decoder.weight": "encoder.weight"})

    def test_shim_is_idempotent_across_repeated_loads(self):
        # BorzoiPlugin.__new__ bypasses the singleton ModelCache a real
        # run would use, so this exercises what a second _load_impl()
        # call (e.g. a second BorzoiPlugin instance in the same
        # process) sees: the class attribute already set, and the
        # `if not hasattr(...)` guard leaving it untouched rather than
        # resetting it.
        instance1 = self._make_instance()
        with (
            mock.patch(
                "pipeline.models.borzoi_plugin.check_pip_package_availability", return_value=PackageCheckStatus.PRESENT
            ),
            mock.patch("borzoi_pytorch.Borzoi.from_pretrained", return_value=_FakeBorzoiModel(value=1.0)),
        ):
            instance1._load_impl()
        first_value = self.Borzoi.all_tied_weights_keys

        instance2 = self._make_instance()
        with (
            mock.patch(
                "pipeline.models.borzoi_plugin.check_pip_package_availability", return_value=PackageCheckStatus.PRESENT
            ),
            mock.patch("borzoi_pytorch.Borzoi.from_pretrained", return_value=_FakeBorzoiModel(value=2.0)),
        ):
            instance2._load_impl()

        self.assertIs(self.Borzoi.all_tied_weights_keys, first_value)


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
        # Mocks ensure_pip_package_available rather than relying on the
        # real package genuinely being pip-installed -- same fix and
        # same rationale as the equivalent Enformer test
        # (test_enformer_plugin.py::TestEnformerMetadataAndAvailability
        # ::test_available_when_flag_on_and_package_installed): unmocked,
        # this used to reach a real unconstrained
        # `pip install borzoi-pytorch` that could silently downgrade the
        # box's transformers pin; post-47748b6 that real call now
        # correctly fails the requirements.txt constraint instead, so
        # this test can no longer assume the real package is present
        # just because is_available() is True.
        with (
            mock.patch("pipeline.models.borzoi_plugin.CONFIG") as mock_config,
            mock.patch("pipeline.models.borzoi_plugin.ensure_pip_package_available", return_value=True),
        ):
            mock_config.splicing.ENABLE_BORZOI = True
            self.assertTrue(BorzoiPlugin.is_available())


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
