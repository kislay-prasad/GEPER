"""
Generic, thread-safe, bounded, TTL'd LRU cache with optional on-disk
JSON-lines persistence.

Factored out of the near-identical implementations in
`pipeline/gnomad/cache.py` and `pipeline/clingen/cache.py` so the three
new evidence-source integrations added alongside them (UniProt,
InterPro/Pfam, AlphaFold DB) don't duplicate that logic a third, fourth,
and fifth time. Each domain module still exposes its own thin,
purpose-named subclass (`UniProtCache`, `InterProCache`,
`AlphaFoldCache`) so callers/tests keep referring to a self-descriptive
class name -- matching the existing `GnomadCache` / `ClinGenCache` /
`MMSplicePredictionCache` shape -- while the actual caching logic lives
in exactly one place.
"""

import json
import os
import time
from collections import OrderedDict
from threading import Lock
from typing import Any, Dict, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


class SimpleTTLCache:
    """Thread-safe, bounded, TTL'd LRU cache of plain-dict values."""

    def __init__(
        self,
        max_size: int = 10_000,
        ttl_seconds: Optional[float] = 24 * 3600,
        disk_path: Optional[str] = None,
        label: str = "cache",
    ):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.disk_path = disk_path
        self.label = label

        self._store: "OrderedDict[str, tuple]" = OrderedDict()  # key -> (expires_at_or_None, value)
        self._lock = Lock()
        self.hits = 0
        self.misses = 0

        if self.disk_path:
            self._load_from_disk()

    # -- in-memory ----------------------------------------------------

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires_at, value = entry
            if expires_at is not None and time.monotonic() > expires_at:
                del self._store[key]
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key: str, value: Dict[str, Any]) -> None:
        expires_at = (time.monotonic() + self.ttl_seconds) if self.ttl_seconds else None
        with self._lock:
            self._store[key] = (expires_at, value)
            self._store.move_to_end(key)
            while len(self._store) > self.max_size:
                self._store.popitem(last=False)
        if self.disk_path:
            self._append_to_disk(key, value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"size": len(self._store), "hits": self.hits, "misses": self.misses}

    # -- on-disk persistence -------------------------------------------
    # Same deliberately-simple append-only JSON-lines design as
    # `pipeline/gnomad/cache.py`: entries here are immutable per
    # (gene/accession, data-source release), so there is never a need
    # to update a line in place, only to append new ones and
    # de-duplicate on load (last write for a given key wins).

    def _append_to_disk(self, key: str, value: Dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self.disk_path) or ".", exist_ok=True)
            with open(self.disk_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"key": key, "value": value}) + "\n")
        except OSError as exc:
            # Disk cache is an optimization, never a correctness
            # requirement -- a read-only filesystem must not break lookups.
            logger.warning(f"Could not persist {self.label} cache entry to '{self.disk_path}': {exc}")

    def _load_from_disk(self) -> None:
        if not self.disk_path or not os.path.exists(self.disk_path):
            return
        loaded = 0
        try:
            with open(self.disk_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        self._store[record["key"]] = (None, record["value"])
                        loaded += 1
                    except (json.JSONDecodeError, KeyError):
                        continue
        except OSError as exc:
            logger.warning(f"Could not read {self.label} disk cache '{self.disk_path}': {exc}")
            return
        if loaded:
            logger.info(
                f"Loaded {loaded} cached {self.label} entr{'y' if loaded == 1 else 'ies'} from '{self.disk_path}'."
            )
            with self._lock:
                while len(self._store) > self.max_size:
                    self._store.popitem(last=False)
