"""
api/tests/test_submission_store_connection_leak.py
──────────────────────────────────────────────────
`SubmissionStore` must not leave SQLite connections open after a call.

WHY THIS TEST IS NOT A WINDOWS TEST, WHICH IS THE WHOLE POINT.

The symptom that exposed this is Windows-only: 32 teardown ERRORs across
`api/tests/`, all `PermissionError: [WinError 32]` raised by
`TemporaryDirectory` cleanup, because Windows refuses to unlink a file
that is still open. Linux CI has never reported one of them.

It would be easy, and wrong, to conclude the defect is Windows-specific.
The connection is left open on EVERY platform. POSIX simply permits
unlinking an open file, so nothing there ever complains. Windows is the
DETECTOR, not the defect.

So this test asserts the resource property directly -- no connection
survives the call -- rather than asserting that a temporary directory can
be deleted. Written the second way it would pass vacuously on Linux and
could never fail there, which would leave CI structurally incapable of
catching this class of bug. Written this way it fails on both.

THE MECHANISM, measured rather than reasoned:

    with sqlite3.connect(path) as conn:
        ...

does NOT close the connection. sqlite3's context manager commits or rolls
back the TRANSACTION and leaves the connection open -- documented, and
easy to misread as `open()`-style cleanup. The connection then survives in
a reference cycle: plain refcounting does not reclaim it (measured -- the
file stays locked after the block), and only a `gc.collect()` pass does.
That makes the release time non-deterministic on every platform, which is
what distinguishes this from a cosmetic complaint.

`gc.collect()` is therefore deliberately NOT called before counting. This
test asks whether the connection was closed, not whether the garbage
collector has got round to it yet.
"""

import gc
import sqlite3
import tempfile
from pathlib import Path

import pytest

from api.submission_store import SubmissionStore


def _live_connections() -> list:
    """Every sqlite3.Connection the interpreter is still holding."""
    return [o for o in gc.get_objects() if isinstance(o, sqlite3.Connection)]


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "leak.db"


def _assert_no_new_connections(before: int, operation: str) -> None:
    after = len(_live_connections())
    assert after == before, (
        f"{operation} left {after - before} SQLite connection(s) open. "
        "sqlite3's `with` block commits the transaction; it does not close "
        "the connection. Wrap the connect in contextlib.closing()."
    )


class TestSubmissionStoreClosesConnections:
    def test_init_closes_its_connection(self, db_path):
        before = len(_live_connections())
        SubmissionStore(db_path)
        _assert_no_new_connections(before, "SubmissionStore.__init__")

    def test_create_submission_closes_its_connection(self, db_path):
        store = SubmissionStore(db_path)
        before = len(_live_connections())
        store.create_submission(
            org_id="org-1",
            submission_key="key-1",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            consent_ref="consent-1",
        )
        _assert_no_new_connections(before, "create_submission")

    def test_read_paths_close_their_connections(self, db_path):
        store = SubmissionStore(db_path)
        submission = store.create_submission(
            org_id="org-1",
            submission_key="key-1",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            consent_ref="consent-1",
        )
        before = len(_live_connections())
        store.get_submission(submission.submission_id)
        store.get_queued_submissions()
        _assert_no_new_connections(before, "get_submission/get_queued_submissions")
