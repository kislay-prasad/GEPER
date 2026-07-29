"""
Prediction-level cache for MMSplice.

This is distinct from `utils.model_cache.ModelCache` (which caches the
*loaded model weights*, exactly once per process, and is used by
`loader.py` for that purpose). `MMSplicePredictionCache` instead caches
*prediction results* keyed by the inputs that fully determine them
(chrom, pos, ref, alt, transcript/exon id, and the configured window
sizes) -- so a VCF with the same variant repeated (multi-sample sites
already normalized to one ALT per record upstream in
`pipeline/vcf_parser.py`, or the same variant re-annotated against
more than one overlapping transcript) never re-runs the five Keras
models for an input it has already scored this run.

A bounded, simple LRU (not an unbounded dict) so a very large VCF
cannot grow this cache without limit inside a single long-running
process (e.g. a FastAPI worker serving many requests).
"""

from collections import OrderedDict
from threading import Lock
from typing import Any, Dict, Optional, Tuple

CacheKey = Tuple[str, int, str, str, str]  # (chrom, pos, ref, alt, transcript_id)


class MMSplicePredictionCache:
    """Thread-safe, bounded LRU cache of finished MMSplice result dicts."""

    def __init__(self, max_size: int = 5000):
        self.max_size = max_size
        self._store: "OrderedDict[CacheKey, Dict[str, Any]]" = OrderedDict()
        self._lock = Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def make_key(chrom: str, pos: int, ref: str, alt: str, transcript_id: str) -> CacheKey:
        return (chrom, pos, ref, alt, transcript_id)

    def get(self, key: CacheKey) -> Optional[Dict[str, Any]]:
        with self._lock:
            value = self._store.get(key)
            if value is not None:
                self._store.move_to_end(key)
                self.hits += 1
                return value
            self.misses += 1
            return None

    def put(self, key: CacheKey, value: Dict[str, Any]) -> None:
        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > self.max_size:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"size": len(self._store), "hits": self.hits, "misses": self.misses}
