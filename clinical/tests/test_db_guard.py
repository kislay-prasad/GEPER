"""
clinical/tests/test_db_guard.py
───────────────────────────────
The disposable-database guard (clinical/tests/db_guard.py), demonstrated
end to end in a child pytest rather than asserted on a helper: the property
that matters is that a session with a DSN but no opt-in stops BEFORE it
opens a connection, and only a real session run can show ordering.

The child is pointed at 127.0.0.1:1 -- nothing listens there, so if the
guard were missing or ran late, the child would reach the reachability
gate and print ITS message instead. Nothing in this file can touch a real
database whatever the parent's environment holds: the child's DSN is
always overwritten.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OFFLINE_TARGET = "clinical/tests/test_exception_models.py"
NOWHERE_DSN = "postgresql://guard:guard@127.0.0.1:1/guard_probe"


def _run_child(disposable: bool) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for name in ("CLINICAL_TEST_DB_DISPOSABLE", "CLINICAL_REQUIRE_DB"):
        env.pop(name, None)
    env["CLINICAL_TEST_DSN"] = NOWHERE_DSN
    if disposable:
        env["CLINICAL_TEST_DB_DISPOSABLE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", OFFLINE_TARGET, "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_dsn_without_opt_in_stops_the_session_before_any_connection():
    proc = _run_child(disposable=False)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 4, out
    assert "CLINICAL_TEST_DB_DISPOSABLE=1 is not" in out, out
    assert "DROP SCHEMA public CASCADE" in out, out
    # The reachability gate's banner. Its absence proves the guard ran first:
    # the child never even tried to connect.
    assert "nothing is answering" not in out, out
    # And the password in the DSN never reaches the log.
    assert "guard:guard" not in out, out


def test_dsn_with_opt_in_passes_the_guard_and_reaches_the_reachability_gate():
    proc = _run_child(disposable=True)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 4, out
    assert "CLINICAL_TEST_DB_DISPOSABLE=1 is not" not in out, out
    assert "nothing is answering" in out, out


def test_opt_in_must_be_exactly_one(monkeypatch):
    from clinical.tests import db_guard

    for value in ("true", "yes", " 1", "0", ""):
        monkeypatch.setenv("CLINICAL_TEST_DB_DISPOSABLE", value)
        assert db_guard.is_disposable() is False, value
    monkeypatch.setenv("CLINICAL_TEST_DB_DISPOSABLE", "1")
    assert db_guard.is_disposable() is True
