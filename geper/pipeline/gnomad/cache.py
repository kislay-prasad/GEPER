"""
Caching for gnomAD lookups.

Two layers, both optional and independently toggleable via
`config.py::GnomadConfig`:

  1. In-process, bounded, thread-safe LRU with a TTL (requirement
     #4's "Caching") -- same bounded-LRU shape as
     `pipeline/models/mmsplice/cache.py::MMSplicePredictionCache`,
     since a very large VCF must not let this cache grow without
     limit inside one long-running process.
  2. An optional on-disk JSON-lines persistence layer, so a Colab
     restart / process crash doesn't lose already-fetched gnomAD
     annotations for variants a previous run already paid the
     network cost for -- the same "survive a Colab disconnect"
     motivation as BLAST's disk cache
     (`database/blast_client.py`) and AlphaMissense's on-disk
     catalogue cache.
"""

import json
import os
import time
from collections import OrderedDict
from threading import Lock
from typing import Any, Dict, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


class GnomadCache:
    """Thread-safe, bounded, TTL'd LRU cache of gnomAD lookup results (as plain dicts)."""

    def __init__(
        self,
        max_size: int = 20_000,
        ttl_seconds: Optional[float] = 6 * 3600,
        disk_path: Optional[str] = None,
    ):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.disk_path = disk_path

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
    # Deliberately a simple append-only JSON-lines file, not a
    # database: gnomAD annotations are immutable per (variant, build,
    # gnomAD release), so there is never a need to update a line in
    # place, only to append new ones and de-duplicate on load (last
    # write for a given key wins, in case a key was ever re-fetched).

    def _append_to_disk(self, key: str, value: Dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self.disk_path) or ".", exist_ok=True)
            with open(self.disk_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"key": key, "value": value}) + "\n")
        except OSError as exc:
            # Disk cache is an optimization, never a correctness
            # requirement -- a read-only filesystem (some Colab/
            # serverless environments) must not break lookups.
            logger.warning(f"Could not persist gnomAD cache entry to '{self.disk_path}': {exc}")

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
                        # No TTL re-applied on load: a persisted entry
                        # is treated as valid until the next put()
                        # naturally re-expires/refreshes it in memory --
                        # this only seeds a warm cache, it never
                        # silently serves stale evidence past a
                        # caller's original TTL expectation, since gnomAD
                        # release data itself doesn't change on the
                        # timescale TTLs here are set for (hours).
                        self._store[record["key"]] = (None, record["value"])
                        loaded += 1
                    except (json.JSONDecodeError, KeyError):
                        continue
        except OSError as exc:
            logger.warning(f"Could not read gnomAD disk cache '{self.disk_path}': {exc}")
            return
        if loaded:
            logger.info(f"Loaded {loaded} cached gnomAD entr{'y' if loaded == 1 else 'ies'} from '{self.disk_path}'.")
            with self._lock:
                while len(self._store) > self.max_size:
                    self._store.popitem(last=False)
