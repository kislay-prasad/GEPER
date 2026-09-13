"""
tests/test_phase_6_submit_for_review.py
───────────────────────────────────────

Phase 6 commit 4: submit_for_review -- the workflow entry point.

draft -> under_review is the one transition that lets a report enter review at
all; commit 3 landed _approve_report accepting only 'under_review' and left
this gap deliberately visible rather than hiding it behind a permissive
precondition. Three preconditions, all substantive:

  1. The session holds the Interpreter role. Reading a draft and adding
     clinical interpretation is the Interpreter's work, so submitting the
     result of it is theirs too.
  2. The report is in 'draft'. Any other state is refused with the state named.
  3. At least one reviewer claim exists against the report's interpretation.
     Zero claims means no review happened -- submitting raw engine output as
     though a human had read it. This is why the explicit 'accept' row is
     load-bearing: a reviewer who agrees with everything still leaves rows.
"""

from __future__ import annotations

import datetime
from datetime import date, timezone
import json
import os
import uuid

import pytest

from clinical.data_access import (
    AuthorizationError,
    BcryptHasher,
    DataAccess,
    NotFoundError,
    SystemClock,
)

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = __import__("pathlib").Path(__file__).parent.parent / "schema.sql"

_CREATE_ROLE = "DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
_CREATE_ROLE_RETENTION = (
    "DO $$ BEGIN CREATE ROLE clinical_retention; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
)


@pytest.fixture()
def conn():
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")
    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(_CREATE_ROLE)
        cur.execute(_CREATE_ROLE_RETENTION)
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


def _role_session(dao, admin_session, org_id, email, role):
    user_id = dao.create_user(org_id, email, "password")
    dao.assign_role(admin_session, user_id, role, basis="Test fixture role grant")
    return dao.login(email, org_id, "password")


@pytest.fixture
def interpreter_a(dao, session_a, org_a):
    return _role_session(dao, session_a, org_a, "interpreter@org-a.test", "Interpreter")


@pytest.fixture
def interpreter_b(dao, session_b, org_b):
    return _role_session(dao, session_b, org_b, "interpreter@org-b.test", "Interpreter")


@pytest.fixture
def approver_a(dao, session_a, org_a):
    """An Approver who is NOT an Interpreter -- the wrong role for this action."""
    return _role_session(dao, session_a, org_a, "approver@org-a.test", "Approver")


def _make_interpretation(dao, conn, session):
    """
    Build the full FK chain an interpretation needs:
    test -> patient -> consent -> order -> sample -> sequencing_run -> vcf -> interpretation.
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


@pytest.fixture
def draft_report_a(dao, session_a, interp_a):
    """A report in org A, in its default 'draft' state, with no claims yet."""
    return dao.create_report(session_a, interp_a)


@pytest.fixture
def draft_report_b(dao, session_b, interp_b):
    return dao.create_report(session_b, interp_b)


def _report_state(conn, report_id):
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM reports WHERE id = %s", (report_id,))
        return cur.fetchone()[0]


def _set_report_state(conn, report_id, state):
    with conn.cursor() as cur:
        cur.execute("UPDATE reports SET state = %s WHERE id = %s", (state, report_id))
    conn.commit()


def _add_claim(dao, session, interp_id, report_id, actor_id):
    """One 'accept' claim -- the minimum that counts as review having happened."""
    return dao._record_accept(
        session,
        interpretation_id=interp_id,
        report_id=report_id,
        actor_id=actor_id,
        reason="Independently re-derived the same classification.",
    )


class TestSubmitForReviewClaimsPrecondition:
    """
    Precondition 3, the substantive one: zero reviewer claims means no review
    happened, and submitting raw engine output is exactly what this refuses.
    """

    def test_zero_claims_refused(self, dao, interpreter_a, draft_report_a):
        with pytest.raises(ValueError, match="claim"):
            dao.submit_for_review(interpreter_a, draft_report_a)

    def test_zero_claims_leaves_state_untouched(self, dao, conn, interpreter_a, draft_report_a):
        with pytest.raises(ValueError):
            dao.submit_for_review(interpreter_a, draft_report_a)
        assert _report_state(conn, draft_report_a) == "draft", "a refused submission must not move the report"

    def test_one_claim_is_enough(self, dao, conn, session_a, interpreter_a, interp_a, draft_report_a):
        _add_claim(dao, session_a, interp_a, draft_report_a, session_a.user_id)
        dao.submit_for_review(interpreter_a, draft_report_a)
        assert _report_state(conn, draft_report_a) == "under_review"

    def test_claims_on_a_different_interpretation_do_not_count(
        self, dao, conn, session_a, interpreter_a, interp_a, draft_report_a
    ):
        """
        A claim must be against THIS report. Another interpretation's
        review -- recorded against that interpretation's own report -- is not
        this report's review.
        """
        other_interp = _make_interpretation(dao, conn, session_a)
        other_report = dao.create_report(session_a, other_interp)
        _add_claim(dao, session_a, other_interp, other_report, session_a.user_id)

        with pytest.raises(ValueError, match="claim"):
            dao.submit_for_review(interpreter_a, draft_report_a)
        assert _report_state(conn, draft_report_a) == "draft"

    def test_another_orgs_claims_do_not_count(
        self, dao, conn, session_b, interpreter_a, interp_b, draft_report_a, draft_report_b
    ):
        """Org B reviewing its own work does not let Org A's report through."""
        _add_claim(dao, session_b, interp_b, draft_report_b, session_b.user_id)

        with pytest.raises(ValueError, match="claim"):
            dao.submit_for_review(interpreter_a, draft_report_a)
        assert _report_state(conn, draft_report_a) == "draft"

    def test_any_claim_type_satisfies_the_precondition(
        self, dao, conn, session_a, interpreter_a, interp_a, draft_report_a
    ):
        """A disagreement is review too -- the precondition is that review happened."""
        dao._record_disagreement(
            session_a,
            interpretation_id=interp_a,
            report_id=draft_report_a,
            variant_key="17-43106534-C-A",
            actor_id=session_a.user_id,
            new_classification="Likely Benign",
            reason="Population frequency too high for a pathogenic call.",
        )
        dao.submit_for_review(interpreter_a, draft_report_a)
        assert _report_state(conn, draft_report_a) == "under_review"


class TestSubmitForReviewStatePrecondition:
    """Precondition 2: only a draft may be submitted, and the state is named on refusal."""

    def test_transitions_draft_to_under_review(self, dao, conn, session_a, interpreter_a, interp_a, draft_report_a):
        _add_claim(dao, session_a, interp_a, draft_report_a, session_a.user_id)
        assert _report_state(conn, draft_report_a) == "draft"

        dao.submit_for_review(interpreter_a, draft_report_a)

        assert _report_state(conn, draft_report_a) == "under_review"

    @pytest.mark.parametrize("state", ["under_review", "returned"])
    def test_non_draft_states_refused(self, dao, conn, session_a, interpreter_a, interp_a, draft_report_a, state):
        _add_claim(dao, session_a, interp_a, draft_report_a, session_a.user_id)
        _set_report_state(conn, draft_report_a, state)

        with pytest.raises(ValueError, match=state):
            dao.submit_for_review(interpreter_a, draft_report_a)

    def test_refusal_names_the_actual_state(self, dao, conn, session_a, interpreter_a, interp_a, draft_report_a):
        _add_claim(dao, session_a, interp_a, draft_report_a, session_a.user_id)
        _set_report_state(conn, draft_report_a, "under_review")

        with pytest.raises(ValueError) as exc:
            dao.submit_for_review(interpreter_a, draft_report_a)
        assert "under_review" in str(exc.value), "the refusal must name the state it found"

    def test_resubmission_refused(self, dao, conn, session_a, interpreter_a, interp_a, draft_report_a):
        """Once submitted, a report is no longer a draft and cannot be submitted again."""
        _add_claim(dao, session_a, interp_a, draft_report_a, session_a.user_id)
        dao.submit_for_review(interpreter_a, draft_report_a)

        with pytest.raises(ValueError):
            dao.submit_for_review(interpreter_a, draft_report_a)
        assert _report_state(conn, draft_report_a) == "under_review"


class TestSubmitForReviewRolePrecondition:
    """Precondition 1: the Interpreter role, on the session invoking this."""

    def test_requires_interpreter_role(self, dao, session_a, interp_a, draft_report_a):
        """An Administrator session holds no Interpreter role and is refused."""
        _add_claim(dao, session_a, interp_a, draft_report_a, session_a.user_id)

        with pytest.raises(AuthorizationError, match="Interpreter"):
            dao.submit_for_review(session_a, draft_report_a)

    def test_approver_role_is_not_enough(self, dao, conn, session_a, approver_a, interp_a, draft_report_a):
        """Approving is a different action from submitting; the roles do not substitute."""
        _add_claim(dao, session_a, interp_a, draft_report_a, session_a.user_id)

        with pytest.raises(AuthorizationError, match="Interpreter"):
            dao.submit_for_review(approver_a, draft_report_a)
        assert _report_state(conn, draft_report_a) == "draft"


class TestSubmitForReviewScoping:
    """Org scoping and lookup failures."""

    def test_unknown_report_rejected(self, dao, interpreter_a):
        with pytest.raises(NotFoundError):
            dao.submit_for_review(interpreter_a, uuid.uuid4())

    def test_cross_org_report_rejected(self, dao, conn, session_b, interpreter_a, interp_b, draft_report_b):
        """
        Org A's Interpreter cannot submit Org B's report, and gets the same
        answer as for an invented id -- it cannot learn the report exists.
        """
        _add_claim(dao, session_b, interp_b, draft_report_b, session_b.user_id)

        with pytest.raises(NotFoundError):
            dao.submit_for_review(interpreter_a, draft_report_b)
        assert _report_state(conn, draft_report_b) == "draft"

    def test_writes_audit_entry(self, dao, conn, session_a, interpreter_a, interp_a, draft_report_a):
        _add_claim(dao, session_a, interp_a, draft_report_a, session_a.user_id)
        dao.submit_for_review(interpreter_a, draft_report_a)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM audit_log WHERE org_id = %s AND action = 'report_submitted_for_review'",
                (interpreter_a.org_id,),
            )
            assert cur.fetchone()[0] == 1, "submission must be audited"

    def test_does_not_touch_approval_columns(self, dao, conn, session_a, interpreter_a, interp_a, draft_report_a):
        """Submission is not approval: approver_id, approved_at and content_hash stay NULL."""
        _add_claim(dao, session_a, interp_a, draft_report_a, session_a.user_id)
        dao.submit_for_review(interpreter_a, draft_report_a)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT approver_id, approved_at, content_hash FROM reports WHERE id = %s",
                (draft_report_a,),
            )
            row = cur.fetchone()
        assert row == (None, None, None)

    def test_other_reports_are_unaffected(self, dao, conn, session_a, interpreter_a, interp_a, draft_report_a):
        """The UPDATE touches one report, not every draft in the org."""
        other_interp = _make_interpretation(dao, conn, session_a)
        other_report = dao.create_report(session_a, other_interp)
        _add_claim(dao, session_a, interp_a, draft_report_a, session_a.user_id)

        dao.submit_for_review(interpreter_a, draft_report_a)

        assert _report_state(conn, draft_report_a) == "under_review"
        assert _report_state(conn, other_report) == "draft"
