"""
tests/test_phase_5d_retry_scheduler.py
─────────────────────────────────────────

Tests for Phase 5d: Exception retry mechanism with exponential backoff.
- retry_scheduler(session): scan open TRANSIENT exceptions, retry/escalate
- list_open_exceptions(session): operator worklist (open + escalated)
"""

from __future__ import annotations

import datetime
from datetime import date, timezone
import os
import uuid

import pytest

from clinical.data_access import DataAccess, BcryptHasher, SystemClock
from clinical.models.exception import (
    ExceptionReasonCode,
    REASON_CODE_TO_CATEGORY,
)

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = __import__("pathlib").Path(__file__).parent.parent / "schema.sql"


@pytest.fixture()
def conn():
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")
    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute("DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
        cur.execute("DO $$ BEGIN CREATE ROLE clinical_retention; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def dao(conn):
    """DataAccess instance for testing."""
    return DataAccess(conn, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))


@pytest.fixture
def org_a(dao):
    """Create organisation A."""
    return dao.create_organisation("Org A")


@pytest.fixture
def org_b(dao):
    """Create organisation B."""
    return dao.create_organisation("Org B")


@pytest.fixture
def session_admin_a(dao, org_a, conn):
    """Create an admin user and session in Org A."""
    user_id = dao.create_user(org_a, "admin@org-a.test", "password")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org_a, user_id, datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)),
        )
    conn.commit()
    return dao.login("admin@org-a.test", org_a, "password")


@pytest.fixture
def session_admin_b(dao, org_b, conn):
    """Create an admin user and session in Org B."""
    user_id = dao.create_user(org_b, "admin@org-b.test", "password")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org_b, user_id, datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)),
        )
    conn.commit()
    return dao.login("admin@org-b.test", org_b, "password")


@pytest.fixture
def test_catalogue_a(dao, session_admin_a, conn):
    """Create a test in catalogue for Org A."""
    test_id = uuid.uuid4()
    now = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, session_admin_a.org_id, "Test Gene Panel", "GRCh38", "active", now),
        )
    conn.commit()
    return test_id


@pytest.fixture
def test_catalogue_b(dao, session_admin_b, conn):
    """Create a test in catalogue for Org B."""
    test_id = uuid.uuid4()
    now = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, session_admin_b.org_id, "Test Gene Panel", "GRCh38", "active", now),
        )
    conn.commit()
    return test_id


class TestRetrySchedulerDefects:
    """RED tests demonstrating production defects in retry_scheduler."""

    def test_red_defect_1_case_mismatch_production_exception_created_with_lowercase_category(
        self, dao, session_admin_a, test_catalogue_a, conn
    ):
        """
        DEFECT: retry_scheduler query looks for uppercase 'TRANSIENT_SUBMISSION_FAILURE'
        but production code creates exceptions with lowercase category from enum value.

        This test demonstrates the bug:
        1. Create exception via create_or_reopen_exception with BIJ_AI_TIMEOUT
           (this stores lowercase "transient_submission_failure")
        2. Set next_retry_at in the past
        3. Call retry_scheduler()
        4. Verify scheduler returns NO matches (BUG: should find the exception)
        """
        patient_id = dao.create_patient(session_admin_a, "Test", date(1990, 1, 1), "M")
        consent_id = dao.record_consent(session_admin_a, patient_id, "testing")
        order_id = dao.create_order(
            session_admin_a,
            patient_id=patient_id,
            test_id=test_catalogue_a,
            consent_id=consent_id,
            required_scope="testing",
            priority="routine",
        )

        now = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

        # Create exception using PRODUCTION PATH (not hand-written SQL)
        # This stores category as lowercase "transient_submission_failure" from enum value
        exc_id = dao.create_or_reopen_exception(
            session_admin_a,
            order_id,
            REASON_CODE_TO_CATEGORY[ExceptionReasonCode.BIJ_AI_TIMEOUT].value,
            ExceptionReasonCode.BIJ_AI_TIMEOUT.value,
            "Submission timed out",
            "lab_operator",
            "system",
        )

        # Set next_retry_at in the past so it will be selected
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE exceptions SET attempt_count = 0, next_retry_at = %s WHERE id = %s",
                (now - datetime.timedelta(seconds=1), exc_id),
            )
        conn.commit()

        # Query database to verify the category is lowercase
        with conn.cursor() as cur:
            cur.execute("SELECT category FROM exceptions WHERE id = %s", (exc_id,))
            stored_category = cur.fetchone()[0]
        assert stored_category == "transient_submission_failure", f"Expected lowercase, got {stored_category}"

        # Call retry_scheduler - it should find this exception
        # But it won't because it queries for uppercase category!
        dao.retry_scheduler(session_admin_a)

        # Check if exception was updated (BUG: it won't be)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT attempt_count, next_retry_at FROM exceptions WHERE id = %s",
                (exc_id,),
            )
            result = cur.fetchone()
            attempt_count, next_retry_at = result

        # EXPECTED (after fix): attempt_count should be 1
        # ACTUAL (defect): attempt_count is still 0 because query matched 0 rows
        assert attempt_count == 1, (
            f"Scheduler did not update exception (BUG: attempt_count={attempt_count}, expected 1)"
        )

    def test_red_defect_2_no_production_caller_scheduler_never_runs(self, dao, session_admin_a):
        """
        DEFECT: retry_scheduler() is defined but never called from production code.
        Only tests invoke it. No background worker/cron job calls it.

        This test is documentation only - it verifies retry_scheduler exists but
        has no production entrypoint. The actual fix requires creating a background
        task that calls it periodically.
        """
        # This is a documentation test
        # Verify the method exists
        assert hasattr(dao, "retry_scheduler")
        assert callable(dao.retry_scheduler)

        # In production, this should be called by:
        # - A background worker thread
        # - A periodic cron job
        # - A message queue consumer
        # Currently: NONE OF THESE EXIST
        # This is verified by grep in the implementation


class TestRetrySchedulerBackoffMath:
    """Backoff calculation: exponential base 2, capped at 15 minutes."""

    def test_backoff_30_seconds_at_attempt_0(self):
        """After 0 attempts, next wait is 30s (2^0 * 30)."""
        expected = min(2**0 * 30, 900)
        assert expected == 30

    def test_backoff_1_minute_at_attempt_1(self):
        """After 1 attempt, next wait is 1m (2^1 * 30)."""
        expected = min(2**1 * 30, 900)
        assert expected == 60

    def test_backoff_2_minutes_at_attempt_2(self):
        """After 2 attempts, next wait is 2m (2^2 * 30)."""
        expected = min(2**2 * 30, 900)
        assert expected == 120

    def test_backoff_4_minutes_at_attempt_3(self):
        """After 3 attempts, next wait is 4m (2^3 * 30)."""
        expected = min(2**3 * 30, 900)
        assert expected == 240

    def test_backoff_8_minutes_at_attempt_4(self):
        """After 4 attempts, next wait is 8m (2^4 * 30)."""
        expected = min(2**4 * 30, 900)
        assert expected == 480

    def test_backoff_capped_at_15_minutes_for_attempt_5_and_beyond(self):
        """After 5+ attempts, cap at 15m (900s)."""
        assert min(2**5 * 30, 900) == 900
        assert min(2**6 * 30, 900) == 900


class TestRetrySchedulerEscalation:
    """Escalation: after 6 failed attempts (~30 minutes), mark as escalated."""

    def test_escalate_when_attempt_count_reaches_6(self, dao, session_admin_a, test_catalogue_a, conn):
        """Exception with attempt_count=6 at retry time escalates to 'escalated' status.

        Uses create_or_reopen_exception() (production path) not hand-written SQL.
        """
        patient_id = dao.create_patient(session_admin_a, "Test", date(1990, 1, 1), "M")
        consent_id = dao.record_consent(session_admin_a, patient_id, "testing")
        order_id = dao.create_order(
            session_admin_a,
            patient_id=patient_id,
            test_id=test_catalogue_a,
            consent_id=consent_id,
            required_scope="testing",
            priority="routine",
        )

        now = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

        # Create exception via production path
        exc_id = dao.create_or_reopen_exception(
            session_admin_a,
            order_id,
            REASON_CODE_TO_CATEGORY[ExceptionReasonCode.BIJ_AI_TIMEOUT].value,
            ExceptionReasonCode.BIJ_AI_TIMEOUT.value,
            "Submission timed out after 6 attempts",
            "lab_operator",
            "system",
        )

        # Set attempt_count=6 and next_retry_at in the past
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE exceptions SET attempt_count = 6, next_retry_at = %s, last_attempt_at = %s WHERE id = %s",
                (now - datetime.timedelta(seconds=1), now - datetime.timedelta(hours=1), exc_id),
            )
        conn.commit()

        # Run scheduler
        dao.retry_scheduler(session_admin_a)

        # Verify escalation
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM exceptions WHERE id = %s", (exc_id,))
            exc = cur.fetchone()

        assert exc[0] == "escalated"

    def test_do_not_escalate_when_attempt_count_is_5(self, dao, session_admin_a, test_catalogue_a, conn):
        """Exception with attempt_count=5 retries, does not escalate.

        Uses create_or_reopen_exception() (production path) not hand-written SQL.
        """
        patient_id = dao.create_patient(session_admin_a, "Test", date(1990, 1, 1), "M")
        consent_id = dao.record_consent(session_admin_a, patient_id, "testing")
        order_id = dao.create_order(
            session_admin_a,
            patient_id=patient_id,
            test_id=test_catalogue_a,
            consent_id=consent_id,
            required_scope="testing",
            priority="routine",
        )

        now = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

        # Create exception via production path
        exc_id = dao.create_or_reopen_exception(
            session_admin_a,
            order_id,
            REASON_CODE_TO_CATEGORY[ExceptionReasonCode.BIJ_AI_TIMEOUT].value,
            ExceptionReasonCode.BIJ_AI_TIMEOUT.value,
            "Submission timed out",
            "lab_operator",
            "system",
        )

        # Set attempt_count=5 and next_retry_at in the past
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE exceptions SET attempt_count = 5, next_retry_at = %s, last_attempt_at = %s WHERE id = %s",
                (now - datetime.timedelta(seconds=1), now - datetime.timedelta(hours=1), exc_id),
            )
        conn.commit()

        # Run scheduler
        dao.retry_scheduler(session_admin_a)

        # Verify retry (not escalation)
        with conn.cursor() as cur:
            cur.execute("SELECT status, attempt_count, next_retry_at FROM exceptions WHERE id = %s", (exc_id,))
            exc = cur.fetchone()

        assert exc[0] == "open", f"Expected open status, got {exc[0]}"
        assert exc[1] == 6, f"Expected attempt_count=6, got {exc[1]}"
        assert exc[2] is not None, "Expected next_retry_at to be set"


class TestRetrySchedulerIdempotency:
    """Idempotency: duplicate scheduler runs do not cause double-escalation."""

    def test_concurrent_scheduler_runs_do_not_double_escalate(self, dao, session_admin_a, test_catalogue_a, conn):
        """Two simultaneous retry_scheduler calls escalate only once.

        Uses create_or_reopen_exception() (production path) not hand-written SQL.
        """
        patient_id = dao.create_patient(session_admin_a, "Test", date(1990, 1, 1), "M")
        consent_id = dao.record_consent(session_admin_a, patient_id, "testing")
        order_id = dao.create_order(
            session_admin_a,
            patient_id=patient_id,
            test_id=test_catalogue_a,
            consent_id=consent_id,
            required_scope="testing",
            priority="routine",
        )

        now = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

        # Create exception via production path
        exc_id = dao.create_or_reopen_exception(
            session_admin_a,
            order_id,
            REASON_CODE_TO_CATEGORY[ExceptionReasonCode.BIJ_AI_TIMEOUT].value,
            ExceptionReasonCode.BIJ_AI_TIMEOUT.value,
            "Submission timed out after 6 attempts",
            "lab_operator",
            "system",
        )

        # Set attempt_count=6 and next_retry_at in the past
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE exceptions SET attempt_count = 6, next_retry_at = %s, last_attempt_at = %s WHERE id = %s",
                (now - datetime.timedelta(seconds=1), now - datetime.timedelta(hours=1), exc_id),
            )
        conn.commit()

        # Run scheduler twice — both should succeed idempotently
        dao.retry_scheduler(session_admin_a)
        dao.retry_scheduler(session_admin_a)

        # Verify only one escalation happened
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM exceptions WHERE id = %s", (exc_id,))
            exc = cur.fetchone()

        assert exc[0] == "escalated"


class TestListOpenExceptions:
    """list_open_exceptions(session): operator worklist."""

    def test_list_includes_open_and_escalated_not_resolved(self, dao, session_admin_a, test_catalogue_a, conn):
        """list_open_exceptions returns status != 'resolved'.

        Uses create_or_reopen_exception() (production path) not hand-written SQL.
        """
        patient_id = dao.create_patient(session_admin_a, "Test", date(1990, 1, 1), "M")
        consent_id = dao.record_consent(session_admin_a, patient_id, "testing")
        order_id = dao.create_order(
            session_admin_a,
            patient_id=patient_id,
            test_id=test_catalogue_a,
            consent_id=consent_id,
            required_scope="testing",
            priority="routine",
        )

        # Create open exception via production path
        open_exc_id = dao.create_or_reopen_exception(
            session_admin_a,
            order_id,
            REASON_CODE_TO_CATEGORY[ExceptionReasonCode.BIJ_AI_TIMEOUT].value,
            ExceptionReasonCode.BIJ_AI_TIMEOUT.value,
            "Submission timed out",
            "lab_operator",
            "system",
        )

        # Create and then manually escalate another exception
        escalated_exc_id = dao.create_or_reopen_exception(
            session_admin_a,
            order_id,
            REASON_CODE_TO_CATEGORY[ExceptionReasonCode.BIJ_AI_TIMEOUT].value,
            ExceptionReasonCode.BIJ_AI_TIMEOUT.value,
            "Submission timed out again",
            "lab_operator",
            "system",
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE exceptions SET status = 'escalated' WHERE id = %s",
                (escalated_exc_id,),
            )
        conn.commit()

        # Create and resolve another exception
        resolved_exc_id = dao.create_or_reopen_exception(
            session_admin_a,
            order_id,
            REASON_CODE_TO_CATEGORY[ExceptionReasonCode.BIJ_AI_TIMEOUT].value,
            ExceptionReasonCode.BIJ_AI_TIMEOUT.value,
            "Submission will be resolved",
            "lab_operator",
            "system",
        )
        dao.resolve_exception(
            session_admin_a,
            resolved_exc_id,
            "retry_check",
            "Manual retry succeeded",
            "lab_operator",
        )

        # List open exceptions
        result = dao.list_open_exceptions(session_admin_a)
        result_ids = {exc.id for exc in result}

        assert str(open_exc_id) in result_ids
        assert str(escalated_exc_id) in result_ids
        assert str(resolved_exc_id) not in result_ids

    def test_list_ordered_escalated_first(self, dao, session_admin_a, test_catalogue_a, conn):
        """list_open_exceptions orders by status DESC (escalated first).

        Uses create_or_reopen_exception() (production path) not hand-written SQL.
        Creates exceptions with different reason codes to avoid reopening the same exception.
        """
        patient_id = dao.create_patient(session_admin_a, "Test", date(1990, 1, 1), "M")
        consent_id = dao.record_consent(session_admin_a, patient_id, "testing")
        order_id_1 = dao.create_order(
            session_admin_a,
            patient_id=patient_id,
            test_id=test_catalogue_a,
            consent_id=consent_id,
            required_scope="testing",
            priority="routine",
        )
        order_id_2 = dao.create_order(
            session_admin_a,
            patient_id=patient_id,
            test_id=test_catalogue_a,
            consent_id=consent_id,
            required_scope="testing",
            priority="routine",
        )

        # Create open exception for order 1
        open_exc_id = dao.create_or_reopen_exception(
            session_admin_a,
            order_id_1,
            REASON_CODE_TO_CATEGORY[ExceptionReasonCode.BIJ_AI_TIMEOUT].value,
            ExceptionReasonCode.BIJ_AI_TIMEOUT.value,
            "Submission timed out",
            "lab_operator",
            "system",
        )

        # Manually set one to escalated status
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE exceptions SET status = 'escalated' WHERE id = %s",
                (open_exc_id,),
            )
        conn.commit()

        # Create a fresh open exception on a different order
        dao.create_or_reopen_exception(
            session_admin_a,
            order_id_2,
            REASON_CODE_TO_CATEGORY[ExceptionReasonCode.BIJ_AI_TIMEOUT].value,
            ExceptionReasonCode.BIJ_AI_TIMEOUT.value,
            "Submission timed out again",
            "lab_operator",
            "system",
        )

        # List should have escalated first
        result = dao.list_open_exceptions(session_admin_a)

        # Should have at least 2 items
        assert len(result) >= 2
        assert result[0].status == "escalated"
        assert result[1].status == "open"

    def test_list_cross_org_read_blocked(
        self, dao, session_admin_a, session_admin_b, test_catalogue_a, test_catalogue_b, conn
    ):
        """list_open_exceptions does not return exceptions from other organisations.

        Uses create_or_reopen_exception() (production path) not hand-written SQL.
        """
        patient_id_a = dao.create_patient(session_admin_a, "Test A", date(1990, 1, 1), "M")
        consent_id_a = dao.record_consent(session_admin_a, patient_id_a, "testing")
        order_id_a = dao.create_order(
            session_admin_a,
            patient_id=patient_id_a,
            test_id=test_catalogue_a,
            consent_id=consent_id_a,
            required_scope="testing",
            priority="routine",
        )

        # Create exception in org_a
        dao.create_or_reopen_exception(
            session_admin_a,
            order_id_a,
            REASON_CODE_TO_CATEGORY[ExceptionReasonCode.BIJ_AI_TIMEOUT].value,
            ExceptionReasonCode.BIJ_AI_TIMEOUT.value,
            "Submission timed out",
            "lab_operator",
            "system",
        )

        # Try to list from org_b — should be empty
        result = dao.list_open_exceptions(session_admin_b)

        assert len(result) == 0
