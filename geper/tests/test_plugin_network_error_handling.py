"""
Regression tests for the 5-site OSError-mislabelling fix (commit
41935a2, "geper: fix 5-site OSError mislabelling in plugin exception
handlers"). Shipped with no persisted tests -- Angela verified it by
hand at the time (live isinstance checks against the real installed
requests/huggingface_hub, plus a full propagation-chain repro against
base.py:120-123 / manager.py:130-134) and flagged that against her own
work: "if someone reintroduces bare OSError to one of these 5 tuples
tomorrow, nothing in the test suite will catch it." This file is what
turns that into something that stays true.

THE DEFECT: a LOCAL resource failure (Windows paging-file exhaustion,
os error 1455, reproduced for real by Andy) was caught by each
plugin's `_NETWORK_ERROR_TYPES` tuple (which used to include bare
`OSError`) and re-raised as "<Model> model unavailable" -- a local
failure relabelled as a connectivity problem, indistinguishable from a
genuine network outage in geper_results.json and the report's Data
Source Provenance section.

THE PROPERTY, not just the mechanism: a local failure must stay
distinguishable from a network one, all the way out to
`ModelManager.get()`. Two things pin that:

  TestNoBareOSErrorInNetworkTuples -- pins the MECHANISM (item 1).
  Deliberately asserts the tuples' exact membership, which is close to
  pinning an observed value in general, but the tuple genuinely IS the
  contract here: the whole defect was tuple membership, so this is a
  direct assertion of the actual fix, not an incidental shape. Fails
  the moment bare OSError is reintroduced to any of the five.

  TestOSErrorPropagationSurvivesRelabeling -- pins the PROPERTY (item
  2), independent of any one plugin's tuple contents. Uses a minimal,
  dependency-free fake `PluginModel` (not any of the five real
  plugins) whose `_load_impl` raises a real OSError directly, run
  through a REAL `ModelManager`/`ModelRegistry` -- the exact base.py
  `load()` / manager.py `get()` code every one of the five plugins
  shares. This is deliberately NOT plugin-specific: base.py and
  manager.py are common infrastructure, identical for all five, so
  proving the property once through the shared machinery covers all
  five call sites' downstream handling. What item 1 guarantees
  (OSError is no longer caught by the plugin's own except clause) plus
  ordinary Python semantics (an exception not caught by any narrower
  handler propagates to the caller unchanged) is what makes this
  combination complete -- there is no GEPER-specific behaviour left
  to verify per-plugin once both of these hold.

  WHY NOT ALSO INJECT A REAL OSError INTO EACH OF THE FIVE REAL
  PLUGINS' OWN `_load_impl` (considered, per the dispatch's invitation
  to make item 1 redundant if possible): enformer and borzoi gate
  their real network call behind `ensure_pip_package_available(...)`,
  which -- per today's floor-wide safety advisory -- can shell out to
  a REAL `pip install` against the shared interpreter if the mock
  doesn't also intercept the lazy `import enformer_pytorch` /
  `import huggingface_hub` that follows it; and `enformer_pytorch`
  itself is demonstrably NOT STABLE in this shared environment as of
  today (see the floor's own forensics), so a test asserting on its
  real import behavior would be asserting on the wrong thing entirely
  -- machine state, not the fix. Testing the shared base.py/manager.py
  wrapping mechanism once, generically, sidesteps both risks while
  still proving the real property.
"""

import importlib

import pytest

from pipeline.models.base import ModelMetadata, PluginModel
from pipeline.models.manager import ModelManager, PluginUnavailableError
from pipeline.models.registry import ModelRegistry
from utils.exceptions import ModelLoadError

_PLUGIN_MODULES = [
    "pipeline.models.enformer_plugin",
    "pipeline.models.borzoi_plugin",
    "pipeline.models.spliceformer_plugin",
    "pipeline.models.splicebert_plugin",
    "pipeline.models.spip_plugin",
]


class TestNoBareOSErrorInNetworkTuples:
    @pytest.mark.parametrize("module_path", _PLUGIN_MODULES, ids=lambda p: p.rsplit(".", 1)[-1])
    def test_network_error_tuple_excludes_bare_oserror(self, module_path):
        module = importlib.import_module(module_path)
        network_types = module._NETWORK_ERROR_TYPES

        assert OSError not in network_types, (
            f"{module_path}._NETWORK_ERROR_TYPES contains bare OSError -- this is exactly the "
            f"regression the fix closed: a local resource failure (e.g. Windows paging-file "
            f"exhaustion) would be caught here and relabelled as a network/unavailability error."
        )

    @pytest.mark.parametrize("module_path", _PLUGIN_MODULES, ids=lambda p: p.rsplit(".", 1)[-1])
    def test_network_error_tuple_is_nonempty_and_all_exception_types(self, module_path):
        """Guards against someone 'fixing' the bare-OSError case by
        emptying the tuple instead of narrowing it -- that would silently
        stop catching genuine network errors at all."""
        module = importlib.import_module(module_path)
        network_types = module._NETWORK_ERROR_TYPES

        assert len(network_types) > 0
        assert all(isinstance(t, type) and issubclass(t, BaseException) for t in network_types)


class _RaisesOSErrorPlugin(PluginModel):
    """Minimal, dependency-free stand-in for any of the five real
    plugins: exercises exactly the same base.py/manager.py code paths
    (load() -> ModelLoadError -> ModelManager.get() -> PluginUnavailableError)
    every real plugin goes through, without needing torch, requests,
    huggingface_hub, or any pip-installed weight-loading library."""

    _ORIGINAL_MESSAGE = "[WinError 1455] The paging file is too small for this operation to complete"

    @classmethod
    def metadata(cls) -> ModelMetadata:
        return ModelMetadata(
            name="FakeLocalFailurePlugin",
            version="0.0-test",
            source="test double",
            license_name="n/a",
            license_url="",
            commercial_use_allowed=None,
        )

    @classmethod
    def is_available(cls) -> bool:
        return True

    def _load_impl(self) -> None:
        # A genuine local-resource OSError, unrelated to any network
        # call -- exactly the class of failure the fix was about.
        # Deliberately NOT one of the network-specific subclasses
        # (ConnectionError/TimeoutError are themselves OSError
        # subclasses) -- this is bare OSError, which none of the five
        # real _NETWORK_ERROR_TYPES tuples catch post-fix.
        raise OSError(1455, self._ORIGINAL_MESSAGE)

    def _infer_impl(self, *args, **kwargs):
        return {}


class TestOSErrorPropagationSurvivesRelabeling:
    def _get_via_real_manager(self):
        registry = ModelRegistry()
        registry.register("fakelocal", _RaisesOSErrorPlugin)
        manager = ModelManager(registry=registry)
        with pytest.raises(PluginUnavailableError) as excinfo:
            manager.get("fakelocal")
        return excinfo.value

    def test_final_error_message_contains_the_original_text(self):
        """The property, end to end: whatever a caller of
        ModelManager.get() ultimately sees must still let them tell
        this was a local resource failure, not a network one."""
        final_exc = self._get_via_real_manager()
        assert _RaisesOSErrorPlugin._ORIGINAL_MESSAGE in str(final_exc)
        assert "network" not in str(final_exc).lower()

    def test_original_oserror_survives_unrelabeled_in_the_cause_chain(self):
        """Walks __cause__ (set by both `raise ... from exc` sites --
        base.py:120-123 then manager.py:130-134) down to the root and
        confirms it is the REAL OSError instance, not a RuntimeError or
        anything else standing in for it. This is what 'relabeling'
        would have broken: the fixed plugins used to swallow the
        original OSError and raise a fresh, generic RuntimeError in its
        place (see e.g. enformer_plugin.py's `raise RuntimeError(...)
        from exc` for genuine network errors) -- for a LOCAL failure,
        post-fix, nothing does that substitution at all."""
        final_exc = self._get_via_real_manager()

        assert isinstance(final_exc, PluginUnavailableError)
        wrapped = final_exc.__cause__
        assert isinstance(wrapped, ModelLoadError), (
            "expected base.py's load() to have wrapped the original failure in ModelLoadError "
            "before manager.py wraps it again"
        )

        root_cause = wrapped.__cause__
        assert type(root_cause) is OSError, (
            f"expected the root cause to be the real, unmodified OSError instance; got "
            f"{type(root_cause)!r} -- the original failure was relabeled somewhere in the chain"
        )
        assert root_cause.args == (1455, _RaisesOSErrorPlugin._ORIGINAL_MESSAGE)
