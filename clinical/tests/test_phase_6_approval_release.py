"""
tests/test_phase_6_approval_release.py
───────────────────────────────────────

Phase 6 commit 3: approval and release (spec 13.3, 13.4).

_approve_report is the only path a report can reach 'approved' by: it enforces
that only an Approver may act, that the actor recorded as approver actually
holds the Approver role (not merely the session invoking it), that the report
was under_review, and it records the three approval facts (approver_id,
approved_at, content_hash) together.

_release_report is the only path a release_events row is written. Release is
an event, not a boolean: releasing an already-released report to a second
consumer inserts a second row rather than being refused.

require_release is the gate every delivery path must call before handing
report content to a consumer. It is a hard stop: an unapproved report refuses.
"""

from __future__ import annotations

import datetime
from datetime import date, timezone
import hashlib
import json
import os
import uuid

import pytest

from clinical.data_access import (
    AuthorizationError,
    DataAccess,
    BcryptHasher,
    SystemClock,
    NotFoundError,
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


def _approver_session(dao, conn, admin_session, org_id, email):
    """Create a user, grant Approver (via the Administrator session), log in as them."""
    user_id = dao.create_user(org_id, email, "password")
    dao.assign_role(admin_session, user_id, "Approver", basis="Board-certified clinical geneticist, licence #A-001")
    return dao.login(email, org_id, "password")


@pytest.fixture
def approver_a(dao, conn, session_a, org_a):
    return _approver_session(dao, conn, session_a, org_a, "approver@org-a.test")


@pytest.fixture
def approver_b(dao, conn, session_b, org_b):
    return _approver_session(dao, conn, session_b, org_b, "approver@org-b.test")


def _make_interpretation(dao, conn, session, run_document=None):
    """
    Build the full FK chain an interpretation needs:
    test -> patient -> consent -> order -> sample -> sequencing_run -> vcf -> interpretation.
    Returns the interpretation id.
    """
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
    document = run_document if run_document is not None else {"variants": []}
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
                json.dumps(document),
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


@pytest.fixture
def report_under_review_a(dao, conn, session_a, interp_a):
    """A report in org A, moved straight to under_review (no submit-for-review method exists yet)."""
    report_id = dao.create_report(session_a, interp_a)
    _set_report_state(conn, report_id, "under_review")
    return report_id


@pytest.fixture
def report_under_review_b(dao, conn, session_b, interp_b):
    report_id = dao.create_report(session_b, interp_b)
    _set_report_state(conn, report_id, "under_review")
    return report_id


class TestApproveReport:
    """_approve_report: spec 13.3 -- approval records who, when, and exactly what."""

    def test_approve_transitions_state_and_records_facts(self, dao, conn, session_a, approver_a, report_under_review_a):
        dao._approve_report(approver_a, report_under_review_a, actor_id=approver_a.user_id)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, approver_id, approved_at, content_hash FROM reports WHERE id = %s",
                (report_under_review_a,),
            )
            state, approver_id, approved_at, content_hash = cur.fetchone()

        assert state == "approved"
        assert approver_id == approver_a.user_id
        assert approved_at is not None
        assert approved_at.tzinfo is not None, "approved_at must be timezone-aware"
        assert content_hash is not None
        assert len(content_hash) == 64, "sha256 hex digest is 64 characters"

    def test_approve_content_hash_reflects_run_document(self, dao, conn, session_a, approver_a, interp_a):
        report_id = dao.create_report(session_a, interp_a)
        _set_report_state(conn, report_id, "under_review")

        dao._approve_report(approver_a, report_id, actor_id=approver_a.user_id)

        expected = hashlib.sha256(
            json.dumps({"variants": []}, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with conn.cursor() as cur:
            cur.execute("SELECT content_hash FROM reports WHERE id = %s", (report_id,))
            actual = cur.fetchone()[0]
        assert actual == expected

    def test_approve_requires_approver_role_on_session(self, dao, session_a, report_under_review_a):
        """The session holder (an Administrator here, not an Approver) may not approve."""
        with pytest.raises(AuthorizationError):
            dao._approve_report(session_a, report_under_review_a, actor_id=session_a.user_id)

    def test_approve_requires_actor_to_hold_approver_role(
        self, dao, conn, session_a, approver_a, report_under_review_a
    ):
        """
        The session gate alone is not enough: the identity RECORDED as approver
        must itself hold the Approver role, or an Approver session could record
        an unqualified colleague as having approved -- exactly the identity gap
        ISO 15189 7.4.1.5 c) exists to close.
        """
        bystander_id = dao.create_user(session_a.org_id, "bystander@org-a.test", "password")
        with pytest.raises(AuthorizationError):
            dao._approve_report(approver_a, report_under_review_a, actor_id=bystander_id)

    def test_approve_rejects_draft_report(self, dao, session_a, approver_a, interp_a):
        report_id = dao.create_report(session_a, interp_a)  # left in 'draft'
        with pytest.raises(ValueError, match="draft"):
            dao._approve_report(approver_a, report_id, actor_id=approver_a.user_id)

    def test_approve_rejects_already_approved_report(self, dao, session_a, approver_a, report_under_review_a):
        dao._approve_report(approver_a, report_under_review_a, actor_id=approver_a.user_id)
        with pytest.raises(ValueError, match="approved"):
            dao._approve_report(approver_a, report_under_review_a, actor_id=approver_a.user_id)

    def test_approve_unknown_report_rejected(self, dao, session_a, approver_a):
        with pytest.raises(NotFoundError):
            dao._approve_report(approver_a, uuid.uuid4(), actor_id=approver_a.user_id)

    def test_approve_cross_org_report_rejected(self, dao, session_a, approver_a, report_under_review_b):
        """A report existing in org B is indistinguishable from a nonexistent one to org A."""
        with pytest.raises(NotFoundError):
            dao._approve_report(approver_a, report_under_review_b, actor_id=approver_a.user_id)

    def test_approve_unknown_actor_rejected(self, dao, approver_a, report_under_review_a):
        with pytest.raises(NotFoundError):
            dao._approve_report(approver_a, report_under_review_a, actor_id=uuid.uuid4())


class TestReleaseReport:
    """_release_report: spec 13.4 -- release is a record of delivery, not a flag."""

    def _approved_report(self, dao, conn, session, approver, interp):
        report_id = dao.create_report(session, interp)
        _set_report_state(conn, report_id, "under_review")
        dao._approve_report(approver, report_id, actor_id=approver.user_id)
        return report_id

    def test_release_inserts_event_and_sets_released_state(self, dao, conn, session_a, approver_a, interp_a):
        report_id = self._approved_report(dao, conn, session_a, approver_a, interp_a)

        release_id = dao._release_report(session_a, report_id, consumer="LIMS", actor_id=approver_a.user_id)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id, report_id, consumer, released_at, released_by, content_hash "
                "FROM release_events WHERE id = %s",
                (release_id,),
            )
            row = cur.fetchone()
            cur.execute("SELECT state, content_hash FROM reports WHERE id = %s", (report_id,))
            state, report_hash = cur.fetchone()

        assert row is not None
        assert row[0] == session_a.org_id
        assert row[1] == report_id
        assert row[2] == "LIMS"
        assert row[3].tzinfo is not None
        assert row[4] == approver_a.user_id
        assert row[5] == report_hash
        assert state == "released"

    def test_release_allows_multiple_consumers(self, dao, conn, session_a, approver_a, interp_a):
        report_id = self._approved_report(dao, conn, session_a, approver_a, interp_a)

        dao._release_report(session_a, report_id, consumer="LIMS", actor_id=approver_a.user_id)
        dao._release_report(session_a, report_id, consumer="Clinician portal", actor_id=approver_a.user_id)

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM release_events WHERE report_id = %s", (report_id,))
            count = cur.fetchone()[0]
            cur.execute("SELECT state FROM reports WHERE id = %s", (report_id,))
            state = cur.fetchone()[0]
        assert count == 2
        assert state == "released"

    def test_release_rejects_draft_report(self, dao, session_a, approver_a, interp_a):
        report_id = dao.create_report(session_a, interp_a)
        with pytest.raises(ValueError, match="draft"):
            dao._release_report(session_a, report_id, consumer="LIMS", actor_id=approver_a.user_id)

    def test_release_rejects_under_review_report(self, dao, session_a, approver_a, report_under_review_a):
        with pytest.raises(ValueError, match="under_review"):
            dao._release_report(session_a, report_under_review_a, consumer="LIMS", actor_id=approver_a.user_id)

    def test_release_requires_consumer_named(self, dao, conn, session_a, approver_a, interp_a):
        report_id = self._approved_report(dao, conn, session_a, approver_a, interp_a)
        with pytest.raises(ValueError, match="consumer"):
            dao._release_report(session_a, report_id, consumer="", actor_id=approver_a.user_id)

    def test_release_unknown_report_rejected(self, dao, session_a, approver_a):
        with pytest.raises(NotFoundError):
            dao._release_report(session_a, uuid.uuid4(), consumer="LIMS", actor_id=approver_a.user_id)

    def test_release_cross_org_report_rejected(self, dao, conn, session_a, session_b, approver_a, approver_b, interp_b):
        report_id = self._approved_report(dao, conn, session_b, approver_b, interp_b)
        with pytest.raises(NotFoundError):
            dao._release_report(session_a, report_id, consumer="LIMS", actor_id=approver_a.user_id)


class TestRequireRelease:
    """require_release: the one gate every delivery path calls. Hard stop if not approved."""

    def test_passes_for_approved_report(self, dao, conn, session_a, approver_a, interp_a):
        report_id = dao.create_report(session_a, interp_a)
        _set_report_state(conn, report_id, "under_review")
        dao._approve_report(approver_a, report_id, actor_id=approver_a.user_id)

        dao.require_release(session_a, report_id)  # must not raise

    def test_passes_for_released_report(self, dao, conn, session_a, approver_a, interp_a):
        report_id = dao.create_report(session_a, interp_a)
        _set_report_state(conn, report_id, "under_review")
        dao._approve_report(approver_a, report_id, actor_id=approver_a.user_id)
        dao._release_report(session_a, report_id, consumer="LIMS", actor_id=approver_a.user_id)

        dao.require_release(session_a, report_id)  # must not raise

    def test_blocks_draft_report(self, dao, session_a, interp_a):
        report_id = dao.create_report(session_a, interp_a)
        with pytest.raises(ValueError, match="draft"):
            dao.require_release(session_a, report_id)

    def test_blocks_under_review_report(self, dao, session_a, report_under_review_a):
        with pytest.raises(ValueError, match="under_review"):
            dao.require_release(session_a, report_under_review_a)

    def test_unknown_report_rejected(self, dao, session_a):
        with pytest.raises(NotFoundError):
            dao.require_release(session_a, uuid.uuid4())

    def test_cross_org_report_rejected(self, dao, session_a, report_under_review_b):
        with pytest.raises(NotFoundError):
            dao.require_release(session_a, report_under_review_b)
