"""
Integration tests for the SpliceFormer plugin, exercised through
`ModelManager` and `pipeline.models.pending_plugins.build_default_registry()`
-- i.e. the actual object graph GEPER would use -- alongside Enformer
and Borzoi, confirming the three real plugins coexist without
interfering with each other and that SpliceFormer's registration
doesn't change anything about the other two. Mirrors
tests/test_new_plugins_integration.py's own structure; kept in a
separate file rather than editing that one, to keep this addition
isolated from existing, already-passing coverage.

Network access to raw.githubusercontent.com is not guaranteed in
every environment this suite runs in, so checkpoint download/model
construction is mocked at the same boundary
tests/test_spliceformer_plugin.py uses; everything else -- registry
wiring, config-driven availability, lazy loading through the manager,
per-plugin instance caching, and graceful failure -- runs for real.
"""

import unittest
from unittest import mock

import torch

from pipeline.models.borzoi_plugin import BorzoiPlugin
from pipeline.models.enformer_plugin import EnformerPlugin
from pipeline.models.manager import ModelManager
from pipeline.models.pending_plugins import (
    SpliceFormerPlugin,
    build_default_registry,
)
from utils.auto_install import PackageCheckStatus

try:
    import enformer_pytorch  # noqa: F401

    _HAS_ENFORMER_PYTORCH = True
except ImportError:
    _HAS_ENFORMER_PYTORCH = False

try:
    import borzoi_pytorch  # noqa: F401

    _HAS_BORZOI_PYTORCH = True
except ImportError:
    _HAS_BORZOI_PYTORCH = False

_ENFORMER_SKIP_REASON = (
    "enformer-pytorch is not installed in this environment -- Enformer is an "
    "optional, config-gated integration (CONFIG.splicing.ENABLE_ENFORMER; see "
    "requirements.txt's own header comment for the rationale), not a broken "
    "environment."
)
_ENFORMER_AND_BORZOI_SKIP_REASON = (
    "enformer-pytorch and/or borzoi-pytorch is not installed in this "
    "environment -- both are optional, config-gated integrations "
    "(CONFIG.splicing.ENABLE_ENFORMER / ENABLE_BORZOI; see requirements.txt's "
    "own header comment for the rationale), not a broken environment."
)


class _FakeSpliceFormerModel:
    def __init__(self, ref_value, alt_value, scored_len=4):
        self.ref_value, self.alt_value, self.scored_len = ref_value, alt_value, scored_len

    def to(self, device):
        return self

    def eval(self):
        return self

    def __call__(self, features):
        ref_out = torch.full((3, self.scored_len), self.ref_value)
        alt_out = torch.full((3, self.scored_len), self.alt_value)
        return torch.stack([ref_out, alt_out], dim=0), None, None, None, None


class TestSpliceFormerRegisteredAlongsideEnformerAndBorzoi(unittest.TestCase):
    """
    Note: `ensure_pip_package_available` is mocked to False for
    Enformer/Borzoi in every test below that doesn't specifically
    exercise their own success path. Without this, `is_available()`
    falls through to a *real* `pip install enformer-pytorch`/
    `pip install borzoi-pytorch` subprocess call in any environment
    where those packages genuinely aren't installed -- slow/network-
    dependent and irrelevant to what these particular tests check
    (SpliceFormer's coexistence with the registry, not Enformer/
    Borzoi's own install path, which test_enformer_plugin.py /
    test_borzoi_plugin.py already cover).
    """

    def test_all_five_keys_registered(self):
        # Was "all four", then "all five" (SPiP's own addition --
        # pipeline/models/spip_plugin.py -- registered a fifth key),
        # then back to "all five" after OpenSpliceAI's never-integrated
        # placeholder was removed entirely -- updated here rather than
        # left stale, same as this test itself updated when
        # SpliceFormer/SpliceBERT were added.
        registry = build_default_registry()
        self.assertEqual(
            sorted(registry.keys()),
            ["borzoi", "enformer", "spip", "splicebert", "spliceformer"],
        )

    def test_manager_predict_never_crashes_for_any_of_the_three(self):
        # Was `assertIsNone(...)` for all three, with Enformer/Borzoi's
        # own `ensure_pip_package_available` mocked False but
        # SpliceFormer's own gate (its `einops` check,
        # pipeline/models/spliceformer_plugin.py:158-166) left
        # unmocked -- so the assertion held only where einops happened
        # to be absent from this box. That is a broken/absent-package
        # proxy pinned as if it were the real contract: the moment
        # einops is installed, SpliceFormer starts working and this
        # test starts failing, for succeeding rather than breaking.
        # Same class of bug as 89dc9c8/test_new_plugins_integration.py
        # ::test_manager_predict_never_crashes_for_either -- the actual
        # contract (this test's own name) is just "never crashes":
        # predict() must return either None (genuinely unavailable) or
        # a real prediction dict (genuinely available), never raise,
        # regardless of which packages happen to be installed on this
        # box. Deliberately does NOT mock einops/ensure_pip_package_
        # available into a fixed state for any of the three -- doing
        # so would just pin a *different* mocked machine state instead
        # of removing the dependency on one.
        manager = ModelManager(registry=build_default_registry())
        for key in ("enformer", "borzoi", "spliceformer"):
            result = manager.predict(key, "A" * 10, "T" * 10)
            self.assertTrue(
                result is None or isinstance(result, dict),
                f"{key}: predict() returned {result!r}, expected None or dict",
            )

    def test_spliceformer_metadata_reachable_regardless_of_availability(self):
        registry = build_default_registry()
        meta = registry.all_metadata()["spliceformer"]
        self.assertTrue(meta.commercial_use_allowed)
        self.assertEqual(meta.license_name, "MIT")


class TestSpliceFormerEnabledAlongsideEnformerAndBorzoiThroughManager(unittest.TestCase):
    """The realistic "all three real plugins turned on" scenario:
    verifies SpliceFormer doesn't interfere with Enformer/Borzoi (or
    vice versa) when managed by the same ModelManager instance, and
    that each plugin instance is independently cached."""

    def setUp(self):
        self.registry = build_default_registry()
        self.manager = ModelManager(registry=self.registry)

        self.enformer_config_patch = mock.patch("pipeline.models.enformer_plugin.CONFIG")
        self.borzoi_config_patch = mock.patch("pipeline.models.borzoi_plugin.CONFIG")
        self.spliceformer_config_patch = mock.patch("pipeline.models.spliceformer_plugin.CONFIG")
        self.splicebert_config_patch = mock.patch("pipeline.models.splicebert_plugin.CONFIG")
        # SPiP is explicitly kept out of this class's scope (its own
        # module docstring/tests cover it) -- forced off here rather
        # than left to this environment's real Rscript availability,
        # same rationale as test_new_plugins_integration.py's own
        # TestEnformerAndBorzoiEnabledTogetherThroughManager.setUp.
        self.spip_config_patch = mock.patch("pipeline.models.spip_plugin.CONFIG")
        mock_enformer_config = self.enformer_config_patch.start()
        mock_borzoi_config = self.borzoi_config_patch.start()
        mock_spliceformer_config = self.spliceformer_config_patch.start()
        mock_splicebert_config = self.splicebert_config_patch.start()
        mock_spip_config = self.spip_config_patch.start()
        mock_enformer_config.splicing.ENABLE_ENFORMER = True
        mock_borzoi_config.splicing.BORZOI_HF_REPO = "johahi/borzoi-replicate-0"
        mock_spliceformer_config.splicing.ENABLE_SPLICEFORMER = True
        mock_spliceformer_config.splicing.SPLICEFORMER_SOURCE_REF = "v1.0.0"
        mock_spliceformer_config.splicing.SPLICEFORMER_CHECKPOINT = "transformer_encoder_40k_171022_0"
        mock_splicebert_config.splicing.ENABLE_SPLICEBERT = True
        mock_splicebert_config.splicing.SPLICEBERT_ZENODO_RECORD = "7995778"
        mock_splicebert_config.splicing.SPLICEBERT_CHECKPOINT = "SpliceBERT.1024nt"
        mock_spip_config.splicing.ENABLE_SPIP = False
        self.addCleanup(self.enformer_config_patch.stop)
        self.addCleanup(self.borzoi_config_patch.stop)
        self.addCleanup(self.spliceformer_config_patch.stop)
        self.addCleanup(self.splicebert_config_patch.stop)
        self.addCleanup(self.spip_config_patch.stop)

    def test_four_available_keys_reported_by_registry(self):
        # Was "three" before SpliceBERT's own addition; see
        # test_all_five_keys_registered's own comment above.
        with (
            mock.patch("pipeline.models.spliceformer_plugin.ensure_pip_package_available", return_value=True),
            mock.patch("pipeline.models.splicebert_plugin.is_pip_package_installed", return_value=True),
            mock.patch("pipeline.models.enformer_plugin.ensure_pip_package_available", return_value=True),
            mock.patch("pipeline.models.borzoi_plugin.ensure_pip_package_available", return_value=True),
        ):
            self.assertEqual(
                sorted(self.registry.available_keys()),
                ["borzoi", "enformer", "splicebert", "spliceformer"],
            )

    @unittest.skipUnless(_HAS_ENFORMER_PYTORCH and _HAS_BORZOI_PYTORCH, _ENFORMER_AND_BORZOI_SKIP_REASON)
    def test_all_three_plugins_load_and_predict_independently(self):
        fake_enformer = mock.Mock()
        fake_enformer.to.return_value = fake_enformer
        fake_enformer.eval.return_value = fake_enformer
        fake_enformer.side_effect = None

        class _FakeEnformerModel:
            def to(self, device):
                return self

            def eval(self):
                return self

            def __call__(self, seqs, head="human"):
                return torch.stack([torch.full((4, 4), 0.0), torch.full((4, 4), 1.0)], dim=0)

        class _FakeBorzoiModel:
            def to(self, device):
                return self

            def eval(self):
                return self

            def __call__(self, x, is_human=True):
                return torch.full((x.shape[0], 6, 4), 0.5)

        fake_spliceformer = _FakeSpliceFormerModel(ref_value=0.0, alt_value=0.6)

        with (
            mock.patch("pipeline.models.enformer_plugin.ensure_pip_package_available", return_value=True),
            mock.patch(
                "pipeline.models.enformer_plugin.check_pip_package_availability",
                return_value=PackageCheckStatus.PRESENT,
            ),
            mock.patch("enformer_pytorch.from_pretrained", return_value=_FakeEnformerModel()),
            mock.patch("pipeline.models.borzoi_plugin.ensure_pip_package_available", return_value=True),
            mock.patch(
                "pipeline.models.borzoi_plugin.check_pip_package_availability", return_value=PackageCheckStatus.PRESENT
            ),
            mock.patch("borzoi_pytorch.Borzoi.from_pretrained", return_value=_FakeBorzoiModel()),
            mock.patch("pipeline.models.spliceformer_plugin.ensure_pip_package_available", return_value=True),
            mock.patch(
                "pipeline.models.spliceformer_plugin.check_pip_package_availability",
                return_value=PackageCheckStatus.PRESENT,
            ),
            mock.patch(
                "pipeline.models.spliceformer_plugin.spliceformer_loader.build_model",
                return_value=fake_spliceformer,
            ),
            mock.patch("pipeline.models.spliceformer_plugin.spliceformer_loader.download_checkpoint"),
            mock.patch("pipeline.models.spliceformer_plugin.spliceformer_loader.load_checkpoint_into"),
        ):
            enformer_result = self.manager.predict("enformer", "A" * 10, "T" * 10)
            borzoi_result = self.manager.predict("borzoi", "A" * 10, "T" * 10)
            spliceformer_result = self.manager.predict("spliceformer", "A" * 10, "T" * 10)

        self.assertIsNotNone(enformer_result)
        self.assertIsNotNone(borzoi_result)
        self.assertIsNotNone(spliceformer_result)
        self.assertEqual(spliceformer_result["meta"]["model"], "spliceformer")
        self.assertEqual(sorted(self.manager.loaded_keys()), ["borzoi", "enformer", "spliceformer"])

    @unittest.skipUnless(_HAS_ENFORMER_PYTORCH, _ENFORMER_SKIP_REASON)
    def test_spliceformer_failing_to_load_does_not_affect_enformer_or_borzoi(self):
        class _FakeEnformerModel:
            def to(self, device):
                return self

            def eval(self):
                return self

            def __call__(self, seqs, head="human"):
                return torch.stack([torch.full((4, 4), 0.0), torch.full((4, 4), 1.0)], dim=0)

        with (
            mock.patch("pipeline.models.enformer_plugin.ensure_pip_package_available", return_value=True),
            mock.patch(
                "pipeline.models.enformer_plugin.check_pip_package_availability",
                return_value=PackageCheckStatus.PRESENT,
            ),
            mock.patch("enformer_pytorch.from_pretrained", return_value=_FakeEnformerModel()),
            mock.patch("pipeline.models.spliceformer_plugin.ensure_pip_package_available", return_value=True),
            mock.patch(
                "pipeline.models.spliceformer_plugin.check_pip_package_availability",
                return_value=PackageCheckStatus.PRESENT,
            ),
            mock.patch(
                "pipeline.models.spliceformer_plugin.spliceformer_loader.build_model",
                return_value=_FakeSpliceFormerModel(0.0, 0.6),
            ),
            mock.patch(
                "pipeline.models.spliceformer_plugin.spliceformer_loader.download_checkpoint",
                side_effect=ConnectionError("unreachable"),
            ),
        ):
            enformer_result = self.manager.predict("enformer", "A" * 10, "T" * 10)
            spliceformer_result = self.manager.predict("spliceformer", "A" * 10, "T" * 10)

        self.assertIsNotNone(enformer_result)  # unaffected
        self.assertIsNone(spliceformer_result)  # gracefully failed
        self.assertIn("spliceformer", self.manager.failed_keys())
        self.assertNotIn("enformer", self.manager.failed_keys())


class TestBackwardsCompatibility(unittest.TestCase):
    """Confirms adding SpliceFormer didn't change any existing
    framework behavior."""

    def test_existing_pending_plugins_import_path_still_works(self):
        from pipeline.models.pending_plugins import (
            BorzoiPlugin as ReexportedBorzoi,
            EnformerPlugin as ReexportedEnformer,
            SpliceFormerPlugin as ReexportedSpliceFormer,
        )

        self.assertIs(ReexportedEnformer, EnformerPlugin)
        self.assertIs(ReexportedBorzoi, BorzoiPlugin)
        self.assertIs(ReexportedSpliceFormer, SpliceFormerPlugin)


if __name__ == "__main__":
    unittest.main()
