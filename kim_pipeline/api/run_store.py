"""
api/run_store.py
─────────────────
SQLite-backed persistent run store (GAP 3).

All PipelineRunner run state is written here so that API restarts
do not lose run history.

Config:
    GEPER_DB_PATH  — path to SQLite db file (default: /tmp/geper_runs.db)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("geper.api.run_store")

_DB_PATH = os.getenv("GEPER_DB_PATH", "/tmp/geper_runs.db")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    sample_id           TEXT,
    status              TEXT,
    stage               TEXT,
    progress_pct        REAL,
    stages_completed    TEXT,
    stages_failed       TEXT,
    started_at          TEXT,
    finished_at         TEXT,
    elapsed_seconds     REAL,
    error               TEXT,
    report_json_path    TEXT,
    report_html_path    TEXT,
    log_path            TEXT,
    fastq_r1            TEXT,
    fastq_r2            TEXT,
    reference_fasta     TEXT,
    created_at          TEXT
)
"""

_COLUMNS = [
    "run_id", "sample_id", "status", "stage", "progress_pct",
    "stages_completed", "stages_failed", "started_at", "finished_at",
    "elapsed_seconds", "error", "report_json_path", "report_html_path",
    "log_path", "fastq_r1", "fastq_r2", "reference_fasta", "created_at",
]

_JSON_COLS = {"stages_completed", "stages_failed"}


def _encode(col: str, val) -> str | None:
    if col in _JSON_COLS:
        if val is None:
            return "[]"
        if isinstance(val, (list, dict)):
            return json.dumps(val)
        return str(val)
    if val is None:
        return None
    return str(val) if not isinstance(val, (int, float, str)) else val


def _decode_row(row: sqlite3.Row) -> Dict:
    d = dict(row)
    for col in _JSON_COLS:
        raw = d.get(col)
        if raw:
            try:
                d[col] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d[col] = []
        else:
            d[col] = []
    # Cast numeric
    for col in ("progress_pct", "elapsed_seconds"):
        val = d.get(col)
        if val is not None:
            try:
                d[col] = float(val)
            except (TypeError, ValueError):
                pass
    return d


class RunStore:
    """Thread-safe SQLite-backed run registry.

    Args:
        db_path: Path to SQLite database file.
    """

    def __init__(self, db_path: str = _DB_PATH) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._connect()

    def _connect(self) -> None:
        # Use check_same_thread=False; we guard with self._lock.
        self._conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()

    def _execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def create(self, run_id: str, fields: Dict) -> None:
        """Insert a new run record."""
        now = datetime.now(timezone.utc).isoformat()
        row = {col: None for col in _COLUMNS}
        row["run_id"] = run_id
        row["created_at"] = now
        for k, v in fields.items():
            if k in row:
                row[k] = _encode(k, v)
        cols = ", ".join(_COLUMNS)
        placeholders = ", ".join("?" for _ in _COLUMNS)
        values = [row[c] for c in _COLUMNS]
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO runs ({cols}) VALUES ({placeholders})",
                values,
            )

    def update(self, run_id: str, **fields) -> None:
        """Update one or more fields on an existing run."""
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields if k in _COLUMNS)
        if not set_clause:
            return
        values = [_encode(k, v) for k, v in fields.items() if k in _COLUMNS]
        values.append(run_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE runs SET {set_clause} WHERE run_id = ?",
                values,
            )

    def get(self, run_id: str) -> Optional[Dict]:
        """Return run dict or None if not found."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return _decode_row(row)

    def list_all(self, limit: int = 100) -> List[Dict]:
        """Return runs ordered by created_at descending."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_decode_row(r) for r in rows]

    def delete(self, run_id: str) -> None:
        """Remove a run record."""
        with self._lock:
            self._conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
