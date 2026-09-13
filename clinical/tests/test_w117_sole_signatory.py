"""
clinical/tests/test_w117_sole_signatory.py
──────────────────────────────────────────

w117 commit 5: the claim kind a ONE-PERSON sign-off can truthfully write,
plus the three public wrappers a delivery path calls instead of reaching
into private methods.

THE DEFECT THIS CLOSES. The product signs off with one person, and that
person's concurrence had nowhere truthful to go. 'accept' is documented --
in the schema's own decision-point note and in _record_accept's docstring --
as an INDEPENDENT CONCURRENCE BY A SECOND CLINICIAN. A same-person accept
therefore writes a row the schema itself defines as false: the stored record
asserts two-clinician review where one person acted, and nothing downstream
can tell the difference, because claim_type is what everything queries.

THE FIX IS A DISTINCT KIND, 'sole_signatory' (human ruling, 2026-09-13), so
that the record is true at the level things query it:

  - an audit asking WHICH REPORTS HAD INDEPENDENT CONCURRENCE answers from
    claim_type alone, without reading prose in `reason`;
  - a site that later adopts two-person sign-off can see WHICH HISTORICAL
    REPORTS WERE SINGLE-SIGNED.

Both of those are queries, so both are tested as queries here -- an assertion
that some free-text field contains a helpful sentence would prove nothing.

THE CONTROLS ARE THE POINT OF THE SUITE, not decoration. This change must
not move the two-person path an inch, so TestTwoPersonPathIsUnchanged
re-runs it end to end: a second clinician's 'accept', submitted by an
Interpreter, approved by an Approver naming themselves. If that class ever
goes red, the change has done the one thing it was forbidden to do.
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


def _session_with_roles(dao, admin_session, org_id, email, *roles):
    user_id = dao.create_user(org_id, email, "password")
    for role in roles:
        dao.assign_role(admin_session, user_id, role, basis="w117 test fixture role grant")
    return dao.login(email, org_id, "password")


@pytest.fixture
def signatory_a(dao, session_a, org_a):
    """
    THE SOLE SIGNATORY: one person who both submits and approves, so they
    hold both roles. That is a real consequence of one-person sign-off, not
    a fixture convenience -- submit_for_review requires Interpreter and
    _approve_report requires Approver, and neither requirement is relaxed by
    this change.
    """
    return _session_with_roles(dao, session_a, org_a, "signatory@org-a.test", "Interpreter", "Approver")


@pytest.fixture
def interpreter_a(dao, session_a, org_a):
    return _session_with_roles(dao, session_a, org_a, "interpreter@org-a.test", "Interpreter")


@pytest.fixture
def approver_a(dao, session_a, org_a):
    return _session_with_roles(dao, session_a, org_a, "approver@org-a.test", "Approver")


@pytest.fixture
def second_clinician_a(dao, session_a, org_a):
    """The independent second reader the two-person path assumes exists."""
    return _session_with_roles(dao, session_a, org_a, "second@org-a.test", "Interpreter")


def _make_interpretation(dao, conn, session):
    org_id = session.org_id
    now = datetime.datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)

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
def report_a(dao, session_a, interp_a):
    return dao.create_report(session_a, interp_a)


@pytest.fixture
def report_b(dao, session_b, interp_b):
    return dao.create_report(session_b, interp_b)


def _claim_rows(conn, report_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT claim_type, variant_key, classification, actor_id, reason "
            'FROM reviewer_claims WHERE report_id = %s ORDER BY "timestamp"',
            (report_id,),
        )
        rows = cur.fetchall()
    conn.commit()
    return rows


def _report_row(conn, report_id):
    with conn.cursor() as cur:
        cur.execute("SELECT state, approver_id, approved_at, content_hash FROM reports WHERE id = %s", (report_id,))
        row = cur.fetchone()
    conn.commit()
    return row


# ───────────────────────────────────────────────────────────────────────────
# The claim kind
# ───────────────────────────────────────────────────────────────────────────


class TestSoleSignatoryIsItsOwnClaimKind:
    def test_the_stored_kind_is_sole_signatory_and_not_accept(self, dao, conn, signatory_a, interp_a, report_a):
        """
        THE WHOLE RULING IN ONE ASSERTION. A sole signatory's concurrence must
        not be stored as 'accept', because 'accept' means a second clinician
        concurred independently and no second clinician exists here.
        """
        dao.record_sole_signatory_claim(
            signatory_a,
            interpretation_id=interp_a,
            report_id=report_a,
            reason="Sole signatory: re-derived every call on this report myself.",
        )
        rows = _claim_rows(conn, report_a)
        assert len(rows) == 1
        assert rows[0][0] == "sole_signatory", (
            "a one-person sign-off was stored under a claim kind the schema defines as "
            "independent concurrence by a SECOND clinician -- the record is false where it is queried"
        )

    def test_an_audit_can_separate_single_signed_from_independently_concurred(
        self, dao, conn, session_a, signatory_a, second_clinician_a, interp_a, report_a
    ):
        """
        The ruling's two named consequences, both as the queries they are:
        "which reports had independent concurrence" and "which reports were
        single-signed". Both must be answerable by claim_type alone.
        """
        other_interp = _make_interpretation(dao, conn, session_a)
        two_person_report = dao.create_report(session_a, other_interp)

        dao.record_sole_signatory_claim(
            signatory_a, interpretation_id=interp_a, report_id=report_a, reason="Sole signatory concurrence."
        )
        dao._record_accept(
            session_a,
            interpretation_id=other_interp,
            report_id=two_person_report,
            actor_id=second_clinician_a.user_id,
            reason="Independently re-derived the same classification.",
        )

        with conn.cursor() as cur:
            cur.execute("SELECT report_id FROM reviewer_claims WHERE claim_type = 'accept'")
            independently_concurred = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT report_id FROM reviewer_claims WHERE claim_type = 'sole_signatory'")
            single_signed = {r[0] for r in cur.fetchall()}
        conn.commit()

        assert independently_concurred == {two_person_report}
        assert single_signed == {report_a}

    def test_the_actor_is_the_authenticated_session_and_cannot_be_someone_else(
        self, dao, conn, signatory_a, interp_a, report_a
    ):
        """
        No actor_id parameter exists, deliberately: letting one identity record
        another person's concurrence is the same falsehood in a different place.
        """
        dao.record_sole_signatory_claim(
            signatory_a, interpretation_id=interp_a, report_id=report_a, reason="Sole signatory concurrence."
        )
        assert _claim_rows(conn, report_a)[0][3] == signatory_a.user_id

    def test_it_is_interpretation_scoped_so_names_no_variant_and_no_classification(
        self, dao, conn, signatory_a, interp_a, report_a
    ):
        dao.record_sole_signatory_claim(
            signatory_a, interpretation_id=interp_a, report_id=report_a, reason="Sole signatory concurrence."
        )
        _kind, variant_key, classification, _actor, _reason = _claim_rows(conn, report_a)[0]
        assert variant_key is None
        assert classification is None

    @pytest.mark.parametrize("bad_reason", ["", "   ", None])
    def test_a_reason_is_required(self, dao, conn, signatory_a, interp_a, report_a, bad_reason):
        with pytest.raises(ValueError, match="reason"):
            dao.record_sole_signatory_claim(
                signatory_a, interpretation_id=interp_a, report_id=report_a, reason=bad_reason
            )
        assert _claim_rows(conn, report_a) == []

    def test_the_report_must_be_a_report_of_this_interpretation(self, dao, conn, session_a, signatory_a, interp_a):
        other_interp = _make_interpretation(dao, conn, session_a)
        other_report = dao.create_report(session_a, other_interp)
        with pytest.raises(ValueError, match="not a report of"):
            dao.record_sole_signatory_claim(
                signatory_a, interpretation_id=interp_a, report_id=other_report, reason="Sole signatory concurrence."
            )

    def test_another_organisations_report_is_simply_absent(self, dao, conn, signatory_a, interp_b, report_b):
        """Cross-org: not 'forbidden' (which would confirm it exists), absent."""
        with pytest.raises(NotFoundError):
            dao.record_sole_signatory_claim(
                signatory_a, interpretation_id=interp_b, report_id=report_b, reason="Sole signatory concurrence."
            )
        assert _claim_rows(conn, report_b) == []

    def test_it_is_visible_through_get_reviewer_claims(self, dao, signatory_a, interp_a, report_a):
        dao.record_sole_signatory_claim(
            signatory_a, interpretation_id=interp_a, report_id=report_a, reason="Sole signatory concurrence."
        )
        claims = dao.get_reviewer_claims(signatory_a, report_a)
        assert [c["claim_type"] for c in claims] == ["sole_signatory"]

    def test_it_satisfies_submit_for_review_on_its_own(self, dao, conn, signatory_a, interp_a, report_a):
        """
        Review happened, and the row says exactly how much of it happened. The
        at-least-one-claim precondition is about review having occurred, so a
        sole_signatory claim satisfies it like any other claim.
        """
        dao.record_sole_signatory_claim(
            signatory_a, interpretation_id=interp_a, report_id=report_a, reason="Sole signatory concurrence."
        )
        dao.submit_for_review(signatory_a, report_a)
        assert _report_row(conn, report_a)[0] == "under_review"

    def test_it_is_audited_under_its_own_action_name(self, dao, conn, signatory_a, interp_a, report_a):
        """
        The audit log must distinguish it too -- recording it as
        'record_accept' would reintroduce the falsehood one layer over.
        """
        dao.record_sole_signatory_claim(
            signatory_a, interpretation_id=interp_a, report_id=report_a, reason="Sole signatory concurrence."
        )
        with conn.cursor() as cur:
            cur.execute("SELECT action FROM audit_log WHERE resource_type = 'reviewer_claim'")
            actions = {r[0] for r in cur.fetchall()}
        conn.commit()
        assert "record_sole_signatory" in actions
        assert "record_accept" not in actions


# ───────────────────────────────────────────────────────────────────────────
# The public wrappers
# ───────────────────────────────────────────────────────────────────────────


def _audit_actions(conn, action):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log WHERE action = %s", (action,))
        n = cur.fetchone()[0]
    conn.commit()
    return n


class TestTheWrappersAuditOnceAndOnlyOnce:
    """
    Each wrapper delegates to a method that is already @auditable, so the
    wrapper declares auditable=False. If a second decorator is ever added, one
    clinical act starts producing two audit entries -- an approval counted
    twice is a worse record than one counted once, because a count is what an
    audit reads.
    """

    def test_approve_report_writes_one_report_approved_entry(self, dao, conn, signatory_a, interp_a, report_a):
        dao.record_sole_signatory_claim(
            signatory_a, interpretation_id=interp_a, report_id=report_a, reason="Sole signatory concurrence."
        )
        dao.submit_for_review(signatory_a, report_a)
        dao.approve_report(signatory_a, report_a)
        assert _audit_actions(conn, "report_approved") == 1

    def test_record_disagreement_claim_writes_one_entry(self, dao, conn, signatory_a, interp_a, report_a):
        dao.record_disagreement_claim(
            signatory_a,
            interpretation_id=interp_a,
            report_id=report_a,
            variant_key="17-43106534-C-A",
            new_classification="Likely Benign",
            reason="Population frequency too high for a pathogenic call.",
        )
        assert _audit_actions(conn, "record_disagreement") == 1

    def test_record_sole_signatory_claim_writes_one_entry(self, dao, conn, signatory_a, interp_a, report_a):
        dao.record_sole_signatory_claim(
            signatory_a, interpretation_id=interp_a, report_id=report_a, reason="Sole signatory concurrence."
        )
        assert _audit_actions(conn, "record_sole_signatory") == 1


class TestApproveReportWrapper:
    def test_records_the_session_user_as_approver(self, dao, conn, signatory_a, interp_a, report_a):
        dao.record_sole_signatory_claim(
            signatory_a, interpretation_id=interp_a, report_id=report_a, reason="Sole signatory concurrence."
        )
        dao.submit_for_review(signatory_a, report_a)
        dao.approve_report(signatory_a, report_a)

        state, approver_id, approved_at, content_hash = _report_row(conn, report_a)
        assert state == "approved"
        assert approver_id == signatory_a.user_id
        assert approved_at is not None
        assert content_hash is not None

    def test_a_session_without_the_approver_role_is_still_refused(
        self, dao, conn, session_a, interpreter_a, interp_a, report_a
    ):
        """
        ENFORCEMENT IS UNCHANGED. The wrapper is not a bypass: an Interpreter
        who is not an Approver cannot approve through it either.
        """
        dao.record_sole_signatory_claim(
            interpreter_a, interpretation_id=interp_a, report_id=report_a, reason="Sole signatory concurrence."
        )
        dao.submit_for_review(interpreter_a, report_a)
        with pytest.raises(AuthorizationError):
            dao.approve_report(interpreter_a, report_a)
        assert _report_row(conn, report_a)[0] == "under_review"

    def test_a_draft_report_is_still_refused(self, dao, conn, signatory_a, interp_a, report_a):
        """Only 'under_review' may be approved -- the wrapper does not skip submission."""
        with pytest.raises(ValueError, match="cannot approve"):
            dao.approve_report(signatory_a, report_a)
        assert _report_row(conn, report_a)[0] == "draft"

    def test_another_organisations_report_is_absent(self, dao, signatory_a, report_b):
        with pytest.raises(NotFoundError):
            dao.approve_report(signatory_a, report_b)


class TestRecordDisagreementClaimWrapper:
    def test_records_a_disagree_claim_attributed_to_the_session(self, dao, conn, signatory_a, interp_a, report_a):
        dao.record_disagreement_claim(
            signatory_a,
            interpretation_id=interp_a,
            report_id=report_a,
            variant_key="17-43106534-C-A",
            new_classification="Likely Benign",
            reason="Population frequency too high for a pathogenic call.",
        )
        kind, variant_key, classification, actor_id, _reason = _claim_rows(conn, report_a)[0]
        assert kind == "disagree"
        assert variant_key == "17-43106534-C-A"
        assert classification == "Likely Benign"
        assert actor_id == signatory_a.user_id

    def test_a_reason_is_still_required(self, dao, conn, signatory_a, interp_a, report_a):
        with pytest.raises(ValueError, match="reason"):
            dao.record_disagreement_claim(
                signatory_a,
                interpretation_id=interp_a,
                report_id=report_a,
                variant_key="17-43106534-C-A",
                new_classification="Likely Benign",
                reason="   ",
            )
        assert _claim_rows(conn, report_a) == []

    def test_a_classification_is_still_required(self, dao, conn, signatory_a, interp_a, report_a):
        with pytest.raises(ValueError, match="classification"):
            dao.record_disagreement_claim(
                signatory_a,
                interpretation_id=interp_a,
                report_id=report_a,
                variant_key="17-43106534-C-A",
                new_classification=None,
                reason="Disagreement without a call is not a call.",
            )
        assert _claim_rows(conn, report_a) == []


class TestGetReportReportsItsState:
    """
    A caller outside this class must be able to ASK the state without being
    given SQL -- review/signoff.py refuses an override of an approved report
    on exactly this answer.
    """

    def test_state_is_returned(self, dao, signatory_a, report_a):
        assert dao.get_report(signatory_a, report_a)["state"] == "draft"

    def test_state_tracks_the_workflow(self, dao, signatory_a, interp_a, report_a):
        dao.record_sole_signatory_claim(
            signatory_a, interpretation_id=interp_a, report_id=report_a, reason="Sole signatory concurrence."
        )
        dao.submit_for_review(signatory_a, report_a)
        assert dao.get_report(signatory_a, report_a)["state"] == "under_review"
        dao.approve_report(signatory_a, report_a)
        assert dao.get_report(signatory_a, report_a)["state"] == "approved"


# ───────────────────────────────────────────────────────────────────────────
# THE CONTROL
# ───────────────────────────────────────────────────────────────────────────


class TestTwoPersonPathIsUnchanged:
    """
    The one thing this change was forbidden to do. Everything here worked
    before w117 and must work identically after it.
    """

    def test_a_second_clinicians_accept_still_works_exactly_as_before(
        self, dao, conn, session_a, second_clinician_a, interpreter_a, approver_a, interp_a, report_a
    ):
        claim_id = dao._record_accept(
            session_a,
            interpretation_id=interp_a,
            report_id=report_a,
            actor_id=second_clinician_a.user_id,
            reason="Independently re-derived the same classification.",
        )
        assert claim_id is not None

        kind, variant_key, classification, actor_id, _reason = _claim_rows(conn, report_a)[0]
        assert kind == "accept", "an independent second clinician's concurrence is still an 'accept'"
        assert variant_key is None and classification is None
        assert actor_id == second_clinician_a.user_id

        dao.submit_for_review(interpreter_a, report_a)
        assert _report_row(conn, report_a)[0] == "under_review"

        dao._approve_report(approver_a, report_a, actor_id=approver_a.user_id)
        state, approver_id, approved_at, content_hash = _report_row(conn, report_a)
        assert state == "approved"
        assert approver_id == approver_a.user_id
        assert approved_at is not None and content_hash is not None

    def test_approving_still_refuses_an_actor_without_the_approver_role(
        self, dao, conn, session_a, second_clinician_a, interpreter_a, approver_a, interp_a, report_a
    ):
        """
        _approve_report's SECOND check -- the recorded approver's own roles --
        is the one a wrapper fixing actor_id to the session could have quietly
        made unreachable. It is still reachable and still refuses.
        """
        dao._record_accept(
            session_a,
            interpretation_id=interp_a,
            report_id=report_a,
            actor_id=second_clinician_a.user_id,
            reason="Independently re-derived the same classification.",
        )
        dao.submit_for_review(interpreter_a, report_a)
        with pytest.raises(AuthorizationError):
            dao._approve_report(approver_a, report_a, actor_id=second_clinician_a.user_id)
        assert _report_row(conn, report_a)[0] == "under_review"
