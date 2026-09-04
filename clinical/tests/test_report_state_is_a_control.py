"""
reports.state is a CONTROL, and this file is what checks it.

WHY THIS FILE EXISTS. D0b's content-immutability trigger deliberately does not
police state: its own comment says it "only ever asks did content change, never
is this transition legal". That is a sound division of labour, but it leaves
`state` writable on an approved report -- measured, on a live database, as
clinical_app AND as the superuser:

    UPDATE reports SET state = 'draft'   on an APPROVED report   ->  PERMITTED

I reported that as a residual. The ruling was to AUDIT it rather than record it,
on the grounds that if the delivery gate filters on state then that filtering is
a control, and an unchecked control is a thing this codebase has been bitten by
before.

THE AUDIT'S ANSWER: it does. require_release() -- "the one gate every delivery
path calls" -- refuses any report whose state is not approved or released, and
create_amendment() refuses on the same basis. Both were already tested for a
report that was NEVER approved (draft, under_review). NEITHER was tested for a
report that WAS approved and is then walked BACK, which is the case the residual
is actually about, and the case where the consequence is not "delivery correctly
refused" but "an approved report effectively WITHDRAWN from the record" -- ISO
15189 7.4.1.8 from the other direction, reached without touching one content
column.

WHAT THIS FILE ASSERTS, AND WHAT IT DELIBERATELY DOES NOT.
  It LOCKS the filtering, because the filtering is the control: if a future
  change dropped the state check, unapproved reports would leave the platform,
  which is worse than the residual itself.
  It also BOUNDS THE BLAST RADIUS by measuring what a walk-back does NOT destroy
  -- the report stays retrievable and the release record survives, because
  release_events is append-only. Those are real mitigations and they should be
  held in place by a test rather than remembered.
  It does NOT assert that a walk-back is acceptable. Locking a known gap in as
  expected behaviour would be the wrong shape entirely: the tests below name the
  exposure as an exposure, and closing it -- if the human rules that way -- is a
  change to the mechanism, not to this file's expectations.
"""

from __future__ import annotations

import datetime
from datetime import date, timezone
import json
import os
import uuid

import pytest

from clinical.data_access import (
    BcryptHasher,
    DataAccess,
    SystemClock,
)

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = __import__("pathlib").Path(__file__).parent.parent / "schema.sql"

_CREATE_APP = "DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
_CREATE_RETENTION = "DO $$ BEGIN CREATE ROLE clinical_retention; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"

# Every state a report can hold that is NOT a delivery state. Named explicitly
# rather than derived from the CHECK constraint: a test that reads its
# expectations out of the thing it is testing agrees with it by construction.
NON_DELIVERY_STATES = ("draft", "under_review", "returned")


@pytest.fixture()
def conn():
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")
    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(_CREATE_APP)
        cur.execute(_CREATE_RETENTION)
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def dao(conn):
    return DataAccess(conn, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))


@pytest.fixture()
def org(dao):
    return dao.create_organisation("State Control Org")


@pytest.fixture()
def session(dao, conn, org):
    user_id = dao.create_user(org, "admin@state.test", "password")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org, user_id, datetime.datetime(2026, 9, 1, tzinfo=timezone.utc)),
        )
    conn.commit()
    admin = dao.login("admin@state.test", org, "password")
    # The Interpreter role as well: create_report requires it, and this file needs
    # one session that can build a report and then ask the gate about it. Roles
    # are additive here, so the session stays an Administrator too.
    dao.assign_role(admin, user_id, "Interpreter", basis="Clinical scientist, local test fixture")
    return dao.login("admin@state.test", org, "password")


@pytest.fixture()
def approver(dao, session, org):
    user_id = dao.create_user(org, "approver@state.test", "password")
    dao.assign_role(session, user_id, "Approver", basis="Board-certified clinical geneticist, licence #A-001")
    return dao.login("approver@state.test", org, "password")


@pytest.fixture()
def interp(dao, conn, session):
    """The full FK chain an interpretation needs."""
    org_id = session.org_id
    now = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    test_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) "
            "VALUES (%s, %s, 'Panel', 'GRCh38', 'active', %s)",
            (test_id, org_id, now),
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

    sample_id, run_id, vcf_id, interp_id = (uuid.uuid4() for _ in range(4))
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
            "VALUES (%s, %s, %s, '/tmp/x.vcf', %s, %s, %s)",
            (org_id, vcf_id, run_id, "0" * 64, now, session.user_id),
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


def _walk_state_back(conn, report_id, state):
    """
    The residual, exercised: move a report's state by raw SQL.

    This is the write the content-immutability trigger permits by design -- it
    asks whether content changed, never whether a transition is legal -- and it
    is permitted to clinical_app and to the superuser alike. If this UPDATE ever
    starts failing, the mechanism has changed and the tests below are measuring
    something other than what they claim.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE reports SET state = %s WHERE id = %s", (state, report_id))
    conn.commit()


@pytest.fixture()
def approved_report(dao, conn, session, approver, interp):
    """
    A report taken through the REAL workflow -- claim, submit, approve -- rather
    than pushed into 'approved' by a raw UPDATE.

    That matters here specifically: this file is about what a raw state write
    does, so the starting state has to be one the application itself produced.
    A fixture that wrote 'approved' directly would be exercising the same bypass
    the tests are meant to be measuring, and would prove nothing about the
    difference between the two.
    """
    report_id = dao.create_report(session, interp)
    # spec 13.2: a report cannot be submitted with zero reviewer claims, because
    # that would present unreviewed engine output as reviewed. An explicit accept
    # counts and is the point of it.
    dao._record_accept(
        approver,
        interp,
        actor_id=approver.user_id,
        reason="Independent concurrence with the engine's classification.",
    )
    dao.submit_for_review(session, report_id)
    dao._approve_report(approver, report_id, actor_id=approver.user_id)
    return report_id


class TestTheWalkBackIsPossibleAtAll:
    """
    The premise of every other test here. If this stopped being true the residual
    would be closed and the rest of this file would be asserting against a
    scenario that can no longer arise -- passing, but meaningless.
    """

    def test_an_approved_reports_state_can_still_be_rewritten(self, conn, dao, session, approved_report):
        _walk_state_back(conn, approved_report, "draft")
        state = conn.execute("SELECT state FROM reports WHERE id = %s", (approved_report,)).fetchone()[0]
        assert state == "draft", (
            "the walk-back was refused -- state is no longer freely writable, so the "
            "residual this file audits has been closed and these tests need revisiting"
        )

    def test_the_approval_facts_did_not_move_with_it(self, conn, dao, session, approved_report):
        # WHAT LIMITS THE EXPOSURE, and the reason it is a residual rather than a
        # breach: the trigger still freezes the approval facts, so a report walked
        # back to draft cannot be re-approved with a DIFFERENT content hash. The
        # tampering path that would actually matter stays shut.
        before = conn.execute(
            "SELECT approver_id, approved_at, content_hash FROM reports WHERE id = %s",
            (approved_report,),
        ).fetchone()
        _walk_state_back(conn, approved_report, "draft")
        after = conn.execute(
            "SELECT approver_id, approved_at, content_hash FROM reports WHERE id = %s",
            (approved_report,),
        ).fetchone()
        assert before == after, "approval facts moved with the state -- the trigger is not holding"


class TestTheDeliveryGateFiltersOnState:
    """
    THE CONTROL. require_release is described in data_access as "the one gate
    every delivery path calls", and it decides by state alone. That makes the
    state column a security-relevant input to a delivery decision, which is
    exactly the kind of thing that must be asserted rather than assumed.
    """

    def test_an_approved_report_is_delivered(self, dao, session, approved_report):
        # THE PERMITTED DIRECTION, and it is not padding: without it, a gate that
        # refused everything would satisfy every refusal test below.
        dao.require_release(session, approved_report)

    def test_a_released_report_is_still_delivered(self, dao, session, approver, approved_report):
        dao._release_report(session, approved_report, consumer="LIMS", actor_id=approver.user_id)
        dao.require_release(session, approved_report)

    @pytest.mark.parametrize("state", NON_DELIVERY_STATES)
    def test_the_gate_refuses_every_non_delivery_state(self, dao, conn, session, approved_report, state):
        # Parametrised over the states rather than written once, so that a new
        # state added to the CHECK constraint without a decision about delivery
        # shows up here as an unlisted case rather than as silence.
        _walk_state_back(conn, approved_report, state)
        with pytest.raises(ValueError, match=state):
            dao.require_release(session, approved_report)

    def test_a_walked_back_report_is_refused_delivery(self, dao, conn, session, approved_report):
        """
        THE RESIDUAL, NAMED. This is not the same test as "a draft report is
        refused", which already existed: this report WAS approved, and delivery
        of it is now refused because a raw-SQL write moved one column that no
        content-immutability mechanism guards.

        The refusal is CORRECT -- the gate is doing its job. What this test
        records is that the gate's correctness is precisely what converts a state
        rewrite into an effective WITHDRAWAL of an approved report, with no
        content column touched. It is asserted here so the behaviour cannot
        change silently, NOT because the exposure is accepted.
        """
        dao.require_release(session, approved_report)  # delivered before

        _walk_state_back(conn, approved_report, "draft")

        with pytest.raises(ValueError, match="no report leaves the platform without approval"):
            dao.require_release(session, approved_report)

    def test_amendment_creation_filters_on_state_too(self, dao, conn, session, approved_report):
        # The second path the enumeration found. Amending a report requires it to
        # be approved or released, so a walk-back blocks amendment as well --
        # which matters because amendment is the ISO 15189 7.4.1.8 mechanism for
        # correcting a report without altering the original.
        _walk_state_back(conn, approved_report, "draft")
        with pytest.raises(ValueError, match="only approved or released"):
            dao.create_amendment(session, approved_report, reason="corrected variant call")


class TestWhatAWalkBackDoesNotDestroy:
    """
    THE BLAST RADIUS, measured rather than assumed. A residual is only as bad as
    what it actually reaches, and two things bound this one. Both are held here
    so they cannot quietly stop being true.
    """

    def test_the_report_itself_is_still_retrievable(self, dao, conn, session, approved_report):
        # get_report does NOT filter on state -- established by reading it, not by
        # searching for the word. So the record is not lost: it is undeliverable,
        # which is a different and smaller thing.
        _walk_state_back(conn, approved_report, "draft")
        assert dao.get_report(session, approved_report) is not None

    def test_the_release_record_survives(self, dao, conn, session, approver, approved_report):
        # release_events is append-only by grant, so the evidence that a report
        # WAS released outlives any later rewrite of the report's own state. The
        # audit trail does not walk back with it.
        dao._release_report(session, approved_report, consumer="LIMS", actor_id=approver.user_id)
        _walk_state_back(conn, approved_report, "draft")
        rows = conn.execute("SELECT consumer FROM release_events WHERE report_id = %s", (approved_report,)).fetchall()
        assert [r[0] for r in rows] == ["LIMS"], "the release record did not survive the walk-back"


class TestRetrievalDoesNotFilterOnState:
    """
    THE GUARD ON A MEASURED ABSENCE, which the ruling asked for in as many words:
    an absence that nothing guards will not stay absent.

    The read-side enumeration found that get_report and trace_report_ancestors
    narrow on org and id only -- no state predicate in the SQL, and no
    state-dependent narrowing in Python after the rows return. That is a
    deliberate property: retrieval of an existing record must not depend on a
    mutable workflow column, or a state rewrite would make records vanish from
    view rather than merely become undeliverable.

    These tests fail if a state filter is ever added to a retrieval path.
    """

    @pytest.mark.parametrize("state", NON_DELIVERY_STATES + ("approved", "released"))
    def test_get_report_returns_the_report_in_every_state(self, dao, conn, session, approved_report, state):
        _walk_state_back(conn, approved_report, state)
        assert dao.get_report(session, approved_report) is not None, (
            f"get_report stopped returning a report in '{state}' -- a state filter has been "
            "added to a retrieval path, which turns a workflow column into a visibility control"
        )

    @pytest.mark.parametrize("state", NON_DELIVERY_STATES)
    def test_lineage_tracing_does_not_depend_on_state(self, dao, conn, session, approved_report, state):
        _walk_state_back(conn, approved_report, state)
        assert dao.trace_report_ancestors(session, approved_report) is not None

    def test_a_missing_report_is_still_a_miss(self, dao, session):
        # THE KNOWN-POSITIVE for this class, and it is load-bearing: if get_report
        # returned something for every id, every assertion above would pass while
        # proving nothing at all about state.
        #
        # It returns None rather than raising -- measured, not assumed. That is
        # get_report's documented contract (Optional[dict]), unlike require_release,
        # which raises NotFoundError. Asserting the wrong one here would have made
        # this guard fail for a reason that has nothing to do with what it guards.
        assert dao.get_report(session, uuid.uuid4()) is None
