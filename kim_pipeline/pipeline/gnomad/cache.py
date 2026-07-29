"""
pipeline/gnomad/cache.py
────────────────────────
Persistent, disk-backed (SQLite) cache for gnomAD lookups.

Deliberately only stores `PRESENT` and `ABSENT` outcomes — never
`UNAVAILABLE`. gnomAD allele frequencies for a fixed dataset/build are
immutable facts, so caching PRESENT/ABSENT forever is correct. But
`UNAVAILABLE` means "the network/API failed this attempt" — a transient
fact about *this run*, not about gnomAD. If we persisted it, one 429 or
one dropped connection today would permanently block PM2 evidence for
that variant on every future run, even after connectivity is restored.
`UNAVAILABLE` is only ever cached in-memory, for the lifetime of a
single `GnomadLookup` instance (see lookup.py) — never written here.

SQLite (not JSONL) per the reliability/indexing requirement: WAL
journal mode + a busy_timeout so concurrent threads within one process
(ThreadPoolExecutor in lookup_batch) and concurrent processes sharing
the same Google-Drive-mounted cache file across Colab sessions don't
corrupt the file or hit "database is locked" errors.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("geper.pipeline.gnomad.cache")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gnomad_cache (
    cache_key   TEXT PRIMARY KEY,
    outcome     TEXT NOT NULL,       -- 'present' | 'absent'
    af          REAL,
    af_popmax   REAL,
    ac          INTEGER,
    an          INTEGER,
    backend     TEXT,
    cached_at   REAL NOT NULL
);
"""


def default_cache_path() -> str:
    return str(Path.home() / ".cache" / "geper" / "gnomad" / "cache.sqlite3")


class GnomadDiskCache:
    """Thin, dependency-free wrapper around a SQLite cache file.

    One instance is safe to share across threads within a process (all
    access is serialized behind a lock plus SQLite's own busy_timeout);
    multiple processes/sessions may point at the same file (e.g. on a
    Google Drive mount) as long as they all use WAL mode, which this
    class always enables.
    """

    def __init__(self, path: Optional[str] = None, ttl_hours: Optional[float] = None) -> None:
        self.path = path or default_cache_path()
        self.ttl_seconds: Optional[float] = (ttl_hours * 3600.0) if ttl_hours else None
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_error: Optional[str] = None
        self._connect()

    # ── setup ────────────────────────────────────────────────────────────

    def _connect(self) -> None:
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute(_SCHEMA)
            conn.commit()
            self._conn = conn
        except (sqlite3.Error, OSError) as exc:
            # A cache that can't open must never take the pipeline down —
            # degrade to "no persistent cache", same as if it were never
            # configured. Callers should still function correctly (just
            # slower / more network-bound), matching requirement 4:
            # never crash the pipeline because gnomAD is unavailable.
            logger.warning(
                "[gnomAD cache] Could not open SQLite cache at '%s' (%s) — "
                "continuing without persistent cache for this run.",
                self.path, exc,
            )
            self._conn = None
            self._init_error = str(exc)

    @property
    def available(self) -> bool:
        return self._conn is not None

    # ── reads ────────────────────────────────────────────────────────────

    def get(self, cache_key: str) -> Optional[dict]:
        """Return a dict with keys outcome/af/af_popmax/ac/an/backend, or
        None on a miss (including when the cache is unavailable or the
        entry has expired past ttl_hours)."""
        if not self._conn:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT outcome, af, af_popmax, ac, an, backend, cached_at "
                    "FROM gnomad_cache WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("[gnomAD cache] read failed for '%s': %s", cache_key, exc)
            return None
        if row is None:
            return None
        outcome, af, af_popmax, ac, an, backend, cached_at = row
        if self.ttl_seconds is not None and (time.time() - cached_at) > self.ttl_seconds:
            return None
        return {
            "outcome": outcome,
            "af": af,
            "af_popmax": af_popmax,
            "ac": ac,
            "an": an,
            "backend": backend,
        }

    def get_many(self, cache_keys) -> Dict[str, dict]:
        """Bulk read, used by lookup_batch() to minimize round trips into
        SQLite for large variant lists."""
        if not self._conn:
            return {}
        keys = list(cache_keys)
        if not keys:
            return {}
        out: Dict[str, dict] = {}
        try:
            with self._lock:
                # SQLite has a default limit of 999 bound parameters —
                # chunk defensively for large variant batches.
                for i in range(0, len(keys), 500):
                    chunk = keys[i:i + 500]
                    placeholders = ",".join("?" * len(chunk))
                    rows = self._conn.execute(
                        f"SELECT cache_key, outcome, af, af_popmax, ac, an, backend, cached_at "
                        f"FROM gnomad_cache WHERE cache_key IN ({placeholders})",
                        chunk,
                    ).fetchall()
            now = time.time()
            for cache_key, outcome, af, af_popmax, ac, an, backend, cached_at in rows:
                if self.ttl_seconds is not None and (now - cached_at) > self.ttl_seconds:
                    continue
                out[cache_key] = {
                    "outcome": outcome, "af": af, "af_popmax": af_popmax,
                    "ac": ac, "an": an, "backend": backend,
                }
        except sqlite3.Error as exc:
            logger.warning("[gnomAD cache] bulk read failed: %s", exc)
            return {}
        return out

    # ── writes ───────────────────────────────────────────────────────────

    def put(
        self,
        cache_key: str,
        outcome: str,
        af: Optional[float] = None,
        af_popmax: Optional[float] = None,
        ac: Optional[int] = None,
        an: Optional[int] = None,
        backend: Optional[str] = None,
    ) -> None:
        """Persist a PRESENT/ABSENT result. Never call with 'unavailable'
        — see module docstring."""
        if not self._conn:
            return
        if outcome not in ("present", "absent"):
            # Defensive: refuse to persist anything else rather than trust
            # every call site to remember the "never cache UNAVAILABLE"
            # rule — this is the one place that rule can be enforced once.
            logger.debug(
                "[gnomAD cache] refusing to persist non-terminal outcome '%s' for '%s'",
                outcome, cache_key,
            )
            return
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO gnomad_cache "
                    "(cache_key, outcome, af, af_popmax, ac, an, backend, cached_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (cache_key, outcome, af, af_popmax, ac, an, backend, time.time()),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("[gnomAD cache] write failed for '%s': %s", cache_key, exc)

    def stats(self) -> dict:
        if not self._conn:
            return {"available": False, "size": 0, "path": self.path}
        try:
            with self._lock:
                (count,) = self._conn.execute("SELECT COUNT(*) FROM gnomad_cache").fetchone()
            return {"available": True, "size": count, "path": self.path}
        except sqlite3.Error:
            return {"available": True, "size": None, "path": self.path}

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
