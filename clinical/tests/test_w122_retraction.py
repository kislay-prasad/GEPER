"""
clinical/tests/test_w122_retraction.py
──────────────────────────────────────

Report retraction, model 3: the approved `reports` row stays BYTE-IDENTICAL
and the retraction is one row in an append-only table beside it.

WHAT THIS FILE HAS TO PROVE, and it is not "retraction works":

  1. THE APPROVED ROW IS UNTOUCHED. Every column of `reports`, compared before
     and after, not just the four the grant admits. That single assertion is
     what the whole design rests on -- a sixth state, or a column, would have
     made the record deny an approval that happened.
  2. `verify_report_integrity` still returns True afterwards, proving the
     retraction stayed OUT of the content hash. A false alarm on an integrity
     check teaches people to ignore integrity checks.
  3. E1's basis is the STORED fact, not a lookup: a report retracted while
     'approved' and released afterwards must still say "approved on", because
     that is what was true when the lab acted.
  4. E3: the gate PERMITS delivery and SAYS the report is retracted.
  5. E6: one notification debt per CONSUMER, recorded as OWED, never as sent.
  6. E8: the retraction cascade-tombstones with its report, and the purge
     clock is unchanged.
  7. Cross-org: another organisation can neither retract this report nor see
     the retraction.

NO COUNTING. MEMORY.md's Phase 5d incident and w120's own postmortem are both
about assertions that a broken state satisfies: `== 1` rows, `not offenders`,
"nothing raised". Every assertion below reads a VALUE. Where an absence is
asserted it is paired with a positive twin that the broken state fails.
"""

from __future__ import annotations

import datetime
from datetime import date, timezone
import json
import os
import pathlib
import uuid

import pytest

from clinical import report_revision_shape as shape
from clinical.data_access import (
    AuthorizationError,
    BcryptHasher,
    BrokenRevisionRecordError,
    DataAccess,
    NotFoundError,
    SystemClock,
)
from clinical.retention import RetentionPrincipal

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = pathlib.Path(__file__).parent.parent / "schema.sql"

_CREATE_APP = "DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
_CREATE_RETENTION = "DO $$ BEGIN CREATE ROLE clinical_retention; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"

T0 = datetime.datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)


def _at(days: int = 0, hours: int = 0) -> datetime.datetime:
    return T0 + datetime.timedelta(days=days, hours=hours)


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


def _admin_session(dao, conn, org_id, email, name):
    user_id = dao.create_user(
        org_id,
        email,
        "password",
        full_name=name,
        registration_number="MCI-" + email.split("@")[0],
        hospital="Apollo Hospitals",
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org_id, user_id, T0),
        )
    conn.commit()
    return dao.login(email, org_id, "password")


def _with_roles(dao, admin_session, org_id, email, name, *roles):
    user_id = dao.create_user(
        org_id,
        email,
        "password",
        full_name=name,
        registration_number="MCI-" + email.split("@")[0],
        hospital="Apollo Hospitals",
    )
    for role in roles:
        dao.assign_role(admin_session, user_id, role, basis="w122 retraction test fixture role grant")
    return dao.login(email, org_id, "password")


@pytest.fixture()
def org_a(dao):
    return dao.create_organisation("Org A")


@pytest.fixture()
def org_b(dao):
    return dao.create_organisation("Org B")


@pytest.fixture()
def admin_a(dao, conn, org_a):
    return _admin_session(dao, conn, org_a, "admin@org-a.test", "Dr Admin A")


@pytest.fixture()
def admin_b(dao, conn, org_b):
    return _admin_session(dao, conn, org_b, "admin@org-b.test", "Dr Admin B")


@pytest.fixture()
def approver_a(dao, admin_a, org_a):
    """One person who interprets, submits and approves: the shortest real route."""
    return _with_roles(
        dao, admin_a, org_a, "rao@org-a.test", "Dr A. Rao", "Interpreter", "Approver", "Orderer", "Lab technician"
    )


@pytest.fixture()
def approver_b(dao, admin_b, org_b):
    return _with_roles(
        dao, admin_b, org_b, "bose@org-b.test", "Dr B. Bose", "Interpreter", "Approver", "Orderer", "Lab technician"
    )


@pytest.fixture()
def interpreter_only_a(dao, admin_a, org_a):
    """E2's negative: an Interpreter who cannot approve must not be able to retract."""
    return _with_roles(dao, admin_a, org_a, "tech@org-a.test", "Mr T. Tech", "Interpreter")


def _chain(dao, conn, session):
    """order -> sample -> run -> vcf -> interpretation, returns the interpretation id."""
    org_id = session.org_id
    test_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, org_id, "Panel", "GRCh38", "active", T0),
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
            (sample_id, order_id, org_id, T0, session.user_id),
        )
        cur.execute(
            "INSERT INTO sequencing_runs (org_id, id, sample_id, created_at, created_by) VALUES (%s, %s, %s, %s, %s)",
            (org_id, run_id, sample_id, T0, session.user_id),
        )
        cur.execute(
            "INSERT INTO vcfs (org_id, id, sequencing_run_id, vcf_path, content_hash, created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (org_id, vcf_id, run_id, f"/tmp/{vcf_id}.vcf", "0" * 64, T0, session.user_id),
        )
        cur.execute(
            "INSERT INTO interpretations "
            "(org_id, id, vcf_id, run_document, submission_key, created_at, created_by, retention_days) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                org_id,
                interp_id,
                vcf_id,
                json.dumps({"variants": []}),
                "sub-" + str(interp_id),
                T0,
                session.user_id,
                1825,
            ),
        )
    conn.commit()
    return interp_id


@pytest.fixture()
def interp_a(dao, conn, approver_a):
    return _chain(dao, conn, approver_a)


def _approved_report(dao, session, interp_id):
    """The REAL workflow -- claim, submit, approve -- never a raw write."""
    report_id = dao.create_report(session, interp_id)
    dao.record_sole_signatory_claim(
        session,
        interpretation_id=interp_id,
        report_id=report_id,
        reason="Sole signatory concurrence with the engine's classification.",
    )
    dao.submit_for_review(session, report_id)
    dao.approve_report(session, report_id)
    return report_id


@pytest.fixture()
def approved_a(dao, approver_a, interp_a):
    return _approved_report(dao, approver_a, interp_a)


REASON = "Sample mix-up: this report was issued against the wrong patient's sample."


def _report_row(conn, report_id):
    """EVERY column of the reports row, as a dict, so nothing can change unseen."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM reports WHERE id = %s", (report_id,))
        columns = [d[0] for d in cur.description]
        row = cur.fetchone()
    conn.commit()
    return dict(zip(columns, row))


def _audit(conn, action):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT resource_id, resource_type, details FROM audit_log "
            "WHERE action = %s AND outcome = 'success' ORDER BY log_id",
            (action,),
        )
        rows = cur.fetchall()
    conn.commit()
    return rows


# ───────────────────────────────────────────────────────────────────────────
# 1. THE CLAIM THE WHOLE DESIGN RESTS ON
# ───────────────────────────────────────────────────────────────────────────


class TestTheApprovedRowIsUntouched:
    def test_every_column_of_the_reports_row_is_identical_before_and_after(self, dao, conn, approver_a, approved_a):
        before = _report_row(conn, approved_a)
        dao.retract_report(approver_a, approved_a, reason=REASON)
        after = _report_row(conn, approved_a)

        # Compared column by column rather than as two dicts, so a failure
        # names WHICH column moved instead of printing two blobs.
        assert set(before) == set(after)
        for column in sorted(before):
            assert before[column] == after[column], f"reports.{column} changed when the report was retracted"

        # And the row still says the true thing: it was approved, and it still
        # is. `state` is deliberately NOT 'retracted' -- there is no such state.
        assert after["state"] == "approved"
        assert after["approver_id"] == approver_a.user_id
        assert after["approved_at"] is not None
        assert after["content_hash"] is not None

    def test_the_comparison_above_can_actually_fail(self, dao, conn, approver_a, approved_a):
        """
        THE KNOWN-POSITIVE for the assertion this file exists for. If
        `_report_row` returned something insensitive to the row's contents,
        the test above would pass against any implementation, including one
        that rewrote the state. A legitimate transition the trigger permits --
        approved -> released -- is used to prove the comparison has teeth.
        """
        before = _report_row(conn, approved_a)
        dao._release_report(approver_a, approved_a, consumer="LIMS", actor_id=approver_a.user_id)
        after = _report_row(conn, approved_a)
        assert before["state"] == "approved"
        assert after["state"] == "released"
        assert before != after

    def test_integrity_still_verifies_after_retraction(self, dao, approver_a, approved_a):
        """
        The retraction stayed OUT of the content hash. Had it been recorded as
        a sixth reviewer_claims kind it would be inside the hash, and this
        would return False -- reporting a retracted report as TAMPERED.
        """
        assert dao.verify_report_integrity(approver_a, approved_a) is True
        dao.retract_report(approver_a, approved_a, reason=REASON)
        assert dao.verify_report_integrity(approver_a, approved_a) is True

    def test_the_approver_is_still_retrievable_after_retraction(self, dao, approver_a, approved_a):
        """ISO 15189 7.4.1.5 c) after retraction as well as before -- the point
        of not rewriting the row."""
        dao.retract_report(approver_a, approved_a, reason=REASON)
        identity = dao.signing_identity_for_report(approver_a, approved_a)
        assert identity["full_name"] == "Dr A. Rao"
        assert identity["registration_number"] == "MCI-rao"


# ───────────────────────────────────────────────────────────────────────────
# 2. THE WRITE PATH AND ITS PRECONDITIONS (E1, E2, E4, E5)
# ───────────────────────────────────────────────────────────────────────────


class TestTheRetractionRecord:
    def test_the_row_records_who_when_why_and_which_state(self, dao, conn, approver_a, approved_a):
        retraction_id = dao.retract_report(approver_a, approved_a, reason=REASON)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id, report_id, retracted_by, reason, retracted_from_state, replacement_report_id "
                "FROM report_retractions WHERE id = %s",
                (retraction_id,),
            )
            row = cur.fetchone()
        conn.commit()
        org_id, report_id, retracted_by, reason, from_state, replacement = row
        assert org_id == approver_a.org_id
        assert report_id == approved_a
        assert retracted_by == approver_a.user_id
        assert reason == REASON
        assert from_state == "approved"
        assert replacement is None

    def test_e1_a_released_report_records_released(self, dao, conn, approver_a, approved_a):
        dao._release_report(approver_a, approved_a, consumer="ordering_clinician", actor_id=approver_a.user_id)
        retraction_id = dao.retract_report(approver_a, approved_a, reason=REASON)
        with conn.cursor() as cur:
            cur.execute("SELECT retracted_from_state FROM report_retractions WHERE id = %s", (retraction_id,))
            assert cur.fetchone()[0] == "released"
        conn.commit()

    @pytest.mark.parametrize("state", ("draft", "under_review"))
    def test_e1_a_report_below_approval_cannot_be_retracted(self, dao, approver_a, interp_a, state):
        report_id = dao.create_report(approver_a, interp_a)
        if state == "under_review":
            dao.record_sole_signatory_claim(
                approver_a, interpretation_id=interp_a, report_id=report_id, reason="Sole signatory concurrence."
            )
            dao.submit_for_review(approver_a, report_id)
        with pytest.raises(ValueError, match=state):
            dao.retract_report(approver_a, report_id, reason=REASON)

    def test_e1_and_the_refusal_left_no_row_behind(self, dao, conn, approver_a, interp_a):
        """
        Paired with the refusal above: a method that raised AFTER inserting
        would satisfy `pytest.raises` and still have retracted the report.
        """
        report_id = dao.create_report(approver_a, interp_a)
        with pytest.raises(ValueError):
            dao.retract_report(approver_a, report_id, reason=REASON)
        with conn.cursor() as cur:
            cur.execute("SELECT reason FROM report_retractions WHERE report_id = %s", (report_id,))
            assert cur.fetchall() == []
        conn.commit()

    def test_e2_any_approver_may_retract_and_a_non_approver_may_not(
        self, dao, conn, admin_a, approver_a, interpreter_only_a, approved_a, interp_a
    ):
        """
        E2: ANY Approver in the org, no new role -- so a SECOND approver, not
        the one who signed, must succeed. Both directions in one test: the
        refusal alone would be satisfied by a method that refused everyone.
        """
        second = _with_roles(
            dao, admin_a, approver_a.org_id, "second@org-a.test", "Dr S. Second", "Approver", "Interpreter"
        )
        with pytest.raises(AuthorizationError):
            dao.retract_report(interpreter_only_a, approved_a, reason=REASON)
        retraction_id = dao.retract_report(second, approved_a, reason=REASON)
        with conn.cursor() as cur:
            cur.execute("SELECT retracted_by FROM report_retractions WHERE id = %s", (retraction_id,))
            assert cur.fetchone()[0] == second.user_id
        conn.commit()

    def test_reason_is_required(self, dao, approver_a, approved_a):
        with pytest.raises(ValueError, match="reason"):
            dao.retract_report(approver_a, approved_a, reason="   ")

    def test_e5_a_second_retraction_is_refused(self, dao, approver_a, approved_a):
        dao.retract_report(approver_a, approved_a, reason=REASON)
        with pytest.raises(ValueError, match="already retracted"):
            dao.retract_report(approver_a, approved_a, reason="changed my mind")

    def test_e4_a_replacement_is_optional_and_recorded_when_given(self, dao, conn, approver_a, interp_a, approved_a):
        replacement_id = _approved_report(dao, approver_a, interp_a)
        retraction_id = dao.retract_report(approver_a, approved_a, reason=REASON, replacement_report_id=replacement_id)
        with conn.cursor() as cur:
            cur.execute("SELECT replacement_report_id FROM report_retractions WHERE id = %s", (retraction_id,))
            assert cur.fetchone()[0] == replacement_id
        conn.commit()

    def test_e4_a_retracted_report_may_still_be_amended(self, dao, conn, approver_a, approved_a):
        """
        E4, ruled: retraction does not end the document's line. The amendment
        path is what keeps traceability from a corrected document back to the
        one it corrects, so forbidding it would push labs toward an unlinked
        new report.
        """
        dao.retract_report(approver_a, approved_a, reason=REASON)
        amendment_report_id, _notification_id = dao.create_amendment(
            approver_a, approved_a, reason="Corrected variant call after re-extraction."
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT original_report_id FROM amendments WHERE amendment_report_id = %s", (amendment_report_id,)
            )
            assert cur.fetchone()[0] == approved_a
        conn.commit()

    def test_the_audit_entry_names_the_report_and_carries_the_retraction_id(self, dao, conn, approver_a, approved_a):
        """
        w120's defect must not be reproduced in a new costume. The entry is
        about a REPORT, so resource_id is the report; the retraction id is
        evidence and lives in details.
        """
        retraction_id = dao.retract_report(approver_a, approved_a, reason=REASON)
        rows = _audit(conn, "report_retracted")
        assert [r[0] for r in rows] == [str(approved_a)]
        assert rows[0][1] == "report"
        assert rows[0][2]["retraction_id"] == str(retraction_id)
        assert rows[0][2]["report_id"] == str(approved_a)


class TestCrossOrganisation:
    def test_another_org_cannot_retract_this_orgs_report(self, dao, conn, approver_a, approver_b, approved_a):
        with pytest.raises(NotFoundError):
            dao.retract_report(approver_b, approved_a, reason=REASON)
        # The positive twin: the same call from the owning org succeeds, so the
        # refusal above is about the ORG and not about the report being
        # unretractable for some other reason.
        assert dao.retract_report(approver_a, approved_a, reason=REASON) is not None

    def test_another_org_cannot_see_the_retraction(self, dao, approver_a, approver_b, approved_a):
        dao.retract_report(approver_a, approved_a, reason=REASON)
        # Org B cannot even reach the report, which is the stronger statement.
        assert dao.get_report(approver_b, approved_a) is None
        # And org A can, and is told. Paired, so "returns None for everything"
        # cannot satisfy the assertion above.
        assert dao.get_report(approver_a, approved_a)["is_retracted"] is True


# ───────────────────────────────────────────────────────────────────────────
# 3. E6 -- THE OBLIGATION, PER CONSUMER
# ───────────────────────────────────────────────────────────────────────────


def _debts(conn, retraction_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT consumer, status, reason_for_retraction FROM retraction_notifications "
            "WHERE retraction_id = %s ORDER BY consumer",
            (retraction_id,),
        )
        rows = cur.fetchall()
    conn.commit()
    return rows


class TestE6NotificationIsOwedPerConsumer:
    def test_one_debt_per_consumer_naming_the_consumer(self, dao, approver_a, approved_a, conn):
        """
        PER CONSUMER, NOT PER ROLE -- the ruling in those words. The assertion
        is on the recorded consumer STRINGS, so an implementation that copied
        create_amendment and wrote 'ordering_clinician' for a LIMS fails here.
        """
        for consumer in ("Apollo LIMS v4", "dr-mehta@apollo.example"):
            dao._release_report(approver_a, approved_a, consumer=consumer, actor_id=approver_a.user_id)
        retraction_id = dao.retract_report(approver_a, approved_a, reason=REASON)

        assert _debts(conn, retraction_id) == [
            ("Apollo LIMS v4", "notification_owed", REASON),
            ("dr-mehta@apollo.example", "notification_owed", REASON),
        ]

    def test_redelivery_to_the_same_consumer_is_one_debt_not_two(self, dao, approver_a, approved_a, conn):
        dao._release_report(approver_a, approved_a, consumer="Apollo LIMS v4", actor_id=approver_a.user_id)
        dao._release_report(approver_a, approved_a, consumer="Apollo LIMS v4", actor_id=approver_a.user_id)
        retraction_id = dao.retract_report(approver_a, approved_a, reason=REASON)
        assert _debts(conn, retraction_id) == [("Apollo LIMS v4", "notification_owed", REASON)]

    def test_a_retraction_with_no_recipients_is_still_recorded(self, dao, conn, approver_a, approved_a):
        """E6 does not BLOCK. An approved-never-released report is retractable
        and simply owes nobody anything."""
        retraction_id = dao.retract_report(approver_a, approved_a, reason=REASON)
        assert _debts(conn, retraction_id) == []
        with conn.cursor() as cur:
            cur.execute("SELECT reason FROM report_retractions WHERE id = %s", (retraction_id,))
            assert cur.fetchone()[0] == REASON
        conn.commit()

    def test_a_consumer_that_receives_it_after_the_retraction_is_also_owed(self, dao, conn, approver_a, approved_a):
        """
        E3 permits delivery of a retracted report, so a NEW recipient can
        appear after the retraction was recorded. If the debt were only
        written by retract_report, "which retractions have unnotified
        recipients" would silently miss exactly the people who received the
        report while it was already retracted.
        """
        retraction_id = dao.retract_report(approver_a, approved_a, reason=REASON)
        assert _debts(conn, retraction_id) == []
        dao._release_report(approver_a, approved_a, consumer="Late LIMS", actor_id=approver_a.user_id)
        assert _debts(conn, retraction_id) == [("Late LIMS", "notification_owed", REASON)]

    def test_the_status_vocabulary_cannot_claim_a_transmission(self, dao, conn, approver_a, approved_a):
        """
        The failure this is written to avoid: an auditor reading these rows and
        concluding recipients were informed. There is no transport in this
        platform, so there is no value that says one happened -- the CHECK
        constraint refuses 'sent'.
        """
        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        dao._release_report(approver_a, approved_a, consumer="Apollo LIMS v4", actor_id=approver_a.user_id)
        retraction_id = dao.retract_report(approver_a, approved_a, reason=REASON)
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO retraction_notifications "
                    "(org_id, id, retraction_id, consumer, reason_for_retraction, status, created_at, created_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        approver_a.org_id,
                        uuid.uuid4(),
                        retraction_id,
                        "someone-else",
                        REASON,
                        "sent",
                        T0,
                        approver_a.user_id,
                    ),
                )
        conn.rollback()
        # Paired positive: the same INSERT with the only truthful value lands,
        # so the refusal above is about the VALUE and not about the statement
        # being malformed.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO retraction_notifications "
                "(org_id, id, retraction_id, consumer, reason_for_retraction, status, created_at, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    approver_a.org_id,
                    uuid.uuid4(),
                    retraction_id,
                    "someone-else",
                    REASON,
                    "notification_owed",
                    T0,
                    approver_a.user_id,
                ),
            )
        conn.commit()


# ───────────────────────────────────────────────────────────────────────────
# 4. E3 -- THE GATE PERMITS, AND SAYS SO
# ───────────────────────────────────────────────────────────────────────────


class TestE3TheGatePermitsAndCarriesTheFact:
    def test_delivery_of_a_retracted_report_is_permitted(self, dao, approver_a, approved_a):
        dao.retract_report(approver_a, approved_a, reason=REASON)
        # Not merely "did not raise": the gate's answer is read.
        assert dao.require_release(approver_a, approved_a) is True

    def test_a_report_that_is_not_retracted_answers_false(self, dao, approver_a, approved_a):
        # The positive twin for the assertion above: a gate that returned True
        # unconditionally would satisfy it.
        assert dao.require_release(approver_a, approved_a) is False

    def test_the_gate_still_refuses_a_report_that_was_never_approved(self, dao, approver_a, interp_a):
        """E3 loosened the gate for RETRACTION only. The approval check is
        untouched, and that is the thing spec 13.4 rests on."""
        report_id = dao.create_report(approver_a, interp_a)
        with pytest.raises(ValueError, match="draft"):
            dao.require_release(approver_a, report_id)

    def test_a_retracted_report_can_actually_be_released_to_a_new_consumer(self, dao, conn, approver_a, approved_a):
        """
        E3 in full: the platform hands the document over rather than forcing
        the lab to send it by hand, unbannered, with no release_events row.
        """
        dao.retract_report(approver_a, approved_a, reason=REASON)
        release_id = dao._release_report(
            approver_a, approved_a, consumer="dr-mehta@apollo.example", actor_id=approver_a.user_id
        )
        with conn.cursor() as cur:
            cur.execute("SELECT consumer FROM release_events WHERE id = %s", (release_id,))
            assert cur.fetchone()[0] == "dr-mehta@apollo.example"
        conn.commit()

    def test_a_retraction_that_cannot_be_stated_stops_the_delivery(self, dao, conn, approver_a, approved_a):
        """
        R7 fail-closed. E3's bargain is that the retraction TRAVELS with the
        document; a retraction whose reason cannot be rendered cannot travel,
        so the document does not go. Constructed by a direct write because
        retract_report refuses an empty reason -- the writer is not the
        mechanism under test here, the gate is.
        """
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO report_retractions "
                "(org_id, id, report_id, retracted_at, retracted_by, reason, retracted_from_state) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (approver_a.org_id, uuid.uuid4(), approved_a, T0, approver_a.user_id, "   ", "approved"),
            )
        conn.commit()
        with pytest.raises(BrokenRevisionRecordError):
            dao.require_release(approver_a, approved_a)


# ───────────────────────────────────────────────────────────────────────────
# 5. THE REVISION READER (the funnel every rendered surface takes)
# ───────────────────────────────────────────────────────────────────────────


class TestTheRevisionReader:
    def test_a_report_that_is_not_retracted_carries_no_retracted_block(self, dao, approver_a, approved_a):
        assert "retracted" not in dao.get_report_revision(approver_a, approved_a)

    def test_a_retracted_report_carries_the_block_the_renderer_consumes(self, dao, approver_a, approved_a):
        dao.retract_report(approver_a, approved_a, reason=REASON)
        block = dao.get_report_revision(approver_a, approved_a)["retracted"]
        # Pinned against the ONE WRITTEN-DOWN CONTRACT, so a key renamed here
        # cannot leave geper/tests/test_w122_retraction_banners.py stale.
        assert set(block) == set(shape.RETRACTED_REQUIRED)
        assert block["issued_basis"] in shape.DATE_BASES
        assert block["reason"] == REASON
        assert block["retracted_by"] == "Dr A. Rao (MCI-rao), Apollo Hospitals"

    def test_the_revision_block_uses_only_declared_keys(self, dao, approver_a, approved_a):
        dao.retract_report(approver_a, approved_a, reason=REASON)
        assert set(dao.get_report_revision(approver_a, approved_a)) <= shape.REVISION_KEYS

    def test_a_replacement_adds_exactly_the_two_replacement_fields(self, dao, approver_a, interp_a, approved_a):
        replacement_id = _approved_report(dao, approver_a, interp_a)
        dao.retract_report(approver_a, approved_a, reason=REASON, replacement_report_id=replacement_id)
        block = dao.get_report_revision(approver_a, approved_a)["retracted"]
        assert set(block) == set(shape.RETRACTED_REQUIRED) | set(shape.RETRACTED_REPLACEMENT_FIELDS)
        assert block["replacement_report_id"] == replacement_id
        assert block["replacement_retained"] is True

    def test_a_tombstoned_replacement_is_reported_as_no_longer_retained(
        self, dao, conn, approver_a, interp_a, approved_a
    ):
        """
        R8 generalised. The retracted report must name a destroyed replacement
        as gone rather than send the reader to fetch it.
        """
        replacement_id = _approved_report(dao, approver_a, interp_a)
        dao.retract_report(approver_a, approved_a, reason=REASON, replacement_report_id=replacement_id)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reports SET tombstoned_at = %s, tombstoned_by = %s WHERE id = %s",
                (_at(days=3650), approver_a.user_id, replacement_id),
            )
        conn.commit()
        block = dao.get_report_revision(approver_a, approved_a)["retracted"]
        assert block["replacement_retained"] is False

    def test_e1_the_basis_is_the_stored_fact_not_a_later_lookup(self, dao, approver_a, approved_a):
        """
        THE REASON E1 EXISTS. A report retracted while 'approved' and released
        afterwards (E3 permits that) must still say "approved on", because
        that is what was true when the lab acted. A reader that re-derived the
        basis from release_events would silently start printing the release
        date under a sentence recorded before any release existed -- R2's
        silent swap, one table over.
        """
        dao.retract_report(approver_a, approved_a, reason=REASON)
        before = dao.get_report_revision(approver_a, approved_a)["retracted"]
        assert before["issued_basis"] == "approved"

        dao._release_report(approver_a, approved_a, consumer="Late LIMS", actor_id=approver_a.user_id)
        after = dao.get_report_revision(approver_a, approved_a)["retracted"]
        assert after["issued_basis"] == "approved"
        assert after["issued_at"] == before["issued_at"]

    def test_a_released_retraction_reports_the_release_date(self, dao, conn, approver_a, approved_a):
        """
        The other arm, and it is load-bearing: without it the assertion above
        is satisfied by a reader that hardcodes 'approved'.
        """
        dao._release_report(approver_a, approved_a, consumer="Apollo LIMS v4", actor_id=approver_a.user_id)
        dao.retract_report(approver_a, approved_a, reason=REASON)
        block = dao.get_report_revision(approver_a, approved_a)["retracted"]
        assert block["issued_basis"] == "released"
        with conn.cursor() as cur:
            cur.execute("SELECT MIN(released_at) FROM release_events WHERE report_id = %s", (approved_a,))
            assert block["issued_at"] == cur.fetchone()[0]
        conn.commit()

    def test_two_retraction_rows_refuse_the_document(self, dao, conn, approver_a, approved_a):
        """R7: the banner would have to pick one, and picking is guessing."""
        dao.retract_report(approver_a, approved_a, reason=REASON)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO report_retractions "
                "(org_id, id, report_id, retracted_at, retracted_by, reason, retracted_from_state) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (approver_a.org_id, uuid.uuid4(), approved_a, _at(hours=1), approver_a.user_id, "other", "approved"),
            )
        conn.commit()
        with pytest.raises(BrokenRevisionRecordError, match="2 retraction records"):
            dao.get_report_revision(approver_a, approved_a)


# ───────────────────────────────────────────────────────────────────────────
# 6. E8 -- THE CASCADE, AND THE CLOCK THAT DID NOT MOVE
# ───────────────────────────────────────────────────────────────────────────


class TestE8RetentionCascade:
    @pytest.fixture()
    def retention(self, dao, conn, approver_a):
        # The org's Phase 5c system principal is what a purge is attributed
        # to; the platform's own method creates it rather than a hand-rolled
        # INSERT that would have to keep up with the users table.
        dao._create_system_session(approver_a.org_id)
        return RetentionPrincipal(conn)

    def _retract_release_and_purge(self, dao, conn, retention, approver_a, approved_a):
        dao._release_report(approver_a, approved_a, consumer="Apollo LIMS v4", actor_id=approver_a.user_id)
        retraction_id = dao.retract_report(approver_a, approved_a, reason=REASON)
        result = retention.purge_expired(approver_a.org_id, "report", now=_at(days=10000))
        return retraction_id, result

    def test_the_retraction_and_its_debts_are_tombstoned_with_the_report(
        self, dao, conn, retention, approver_a, approved_a
    ):
        retraction_id, result = self._retract_release_and_purge(dao, conn, retention, approver_a, approved_a)
        assert approved_a in result.tombstoned_ids

        with conn.cursor() as cur:
            cur.execute("SELECT tombstoned_at FROM report_retractions WHERE id = %s", (retraction_id,))
            retraction_tombstone = cur.fetchone()[0]
            cur.execute(
                "SELECT consumer, tombstoned_at FROM retraction_notifications WHERE retraction_id = %s",
                (retraction_id,),
            )
            debts = cur.fetchall()
            cur.execute("SELECT tombstoned_at FROM reports WHERE id = %s", (approved_a,))
            report_tombstone = cur.fetchone()[0]
        conn.commit()

        # SAME TRANSACTION, SAME STAMP -- asserted as equality to the report's
        # own timestamp rather than as "not null", because "not null" is
        # satisfied by any independent later purge.
        assert retraction_tombstone == report_tombstone
        assert debts == [("Apollo LIMS v4", report_tombstone)]

    def test_a_live_retraction_is_not_tombstoned_before_its_report_is(
        self, dao, conn, retention, approver_a, approved_a
    ):
        """
        The positive twin for the cascade: if everything were stamped
        unconditionally the test above would pass while retention was broken.
        """
        dao._release_report(approver_a, approved_a, consumer="Apollo LIMS v4", actor_id=approver_a.user_id)
        retraction_id = dao.retract_report(approver_a, approved_a, reason=REASON)
        result = retention.purge_expired(approver_a.org_id, "report", now=_at(days=1))
        assert result.tombstoned_ids == []
        with conn.cursor() as cur:
            cur.execute("SELECT tombstoned_at FROM report_retractions WHERE id = %s", (retraction_id,))
            assert cur.fetchone()[0] is None
        conn.commit()

    def test_e8_retraction_does_not_move_the_purge_clock(self, dao, conn, retention, approver_a, interp_a):
        """
        E8: NO CHANGE to eligibility. Two reports on the same released day,
        one retracted and one not, become eligible on the SAME day -- so a
        retraction neither shortens the window nor blocks the purge the way a
        live amendment does (D6).
        """
        retracted_report = _approved_report(dao, approver_a, interp_a)
        plain_report = _approved_report(dao, approver_a, interp_a)
        for report_id in (retracted_report, plain_report):
            dao._release_report(approver_a, report_id, consumer="Apollo LIMS v4", actor_id=approver_a.user_id)
        dao.retract_report(approver_a, retracted_report, reason=REASON)

        early = retention.purge_expired(approver_a.org_id, "report", now=_at(days=1))
        assert early.tombstoned_ids == []
        late = retention.purge_expired(approver_a.org_id, "report", now=_at(days=10000))
        assert sorted(str(i) for i in late.tombstoned_ids) == sorted(str(i) for i in (retracted_report, plain_report))


# ───────────────────────────────────────────────────────────────────────────
# 7. THE SCHEMA'S OWN GUARANTEES
# ───────────────────────────────────────────────────────────────────────────


class TestTheSchemaRefusesWhatItMust:
    def test_a_retraction_naming_another_orgs_user_is_refused_by_the_fk(self, conn, approver_a, approver_b, approved_a):
        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                cur.execute(
                    "INSERT INTO report_retractions "
                    "(org_id, id, report_id, retracted_at, retracted_by, reason, retracted_from_state) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (approver_a.org_id, uuid.uuid4(), approved_a, T0, approver_b.user_id, REASON, "approved"),
                )
        conn.rollback()
        # Paired positive: the same row with this org's own user lands.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO report_retractions "
                "(org_id, id, report_id, retracted_at, retracted_by, reason, retracted_from_state) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (approver_a.org_id, uuid.uuid4(), approved_a, T0, approver_a.user_id, REASON, "approved"),
            )
        conn.commit()

    def test_a_report_cannot_be_its_own_replacement(self, conn, approver_a, approved_a):
        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO report_retractions "
                    "(org_id, id, report_id, retracted_at, retracted_by, reason, retracted_from_state, "
                    "replacement_report_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        approver_a.org_id,
                        uuid.uuid4(),
                        approved_a,
                        T0,
                        approver_a.user_id,
                        REASON,
                        "approved",
                        approved_a,
                    ),
                )
        conn.rollback()

    def test_a_third_retracted_from_state_is_not_representable(self, conn, approver_a, approved_a):
        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO report_retractions "
                    "(org_id, id, report_id, retracted_at, retracted_by, reason, retracted_from_state) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (approver_a.org_id, uuid.uuid4(), approved_a, T0, approver_a.user_id, REASON, "draft"),
                )
        conn.rollback()

    def test_the_reports_grant_line_is_unchanged(self):
        """
        THE LINE THIS WAVE WAS TOLD NOT TO TOUCH, asserted as a literal.
        A column added to `reports` for retraction would have had to widen it,
        re-opening the privilege surface two earlier commits spent narrowing.
        """
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        assert "GRANT UPDATE (state, approver_id, approved_at, content_hash) ON reports TO clinical_app;" in schema
        assert "REVOKE UPDATE ON reports, interpretations, vcfs FROM clinical_app;" in schema

    def test_the_report_state_check_still_has_exactly_five_values(self):
        """
        Model 3's other promise. A sixth state would have made every existing
        reader refuse a retracted report for free -- and would have bought that
        by making the row deny an approval that happened.
        """
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        assert "CHECK (state IN ('draft', 'under_review', 'approved', 'released', 'returned'))" in schema
        assert "'retracted'" not in schema.split("CREATE TABLE reports (")[1].split(");")[0]
