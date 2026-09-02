"""
clinical/tests/conftest.py
───────────────────────────
The fail-not-skip guard.

THE DEFECT THIS EXISTS FOR: twenty-two tests that skip and twenty-two that
pass produce the same exit code, and CI reports both as a green job. A suite
that silently declines to run is worse than no suite, because the gap stops
being visible -- the dashboard says covered.

So: when CLINICAL_REQUIRE_DB is set (CI sets it), every skip in this package
becomes a failure, and a missing DSN fails at collection before any test
runs. Locally the variable is unset and skipping still works, because a
developer without Postgres should be able to run TestStructuralIsolation
without the suite exploding -- there, a skip really is an honest gap and it
is reported as one.

Deliberately broader than "skips caused by the database": under
CLINICAL_REQUIRE_DB ANY skip fails the run. A skip introduced later for some
other reason would be an equally silent hole in the coverage this job exists
to guarantee, and having to add the exception explicitly is the point.
"""

from __future__ import annotations

import os

import pytest

REQUIRE_DB_ENV = "CLINICAL_REQUIRE_DB"
DSN_ENV = "CLINICAL_TEST_DSN"


def _require_db() -> bool:
    return os.getenv(REQUIRE_DB_ENV, "").strip() not in ("", "0", "false", "False")


def pytest_configure(config: pytest.Config) -> None:
    """
    Fail at collection, not at test time, when the job demands a database and
    has not been given one. UsageError exits pytest with code 4 before a
    single test runs, so a misconfigured CI job cannot report a green
    "0 failed" for a suite it never executed.
    """
    if not _require_db():
        return
    if not os.getenv(DSN_ENV):
        raise pytest.UsageError(
            f"{REQUIRE_DB_ENV} is set but {DSN_ENV} is not. This job exists to run the "
            "identity suite against a real PostgreSQL; refusing to run a suite that "
            "would skip and report success."
        )


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_makereport(item, call):
    """
    Convert every skip into a failure while CLINICAL_REQUIRE_DB is set.

    A hookwrapper on the report rather than a collection-time check on
    purpose: it catches skips from every source -- the requires_db marker,
    `pytest.importorskip` for psycopg, a `pytest.skip()` inside a fixture,
    an xfail that turned into a skip. A collection-time check only sees the
    markers, and the ones that bite are the runtime ones.
    """
    outcome = yield
    if not _require_db():
        return
    report = outcome.get_result()
    if report.skipped:
        report.outcome = "failed"
        reason = getattr(report, "longrepr", None)
        report.longrepr = (
            f"SKIPPED WITH {REQUIRE_DB_ENV} SET -- treated as a failure.\n"
            f"This job must actually execute every test; a skip here is a silent "
            f"coverage hole, not an honest gap.\nOriginal skip reason: {reason}"
        )
