"""
Base classes for GEPER's generic AI model plugin framework.

This is intentionally a separate, more general lifecycle contract than
`models.base_model.BaseGenomicModel` (used by the HyenaDNA/
Evo2/RNA-FM/ESM-2 family). That family's contract is specifically
"one sequence in, one embedding/logit dict out." The plugin framework
here exists for a different, broader shape of model: ones that may
need per-plugin licensing/version metadata surfaced to the report
layer, that are disabled-by-default pending an operator's own license
verification (see `pipeline/models/manager.py` module docstring), and
that are registered/discovered dynamically rather than imported by
name at each call site.

Nothing in `models/base_model.py`, `utils/model_cache.py`, or any
existing model wrapper is modified or imported *from* here -- this is
a pure addition, so every existing model keeps behaving exactly as it
does today.
"""

import abc
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from utils.device_utils import get_device
from utils.exceptions import ModelInferenceError, ModelLoadError
from utils.logger import get_logger


@dataclass(frozen=True)
class ModelMetadata:
    """
    Everything the report layer / a licensing audit needs to know
    about one plugin, without loading it.

    `commercial_use_allowed` is deliberately a `Optional[bool]`, not a
    plain `bool`: `None` means "not yet verified against a primary
    source" and must never be silently treated as either True or
    False by calling code. This mirrors the project's established
    practice (see CONFIG.splicing's docstring and each plugin
    module's docstring) of never guessing a license status.
    """

    name: str
    version: str
    source: str
    license_name: str
    license_url: str
    commercial_use_allowed: Optional[bool]
    license_notes: str = ""


class PluginModel(abc.ABC):
    """
    Common lifecycle for every plugin registered with
    `pipeline.models.manager.ModelManager`.

    Subclasses implement:
        metadata()        -> ModelMetadata (no loading required)
        is_available()    -> classmethod, cheap availability check
        unavailability_reason() -> classmethod, human-readable reason
        _load_impl()      -> loads weights, sets self.model (+ anything else)
        _infer_impl(...)  -> runs inference, returns a result dict

    `load()` / `predict()` wrap those with device placement, timing,
    and exception translation -- the same shape as
    `BaseGenomicModel`, deliberately, so the two families are easy to
    reason about side by side even though they're independent.
    """

    def __init__(self):
        self.logger = get_logger(self.__class__.__module__)
        self.device = get_device()
        self.model: Any = None
        self._loaded: bool = False

    # ------------------------------------------------------------------
    @classmethod
    @abc.abstractmethod
    def metadata(cls) -> ModelMetadata:
        """Static metadata about this plugin -- must not require loading it."""

    @classmethod
    def is_available(cls) -> bool:
        """
        Whether this plugin may even be attempted right now.

        Default: False. A plugin becomes available only once it
        explicitly overrides this (typically gated by both a config
        opt-in flag *and* an actually-verified commercial license --
        see each plugin's own override for its specific conditions).
        Defaulting to False (rather than True, as
        `BaseGenomicModel.is_available` does) is intentional here: an
        unfinished/unverified plugin must never be silently attempted
        just because someone registered its class.
        """
        return False

    @classmethod
    def unavailability_reason(cls) -> str:
        return "plugin not yet enabled"

    # ------------------------------------------------------------------
    @abc.abstractmethod
    def _load_impl(self) -> None:
        """Load weights; must set `self.model`."""

    @abc.abstractmethod
    def _infer_impl(self, *args, **kwargs) -> Dict[str, Any]:
        """Run inference; return a plain result dict."""

    # ------------------------------------------------------------------
    def load(self) -> "PluginModel":
        if self._loaded:
            return self
        start = time.time()
        try:
            self._load_impl()
        except Exception as exc:  # noqa: BLE001
            raise ModelLoadError(f"Failed to load plugin '{self.metadata().name}': {exc}") from exc
        self._loaded = True
        self.logger.info(f"Loaded plugin '{self.metadata().name}' on {self.device} in {time.time() - start:.1f}s.")
        return self

    def predict(self, *args, **kwargs) -> Dict[str, Any]:
        if not self._loaded:
            self.load()
        start = time.time()
        try:
            result = self._infer_impl(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise ModelInferenceError(f"Inference failed for plugin '{self.metadata().name}': {exc}") from exc
        result.setdefault("meta", {})
        result["meta"].update(
            {
                "model": self.metadata().name,
                "version": self.metadata().version,
                "device": str(self.device),
                "inference_seconds": round(time.time() - start, 4),
            }
        )
        return result

    def unload(self) -> None:
        self.model = None
        self._loaded = False
