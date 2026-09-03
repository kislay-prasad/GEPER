"""
tests/test_exception_endpoints.py
─────────────────────────────────
Tests for exception workflow endpoints (get, worklist, resolve).
"""

import datetime
import os
import pytest
import uuid

from clinical.data_access import DataAccess, BcryptHasher, SystemClock
from clinical.models.exception import (
    ExceptionCategory,
    ExceptionReasonCode,
    ExceptionStatus,
    ExceptionEventAction,
    ResolutionAction,
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
def session_admin(dao, org_a, conn):
    """Create an admin user and session in Org A."""
    user_id = dao.create_user(org_a, "admin@org-a.test", "password")
    # Insert admin role directly to break bootstrap cycle
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org_a, user_id, datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)),
        )
    conn.commit()
    return dao.login("admin@org-a.test", org_a, "password")


@pytest.fixture
def test_catalogue(dao, session_admin, conn):
    """Create a test in the catalogue."""
    test_id = uuid.uuid4()
    now = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, session_admin.org_id, "Test Gene Panel", "GRCh38", "active", now),
        )
    conn.commit()
    return test_id


@pytest.fixture
def patient_and_order(dao, session_admin, test_catalogue, conn):
    """Create a patient and order for testing."""
    patient_id = dao.create_patient(session_admin, "Test Patient", datetime.date(1990, 1, 1), "M")
    consent_id = dao.record_consent(session_admin, patient_id, "testing")
    order_id = dao.create_order(
        session_admin,
        patient_id=patient_id,
        test_id=test_catalogue,
        required_scope="testing",
        consent_id=consent_id,
        priority="routine",
    )
    return patient_id, order_id


class TestGetExceptionsForOrder:
    """Test GET /orders/{id}/exceptions endpoint."""

    def test_get_exceptions_returns_empty_for_order_with_no_exceptions(
        self, dao, session_admin, patient_and_order, conn
    ):
        """Order with no exceptions returns empty list."""
        _, order_id = patient_and_order
        exceptions = dao.get_exceptions_for_order(session_admin, order_id)
        assert exceptions == []

    def test_get_exceptions_returns_open_and_resolved(self, dao, session_admin, patient_and_order, conn):
        """Retrieve both open and resolved exceptions for an order."""
        _, order_id = patient_and_order

        # Create open exception
        open_exc_id = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO exceptions (id, org_id, order_id, category, reason_code, "
                "                        error_message, status, owner, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    open_exc_id,
                    session_admin.org_id,
                    order_id,
                    ExceptionCategory.PRECONDITION_FAILURE.value,
                    ExceptionReasonCode.CONSENT_MISSING.value,
                    "Consent not provided",
                    ExceptionStatus.OPEN.value,
                    "orderer",
                    datetime.datetime.now(tz=datetime.timezone.utc),
                ),
            )
        conn.commit()

        # Create resolved exception
        resolved_exc_id = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO exceptions (id, org_id, order_id, category, reason_code, "
                "                        error_message, status, owner, created_at, "
                "                        last_resolved_by, last_resolved_at, "
                "                        resolution_action, resolution_note) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    resolved_exc_id,
                    session_admin.org_id,
                    order_id,
                    ExceptionCategory.PRECONDITION_FAILURE.value,
                    ExceptionReasonCode.CONSENT_MISSING.value,
                    "Consent not provided",
                    ExceptionStatus.RESOLVED.value,
                    "orderer",
                    datetime.datetime.now(tz=datetime.timezone.utc),
                    "orderer-1",
                    datetime.datetime.now(tz=datetime.timezone.utc),
                    ResolutionAction.RETRY_CHECK.value,
                    "Consent obtained",
                ),
            )
        conn.commit()

        exceptions = dao.get_exceptions_for_order(session_admin, order_id)

        assert len(exceptions) == 2
        statuses = {exc.status for exc in exceptions}
        assert "open" in statuses
        assert "resolved" in statuses


class TestGetOpenExceptionsByOwner:
    """Test GET /exceptions/worklist endpoint."""

    def test_worklist_returns_empty_for_owner_with_no_open_exceptions(self, dao, session_admin):
        """Worklist returns empty when no open exceptions."""
        exceptions = dao.get_open_exceptions_by_owner(session_admin, "lab_operator")
        assert exceptions == []

    def test_worklist_returns_only_open_exceptions_for_owner(
        self, dao, session_admin, patient_and_order, test_catalogue, conn
    ):
        """Worklist returns only open exceptions owned by the queried role."""
        _, order_id_1 = patient_and_order

        # Create another order for the resolved exception
        patient_id, _ = patient_and_order
        consent_id = dao.record_consent(session_admin, patient_id, "testing")
        order_id_2 = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_catalogue,
            required_scope="testing",
            consent_id=consent_id,
            priority="routine",
        )

        # Create open exception for lab_operator
        open_exc_id = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO exceptions (id, org_id, order_id, category, reason_code, "
                "                        status, owner, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    open_exc_id,
                    session_admin.org_id,
                    order_id_1,
                    ExceptionCategory.VALIDATION_FAILURE.value,
                    ExceptionReasonCode.VCF_INVALID.value,
                    ExceptionStatus.OPEN.value,
                    "lab_operator",
                    datetime.datetime.now(tz=datetime.timezone.utc),
                ),
            )
        conn.commit()

        # Create resolved exception for lab_operator (should not appear)
        resolved_exc_id = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO exceptions (id, org_id, order_id, category, reason_code, "
                "                        status, owner, created_at, last_resolved_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    resolved_exc_id,
                    session_admin.org_id,
                    order_id_2,
                    ExceptionCategory.VALIDATION_FAILURE.value,
                    ExceptionReasonCode.VCF_INVALID.value,
                    ExceptionStatus.RESOLVED.value,
                    "lab_operator",
                    datetime.datetime.now(tz=datetime.timezone.utc),
                    "lab-operator-1",
                ),
            )
        conn.commit()

        exceptions = dao.get_open_exceptions_by_owner(session_admin, "lab_operator")

        assert len(exceptions) == 1
        assert exceptions[0].status == "open"
        assert exceptions[0].owner == "lab_operator"


class TestResolveException:
    """Test PATCH /exceptions/{id} endpoint."""

    def test_resolve_exception_updates_status_and_creates_event(self, dao, session_admin, patient_and_order, conn):
        """Resolve exception creates event and updates status atomically."""
        _, order_id = patient_and_order
        exc_id = uuid.uuid4()

        # Create open exception
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO exceptions (id, org_id, order_id, category, reason_code, "
                "                        status, owner, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    exc_id,
                    session_admin.org_id,
                    order_id,
                    ExceptionCategory.PRECONDITION_FAILURE.value,
                    ExceptionReasonCode.CONSENT_MISSING.value,
                    ExceptionStatus.OPEN.value,
                    "orderer",
                    datetime.datetime.now(tz=datetime.timezone.utc),
                ),
            )
        conn.commit()

        # Resolve it
        dao.resolve_exception(
            session_admin,
            exc_id,
            ResolutionAction.RETRY_CHECK.value,
            "Consent obtained from patient",
            "orderer-1",
        )

        # Verify status updated
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, last_resolved_by, resolution_action, resolution_note "
                "FROM exceptions WHERE id = %s AND org_id = %s",
                (exc_id, session_admin.org_id),
            )
            exc = cur.fetchone()

        assert exc[0] == ExceptionStatus.RESOLVED.value
        assert exc[1] == "orderer-1"
        assert exc[2] == ResolutionAction.RETRY_CHECK.value
        assert "Consent obtained" in exc[3]

        # Verify event created
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, actor FROM exception_events WHERE exception_id = %s",
                (exc_id,),
            )
            events = cur.fetchall()

        assert len(events) == 1
        assert events[0][0] == ExceptionEventAction.RESOLVE.value
        assert events[0][1] == "orderer-1"
