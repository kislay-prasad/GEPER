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


@pytest.fixture
def org_b_and_session(dao, conn):
    """Create organisation B and admin session."""
    org_b = dao.create_organisation("Org B")
    user_id = dao.create_user(org_b, "admin@org-b.test", "password")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org_b, user_id, datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)),
        )
    conn.commit()
    return org_b, dao.login("admin@org-b.test", org_b, "password")


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

        # Create open exception using the method
        dao.create_or_reopen_exception(
            session_admin,
            order_id,
            ExceptionCategory.PRECONDITION_FAILURE.value,
            ExceptionReasonCode.CONSENT_MISSING.value,
            "Consent not provided",
            "orderer",
            "orderer-1",
        )

        # Create and resolve another exception
        resolved_exc_id = dao.create_or_reopen_exception(
            session_admin,
            order_id,
            ExceptionCategory.VALIDATION_FAILURE.value,
            ExceptionReasonCode.VCF_INVALID.value,
            "VCF invalid",
            "lab_operator",
            "lab-1",
        )
        dao.resolve_exception(
            session_admin,
            resolved_exc_id,
            ResolutionAction.RETRY_CHECK.value,
            "Consent obtained",
            "orderer-1",
        )

        exceptions = dao.get_exceptions_for_order(session_admin, order_id)

        assert len(exceptions) == 2
        statuses = {exc.status for exc in exceptions}
        assert "open" in statuses
        assert "resolved" in statuses

    def test_get_exceptions_cross_org_read_blocked(
        self, dao, session_admin, patient_and_order, org_b_and_session, conn
    ):
        """Cross-org read attempt returns nothing (org-scoping enforced)."""
        _, order_id = patient_and_order
        org_b, session_org_b = org_b_and_session

        # Create exception in Org A
        exc_id = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO exceptions (id, org_id, order_id, category, reason_code, "
                "                        error_message, status, owner, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    exc_id,
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

        # Try to read from Org B using same order_id (cross-org read)
        # Should return empty (org-scoping enforced)
        exceptions = dao.get_exceptions_for_order(session_org_b, order_id)
        assert exceptions == [], "Org B should not see Org A's exceptions"


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

        # Create open exception for lab_operator using the method
        dao.create_or_reopen_exception(
            session_admin,
            order_id_1,
            ExceptionCategory.VALIDATION_FAILURE.value,
            ExceptionReasonCode.VCF_INVALID.value,
            "VCF invalid",
            "lab_operator",
            "lab-1",
        )

        # Create and resolve another exception for lab_operator (should not appear)
        resolved_exc_id = dao.create_or_reopen_exception(
            session_admin,
            order_id_2,
            ExceptionCategory.VALIDATION_FAILURE.value,
            ExceptionReasonCode.VCF_INVALID.value,
            "VCF invalid",
            "lab_operator",
            "lab-1",
        )
        dao.resolve_exception(
            session_admin,
            resolved_exc_id,
            ResolutionAction.RETRY_CHECK.value,
            "Fixed VCF",
            "lab-operator-1",
        )

        exceptions = dao.get_open_exceptions_by_owner(session_admin, "lab_operator")

        assert len(exceptions) == 1
        assert exceptions[0].status == "open"
        assert exceptions[0].owner == "lab_operator"

    def test_worklist_cross_org_read_blocked(self, dao, session_admin, patient_and_order, org_b_and_session, conn):
        """Cross-org worklist read returns nothing (org-scoping enforced)."""
        _, order_id = patient_and_order
        org_b, session_org_b = org_b_and_session

        # Create open exception in Org A
        open_exc_id = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO exceptions (id, org_id, order_id, category, reason_code, "
                "                        status, owner, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    open_exc_id,
                    session_admin.org_id,
                    order_id,
                    ExceptionCategory.VALIDATION_FAILURE.value,
                    ExceptionReasonCode.VCF_INVALID.value,
                    ExceptionStatus.OPEN.value,
                    "lab_operator",
                    datetime.datetime.now(tz=datetime.timezone.utc),
                ),
            )
        conn.commit()

        # Query same owner/role from Org B (cross-org read)
        # Should return empty (org-scoping enforced)
        exceptions = dao.get_open_exceptions_by_owner(session_org_b, "lab_operator")
        assert exceptions == [], "Org B should not see Org A's worklist"


class TestResolveException:
    """Test PATCH /exceptions/{id} endpoint."""

    def test_resolve_exception_updates_status_and_creates_event(self, dao, session_admin, patient_and_order, conn):
        """Resolve exception creates event and updates status atomically."""
        _, order_id = patient_and_order

        # Create open exception using the method
        exc_id = dao.create_or_reopen_exception(
            session_admin,
            order_id,
            ExceptionCategory.PRECONDITION_FAILURE.value,
            ExceptionReasonCode.CONSENT_MISSING.value,
            "Consent not provided",
            "orderer",
            "orderer-1",
        )

        # Resolve it
        dao.resolve_exception(
            session_admin,
            exc_id,
            ResolutionAction.RETRY_CHECK.value,
            "Consent obtained from patient",
            "orderer-1",
        )

        # Verify via get_exceptions_for_order that the resolution was recorded
        exceptions = dao.get_exceptions_for_order(session_admin, order_id)
        assert len(exceptions) == 1
        exc_dto = exceptions[0]

        assert exc_dto.status == ExceptionStatus.RESOLVED.value
        assert exc_dto.last_resolved_by == "orderer-1"
        assert exc_dto.resolution_action == ResolutionAction.RETRY_CHECK.value
        assert "Consent obtained" in exc_dto.resolution_note

        # Verify event was recorded (get the full exception with events)
        events = exc_dto.events
        assert len(events) == 2  # OPEN event + RESOLVE event
        assert events[0].action == ExceptionEventAction.OPEN.value
        assert events[1].action == ExceptionEventAction.RESOLVE.value
        assert events[1].actor == "orderer-1"


class TestCreateOrReopenException:
    """Test exception creation and reopening logic."""

    def test_create_exception_on_precondition_failure(self, dao, session_admin, patient_and_order, conn):
        """Create new exception for precondition failure."""
        _, order_id = patient_and_order

        exc_id = dao.create_or_reopen_exception(
            session_admin,
            order_id,
            ExceptionCategory.PRECONDITION_FAILURE.value,
            ExceptionReasonCode.CONSENT_MISSING.value,
            "Consent not provided",
            "orderer",
            "orderer-1",
        )

        # Verify exception created
        with conn.cursor() as cur:
            cur.execute(
                "SELECT category, reason_code, status, owner FROM exceptions WHERE id = %s AND org_id = %s",
                (exc_id, session_admin.org_id),
            )
            exc = cur.fetchone()

        assert exc[0] == ExceptionCategory.PRECONDITION_FAILURE.value
        assert exc[1] == ExceptionReasonCode.CONSENT_MISSING.value
        assert exc[2] == ExceptionStatus.OPEN.value
        assert exc[3] == "orderer"

        # Verify OPEN event created
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, actor FROM exception_events WHERE exception_id = %s",
                (exc_id,),
            )
            events = cur.fetchall()

        assert len(events) == 1
        assert events[0][0] == ExceptionEventAction.OPEN.value
        assert events[0][1] == "orderer-1"

    def test_reopen_exception_on_same_reason_failure(self, dao, session_admin, patient_and_order, conn):
        """Reopen existing exception when same reason fails again."""
        _, order_id = patient_and_order

        # Create first exception
        exc_id_1 = dao.create_or_reopen_exception(
            session_admin,
            order_id,
            ExceptionCategory.PRECONDITION_FAILURE.value,
            ExceptionReasonCode.CONSENT_MISSING.value,
            "Consent not provided",
            "orderer",
            "orderer-1",
        )

        # Try to create same exception again (should reopen)
        exc_id_2 = dao.create_or_reopen_exception(
            session_admin,
            order_id,
            ExceptionCategory.PRECONDITION_FAILURE.value,
            ExceptionReasonCode.CONSENT_MISSING.value,
            "Consent still missing",
            "orderer",
            "orderer-2",
        )

        # Should return same exception ID
        assert exc_id_1 == exc_id_2

        # Verify two events: OPEN and REOPEN
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, actor FROM exception_events WHERE exception_id = %s ORDER BY timestamp ASC",
                (exc_id_1,),
            )
            events = cur.fetchall()

        assert len(events) == 2
        assert events[0][0] == ExceptionEventAction.OPEN.value
        assert events[1][0] == ExceptionEventAction.REOPEN.value
        assert events[1][1] == "orderer-2"

    def test_create_separate_exception_for_different_reason(self, dao, session_admin, patient_and_order, conn):
        """Create separate exception when different reason code fails."""
        _, order_id = patient_and_order

        # Create first exception
        exc_id_1 = dao.create_or_reopen_exception(
            session_admin,
            order_id,
            ExceptionCategory.PRECONDITION_FAILURE.value,
            ExceptionReasonCode.CONSENT_MISSING.value,
            "Consent not provided",
            "orderer",
            "orderer-1",
        )

        # Create different exception
        exc_id_2 = dao.create_or_reopen_exception(
            session_admin,
            order_id,
            ExceptionCategory.VALIDATION_FAILURE.value,
            ExceptionReasonCode.VCF_INVALID.value,
            "VCF file invalid",
            "lab_operator",
            "lab-1",
        )

        # Should be different IDs
        assert exc_id_1 != exc_id_2

        # Verify both exceptions exist
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM exceptions WHERE order_id = %s AND org_id = %s",
                (order_id, session_admin.org_id),
            )
            count = cur.fetchone()[0]

        assert count == 2
