"""
Integration tests for the Enformer + Borzoi plugin integrations,
exercised together through `ModelManager` and
`pipeline.models.pending_plugins.build_default_registry()` -- i.e.
the actual object graph GEPER would use, not each plugin tested in
isolation (see test_enformer_plugin.py / test_borzoi_plugin.py for
that).

Network access to huggingface.co is unavailable in this environment
(confirmed: proxy returns 403), so `from_pretrained` is mocked at the
same boundary as the unit tests; everything else -- registry
wiring, config-driven availability, lazy loading through the manager,
caching, and graceful failure -- runs for real.
"""

import unittest
from unittest import mock

import torch

from pipeline.models.borzoi_plugin import BorzoiPlugin
from pipeline.models.enformer_plugin import EnformerPlugin
from pipeline.models.manager import ModelManager
from pipeline.models.pending_plugins import build_default_registry


class _FakeEnformerModel:
    def __init__(self, ref_value, alt_value):
        self.ref_value, self.alt_value = ref_value, alt_value

    def to(self, device):
        return self

    def eval(self):
        return self

    def __call__(self, seqs, head="human"):
        return torch.stack(
            [torch.full((4, 4), self.ref_value), torch.full((4, 4), self.alt_value)], dim=0
        )


class _FakeBorzoiModel:
    def __init__(self):
        self._n = 0
        self._values = [0.0, 1.0]

    def to(self, device):
        return self

    def eval(self):
        return self

    def __call__(self, x, is_human=True):
        v = self._values[self._n % 2]
        self._n += 1
        return torch.full((x.shape[0], 6, 4), v)


class TestBothPluginsDisabledByDefault(unittest.TestCase):
    def test_enformer_borzoi_dependency_gated(self):
        # Enformer/Borzoi are license-cleared and enabled by default,
        # so their availability tracks their optional pip package
        # rather than being hardcoded here.
        registry = build_default_registry()
        available = set(registry.available_keys())
        self.assertEqual("enformer" in available, EnformerPlugin.is_available())
        self.assertEqual("borzoi" in available, BorzoiPlugin.is_available())

    def test_manager_predict_never_crashes_for_either(self):
        manager = ModelManager(registry=build_default_registry())
        for key in ("enformer", "borzoi"):
            self.assertIsNone(manager.predict(key, "A" * 10, "T" * 10))


class TestEnformerAndBorzoiEnabledTogetherThroughManager(unittest.TestCase):
    """The realistic "both plugins turned on" scenario: verifies they
    don't interfere with each other when managed by the same
    ModelManager instance."""

    def setUp(self):
        self.registry = build_default_registry()
        self.manager = ModelManager(registry=self.registry)

        self.enformer_config_patch = mock.patch("pipeline.models.enformer_plugin.CONFIG")
        self.borzoi_config_patch = mock.patch("pipeline.models.borzoi_plugin.CONFIG")
        # This class is scoped to "just Enformer+Borzoi" (see class
        # docstring), so SpliceFormer/SpliceBERT/SPiP are explicitly
        # forced off here rather than left to whatever this
        # environment's real network/dependency availability happens
        # to be -- all three are enabled-by-default plugins
        # (pipeline/models/spliceformer_plugin.py /
        # splicebert_plugin.py / spip_plugin.py), so without this
        # they'd silently leak into `available_keys()` in any
        # environment where their own dependencies genuinely resolve
        # (e.g. real network access to GitHub/Zenodo, or -- for SPiP --
        # a real R install, unlike this file's own docstring
        # assumption for huggingface.co).
        self.spliceformer_config_patch = mock.patch("pipeline.models.spliceformer_plugin.CONFIG")
        self.splicebert_config_patch = mock.patch("pipeline.models.splicebert_plugin.CONFIG")
        self.spip_config_patch = mock.patch("pipeline.models.spip_plugin.CONFIG")
        mock_enformer_config = self.enformer_config_patch.start()
        mock_borzoi_config = self.borzoi_config_patch.start()
        mock_spliceformer_config = self.spliceformer_config_patch.start()
        mock_splicebert_config = self.splicebert_config_patch.start()
        mock_spip_config = self.spip_config_patch.start()
        mock_enformer_config.splicing.ENABLE_ENFORMER = True
        mock_borzoi_config.splicing.BORZOI_HF_REPO = "johahi/borzoi-replicate-0"
        mock_spliceformer_config.splicing.ENABLE_SPLICEFORMER = False
        mock_splicebert_config.splicing.ENABLE_SPLICEBERT = False
        mock_spip_config.splicing.ENABLE_SPIP = False
        self.addCleanup(self.enformer_config_patch.stop)
        self.addCleanup(self.borzoi_config_patch.stop)
        self.addCleanup(self.spliceformer_config_patch.stop)
        self.addCleanup(self.splicebert_config_patch.stop)
        self.addCleanup(self.spip_config_patch.stop)

    def test_multiple_available_keys_reported_by_registry(self):
        self.assertEqual(sorted(self.registry.available_keys()), ["borzoi", "enformer"])

    def test_both_plugins_load_and_predict_independently(self):
        fake_enformer = _FakeEnformerModel(ref_value=0.0, alt_value=1.0)
        fake_borzoi = _FakeBorzoiModel()

        with mock.patch(
            "pipeline.models.enformer_plugin.ensure_pip_package_available", return_value=True
        ), mock.patch(
            "enformer_pytorch.from_pretrained", return_value=fake_enformer
        ), mock.patch(
            "pipeline.models.borzoi_plugin.ensure_pip_package_available", return_value=True
        ), mock.patch(
            "borzoi_pytorch.Borzoi.from_pretrained", return_value=fake_borzoi
        ):
            enformer_result = self.manager.predict("enformer", "A" * 10, "T" * 10)
            borzoi_result = self.manager.predict("borzoi", "A" * 10, "T" * 10)

        self.assertIsNotNone(enformer_result)
        self.assertIsNotNone(borzoi_result)
        self.assertEqual(enformer_result["meta"]["model"], "enformer")
        self.assertEqual(borzoi_result["meta"]["model"], "borzoi")
        # Loaded independently -- each plugin instance is its own
        # object, isolated by the manager's per-key instance cache.
        self.assertEqual(sorted(self.manager.loaded_keys()), ["borzoi", "enformer"])

    def test_one_plugin_failing_to_load_does_not_affect_the_other(self):
        fake_borzoi = _FakeBorzoiModel()

        with mock.patch(
            "pipeline.models.enformer_plugin.ensure_pip_package_available", return_value=True
        ), mock.patch(
            "enformer_pytorch.from_pretrained", side_effect=ConnectionError("unreachable")
        ), mock.patch(
            "pipeline.models.borzoi_plugin.ensure_pip_package_available", return_value=True
        ), mock.patch(
            "borzoi_pytorch.Borzoi.from_pretrained", return_value=fake_borzoi
        ):
            enformer_result = self.manager.predict("enformer", "A" * 10, "T" * 10)
            borzoi_result = self.manager.predict("borzoi", "A" * 10, "T" * 10)

        self.assertIsNone(enformer_result)  # gracefully failed
        self.assertIsNotNone(borzoi_result)  # unaffected
        self.assertIn("enformer", self.manager.failed_keys())
        self.assertNotIn("borzoi", self.manager.failed_keys())


class TestBackwardsCompatibility(unittest.TestCase):
    """Confirms adding these plugins didn't change any existing
    framework behavior for plugins that were already covered by
    test_model_manager.py's mocked fakes."""

    def test_registry_still_rejects_non_plugin_classes(self):
        from pipeline.models.registry import ModelRegistry

        registry = ModelRegistry()
        with self.assertRaises(TypeError):
            registry.register("bad", object)

    def test_existing_pending_plugins_import_path_still_works(self):
        # EnformerPlugin, BorzoiPlugin, and build_default_registry must
        # all still be importable from pending_plugins (re-exported),
        # even though Enformer/Borzoi's real implementations now live
        # in their own modules.
        from pipeline.models.pending_plugins import (
            BorzoiPlugin as ReexportedBorzoi,
            EnformerPlugin as ReexportedEnformer,
        )

        self.assertIs(ReexportedEnformer, EnformerPlugin)
        self.assertIs(ReexportedBorzoi, BorzoiPlugin)


if __name__ == "__main__":
    unittest.main()
