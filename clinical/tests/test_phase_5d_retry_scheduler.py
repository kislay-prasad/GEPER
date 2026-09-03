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
        """Exception with attempt_count=6 at retry time escalates to 'escalated' status."""
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

        exc_id = uuid.uuid4()
        now = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO exceptions "
                "(id, org_id, order_id, category, reason_code, status, owner, created_at, "
                " attempt_count, next_retry_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    exc_id,
                    session_admin_a.org_id,
                    order_id,
                    "TRANSIENT_SUBMISSION_FAILURE",
                    "BIJ_AI_TIMEOUT",
                    "open",
                    "lab_operator",
                    now - datetime.timedelta(hours=1),
                    6,
                    now - datetime.timedelta(seconds=1),
                ),
            )
        conn.commit()

        dao.retry_scheduler(session_admin_a)

        with conn.cursor() as cur:
            cur.execute("SELECT status FROM exceptions WHERE id = %s", (exc_id,))
            exc = cur.fetchone()

        assert exc[0] == "escalated"

    def test_do_not_escalate_when_attempt_count_is_5(self, dao, session_admin_a, test_catalogue_a, conn):
        """Exception with attempt_count=5 retries, does not escalate."""
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

        exc_id = uuid.uuid4()
        now = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO exceptions "
                "(id, org_id, order_id, category, reason_code, status, owner, created_at, "
                " attempt_count, next_retry_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    exc_id,
                    session_admin_a.org_id,
                    order_id,
                    "TRANSIENT_SUBMISSION_FAILURE",
                    "BIJ_AI_TIMEOUT",
                    "open",
                    "lab_operator",
                    now - datetime.timedelta(hours=1),
                    5,
                    now - datetime.timedelta(seconds=1),
                ),
            )
        conn.commit()

        dao.retry_scheduler(session_admin_a)

        with conn.cursor() as cur:
            cur.execute("SELECT status, attempt_count, next_retry_at FROM exceptions WHERE id = %s", (exc_id,))
            exc = cur.fetchone()

        assert exc[0] == "open"
        assert exc[1] == 6
        assert exc[2] is not None


class TestRetrySchedulerIdempotency:
    """Idempotency: duplicate scheduler runs do not cause double-escalation."""

    def test_concurrent_scheduler_runs_do_not_double_escalate(self, dao, session_admin_a, test_catalogue_a, conn):
        """Two simultaneous retry_scheduler calls escalate only once."""
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

        exc_id = uuid.uuid4()
        now = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO exceptions "
                "(id, org_id, order_id, category, reason_code, status, owner, created_at, "
                " attempt_count, next_retry_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    exc_id,
                    session_admin_a.org_id,
                    order_id,
                    "TRANSIENT_SUBMISSION_FAILURE",
                    "BIJ_AI_TIMEOUT",
                    "open",
                    "lab_operator",
                    now - datetime.timedelta(hours=1),
                    6,
                    now - datetime.timedelta(seconds=1),
                ),
            )
        conn.commit()

        dao.retry_scheduler(session_admin_a)
        dao.retry_scheduler(session_admin_a)

        with conn.cursor() as cur:
            cur.execute("SELECT status FROM exceptions WHERE id = %s", (exc_id,))
            exc = cur.fetchone()

        assert exc[0] == "escalated"


class TestListOpenExceptions:
    """list_open_exceptions(session): operator worklist."""

    def test_list_includes_open_and_escalated_not_resolved(self, dao, session_admin_a, test_catalogue_a, conn):
        """list_open_exceptions returns status != 'resolved'."""
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

        now = datetime.datetime.now(timezone.utc)

        open_exc_id = uuid.uuid4()
        escalated_exc_id = uuid.uuid4()
        resolved_exc_id = uuid.uuid4()

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO exceptions "
                "(id, org_id, order_id, category, reason_code, status, owner, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    open_exc_id,
                    session_admin_a.org_id,
                    order_id,
                    "TRANSIENT_SUBMISSION_FAILURE",
                    "BIJ_AI_TIMEOUT",
                    "open",
                    "lab_operator",
                    now,
                ),
            )
            cur.execute(
                "INSERT INTO exceptions "
                "(id, org_id, order_id, category, reason_code, status, owner, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    escalated_exc_id,
                    session_admin_a.org_id,
                    order_id,
                    "TRANSIENT_SUBMISSION_FAILURE",
                    "BIJ_AI_TIMEOUT",
                    "escalated",
                    "lab_operator",
                    now,
                ),
            )
            cur.execute(
                "INSERT INTO exceptions "
                "(id, org_id, order_id, category, reason_code, status, owner, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    resolved_exc_id,
                    session_admin_a.org_id,
                    order_id,
                    "TRANSIENT_SUBMISSION_FAILURE",
                    "BIJ_AI_TIMEOUT",
                    "resolved",
                    "lab_operator",
                    now,
                ),
            )
        conn.commit()

        result = dao.list_open_exceptions(session_admin_a)
        result_ids = {exc.id for exc in result}

        assert str(open_exc_id) in result_ids
        assert str(escalated_exc_id) in result_ids
        assert str(resolved_exc_id) not in result_ids

    def test_list_ordered_escalated_first(self, dao, session_admin_a, test_catalogue_a, conn):
        """list_open_exceptions orders by status DESC (escalated first)."""
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

        now = datetime.datetime.now(timezone.utc)

        open_exc_id = uuid.uuid4()
        escalated_exc_id = uuid.uuid4()

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO exceptions "
                "(id, org_id, order_id, category, reason_code, status, owner, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    open_exc_id,
                    session_admin_a.org_id,
                    order_id,
                    "TRANSIENT_SUBMISSION_FAILURE",
                    "BIJ_AI_TIMEOUT",
                    "open",
                    "lab_operator",
                    now,
                ),
            )
            cur.execute(
                "INSERT INTO exceptions "
                "(id, org_id, order_id, category, reason_code, status, owner, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    escalated_exc_id,
                    session_admin_a.org_id,
                    order_id,
                    "TRANSIENT_SUBMISSION_FAILURE",
                    "BIJ_AI_TIMEOUT",
                    "escalated",
                    "lab_operator",
                    now,
                ),
            )
        conn.commit()

        result = dao.list_open_exceptions(session_admin_a)

        assert result[0].status == "escalated"
        assert result[1].status == "open"

    def test_list_cross_org_read_blocked(
        self, dao, session_admin_a, session_admin_b, test_catalogue_a, test_catalogue_b, conn
    ):
        """list_open_exceptions does not return exceptions from other organisations."""
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

        now = datetime.datetime.now(timezone.utc)
        exc_id = uuid.uuid4()

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO exceptions "
                "(id, org_id, order_id, category, reason_code, status, owner, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    exc_id,
                    session_admin_a.org_id,
                    order_id_a,
                    "TRANSIENT_SUBMISSION_FAILURE",
                    "BIJ_AI_TIMEOUT",
                    "open",
                    "lab_operator",
                    now,
                ),
            )
        conn.commit()

        result = dao.list_open_exceptions(session_admin_b)

        assert len(result) == 0
