"""
tests/test_phase_7_commit2_amendments.py
─────────────────────────────────────────

Phase 7 commit 2: Amendment workflow (ISO 15189 7.4.1.8). An existing
approved/released report is amended: a new report is created, the original is
never modified, the reason for change is recorded, and the ordering clinician
is notified.

This file replaces an earlier version written against a schema that did not
exist: it assumed an amendments.current_version boolean column (there is
none) and mutated it with UPDATE (amendments is append-only against
clinical_app -- see schema.sql's grants). Discarded and rebuilt against the
actual schema:

  amendments (org_id, id, original_report_id, amendment_report_id, reason,
              supersedes_amendment_id, created_at, created_by)
  amendment_notifications (org_id, id, amendment_report_id, reason_for_change,
                            delivered_to_role, read, created_at, created_by)

"Current amendment" is derived (the row nothing else's supersedes_amendment_id
names), never stored, exactly like reviewer_claims.supersedes in Phase 6.
create_amendment() performs three INSERTs and zero UPDATEs.
"""

from __future__ import annotations

import datetime
from datetime import date, timezone
import json
import os
import uuid

import pytest

from clinical.data_access import (
    DataAccess,
    NotFoundError,
    BcryptHasher,
    SystemClock,
)

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = __import__("pathlib").Path(__file__).parent.parent / "schema.sql"

_CREATE_ROLE = "DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"


@pytest.fixture()
def conn():
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")
    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(_CREATE_ROLE)
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def dao(conn):
    return DataAccess(conn, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))


@pytest.fixture
def org_a(dao):
    return dao.create_organisation("Org A")


@pytest.fixture
def org_b(dao):
    return dao.create_organisation("Org B")


def _admin_session(dao, conn, org_id, email):
    user_id = dao.create_user(org_id, email, "password")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org_id, user_id, datetime.datetime(2026, 9, 1, tzinfo=timezone.utc)),
        )
    conn.commit()
    return dao.login(email, org_id, "password")


@pytest.fixture
def session_a(dao, org_a, conn):
    return _admin_session(dao, conn, org_a, "admin@org-a.test")


@pytest.fixture
def session_b(dao, org_b, conn):
    return _admin_session(dao, conn, org_b, "admin@org-b.test")


def _make_interpretation(dao, conn, session):
    org_id = session.org_id
    now = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

    test_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, org_id, "Panel", "GRCh38", "active", now),
        )
    conn.commit()

    patient_id = dao.create_patient(session, "Test", date(1990, 1, 1), "M")
    consent_id = dao.record_consent(session, patient_id, "testing")
    order_id = dao.create_order(
        session,
        patient_id=patient_id,
        test_id=test_id,
        consent_id=consent_id,
        required_scope="testing",
        priority="routine",
    )

    sample_id = uuid.uuid4()
    run_id = uuid.uuid4()
    vcf_id = uuid.uuid4()
    interp_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO samples (sample_id, order_id, org_id, type, collected_at, collected_by, qc_status) "
            "VALUES (%s, %s, %s, 'blood', %s, %s, 'passed')",
            (sample_id, order_id, org_id, now, session.user_id),
        )
        cur.execute(
            "INSERT INTO sequencing_runs (org_id, id, sample_id, created_at, created_by) VALUES (%s, %s, %s, %s, %s)",
            (org_id, run_id, sample_id, now, session.user_id),
        )
        cur.execute(
            "INSERT INTO vcfs (org_id, id, sequencing_run_id, vcf_path, content_hash, created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (org_id, vcf_id, run_id, "/tmp/x.vcf", "0" * 64, now, session.user_id),
        )
        cur.execute(
            "INSERT INTO interpretations "
            "(org_id, id, vcf_id, run_document, submission_key, created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                org_id,
                interp_id,
                vcf_id,
                json.dumps({"variants": []}),
                "sub-" + str(interp_id),
                now,
                session.user_id,
            ),
        )
    conn.commit()
    return interp_id


@pytest.fixture
def interp_a(dao, conn, session_a):
    return _make_interpretation(dao, conn, session_a)


@pytest.fixture
def interp_b(dao, conn, session_b):
    return _make_interpretation(dao, conn, session_b)


def _set_report_state(conn, report_id, state):
    with conn.cursor() as cur:
        cur.execute("UPDATE reports SET state = %s WHERE id = %s", (state, report_id))
    conn.commit()


def _set_report_approved(conn, report_id, approver_id):
    now = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE reports SET state = %s, approver_id = %s, approved_at = %s, content_hash = %s WHERE id = %s",
            ("approved", approver_id, now, "0" * 64, report_id),
        )
    conn.commit()


def _get_report_state(conn, report_id):
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM reports WHERE id = %s", (report_id,))
        row = cur.fetchone()
        return row[0] if row else None


def _approved_original(dao, conn, session, interp_id):
    original = dao.create_report(session, interp_id)
    _set_report_approved(conn, original, session.user_id)
    return original


def _current_amendment_id(conn, org_id, original_report_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM amendments "
            "WHERE org_id = %s AND original_report_id = %s "
            "AND id NOT IN ("
            "  SELECT supersedes_amendment_id FROM amendments "
            "  WHERE org_id = %s AND original_report_id = %s AND supersedes_amendment_id IS NOT NULL"
            ")",
            (org_id, original_report_id, org_id, original_report_id),
        )
        row = cur.fetchone()
        return row[0] if row else None


class TestAmendmentCreation:
    """Basic amendment creation: new report, amendments row, notification -- three INSERTs, zero UPDATEs."""

    def test_creates_new_report_in_draft_state(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)

        amendment_report_id, _ = dao.create_amendment(session_a, original, "Updated findings")

        assert amendment_report_id != original
        assert _get_report_state(conn, amendment_report_id) == "draft"

    def test_records_amendment_relationship(self, dao, conn, session_a, interp_a):
        """amendments row: original_report_id, amendment_report_id, reason, supersedes_amendment_id NULL."""
        original = _approved_original(dao, conn, session_a, interp_a)

        amendment_report_id, _ = dao.create_amendment(session_a, original, "Updated findings")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT original_report_id, amendment_report_id, reason, supersedes_amendment_id "
                "FROM amendments WHERE org_id = %s AND amendment_report_id = %s",
                (session_a.org_id, amendment_report_id),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == original
        assert row[1] == amendment_report_id
        assert row[2] == "Updated findings"
        assert row[3] is None, "the first amendment of an original supersedes nothing"

    def test_no_current_version_column_exists(self, conn):
        """
        The old (discarded) design stored a current_version boolean.
        current amendment is derived, not stored -- confirm the column is
        genuinely gone rather than merely unused.
        """
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'amendments' AND column_name = 'current_version'"
            )
            assert cur.fetchone() is None

    def test_records_notification_as_sent(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)

        amendment_report_id, notification_id = dao.create_amendment(session_a, original, "Updated findings")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT amendment_report_id, reason_for_change, delivered_to_role, read "
                "FROM amendment_notifications WHERE org_id = %s AND id = %s",
                (session_a.org_id, notification_id),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == amendment_report_id
        assert row[1] == "Updated findings"
        assert row[2] == "ordering_clinician"
        assert row[3] is False  # sent but not read

    def test_original_report_unchanged(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, approver_id, content_hash FROM reports WHERE id = %s AND org_id = %s",
                (original, session_a.org_id),
            )
            before = cur.fetchone()

        dao.create_amendment(session_a, original, "Updated findings")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, approver_id, content_hash FROM reports WHERE id = %s AND org_id = %s",
                (original, session_a.org_id),
            )
            after = cur.fetchone()

        assert before == after

    def test_both_original_and_amendment_stay_live(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)

        amendment_report_id, _ = dao.create_amendment(session_a, original, "Updated findings")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM reports WHERE org_id = %s AND id IN (%s, %s)",
                (session_a.org_id, original, amendment_report_id),
            )
            count = cur.fetchone()[0]
        assert count == 2

    def test_no_update_to_amendments_or_notifications_tables(self, dao, conn, session_a, interp_a):
        """
        Structural proof create_amendment() is append-only: clinical_app has
        no UPDATE privilege on either table, so any UPDATE attempt inside
        create_amendment() would raise InsufficientPrivilege. This test
        connects as the superuser test role (same as every fixture here),
        so it cannot observe a privilege error directly -- instead it
        confirms create_amendment() succeeds and leaves exactly one row in
        each table per call, which is what "only ever INSERT, never UPDATE"
        implies for a single call with nothing to supersede.
        """
        original = _approved_original(dao, conn, session_a, interp_a)

        dao.create_amendment(session_a, original, "Updated findings")

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM amendments WHERE org_id = %s", (session_a.org_id,))
            amendment_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM amendment_notifications WHERE org_id = %s", (session_a.org_id,))
            notification_count = cur.fetchone()[0]
        assert amendment_count == 1
        assert notification_count == 1


class TestAmendmentChain:
    """Current-amendment derivation and the forward-pointer chain across repeated amendments."""

    def test_current_amendment_derivation_single_amendment(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)
        amendment_report_id, _ = dao.create_amendment(session_a, original, "First revision")

        with conn.cursor() as cur:
            cur.execute("SELECT id FROM amendments WHERE amendment_report_id = %s", (amendment_report_id,))
            amendment_id = cur.fetchone()[0]

        assert _current_amendment_id(conn, session_a.org_id, original) == amendment_id

    def test_second_amendment_supersedes_the_first(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)

        first_report_id, _ = dao.create_amendment(session_a, original, "First revision")
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM amendments WHERE amendment_report_id = %s", (first_report_id,))
            first_amendment_id = cur.fetchone()[0]

        second_report_id, _ = dao.create_amendment(session_a, original, "Second revision")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT supersedes_amendment_id FROM amendments WHERE amendment_report_id = %s",
                (second_report_id,),
            )
            second_supersedes = cur.fetchone()[0]

        assert second_supersedes == first_amendment_id

    def test_current_amendment_after_second_amendment_is_the_second(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)
        dao.create_amendment(session_a, original, "First revision")
        second_report_id, _ = dao.create_amendment(session_a, original, "Second revision")

        with conn.cursor() as cur:
            cur.execute("SELECT id FROM amendments WHERE amendment_report_id = %s", (second_report_id,))
            second_amendment_id = cur.fetchone()[0]

        assert _current_amendment_id(conn, session_a.org_id, original) == second_amendment_id

    def test_first_amendment_row_never_modified_by_the_second(self, dao, conn, session_a, interp_a):
        """The append-only guarantee made concrete: the first row's own columns are untouched."""
        original = _approved_original(dao, conn, session_a, interp_a)
        first_report_id, _ = dao.create_amendment(session_a, original, "First revision")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT reason, supersedes_amendment_id, created_at FROM amendments WHERE amendment_report_id = %s",
                (first_report_id,),
            )
            before = cur.fetchone()

        dao.create_amendment(session_a, original, "Second revision")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT reason, supersedes_amendment_id, created_at FROM amendments WHERE amendment_report_id = %s",
                (first_report_id,),
            )
            after = cur.fetchone()

        assert before == after
        assert before[0] == "First revision"
        assert before[1] is None, "the first amendment's own row is never rewritten to point forward"

    def test_three_amendments_form_a_chain(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)
        r1, _ = dao.create_amendment(session_a, original, "Revision 1")
        r2, _ = dao.create_amendment(session_a, original, "Revision 2")
        r3, _ = dao.create_amendment(session_a, original, "Revision 3")

        def _row(report_id):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, supersedes_amendment_id FROM amendments WHERE amendment_report_id = %s",
                    (report_id,),
                )
                return cur.fetchone()

        a1, a2, a3 = _row(r1), _row(r2), _row(r3)
        assert a1[1] is None
        assert a2[1] == a1[0]
        assert a3[1] == a2[0]
        assert _current_amendment_id(conn, session_a.org_id, original) == a3[0]


class TestAmendmentPreconditions:
    """Precondition validation: original must exist, be approved/released, reason required."""

    def test_rejects_unknown_report(self, dao, session_a):
        with pytest.raises(NotFoundError):
            dao.create_amendment(session_a, uuid.uuid4(), "Updated findings")

    def test_rejects_draft_report(self, dao, conn, session_a, interp_a):
        original = dao.create_report(session_a, interp_a)

        with pytest.raises(ValueError, match="draft"):
            dao.create_amendment(session_a, original, "Updated findings")

    def test_rejects_under_review_report(self, dao, conn, session_a, interp_a):
        original = dao.create_report(session_a, interp_a)
        _set_report_state(conn, original, "under_review")

        with pytest.raises(ValueError, match="under_review"):
            dao.create_amendment(session_a, original, "Updated findings")

    def test_rejects_returned_report(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)
        _set_report_state(conn, original, "returned")

        with pytest.raises(ValueError, match="returned"):
            dao.create_amendment(session_a, original, "Updated findings")

    def test_accepts_approved_report(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)

        amendment_report_id, _ = dao.create_amendment(session_a, original, "Updated findings")
        assert amendment_report_id is not None

    def test_accepts_released_report(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)
        _set_report_state(conn, original, "released")

        amendment_report_id, _ = dao.create_amendment(session_a, original, "Updated findings")
        assert amendment_report_id is not None

    def test_rejects_empty_reason(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)

        with pytest.raises(ValueError, match="reason"):
            dao.create_amendment(session_a, original, "")

    def test_rejects_whitespace_only_reason(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)

        with pytest.raises(ValueError, match="reason"):
            dao.create_amendment(session_a, original, "   ")

    def test_rejects_none_reason(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)

        with pytest.raises(ValueError, match="reason"):
            dao.create_amendment(session_a, original, None)


class TestAmendmentScoping:
    """Org-scoping: original and amendment must be in the same org."""

    def test_cross_org_original_rejected(self, dao, conn, session_a, session_b, interp_b):
        original = _approved_original(dao, conn, session_b, interp_b)

        with pytest.raises(NotFoundError):
            dao.create_amendment(session_a, original, "Updated findings")

    def test_amendment_inherits_original_org(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)

        amendment_report_id, _ = dao.create_amendment(session_a, original, "Updated findings")

        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM reports WHERE id = %s", (amendment_report_id,))
            org_id = cur.fetchone()[0]
        assert org_id == session_a.org_id

    def test_audit_entry_created(self, dao, conn, session_a, interp_a):
        original = _approved_original(dao, conn, session_a, interp_a)

        dao.create_amendment(session_a, original, "Updated findings")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM audit_log WHERE org_id = %s AND action = 'amendment_created'",
                (session_a.org_id,),
            )
            count = cur.fetchone()[0]
        assert count >= 1
