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

import ast
import datetime
from datetime import date, timezone
import json
import os
import pathlib
import re
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

    def test_get_report_returns_a_retracted_report_and_says_so(self, dao, session, approver, approved_report):
        """
        HUMAN RULING E7, and it is the same guard one wave later. A retracted
        report is STILL RETURNED, carrying the fact. Filtering it out would be
        a state filter on a retrieval path -- the exact property this class
        exists to forbid -- and would make a retracted report unfindable by the
        route geper/api/submission_worker.py uses on the duplicate-submission
        path.

        Both directions asserted on the VALUE of the flag, in one test, so
        neither "always True" nor "always False" survives.
        """
        assert dao.get_report(session, approved_report)["is_retracted"] is False
        dao.retract_report(approver, approved_report, reason="Sample mix-up: wrong patient's sample.")
        after = dao.get_report(session, approved_report)
        assert after is not None, "get_report stopped returning a retracted report -- that is a state filter"
        assert after["is_retracted"] is True
        assert after["state"] == "approved", "the reports row moved; retraction must not touch it"

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


# ─── HUMAN RULING E10 (w122): the enumeration that catches a forgetful reader ─
#
# THE RESIDUAL THIS ANSWERS, STATED HONESTLY BECAUSE IT IS REAL. Under the
# approved retraction model (model 3) the retraction lives in its own table
# beside the report, and NOTHING IN THE DATABASE stops a reader that checks
# `reports.state` and forgets to check for a retraction. The trigger cannot
# help -- there is no row to refuse. The grant cannot help -- reading is
# permitted. A sixth state would have refused a retracted report in every
# existing reader for free, and would have bought that by rewriting a row that
# says a report was approved into one that says it was not.
#
# The human accepted that trade on the condition that the forgetfulness is
# DETECTED. This is the detector, and its design matters more than its
# existence: an enumeration that pinned the CURRENT SET of composing methods
# would pass happily while a new reader forgot. What is pinned instead is the
# RELATIONSHIP -- "gates on the delivery states" implies "composes retraction"
# -- so a method written tomorrow is measured by the same rule.
#
# HOW A "READER" IS IDENTIFIED, and why this shape rather than a grep. It is
# a method defined directly in class DataAccess, in this one file, whose body
# contains an ==, !=, in or not in comparison that NAMES AT LEAST ONE DELIVERY
# STATE as a string constant. The name may appear directly ('approved'), inside
# a literal tuple/list/set, or through a module-level constant bound to such a
# literal. That is the whole rule, and it is deliberately one rule rather than
# a shape per call site.
#
# WHAT THE RULE USED TO BE, AND WHY IT WAS WIDENED (w123). It used to demand
# all three of: an @auditable CALL decorator, a containment operator, and a
# literal sequence holding BOTH states. Measured against a4907d3 with three
# mutant readers inserted into DataAccess, only one of the three was caught:
#   decorated   + `if state in ('approved', 'released')`   CAUGHT
#   decorated   + `if state == 'released'`                 MISSED -- 21 passed
#   undecorated + `if state in ('approved', 'released')`   MISSED -- 21 passed
# Dropping the decorator requirement and admitting equality against a single
# delivery state catches all three. The decorator requirement was the weakest
# of the three anyway: it asserted a convention (every entry point is audited)
# as if it were the definition of a reader, so the cheapest way to escape the
# detector was to write the gate in a helper -- which is where gates normally
# end up.
#
# NEGATION FORMS, AND THE ONE THAT IS DELIBERATELY NOT MATCHED.
#   INCLUDED: `not in ('approved', 'released')` and `!= 'released'`. Those are
#   the canonical shapes in this file -- require_release, retract_report and
#   create_amendment all raise on `state not in (...)`, and _release_report
#   tests `state != 'released'`. A decision taken BY NEGATING a delivery state
#   is still a decision taken on the delivery states.
#   NOT INCLUDED: the complement over NON-delivery states -- `!= 'draft'`,
#   `not in ('draft', 'under_review', 'returned')`. It admits the delivery
#   states by omission, so on paper it is a delivery gate too. In practice it
#   is the shape of every ordinary workflow precondition: place_order tests
#   `orders.state != 'draft'` and has nothing to do with reports at all;
#   submit_for_review and _approve_report gate on ENTRY to the workflow, not on
#   EXIT to a consumer. Matching it would pull three unrelated methods in and
#   each would need an exemption, and a detector that hands out exemptions to
#   stay quiet is how this guarantee dies. It is listed as a blind spot on the
#   test below rather than silently dropped.
#
# HOW COMPOSITION IS IDENTIFIED: the method calls `self._read_retraction_row`,
# the one-row reader a gate asks the retraction question with. Routing every
# COMPOSER through one helper is what makes this question mechanical instead
# of textual. Note what this does and does not establish: it proves the
# QUESTION WAS ASKED, not that the answer was used.
#
# AND NOTE WHAT IT DOES NOT SAY (w124). That helper is not the only code that
# reads `report_retractions` -- its w122 docstring claimed to be and was
# wrong. `_read_retracted` and clinical/retention.py's tombstone cascade each
# run their own SELECT, for reasons recorded at both sites. The property this
# enumeration needs is narrower and is the one that holds: every DELIVERY-STATE
# GATE in DataAccess goes through the helper. test_the_retraction_reader_
# docstring_names_every_reader_of_the_table below measures the wider set so
# the narrower claim cannot quietly turn back into a census.

_DATA_ACCESS = pathlib.Path(__file__).resolve().parent.parent / "data_access.py"

DELIVERY_STATES = frozenset({"approved", "released"})

# The one-row retraction read. A composer calls it; a forgetful reader does
# not. (Not the only reader of the table -- see the note above.)
RETRACTION_READER = "_read_retraction_row"

# Deliberately exempt, with a reason as specific as the ones in
# test_w120_audit_resource_id.py's own exemption list. Growing this list needs
# a ruling, not a convenience.
RETRACTION_COMPOSITION_EXEMPT = {
    # HUMAN RULING E4: "a replacement is optional; a retracted report may
    # still be amended." Amendment is the mechanism that keeps traceability
    # from a corrected document back to the one it corrects, so it is
    # DELIBERATELY indifferent to retraction -- forbidding it would push a lab
    # toward issuing an unlinked new report and losing the link. This is the
    # one gate whose correct behaviour is to not care.
    "create_amendment",
    # PULLED INTO SCOPE BY THE w123 WIDENING, listed here rather than left to
    # be discovered, because an exemption nobody wrote down is the same thing
    # as a matcher that never matched.
    #
    # It IS the retraction reader -- the function that turns a retraction row
    # into the banner a report prints -- so requiring it to compose retraction
    # is requiring it to call itself. It reaches the retraction table with its
    # own SELECT rather than through _read_retraction_row -- w122 called that a
    # crack worth fixing; w124 measured it and ruled the other way. The helper
    # is a `_query_one` and this reader must see a SECOND row to refuse it, so
    # the CODE is right and it was the helper's "one place" SENTENCE that was
    # wrong. That sentence is now corrected; this exemption stands on the
    # reader/composer distinction rather than on a defect. Its `== 'approved'` /
    # `== 'released'` comparisons are against retracted_from_state -- the state
    # the report was retracted FROM, stored on the retraction row -- not
    # against reports.state, so it gates nothing that reaches a consumer.
    "_read_retracted",
    # Also pulled in by the widening (undecorated helper, `in ('approved',
    # 'released')`). Its membership test is about the AMENDMENT's state, not
    # the state of the report being read: it picks the latest amendment that
    # has actually been issued. Human ruling E4 again -- a retracted report may
    # still be amended, and the amendment banner must keep naming its
    # amendment. The caller that assembles the report around it is the one that
    # must compose retraction, and get_report does.
    "_read_superseded_by",
}


def _module_level_state_constants(tree):
    """
    Module-level names bound to a literal sequence of strings.

    Only module level, and only a literal: a name assigned inside a function,
    or built by a call this walk cannot evaluate, is not resolved and the
    comparison that uses it is simply not matched. Guessing at a value would
    be worse than a stated blind spot.
    """
    constants = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            # frozenset({...}) / set([...]) / tuple(...) around a literal.
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in ("frozenset", "set", "tuple", "list")
                and len(value.args) == 1
            ):
                value = value.args[0]
            if not isinstance(value, (ast.Tuple, ast.List, ast.Set)):
                continue
            strings = {e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = strings
    return constants


def _names_a_delivery_state(node, constants):
    """
    Does this comparison operand name at least one delivery state?

    Three shapes, and only three: the bare constant ('approved'), a literal
    sequence containing one ({'approved', 'released'}), and a module-level
    name bound to such a literal. Anything else -- an enum member, an f-string,
    a value fetched from a column, a name assigned locally -- is not resolved.
    """
    if isinstance(node, ast.Constant):
        return node.value in DELIVERY_STATES
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(isinstance(e, ast.Constant) and e.value in DELIVERY_STATES for e in node.elts)
    if isinstance(node, ast.Name):
        return bool(constants.get(node.id, frozenset()) & DELIVERY_STATES)
    return False


def _delivery_state_gates(tree):
    """
    Every DataAccess method whose body compares something against a delivery
    state, with whether it composes retraction.

    Decorated or not; ==, !=, in or not in; one state or both. See the block
    comment above for why each of those was widened and for the one negation
    form (the complement over NON-delivery states) that is left out.
    """
    constants = _module_level_state_constants(tree)
    found = {}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DataAccess"]:
        for node in cls.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            gates = False
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Compare):
                    continue
                for op, comparator in zip(inner.ops, inner.comparators):
                    if isinstance(op, (ast.In, ast.NotIn)):
                        # `x in SEQ`: only the container names the states.
                        if _names_a_delivery_state(comparator, constants):
                            gates = True
                    elif isinstance(op, (ast.Eq, ast.NotEq)):
                        # Either side, so `'approved' == state` is not a hole.
                        if _names_a_delivery_state(comparator, constants) or _names_a_delivery_state(
                            inner.left, constants
                        ):
                            gates = True
            if not gates:
                continue
            composes = any(
                isinstance(inner, ast.Attribute) and inner.attr == RETRACTION_READER for inner in ast.walk(node)
            )
            found[node.name] = composes
    return found


def test_every_delivery_state_gate_composes_retraction():
    """
    THE RELATIONSHIP, not the roster -- within one file, over one shape.

    WHAT IT ENFORCES, EXACTLY. Every method defined directly in class
    DataAccess in clinical/data_access.py whose PYTHON SOURCE contains an
    ==, !=, in or not in comparison naming 'approved' or 'released' as a
    string constant (bare, in a literal sequence, or via a module-level
    constant bound to one) must also call self._read_retraction_row, or be
    named in RETRACTION_COMPOSITION_EXEMPT with a reason.

    WHAT IT CANNOT SEE. This is an AST walk over one Python file, and the
    blind spots are not hypothetical -- the first one is a shape this very
    codebase uses on every query:

      1. SQL STRING GATES, and this is permanent. A gate written as
         `WHERE state IN ('approved', 'released')` -- or `state = 'approved'`,
         or pushed down into a view, a stored function or a trigger -- is a
         string constant to Python. An AST walk sees a str, not a comparison,
         and no amount of widening changes that without becoming a grep over
         SQL text. A delivery path that filters in the database and never
         mentions a state in Python passes this test while forgetting
         retraction entirely.
      2. INDIRECTION IN THE STATE NAME. An enum member (ReportState.APPROVED),
         an f-string, a locally-assigned variable, a value read from another
         row, a set built by a call -- none resolve here. Only bare constants,
         literal sequences and module-level constants bound to literals do.
      3. ANYTHING OUTSIDE DataAccess. Gates in geper/api routes, in
         submission_worker, in a renderer, or in a subclass or mixin, are not
         walked. Only methods in this one class body in this one file are.
      4. COMPOSITION IS A CALL, NOT A USE. Matching self._read_retraction_row
         proves the question was ASKED. A method that calls it and throws the
         answer away passes. Nothing here checks what the caller does with a
         retraction it found.
      5. THE COMPLEMENT FORM IS NOT MATCHED ON PURPOSE. `state != 'draft'`
         admits the delivery states by omission and is not treated as a
         delivery gate; see the block comment above for why, and for the three
         methods that would otherwise have needed exemptions.

    So: this catches a forgetful reader that decides in Python, in this class,
    by naming a delivery state. That is narrower than "a reader added
    tomorrow", which is what this docstring used to claim. The residual is
    real and the human accepted it against model 3; an overclaim here is worse
    than the narrower guarantee honestly described, because the overclaim is
    what gets quoted into a decision.
    """
    gates = _delivery_state_gates(ast.parse(_DATA_ACCESS.read_text(encoding="utf-8")))

    # THE ENUMERATION MUST NOT BE EMPTY, and this is not padding. If the AST
    # shape above ever stops matching anything -- a refactor to a module-level
    # constant, a decorator rename -- `offenders` is empty and the assertion
    # below passes forever while detecting nothing. That is the cannot-fail
    # shape this project has already been bitten by four times in one night.
    assert gates, (
        "the delivery-state enumeration matched NO methods at all; the AST shape it looks for has "
        "stopped describing this file and it is now detecting nothing"
    )
    # And it must still find the gates we know are there, BY NAME, so a method
    # cannot slip out of scope by losing its decorator or rewriting its
    # comparison into a shape the walk does not see. Each of the last three
    # pins one ARM of the w123 widening, so that reverting the widening fails
    # here rather than passing quietly on a matcher that sees less:
    #   retract_report      the `not in (...)` gate (unchanged behaviour)
    #   _read_retracted     equality against a SINGLE delivery state
    #   _read_superseded_by an UNDECORATED helper
    assert {
        "require_release",
        "_release_report",
        "create_amendment",
        "retract_report",
        "_read_retracted",
        "_read_superseded_by",
    } <= set(gates), sorted(gates)

    offenders = sorted(
        name for name, composes in gates.items() if not composes and name not in RETRACTION_COMPOSITION_EXEMPT
    )
    assert not offenders, (
        f"DataAccess method(s) that gate on {sorted(DELIVERY_STATES)} without composing retraction: "
        f"{offenders}. A reader that checks approval and forgets retraction will hand over a report "
        f"the lab has withdrawn, and no database constraint stops it (that is the accepted residual of "
        f"the approved model). Call self.{RETRACTION_READER}(session, report_id) and decide what to do "
        f"with the answer, or add the method to RETRACTION_COMPOSITION_EXEMPT with a ruling behind it."
    )


def test_the_exempt_gates_still_exist():
    """
    An exemption for a method that has been renamed or deleted is a hole
    nobody can see. Pairs with the assertion above: without this, emptying
    the codebase would satisfy it.
    """
    gates = _delivery_state_gates(ast.parse(_DATA_ACCESS.read_text(encoding="utf-8")))
    stale = sorted(RETRACTION_COMPOSITION_EXEMPT - set(gates))
    assert not stale, f"exempted method(s) that no longer gate on the delivery states: {stale}"


# ─── w124: the census the definite article was claiming ───────────────────────
#
# THE DEFECT THIS EXISTS FOR WAS A SENTENCE, not a behaviour. w122 wrote that
# _read_retraction_row is "THE ONE PLACE retraction is read from the database";
# _read_retracted was already reaching the same table with its own SELECT, and
# clinical/retention.py's tombstone cascade with a third. HUMAN RULING (w124):
# fix the claim rather than the code -- the helper is a `_query_one` and
# _read_retracted must see a SECOND row in order to refuse it, so routing the
# reader through the helper would cost a real refusal to buy a true sentence.
#
# THE TELL, in the human's words: THE DEFINITE ARTICLE. "The single read
# point", "the one gate", "the only place" -- each is claiming a census, and a
# census has to be measured. This test is that measurement, kept executable so
# the corrected sentence cannot rot back into the claim it replaced. It reads
# the docstring it is about, in the shape TestHpoTermsDocstringCitations uses.

_RETENTION = pathlib.Path(__file__).resolve().parent.parent / "retention.py"

# A SELECT against the table, in the only form this codebase writes queries:
# an SQL string literal inside a method. UPDATE/INSERT are writes and are not
# counted; the claim under test is about READS.
_RETRACTION_TABLE_READ = re.compile(r"FROM\s+report_retractions")

# Measured, at w124, over clinical/data_access.py and clinical/retention.py.
# Every one of these is named in _read_retraction_row's docstring with what it
# is for. A fourth reader makes this test red, which is the point: the
# docstring now enumerates, and an enumeration nobody re-measures is the
# census claim again with extra steps.
EXPECTED_RETRACTION_TABLE_READERS = {
    "data_access.py::_read_retraction_row",
    "data_access.py::_read_retracted",
    "retention.py::_cascade_tombstone_report_dependents",
}

# The four DataAccess methods that ask the retraction question through the
# helper. The w122 docstring listed "the delivery gate, the release recorder,
# the revision reader, get_report" -- and the revision reader (_read_retracted)
# was never one of them, while retract_report's own already-retracted refusal
# was left out. Measured here so a wrong roster cannot be written twice.
EXPECTED_HELPER_CALLERS = {
    "get_report",
    "_release_report",
    "require_release",
    "retract_report",
}


def _functions_with_sql_matching(path, pattern):
    """
    Names of functions in `path` holding an SQL string literal that matches.

    The DOCSTRING IS SKIPPED DELIBERATELY. This file's whole subject is a
    docstring that talks about queries; a prose mention of one is not one, and
    counting it would make the guard agree with whatever the docstring says.
    """
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        for statement in body:
            for inner in ast.walk(statement):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str) and pattern.search(inner.value):
                    found.add(node.name)
    return found


def _data_access_methods_calling(attr):
    tree = ast.parse(_DATA_ACCESS.read_text(encoding="utf-8"))
    callers = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "DataAccess":
            continue
        for method in node.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or method.name == attr:
                continue
            for inner in ast.walk(method):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) and inner.func.attr == attr:
                    callers.add(method.name)
    return callers


def _retraction_reader_docstring():
    for node in ast.walk(ast.parse(_DATA_ACCESS.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == RETRACTION_READER:
            return ast.get_docstring(node) or ""
    raise AssertionError(f"{RETRACTION_READER} is gone from clinical/data_access.py; its docstring cannot be checked")


def test_the_retraction_reader_docstring_names_every_reader_of_the_table():
    """
    The corrected sentence is true of the code, measured rather than recalled.

    Two things, and each fails for a different real reason:
      1. the set of functions that SELECT from report_retractions is exactly
         the three the docstring enumerates -- a fourth reader, or the loss of
         one, is red;
      2. the docstring names each of the other two readers, so the
         enumeration cannot be quietly emptied back to the helper alone.

    NO GREP FOR THE OFFENDING PHRASE, deliberately. The corrected docstring
    QUOTES the false sentence in order to record that it was false, so a check
    for "the one place" would fire on the correction itself. Measuring the
    readers is the check that has teeth anyway: the phrase was only ever wrong
    because the set had three members.

    WHAT IT CANNOT SEE, stated rather than implied: SQL built by concatenation
    or f-string, a read through a view, and any module other than
    data_access.py and retention.py. It is the same AST blind spot the E10
    enumeration above documents, and for the same reason.
    """
    measured = {
        f"data_access.py::{name}" for name in _functions_with_sql_matching(_DATA_ACCESS, _RETRACTION_TABLE_READ)
    }
    measured |= {f"retention.py::{name}" for name in _functions_with_sql_matching(_RETENTION, _RETRACTION_TABLE_READ)}

    assert measured, (
        "no function in data_access.py or retention.py appears to SELECT from report_retractions at all; "
        "the SQL shape this guard looks for has stopped describing the codebase and it is now measuring nothing"
    )
    assert measured == EXPECTED_RETRACTION_TABLE_READERS, (
        f"the readers of report_retractions have changed: {sorted(measured)}. _read_retraction_row's docstring "
        f"enumerates them by name and says what each is for; update the docstring AND this set together, and "
        f"do not replace the enumeration with a count or with 'the one place' -- that sentence was already "
        f"false once (w124)."
    )

    docstring = _retraction_reader_docstring()
    for name in ("_read_retracted", "retention.py"):
        assert name in docstring, (
            f"{RETRACTION_READER}'s docstring no longer names {name!r}, which reads the same table with its "
            f"own SELECT. A docstring that stops naming the other readers is back to claiming to be the only one."
        )


def test_the_retraction_reader_docstring_names_its_actual_callers():
    """
    The w122 docstring's roster of composers named a method that does not call
    it and omitted one that does. Rosters in prose drift; this one is read.
    """
    callers = _data_access_methods_calling(RETRACTION_READER)
    assert callers == EXPECTED_HELPER_CALLERS, (
        f"the callers of {RETRACTION_READER} have changed: {sorted(callers)}. Its docstring lists them with "
        f"the ruling each one serves; update both."
    )
    docstring = _retraction_reader_docstring()
    missing = sorted(name for name in EXPECTED_HELPER_CALLERS if name not in docstring)
    assert not missing, f"{RETRACTION_READER}'s docstring does not name its caller(s) {missing}"
