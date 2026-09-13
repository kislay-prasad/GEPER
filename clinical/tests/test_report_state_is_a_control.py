"""
reports.state is a CONTROL, and this file is what checks it.

WHY THIS FILE EXISTS. D0b's content-immutability trigger deliberately does not
police state: its own comment says it "only ever asks did content change, never
is this transition legal". That is a sound division of labour, but it leaves
`state` writable on an approved report -- measured, on a live database, as
clinical_app AND as the superuser:

    UPDATE reports SET state = 'draft'   on an APPROVED report   ->  PERMITTED

THAT IS NO LONGER TRUE, AND THIS FILE IS WHY. The residual was audited rather
than recorded; the audit established that state GATES DELIVERY and is therefore
a control; and the mechanism ruling was then revised so the trigger refuses a
walk-back outright. The paragraph above describes the world this file was written
in, kept because it is the reason the file exists.

WHAT CHANGED HERE WHEN THE GAP CLOSED. Fifteen of the original twenty tests went
red, which is not a defect in either piece of work: a walk-back on an approved
report was this file's shared SETUP, not just one test's subject. Fourteen tests
used it to construct an "approved then corrupted" report so they could assert
something about the gate, retrieval or blast radius. Prevention deleted the
executable evidence those tests were standing on.

The line that decided each one: IF THE TEST'S SUBJECT WAS THE GAP, it is retired
or inverted. IF THE TEST MERELY USED THE GAP AS A MECHANISM to reach a subject
about something else -- the gate, retrieval -- it is rewritten to construct the
state directly. That is not a loophole around this file's own fixture rule. The
rule is that a fixture must not use THE MECHANISM THE TEST MEASURES; those tests
measure the gate and retrieval, and a report CREATED in draft, under_review or
returned is a genuine report in that state rather than one pushed there.

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
  It never asserted that a walk-back was acceptable, and that is the whole reason
  this transition could be closed by editing the mechanism rather than by
  arguing with a test. A test that exists to document a gap should FAIL when the
  gap closes. These did, loudly, and the inverted premise test below now asserts
  the refusal instead.
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


def _attempt_state_write(conn, report_id, state):
    """
    Attempt a raw state write. USED ONLY WHERE THE ATTEMPT ITSELF IS THE SUBJECT.

    This was `_walk_state_back`, and it used to be this file's shared setup: it
    SUCCEEDED on an approved report, and fourteen tests leaned on that to build an
    "approved then corrupted" row. It cannot succeed on one any more -- the
    content-immutability trigger now refuses a walk-back from approved or
    released -- so it is deliberately no longer a fixture helper. Anything that
    needs a report in a non-delivery state constructs one in that state instead,
    via `report_in_state` below.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE reports SET state = %s WHERE id = %s", (state, report_id))
    conn.commit()


@pytest.fixture()
def report_in_state(dao, conn, session, approver, interp):
    """
    A report GENUINELY IN a given state, never one walked back into it.

    WHY THIS IS NOT THE BYPASS THIS FILE OTHERWISE FORBIDS. The rule is that a
    fixture must not use the mechanism the test measures. The tests using this
    fixture measure the DELIVERY GATE and RETRIEVAL -- neither of which requires
    the report to have passed through approval -- and the trigger is BEFORE
    UPDATE, so a report created in draft and left there is exactly a draft
    report. Where a real workflow path exists it is used in preference to a
    direct write, which is the same discipline `approved_report` follows.

      draft         create_report leaves it there. No write at all.
      under_review  the real path: record a reviewer claim, then submit_for_review.
      returned      no workflow path to it exists yet (spec 13.1 has the state;
                    the method that sends a report back is Phase 6 commit 3's
                    work, not written at this commit). Constructed by a direct
                    write on a report that is NOT approved -- a transition the
                    trigger permits and which is not the mechanism under test.
                    When that method lands, this arm should use it.
    """

    def _make(state):
        report_id = dao.create_report(session, interp)
        if state == "draft":
            return report_id
        if state == "under_review":
            dao._record_accept(
                approver,
                interp,
                report_id=report_id,
                actor_id=approver.user_id,
                reason="Independent concurrence with the engine's classification.",
            )
            dao.submit_for_review(session, report_id)
            return report_id
        if state == "returned":
            _attempt_state_write(conn, report_id, "returned")
            return report_id
        raise AssertionError(f"no construction path for state {state!r}")

    return _make


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
        report_id=report_id,
        actor_id=approver.user_id,
        reason="Independent concurrence with the engine's classification.",
    )
    dao.submit_for_review(session, report_id)
    dao._approve_report(approver, report_id, actor_id=approver.user_id)
    return report_id


class TestTheWalkBackIsNowRefused:
    """
    INVERTED, NOT DELETED. This class used to assert that an approved report's
    state COULD be rewritten -- it existed to document the residual, and to say
    out loud that if the walk-back ever stopped being possible the rest of the
    file was measuring a scenario that could no longer arise.

    That is exactly what happened. A test that exists to document a gap should
    fail when the gap closes, and this one did. It now asserts the refusal, so
    the closure is held in place rather than merely remembered -- and so that
    anyone reading the file can see that the gap was real, was measured, and was
    shut, rather than finding no trace of it.
    """

    def test_an_approved_report_cannot_have_its_state_walked_back(self, conn, approved_report):
        # The wall is named, as everywhere else in this suite: a bare PL/pgSQL
        # RAISE, so SQLSTATE P0001 and psycopg's RaiseException. Asserting merely
        # "something was raised" would pass on a NOT NULL violation.
        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException) as caught:
                cur.execute("UPDATE reports SET state = 'draft' WHERE id = %s", (approved_report,))
        conn.rollback()
        assert caught.value.sqlstate == "P0001"

        # AND THE REFUSAL HELD, not merely raised. Read back in the same test
        # rather than in one of its own: a separate test that only reads a
        # fixture nothing has written to cannot fail for the right reason -- it
        # would pass just as happily against a schema with no trigger at all.
        state = conn.execute("SELECT state FROM reports WHERE id = %s", (approved_report,)).fetchone()[0]
        assert state == "approved", "the report's state moved despite the refusal"

        # THE SUPERUSER IS BOUND, and that is the point of the mechanism ruling
        # rather than an aside: `conn` is the schema owner, so no GRANT is doing
        # this work and none could -- a grant cannot see OLD.state. Only a
        # BEFORE UPDATE trigger holds OLD and NEW together.

    def test_a_released_report_cannot_be_reopened_either(self, dao, conn, session, approver, approved_report):
        # The other arm of the check, and a distinct branch: release is the
        # platform's exit, so nothing legitimately reopens it -- released ->
        # anything is refused, not just approved -> draft. Covered separately
        # because a trigger that only guarded OLD.state = 'approved' would pass
        # every other test in this class while leaving released rows rewritable.
        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        dao._release_report(session, approved_report, consumer="LIMS", actor_id=approver.user_id)
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException) as caught:
                cur.execute("UPDATE reports SET state = 'under_review' WHERE id = %s", (approved_report,))
        conn.rollback()
        assert caught.value.sqlstate == "P0001"

    def test_but_the_one_legal_forward_transition_still_works(self, dao, conn, session, approver, approved_report):
        # THE PAIRED PERMITTED DIRECTION, and without it this class is satisfied
        # by a trigger that refuses every state change -- which would break
        # release outright while every refusal above went on passing. approved ->
        # released is precisely what _release_report performs.
        dao._release_report(session, approved_report, consumer="LIMS", actor_id=approver.user_id)
        state = conn.execute("SELECT state FROM reports WHERE id = %s", (approved_report,)).fetchone()[0]
        assert state == "released"


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
    def test_the_gate_refuses_every_non_delivery_state(self, dao, session, report_in_state, state):
        # REWRITTEN. This used to approve a report and walk it back into `state`,
        # which the trigger now refuses. The SUBJECT was never the walk-back --
        # it is the gate's decision rule -- so the report is now constructed in
        # the state directly and the assertion is unchanged.
        #
        # Parametrised over the states rather than written once, so a new state
        # added to the CHECK constraint without a decision about delivery shows
        # up here as an unlisted case rather than as silence.
        report_id = report_in_state(state)
        with pytest.raises(ValueError, match=state):
            dao.require_release(session, report_id)

    def test_amendment_creation_filters_on_state_too(self, dao, session, report_in_state):
        # REWRITTEN, same reasoning. The second path the read-side enumeration
        # found: amending requires the original to be approved or released, which
        # matters because amendment is the ISO 15189 7.4.1.8 mechanism for
        # correcting a report without altering the original.
        report_id = report_in_state("draft")
        with pytest.raises(ValueError, match="only approved or released"):
            dao.create_amendment(session, report_id, reason="corrected variant call")


# ─── RETIRED: three tests whose SUBJECT was the gap itself ───────────────────
#
# test_a_walked_back_report_is_refused_delivery, and the two in
# TestWhatAWalkBackDoesNotDestroy (the report stays retrievable; the release
# record survives).
#
# All three asked what happens AFTER an approved report has been walked back.
# That event can no longer occur -- the trigger refuses it, for clinical_app and
# for the superuser alike -- so they measured the blast radius of something that
# does not happen, and a test asserting a consequence of an impossible premise
# passes for the wrong reason forever.
#
# Prevention superseded them; it did not merely make them redundant. The
# properties they were protecting are not lost:
#   - that the gate refuses non-delivery states is still asserted above, per
#     state, on reports genuinely in those states.
#   - that retrieval does not depend on state is still asserted below, which is
#     the guard on a measured absence and the reason this file must not shrink
#     to only the tests that were easy to keep.
#   - that release_events outlives a report's own state is a property of the
#     append-only grant, already asserted from a real role login in
#     test_privilege_boundary.py.
# Recorded here rather than deleted silently, so the closure leaves a trace.


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

    @pytest.mark.parametrize("state", NON_DELIVERY_STATES)
    def test_get_report_returns_a_report_in_every_non_delivery_state(self, dao, session, report_in_state, state):
        # REWRITTEN to construct the state rather than walk back into it. THE
        # GUARD IS UNWEAKENED: it still fails the moment a state predicate is
        # added to a retrieval path, which is the only thing it was ever for.
        report_id = report_in_state(state)
        assert dao.get_report(session, report_id) is not None, (
            f"get_report stopped returning a report in '{state}' -- a state filter has been "
            "added to a retrieval path, which turns a workflow column into a visibility control"
        )

    @pytest.mark.parametrize("state", ("approved", "released"))
    def test_get_report_returns_a_delivery_state_report_too(self, dao, conn, session, approver, approved_report, state):
        # The other half, kept separate because these two states are now reached
        # through the workflow rather than by a raw write -- approved by the
        # fixture, released by the one forward transition the trigger permits.
        if state == "released":
            dao._release_report(session, approved_report, consumer="LIMS", actor_id=approver.user_id)
        assert dao.get_report(session, approved_report) is not None

    @pytest.mark.parametrize("state", NON_DELIVERY_STATES)
    def test_lineage_tracing_does_not_depend_on_state(self, dao, session, report_in_state, state):
        report_id = report_in_state(state)
        assert dao.trace_report_ancestors(session, report_id) is not None

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
