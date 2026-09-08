"""
tests/test_worker_clinical_dsn_fail_closed.py
─────────────────────────────────────────────
F4 (2026-09-08): CLINICAL_DSN had OPPOSITE policies in two adjacent
files -- exception_retry_worker.py refused to start without it, while
submission_worker.py logged a warning and carried on, processing
submissions and silently creating no clinical exception records at all.
Same failure, two policies, chosen by nobody.

These tests pin the refusing policy in BOTH workers, and they cover BOTH
routes to the silent-nothing state, not just the obvious one:

  route 1  CLINICAL_DSN unset
  route 2  CLINICAL_DSN set, but the connection/import fails

Route 2 matters on its own: fixing only route 1 would have left a real
database outage still downgraded to a warning, reaching the identical
end state one step later.

Each test asserts the worker DOES NOT REACH run_loop(), not merely that
it logged something -- a warning nobody reads and an error nobody reads
fail identically, so the log level is not the control being tested.
"""

import sys
import types
from unittest.mock import patch

import pytest

from api import exception_retry_worker, submission_worker


def _fake_clinical_data_access():
    """A stand-in for the `clinical.data_access` module (see below)."""
    module = types.ModuleType("clinical.data_access")
    module.DataAccess = lambda connection: object()
    return module


@pytest.fixture
def store_path(tmp_path, monkeypatch):
    """Point both workers' SubmissionStore at a throwaway file."""
    monkeypatch.setenv("GEPER_SUBMISSION_STORE_PATH", str(tmp_path / "submissions.db"))
    return tmp_path


@pytest.fixture
def no_dsn(monkeypatch):
    monkeypatch.delenv("CLINICAL_DSN", raising=False)


class TestSubmissionWorkerFailsClosed:
    """geper/api/submission_worker.py::main"""

    def test_refuses_to_start_when_clinical_dsn_unset(self, store_path, no_dsn):
        with patch.object(submission_worker.InterpretationWorker, "run_loop") as run_loop:
            with pytest.raises(SystemExit) as exc:
                submission_worker.main()

        assert exc.value.code == 1, "must exit non-zero so a supervisor sees a failure"
        # The worker must not process a single submission.
        run_loop.assert_not_called()

    def test_refuses_to_start_when_connection_fails(self, store_path, monkeypatch):
        """CLINICAL_DSN is set, but the database is unreachable.

        Previously an `except Exception` downgraded this to a warning and the
        worker carried on with data_access=None -- the same end state as an
        unset DSN, reached by a different route.
        """
        monkeypatch.setenv("CLINICAL_DSN", "postgresql://nobody@127.0.0.1:1/nonexistent")

        import psycopg

        def boom(*args, **kwargs):
            raise psycopg.OperationalError("connection refused (simulated)")

        with patch.object(psycopg, "connect", side_effect=boom):
            with patch.object(submission_worker.InterpretationWorker, "run_loop") as run_loop:
                with pytest.raises(SystemExit) as exc:
                    submission_worker.main()

        assert exc.value.code == 1
        run_loop.assert_not_called()

    def test_starts_normally_when_dsn_is_usable(self, store_path, monkeypatch):
        """The refusal must not fire on the healthy path.

        Without this, a fail-closed change that broke startup outright would
        still pass both tests above.
        """
        monkeypatch.setenv("CLINICAL_DSN", "postgresql://user@localhost/clinical")

        import psycopg

        # `clinical` is a sibling package of `geper` and is not importable with
        # the suite's rootdir, so the real import would raise ImportError and
        # (correctly) trip the new refusal. Stub it, so this test exercises the
        # healthy path rather than route 2 a second time.
        with patch.dict(sys.modules, {"clinical.data_access": _fake_clinical_data_access()}):
            with patch.object(psycopg, "connect", return_value=object()):
                with patch.object(submission_worker.InterpretationWorker, "run_loop") as run_loop:
                    submission_worker.main()

        run_loop.assert_called_once()


class TestExceptionRetryWorkerFailsClosed:
    """geper/api/exception_retry_worker.py::main

    This worker already declined to run without CLINICAL_DSN, but it did so
    with a bare `return`, which exits 0. A process supervisor reads that as a
    clean, successful run: the retry lifecycle stops dead with nothing
    restarting it and nothing alerting. The refusal now exits non-zero.
    """

    def test_refuses_and_exits_nonzero_when_clinical_dsn_unset(self, store_path, no_dsn):
        with patch.object(exception_retry_worker.ExceptionRetryWorker, "run_loop") as run_loop:
            with pytest.raises(SystemExit) as exc:
                exception_retry_worker.main()

        assert exc.value.code == 1, "exiting 0 here would report a refused startup as a successful run"
        run_loop.assert_not_called()

    def test_refuses_and_exits_nonzero_when_connection_fails(self, store_path, monkeypatch):
        monkeypatch.setenv("CLINICAL_DSN", "postgresql://nobody@127.0.0.1:1/nonexistent")

        import psycopg

        def boom(*args, **kwargs):
            raise psycopg.OperationalError("connection refused (simulated)")

        with patch.object(psycopg, "connect", side_effect=boom):
            with patch.object(exception_retry_worker.ExceptionRetryWorker, "run_loop") as run_loop:
                with pytest.raises(SystemExit) as exc:
                    exception_retry_worker.main()

        assert exc.value.code == 1
        run_loop.assert_not_called()


def test_no_worker_reads_clinical_dsn_without_refusing():
    """Guard against a third site growing the old warn-and-continue shape.

    Not a behavioural test -- a source check, deliberately. The defect F4
    fixed was a POLICY DIVERGENCE between two files, which no single-file
    behavioural test can detect; it only becomes visible when you look at
    every reader of the variable at once.
    """
    import pathlib

    api_dir = pathlib.Path(exception_retry_worker.__file__).parent
    readers = sorted(p.name for p in api_dir.glob("*.py") if "CLINICAL_DSN" in p.read_text(encoding="utf-8"))
    assert readers == ["exception_retry_worker.py", "submission_worker.py"], (
        f"a new reader of CLINICAL_DSN appeared: {readers}. Give it the same "
        "fail-closed policy as the other two, or this test is the only thing "
        "that will ever notice the divergence."
    )
