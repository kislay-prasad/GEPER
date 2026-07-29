"""
Global, thread-safe model cache.

GEPER may process many variants in a single run, and several pretrained
models weigh hundreds of MB to several GB. This registry guarantees
each model class is instantiated and loaded into memory exactly once
per process, regardless of how many times the pipeline requests it.

Usage pattern (inside a model class):

    from utils.model_cache import ModelCache

    class HyenaDNAModel(BaseGenomicModel):
        def _get_cache_key(self) -> str:
            return "hyenadna"

        def _load_impl(self):
            ... build tokenizer/model ...
            return tokenizer, model

    instance = ModelCache.get_or_create("hyenadna", HyenaDNAModel)
"""

import threading
from typing import Callable, Dict

from utils.logger import get_logger

logger = get_logger(__name__)


class ModelCache:
    """Process-wide singleton registry for loaded model instances."""

    _instances: Dict[str, object] = {}
    _lock = threading.Lock()

    @classmethod
    def get_or_create(cls, key: str, factory: Callable[[], object]) -> object:
        """
        Return the cached instance for `key`, constructing it via
        `factory()` on first request. Double-checked locking avoids
        redundant loads under concurrent access (e.g. FastAPI workers).
        """
        instance = cls._instances.get(key)
        if instance is not None:
            logger.debug(f"Cache hit for model '{key}'.")
            return instance

        with cls._lock:
            instance = cls._instances.get(key)
            if instance is None:
                logger.info(f"Cache miss for model '{key}'. Loading now...")
                instance = factory()
                cls._instances[key] = instance
                logger.info(f"Model '{key}' loaded and cached.")
            return instance

    @classmethod
    def is_cached(cls, key: str) -> bool:
        return key in cls._instances

    @classmethod
    def evict(cls, key: str) -> None:
        """Remove a model from the cache, freeing it for garbage collection."""
        with cls._lock:
            if key in cls._instances:
                del cls._instances[key]
                logger.info(f"Evicted model '{key}' from cache.")

    @classmethod
    def clear(cls) -> None:
        """Evict all cached models. Useful for freeing GPU memory between runs."""
        with cls._lock:
            cls._instances.clear()
            logger.info("Cleared entire model cache.")

    @classmethod
    def cached_keys(cls) -> list:
        return list(cls._instances.keys())
