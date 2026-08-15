"""
Unit tests for the new AI model plugin framework
(`pipeline/models/{base,cache,registry,manager}.py`) -- Part 2 of the
project spec.

Every plugin used here is a small mocked/fake `PluginModel` defined
locally in this file, not a real Enformer/Borzoi integration --
these tests exercise the manager's own mechanics
(lazy loading, instance caching, failure isolation, availability
gating, version tracking), independent of any real model weights or
network access.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline.models.base import ModelMetadata, PluginModel
from pipeline.models.cache import WeightCache
from pipeline.models.manager import ModelManager, PluginUnavailableError
from pipeline.models.registry import ModelRegistry


# ---------------------------------------------------------------------
# Fake plugins used only by these tests.
# ---------------------------------------------------------------------
class _AlwaysAvailableFakePlugin(PluginModel):
    """Loads and predicts successfully; tracks call counts so tests can
    assert `_load_impl` runs at most once per process (the manager's
    caching guarantee)."""

    load_calls = 0
    infer_calls = 0

    @classmethod
    def metadata(cls) -> ModelMetadata:
        return ModelMetadata(
            name="fake_available",
            version="1.0.0-test",
            source="local test double",
            license_name="MIT",
            license_url="https://example.invalid/license",
            commercial_use_allowed=True,
        )

    @classmethod
    def is_available(cls) -> bool:
        return True

    def _load_impl(self) -> None:
        type(self).load_calls += 1
        self.model = object()

    def _infer_impl(self, ref_seq, alt_seq, **kwargs):
        type(self).infer_calls += 1
        return {"score": 0.42, "classification": "test", "confidence": 0.9}


class _UnavailableFakePlugin(PluginModel):
    """Never available -- e.g. representing Enformer/Borzoi before
    license verification, but as a fully generic case."""

    @classmethod
    def metadata(cls) -> ModelMetadata:
        return ModelMetadata(
            name="fake_unavailable",
            version="0.0.0",
            source="local test double",
            license_name="unverified",
            license_url="https://example.invalid/license",
            commercial_use_allowed=None,
        )

    @classmethod
    def is_available(cls) -> bool:
        return False

    @classmethod
    def unavailability_reason(cls) -> str:
        return "license unverified (test)"

    def _load_impl(self) -> None:
        raise AssertionError("_load_impl must never run for an unavailable plugin")

    def _infer_impl(self, *args, **kwargs):
        raise AssertionError("_infer_impl must never run for an unavailable plugin")


class _FailsToLoadFakePlugin(PluginModel):
    """Available, but always throws during load -- e.g. representing a
    real download failure."""

    @classmethod
    def metadata(cls) -> ModelMetadata:
        return ModelMetadata(
            name="fake_broken",
            version="1.0.0-test",
            source="local test double",
            license_name="MIT",
            license_url="https://example.invalid/license",
            commercial_use_allowed=True,
        )

    @classmethod
    def is_available(cls) -> bool:
        return True

    def _load_impl(self) -> None:
        raise RuntimeError("simulated download failure")

    def _infer_impl(self, *args, **kwargs):
        return {"score": 0.0}


class _FailsToInferFakePlugin(PluginModel):
    """Loads fine, but always throws during inference."""

    @classmethod
    def metadata(cls) -> ModelMetadata:
        return ModelMetadata(
            name="fake_flaky",
            version="1.0.0-test",
            source="local test double",
            license_name="MIT",
            license_url="https://example.invalid/license",
            commercial_use_allowed=True,
        )

    @classmethod
    def is_available(cls) -> bool:
        return True

    def _load_impl(self) -> None:
        self.model = object()

    def _infer_impl(self, *args, **kwargs):
        raise RuntimeError("simulated inference crash")


class TestModelRegistry(unittest.TestCase):
    def test_register_and_get(self):
        registry = ModelRegistry()
        registry.register("fake", _AlwaysAvailableFakePlugin)
        self.assertIs(registry.get("fake"), _AlwaysAvailableFakePlugin)

    def test_rejects_non_plugin_classes(self):
        registry = ModelRegistry()
        with self.assertRaises(TypeError):
            registry.register("not_a_plugin", object)

    def test_unknown_key_raises_keyerror_with_registered_keys_listed(self):
        registry = ModelRegistry()
        registry.register("fake", _AlwaysAvailableFakePlugin)
        with self.assertRaises(KeyError):
            registry.get("does_not_exist")

    def test_available_keys_filters_by_is_available(self):
        registry = ModelRegistry()
        registry.register("available", _AlwaysAvailableFakePlugin)
        registry.register("unavailable", _UnavailableFakePlugin)
        self.assertEqual(registry.available_keys(), ["available"])

    def test_available_keys_empty_when_nothing_available(self):
        registry = ModelRegistry()
        registry.register("unavailable_a", _UnavailableFakePlugin)
        self.assertEqual(registry.available_keys(), [])

    def test_available_keys_with_multiple_available_plugins(self):
        class _SecondAvailableFakePlugin(_AlwaysAvailableFakePlugin):
            @classmethod
            def metadata(cls) -> ModelMetadata:
                meta = super().metadata()
                return ModelMetadata(
                    name="fake_available_2",
                    version=meta.version,
                    source=meta.source,
                    license_name=meta.license_name,
                    license_url=meta.license_url,
                    commercial_use_allowed=meta.commercial_use_allowed,
                )

        registry = ModelRegistry()
        registry.register("available_a", _AlwaysAvailableFakePlugin)
        registry.register("available_b", _SecondAvailableFakePlugin)
        registry.register("unavailable", _UnavailableFakePlugin)
        self.assertEqual(registry.available_keys(), ["available_a", "available_b"])

    def test_all_metadata_returns_every_registered_key(self):
        registry = ModelRegistry()
        registry.register("available", _AlwaysAvailableFakePlugin)
        registry.register("unavailable", _UnavailableFakePlugin)
        meta = registry.all_metadata()
        self.assertEqual(set(meta), {"available", "unavailable"})
        self.assertIsNone(meta["unavailable"].commercial_use_allowed)
        self.assertTrue(meta["available"].commercial_use_allowed)


class TestWeightCache(unittest.TestCase):
    def test_path_for_and_exists(self):
        with tempfile.TemporaryDirectory() as d:
            cache = WeightCache(cache_dir=d)
            self.assertFalse(cache.exists("plugin_a", "weights.bin"))
            cache.ensure_dir("plugin_a")
            (Path(d) / "plugin_a" / "weights.bin").write_bytes(b"hello")
            self.assertTrue(cache.exists("plugin_a", "weights.bin"))

    def test_checksum_none_expected_always_passes(self):
        with tempfile.TemporaryDirectory() as d:
            cache = WeightCache(cache_dir=d)
            path = Path(d) / "f.bin"
            path.write_bytes(b"data")
            self.assertTrue(cache.verify_checksum(path, expected_sha256=None))

    def test_checksum_match_and_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            cache = WeightCache(cache_dir=d)
            path = Path(d) / "f.bin"
            path.write_bytes(b"hello world")
            correct = cache.sha256_of(path)
            self.assertTrue(cache.verify_checksum(path, correct))
            self.assertFalse(cache.verify_checksum(path, "0" * 64))

    def test_verify_checksum_missing_file_is_false(self):
        with tempfile.TemporaryDirectory() as d:
            cache = WeightCache(cache_dir=d)
            missing = Path(d) / "nope.bin"
            self.assertFalse(cache.verify_checksum(missing, "0" * 64))

    def test_clear_removes_plugin_directory(self):
        with tempfile.TemporaryDirectory() as d:
            cache = WeightCache(cache_dir=d)
            cache.ensure_dir("plugin_a")
            (Path(d) / "plugin_a" / "weights.bin").write_bytes(b"x")
            cache.clear("plugin_a")
            self.assertFalse((Path(d) / "plugin_a").exists())

    def test_clear_on_nonexistent_plugin_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            cache = WeightCache(cache_dir=d)
            cache.clear("never_existed")  # must not raise


class TestModelManager(unittest.TestCase):
    def setUp(self):
        self.registry = ModelRegistry()
        self.registry.register("available", _AlwaysAvailableFakePlugin)
        self.registry.register("unavailable", _UnavailableFakePlugin)
        self.registry.register("broken_load", _FailsToLoadFakePlugin)
        self.registry.register("flaky_infer", _FailsToInferFakePlugin)
        self.manager = ModelManager(registry=self.registry)
        _AlwaysAvailableFakePlugin.load_calls = 0
        _AlwaysAvailableFakePlugin.infer_calls = 0

    def test_get_loads_and_caches_instance(self):
        first = self.manager.get("available")
        second = self.manager.get("available")
        self.assertIs(first, second)
        self.assertEqual(_AlwaysAvailableFakePlugin.load_calls, 1)

    def test_predict_runs_inference_and_tags_meta(self):
        result = self.manager.predict("available", "ACGT", "ACGA")
        self.assertIsNotNone(result)
        self.assertEqual(result["score"], 0.42)
        self.assertEqual(result["meta"]["model"], "fake_available")
        self.assertEqual(result["meta"]["version"], "1.0.0-test")

    def test_predict_reuses_loaded_instance_across_calls(self):
        self.manager.predict("available", "ACGT", "ACGA")
        self.manager.predict("available", "GGGG", "GGGA")
        self.assertEqual(_AlwaysAvailableFakePlugin.load_calls, 1)
        self.assertEqual(_AlwaysAvailableFakePlugin.infer_calls, 2)

    def test_manager_handles_multiple_available_plugins_independently(self):
        class _SecondAvailableFakePlugin(PluginModel):
            load_calls = 0

            @classmethod
            def metadata(cls) -> ModelMetadata:
                return ModelMetadata(
                    name="fake_available_2",
                    version="2.0.0-test",
                    source="local test double",
                    license_name="MIT",
                    license_url="https://example.invalid/license",
                    commercial_use_allowed=True,
                )

            @classmethod
            def is_available(cls) -> bool:
                return True

            def _load_impl(self) -> None:
                type(self).load_calls += 1
                self.model = object()

            def _infer_impl(self, ref_seq, alt_seq, **kwargs):
                return {"score": 0.77, "classification": "test2"}

        self.registry.register("available_2", _SecondAvailableFakePlugin)

        result_a = self.manager.predict("available", "ACGT", "ACGA")
        result_b = self.manager.predict("available_2", "ACGT", "ACGA")

        self.assertEqual(result_a["score"], 0.42)
        self.assertEqual(result_b["score"], 0.77)
        self.assertEqual(sorted(self.manager.loaded_keys()), ["available", "available_2"])
        # Each plugin's own load only ran once, independent of the other.
        self.assertEqual(_AlwaysAvailableFakePlugin.load_calls, 1)
        self.assertEqual(_SecondAvailableFakePlugin.load_calls, 1)

    def test_registry_discovery_through_manager(self):
        # The manager exposes registry discovery without callers
        # needing to reach into `manager.registry` directly.
        self.assertEqual(
            sorted(self.manager.registry.keys()),
            ["available", "broken_load", "flaky_infer", "unavailable"],
        )
        # "available" (is_available()==True) here means broken_load
        # and flaky_infer too -- they are legitimately available, and
        # only fail later, at load-time / inference-time respectively.
        # That's the whole point of testing them separately from
        # "unavailable" (is_available()==False, gated before any
        # attempt is made at all).
        self.assertEqual(
            sorted(self.manager.registry.available_keys()),
            ["available", "broken_load", "flaky_infer"],
        )
        self.assertNotIn("unavailable", self.manager.registry.available_keys())

        with self.assertRaises(PluginUnavailableError) as ctx:
            self.manager.get("unavailable")
        self.assertIn("license unverified (test)", str(ctx.exception))

    def test_predict_unavailable_plugin_returns_none_never_raises(self):
        result = self.manager.predict("unavailable", "ACGT", "ACGA")
        self.assertIsNone(result)

    def test_get_on_load_failure_raises_plugin_unavailable_error(self):
        with self.assertRaises(PluginUnavailableError):
            self.manager.get("broken_load")

    def test_load_failure_is_remembered_not_retried(self):
        with self.assertRaises(PluginUnavailableError):
            self.manager.get("broken_load")
        # Second attempt must return the *same* remembered failure
        # without invoking the plugin class's _load_impl again.
        with mock.patch.object(_FailsToLoadFakePlugin, "_load_impl", side_effect=AssertionError("must not retry")):
            with self.assertRaises(PluginUnavailableError):
                self.manager.get("broken_load")

    def test_predict_on_load_failure_returns_none(self):
        result = self.manager.predict("broken_load", "ACGT", "ACGA")
        self.assertIsNone(result)

    def test_predict_on_inference_failure_returns_none(self):
        result = self.manager.predict("flaky_infer", "ACGT", "ACGA")
        self.assertIsNone(result)

    def test_reset_clears_instances_and_failures(self):
        self.manager.get("available")
        with self.assertRaises(PluginUnavailableError):
            self.manager.get("broken_load")
        self.assertEqual(self.manager.loaded_keys(), ["available"])
        self.assertIn("broken_load", self.manager.failed_keys())

        self.manager.reset()
        self.assertEqual(self.manager.loaded_keys(), [])
        self.assertEqual(self.manager.failed_keys(), {})

    def test_version_info_covers_every_registered_plugin_regardless_of_availability(self):
        versions = self.manager.version_info()
        self.assertEqual(
            set(versions),
            {"available", "unavailable", "broken_load", "flaky_infer"},
        )

    def test_is_available_delegates_to_registry(self):
        self.assertTrue(self.manager.is_available("available"))
        self.assertFalse(self.manager.is_available("unavailable"))

    def test_device_returns_a_torch_device(self):
        import torch

        self.assertIsInstance(self.manager.device(), torch.device)

    def test_manager_exposes_working_weight_cache(self):
        with tempfile.TemporaryDirectory() as d:
            manager = ModelManager(registry=self.registry, weight_cache=WeightCache(cache_dir=d))
            self.assertFalse(manager.weight_cache.exists("available", "weights.bin"))
            manager.weight_cache.ensure_dir("available")
            (Path(d) / "available" / "weights.bin").write_bytes(b"cached")
            self.assertTrue(manager.weight_cache.exists("available", "weights.bin"))

    def test_manager_defaults_to_its_own_weight_cache_when_none_given(self):
        manager = ModelManager(registry=self.registry)
        self.assertIsInstance(manager.weight_cache, WeightCache)


class TestPendingPluginsRegisterAsUnavailable(unittest.TestCase):
    """Confirms Enformer/Borzoi/SpliceFormer participate in the exact
    same framework the mocked tests above exercise. All three are
    license-cleared (see pipeline/models/pending_plugins.py's
    docstring) and so default to enabled, becoming available
    automatically whenever their optional pip package/dependency is
    installed or successfully auto-installed."""

    def test_default_registry_has_all_five_new_models(self):
        # Was "all four", then "all five" (SPiP's own addition --
        # pipeline/models/spip_plugin.py -- registered a fifth key),
        # then back to "all five" after OpenSpliceAI's never-integrated
        # placeholder was removed entirely.
        from pipeline.models.pending_plugins import build_default_registry

        registry = build_default_registry()
        self.assertEqual(
            set(registry.keys()),
            {"enformer", "borzoi", "spliceformer", "splicebert", "spip"},
        )

    def test_enformer_borzoi_spliceformer_are_dependency_gated(self):
        from pipeline.models.pending_plugins import (
            BorzoiPlugin,
            EnformerPlugin,
            SpliceFormerPlugin,
            build_default_registry,
        )

        registry = build_default_registry()
        available = set(registry.available_keys())
        # Enformer/Borzoi/SpliceFormer are available if and only if
        # their package/dependency is importable (or auto-installable)
        # -- never hardcoded either way here, since that depends on
        # the environment.
        self.assertEqual("enformer" in available, EnformerPlugin.is_available())
        self.assertEqual("borzoi" in available, BorzoiPlugin.is_available())
        self.assertEqual("spliceformer" in available, SpliceFormerPlugin.is_available())

    def test_manager_predict_never_crashes_for_any_pending_plugin(self):
        # Round 21: this used to assert `predict()` returns None for
        # each key, which was really asserting "none of these optional
        # packages happen to be pip-installed in whatever environment
        # runs this test" -- true by accident, not by design (this
        # class's own docstring says the opposite: these plugins
        # "become available automatically whenever their optional pip
        # package/dependency is installed"). It broke the moment
        # enformer-pytorch was actually installed and Enformer loaded
        # for real -- ~400s of genuine weight loading + inference on an
        # 8GB machine, exactly what this module's own docstring says
        # every test here must stay independent of.
        #
        # What the test's own name actually promises -- predict() never
        # *crashes*, for any pending plugin, regardless of what's
        # installed -- doesn't need real installs either way: `.get()`
        # short-circuits to `PluginUnavailableError` (which `.predict()`
        # catches and turns into a plain `None`, per its own docstring)
        # the moment `model_cls.is_available()` says no, before any
        # package import or weight load is attempted. Forcing that
        # False deterministically tests the actual contract -- "never
        # crashes when unavailable" -- without depending on, or being
        # broken by, this environment's own installed packages.
        from pipeline.models.borzoi_plugin import BorzoiPlugin
        from pipeline.models.enformer_plugin import EnformerPlugin
        from pipeline.models.pending_plugins import build_default_registry
        from pipeline.models.spliceformer_plugin import SpliceFormerPlugin

        manager = ModelManager(registry=build_default_registry())
        with (
            mock.patch.object(EnformerPlugin, "is_available", return_value=False),
            mock.patch.object(BorzoiPlugin, "is_available", return_value=False),
            mock.patch.object(SpliceFormerPlugin, "is_available", return_value=False),
        ):
            for key in ("enformer", "borzoi", "spliceformer"):
                self.assertIsNone(manager.predict(key, "ACGT...ref", "ACGT...alt"))

    def test_metadata_never_asserts_unverified_license_as_true(self):
        # Updated for the completed license audit: Enformer and Borzoi
        # are now genuinely verified as commercially usable (see
        # pipeline/models/enformer_plugin.py and borzoi_plugin.py's
        # docstrings for the sourcing), so commercial_use_allowed=True
        # is correct for them, not a guess.
        from pipeline.models.pending_plugins import build_default_registry

        registry = build_default_registry()
        meta = registry.all_metadata()
        self.assertTrue(meta["enformer"].commercial_use_allowed)
        self.assertTrue(meta["borzoi"].commercial_use_allowed)
        self.assertTrue(meta["spliceformer"].commercial_use_allowed)

    def test_disabling_config_flag_forces_plugin_unavailable_regardless_of_dependency(self):
        # Enformer/Borzoi are enabled by default (license-cleared), but
        # the flag must still work as an explicit *opt-out* -- setting
        # it False must force the plugin unavailable even if its pip
        # package is already installed.
        from pipeline.models.pending_plugins import EnformerPlugin

        with mock.patch("pipeline.models.enformer_plugin.CONFIG") as mock_config:
            mock_config.splicing.ENABLE_ENFORMER = False
            self.assertFalse(EnformerPlugin.is_available())


if __name__ == "__main__":
    unittest.main()
