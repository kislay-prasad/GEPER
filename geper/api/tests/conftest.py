"""
api/tests/conftest.py
─────────────────────
A real clinical PostgreSQL for the API/worker tests that link a pipeline
run to clinical records.

Same contract as clinical/tests, through the same guard
(clinical/tests/db_guard.py):

  - CLINICAL_TEST_DSN unset: tests that need the database SKIP (honest
    locally) -- unless CLINICAL_REQUIRE_DB is set, in which case they FAIL.
    CI sets it in the geper job, so a missing or misnamed service cannot
    turn this coverage into a green row of skips.
  - CLINICAL_TEST_DSN set without CLINICAL_TEST_DB_DISPOSABLE=1: the whole
    session stops before any connection, because `clinical_conn` drops the
    schema of the database the DSN names before every test.

Only tests that request `clinical_conn` (directly or through a fixture
built on it) touch the database; the rest of this package is unaffected.
"""

from __future__ import annotations

import os

import pytest

from clinical.tests import db_guard


@pytest.fixture(scope="session")
def clinical_dsn() -> str:
    dsn = os.getenv(db_guard.DSN_ENV)
    if not dsn:
        if db_guard.require_db():
            pytest.fail(
                f"{db_guard.REQUIRE_DB_ENV} is set but {db_guard.DSN_ENV} is not: this job "
                "must run the clinical-link tests against a real PostgreSQL, not skip them."
            )
        pytest.skip(f"{db_guard.DSN_ENV} not set")
    db_guard.exit_unless_disposable(dsn)
    return dsn


@pytest.fixture
def clinical_conn(clinical_dsn):
    # Imported here, not skipped on absence: psycopg[binary] is a declared
    # geper requirement, so its absence is a broken environment, not a gap.
    import psycopg

    connection = psycopg.connect(clinical_dsn, autocommit=False, connect_timeout=10)
    try:
        db_guard.reset_schema(connection)
        yield connection
    finally:
        connection.close()
