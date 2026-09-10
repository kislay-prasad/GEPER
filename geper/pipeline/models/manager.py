"""
AI Model Manager.

Central place a caller goes to run a registered plugin without caring
whether it's loaded yet, whether it's even available in this
environment, or what happens if it fails -- mirroring, at the plugin
level, the same "load each model only once, degrade gracefully, never
take the whole run down" architecture `pipeline/orchestrator.py`
already applies to the HyenaDNA/Evo2/RNA-FM/ESM-2/MMSplice
family (see that file's module docstring).

`pipeline/orchestrator.py` constructs one `ModelManager` (via
`pipeline.models.pending_plugins.build_default_registry()`) and one
`EnsembleManager` wrapping it, and calls the ensemble once per variant
(`_run_ensemble_stage`); the result flows into
`pipeline/interpretation.py`'s ACMG PP3/BP4 evaluation and into the
clinical report (`report/json_builder.py`, `report/report_generator.py`)
exactly like every other evidence source. See `pipeline/models/ensemble.py`
for the two-model (Enformer + Borzoi) consensus this produces, and
`pipeline/models/enformer_plugin.py` / `borzoi_plugin.py` for the real,
license-audited integrations themselves.
"""

import threading
from typing import Any, Dict, Optional

from pipeline.models.base import ModelMetadata, PluginModel
from pipeline.models.cache import WeightCache
from pipeline.models.registry import ModelRegistry
from utils.device_utils import get_device
from utils.exceptions import ModelInferenceError, ModelLoadError
from utils.logger import get_logger

logger = get_logger(__name__)


class PluginUnavailableError(ModelLoadError):
    """Raised by `ModelManager.get`/`predict` when the requested
    plugin is registered but not currently available (disabled by
    config, unmet license verification, missing dependency, etc).
    Subclasses `ModelLoadError` so any caller already catching that
    (e.g. `pipeline/orchestrator.py`'s existing exception handling
    for the other model family) transparently also handles this."""


class ModelManager:
    """
    Owns: a `ModelRegistry` (which plugin classes exist), a
    process-wide instance cache (so a plugin is instantiated/loaded
    at most once per process, same guarantee `utils.model_cache.
    ModelCache` gives the other model family), and a `WeightCache`
    (on-disk weight file cache, shared across plugins).
    """

    def __init__(self, registry: Optional[ModelRegistry] = None, weight_cache: Optional[WeightCache] = None):
        self.registry = registry or ModelRegistry()
        self.weight_cache = weight_cache or WeightCache()
        self._instances: Dict[str, PluginModel] = {}
        self._lock = threading.Lock()
        self._failed: Dict[str, str] = {}
        # Distinct from `_failed` above (which is permanent -- a load
        # failure means "never retry this process"): inference can
        # fail for one variant and succeed for the next (e.g. a
        # variant's window is malformed, or a transient OOM), so this
        # is overwritten/cleared per call rather than sticking forever.
        # Exists so callers (pipeline/orchestrator.py's AI model status
        # table) can distinguish "this model failed for this variant"
        # from "this model was never used" instead of both silently
        # looking like a plain skip.
        self._last_inference_error: Dict[str, str] = {}

    def register(self, key: str, model_cls) -> None:
        self.registry.register(key, model_cls)

    def device(self):
        """Centralized device selection -- identical resolution
        (CUDA -> MPS -> CPU) every plugin gets, same as the existing
        model family via `utils.device_utils.get_device`."""
        return get_device()

    def is_available(self, key: str) -> bool:
        return self.registry.get(key).is_available()

    def metadata(self, key: str) -> ModelMetadata:
        return self.registry.get(key).metadata()

    def all_metadata(self) -> Dict[str, ModelMetadata]:
        return self.registry.all_metadata()

    def version_info(self) -> Dict[str, str]:
        """Key -> version string for every registered plugin,
        regardless of current availability -- lets a caller (e.g. a
        report footer or an audit script) always see what's
        registered and at what version, even for a disabled plugin."""
        return {key: meta.version for key, meta in self.all_metadata().items()}

    def get(self, key: str) -> PluginModel:
        """
        Lazily instantiate + load the plugin for `key`, reusing a
        cached instance on subsequent calls. Raises
        `PluginUnavailableError` (never a raw KeyError/ImportError)
        if the plugin isn't currently available, and never re-attempts
        a load that has already failed once this process (mirrors
        `utils.auto_install`'s "cache the outcome" pattern) --
        surfacing the original failure reason instead of a fresh,
        possibly confusing retry failure.
        """
        if key in self._failed:
            raise PluginUnavailableError(self._failed[key])

        cached = self._instances.get(key)
        if cached is not None:
            return cached

        with self._lock:
            cached = self._instances.get(key)
            if cached is not None:
                return cached

            model_cls = self.registry.get(key)
            if not model_cls.is_available():
                reason = model_cls.unavailability_reason()
                raise PluginUnavailableError(f"Plugin '{key}' is not available: {reason}")

            try:
                instance = model_cls()
                instance.load()
            except Exception as exc:  # noqa: BLE001
                message = f"Plugin '{key}' failed to load: {exc}"
                self._failed[key] = message
                logger.error(message)
                raise PluginUnavailableError(message) from exc

            self._instances[key] = instance
            return instance

    def predict(self, key: str, *args, **kwargs) -> Optional[Dict[str, Any]]:
        """
        Runs inference for one plugin, isolating its failure from
        every other plugin/caller: returns None (never raises) when
        the plugin is unavailable or fails, after logging why --
        matching the project requirement "never crash" for every
        plugin in this framework. Callers that need to distinguish
        "unavailable" from "failed" should call `get()` themselves.
        """
        try:
            instance = self.get(key)
        except PluginUnavailableError as exc:
            logger.info(str(exc))
            return None

        try:
            result = instance.predict(*args, **kwargs)
        except ModelInferenceError as exc:
            logger.error(f"Plugin '{key}' inference failed: {exc}")
            self._last_inference_error[key] = str(exc)[:300]
            return None
        else:
            self._last_inference_error.pop(key, None)
            return result

    def last_inference_errors(self) -> Dict[str, str]:
        """Key -> most recent inference-failure message, for whichever
        plugin(s) failed on their most recent `predict()` call. Only
        reflects the *last* call per key (this is a per-variant status
        signal, not a history) -- cleared automatically on that key's
        next successful `predict()`."""
        return dict(self._last_inference_error)

    def loaded_keys(self):
        return sorted(self._instances)

    def failed_keys(self) -> Dict[str, str]:
        return dict(self._failed)

    def reset(self) -> None:
        """Clears cached instances and failure memory -- for tests, or
        a caller that wants to retry a plugin after e.g. flipping a
        config flag mid-process."""
        with self._lock:
            self._instances.clear()
            self._failed.clear()
