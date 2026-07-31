"""
Registry of plugin model classes.

A thin, dependency-free mapping from a short key (e.g. "enformer")
to a `PluginModel` subclass -- deliberately NOT a global mutable
singleton auto-populated at import time (that would force importing
every plugin module, and therefore every plugin's optional heavy
dependency, just to import this file). Callers build a `ModelRegistry`
explicitly and register only the plugin classes they want, exactly
like `pipeline/orchestrator.py` already does for the existing
`MODEL_REGISTRY` dict (see its module docstring for why that one is
assembled explicitly rather than auto-discovered, too).
"""

from typing import Dict, List, Type

from pipeline.models.base import ModelMetadata, PluginModel


class ModelRegistry:
    """Simple key -> PluginModel-subclass registry."""

    def __init__(self):
        self._classes: Dict[str, Type[PluginModel]] = {}

    def register(self, key: str, model_cls: Type[PluginModel]) -> None:
        if not issubclass(model_cls, PluginModel):
            raise TypeError(
                f"'{model_cls}' must subclass PluginModel to be registered "
                f"under key '{key}'."
            )
        self._classes[key] = model_cls

    def get(self, key: str) -> Type[PluginModel]:
        try:
            return self._classes[key]
        except KeyError:
            raise KeyError(
                f"No plugin registered under key '{key}'. Registered keys: "
                f"{sorted(self._classes)}"
            ) from None

    def keys(self) -> List[str]:
        return sorted(self._classes)

    def available_keys(self) -> List[str]:
        """Keys whose class currently reports `is_available() is True`
        -- i.e. both any config opt-in AND any license/environment
        gating that class enforces are satisfied right now."""
        return [key for key, cls in self._classes.items() if cls.is_available()]

    def all_metadata(self) -> Dict[str, ModelMetadata]:
        return {key: cls.metadata() for key, cls in self._classes.items()}

    def __contains__(self, key: str) -> bool:
        return key in self._classes

    def __len__(self) -> int:
        return len(self._classes)
