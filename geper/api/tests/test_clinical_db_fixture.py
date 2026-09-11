"""
api/tests/test_clinical_db_fixture.py
─────────────────────────────────────
The geper side of the disposable-database guard: the `clinical_conn`
fixture in api/tests/conftest.py, which the worker/clinical-link tests use.

Two properties, each demonstrated rather than assumed:

  1. Each test gets a FRESH schema. The first test below writes an
     organisation; the second asserts none exists. Run in file order, a
     fixture that reused state would fail the second.
  2. A DSN without CLINICAL_TEST_DB_DISPOSABLE=1 stops the session before
     any connection, shown in a child pytest pointed at an address where
     nothing listens (127.0.0.1:1).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

GEPER_ROOT = Path(__file__).resolve().parents[2]
NOWHERE_DSN = "postgresql://guard:guard@127.0.0.1:1/guard_probe"


def test_fixture_gives_a_working_schema(clinical_conn):
    with clinical_conn.cursor() as cur:
        cur.execute("INSERT INTO organisations (name, created_at) VALUES ('fixture-probe', now())")
    clinical_conn.commit()
    with clinical_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM organisations")
        assert cur.fetchone()[0] == 1


def test_fixture_schema_is_fresh_for_every_test(clinical_conn):
    with clinical_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM organisations")
        assert cur.fetchone()[0] == 0


def test_dsn_without_opt_in_stops_the_geper_session_before_any_connection():
    env = dict(os.environ)
    for name in ("CLINICAL_TEST_DB_DISPOSABLE", "CLINICAL_REQUIRE_DB"):
        env.pop(name, None)
    env["CLINICAL_TEST_DSN"] = NOWHERE_DSN
    env.setdefault("GEPER_DEV_INSECURE", "1")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "api/tests/test_clinical_db_fixture.py::test_fixture_schema_is_fresh_for_every_test",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=GEPER_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 4, out
    assert "CLINICAL_TEST_DB_DISPOSABLE=1 is not" in out, out
    assert "guard:guard" not in out, out
