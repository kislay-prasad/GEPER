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
# HOW A "READER" IS IDENTIFIED, and why this shape rather than a grep:
#   1. It is a method of DataAccess decorated with @auditable. Every entry
#      point into this class is; a delivery path written outside one would
#      have no audit trail at all and would fail w120's own structural test
#      long before it reached here.
#   2. Its body compares something against the DELIVERY-STATE PAIR -- a
#      containment test whose right-hand side is a literal sequence holding
#      both 'approved' and 'released'. That pair is precisely "may this
#      document go to a consumer", and it is the decision retraction has to
#      travel with. Plain helpers that happen to mention the two states about
#      SOME OTHER report (_read_superseded_by tests the AMENDMENT's state) are
#      not entry points and are deliberately out of scope.
#
# HOW COMPOSITION IS IDENTIFIED: the method calls `self._read_retraction_row`,
# the one place retraction is read from the database. Routing every composer
# through one helper is what makes this question mechanical instead of
# textual.

_DATA_ACCESS = pathlib.Path(__file__).resolve().parent.parent / "data_access.py"

DELIVERY_STATES = frozenset({"approved", "released"})

# The one place retraction is read. A composer calls it; a forgetful reader
# does not.
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
}


def _delivery_state_gates(tree):
    """
    Every @auditable DataAccess method whose body tests membership in the
    delivery-state pair, with whether it composes retraction.
    """
    found = {}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DataAccess"]:
        for node in cls.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            decorated = any(
                isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "auditable"
                for dec in node.decorator_list
            )
            if not decorated:
                continue
            gates = False
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Compare):
                    continue
                for op, comparator in zip(inner.ops, inner.comparators):
                    if not isinstance(op, (ast.In, ast.NotIn)):
                        continue
                    if not isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                        continue
                    values = {
                        e.value for e in comparator.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    }
                    if DELIVERY_STATES <= values:
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
    THE RELATIONSHIP, not the roster. A reader added tomorrow that asks
    "is this report approved or released" and does not ask "has it been
    retracted" fails here -- which is the whole of what the human accepted
    in place of a database constraint.
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
    # comparison into a shape the walk does not see.
    assert {"require_release", "_release_report", "create_amendment"} <= set(gates), sorted(gates)

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
