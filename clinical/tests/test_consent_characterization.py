"""
clinical/tests/test_consent_characterization.py
───────────────────────────────────────────────
CONSENT CHARACTERIZATION (Backend Phase 1). These tests RECORD WHAT THE CODE DOES TODAY around consent withdrawal.
They are not a statement of what it SHOULD do.

READ THIS BEFORE CHANGING ANY ROW HERE
  * A row named `..._is_currently_accepted` documents that a stage does NOT consult consent at this commit. It
    documents the CURRENT IMPLEMENTATION ONLY. It is NOT approved clinical or legal policy, it is NOT a ruling that
    the stage should accept a withdrawn consent, and nothing here decides what withdrawal means for work already in
    flight (`DataAccess.withdraw_consent` states that this is "NOT DECIDED ANYWHERE IN THIS TREE").
  * When a human decides a stage must refuse after withdrawal, the matching row here is EXPECTED to change, in the
    same commit as the enforcement and the matrix in docs/CONSENT_ENFORCEMENT_MATRIX.md. A characterization row that
    fails because behaviour was deliberately changed is updated; one that fails by accident is a regression.
  * Nothing here is a claim of DPDP or NABL compliance, of clinical validation, or of production readiness.

WHAT IS PINNED
  1. The three consent rules, side by side (create_order / check_consent / Policy C), including where they differ.
  2. Which stages consult consent after an order exists (none, except the start of a pipeline attempt and genomic
     reads), by driving each stage after a real withdrawal.
  3. What the patient portal and the staff reads show after withdrawal, release and retraction.
  4. The audit trail around a withdrawal.

All state is built through the real writers on a disposable database; raw SQL only reads results back, and in two
places arranges a consent whose recorded_at ordering the test needs. Rows already covered elsewhere are not repeated:
`test_phase_5d_orchestration` (withdrawn consent at submission start), `test_w166_catalogue_and_orders` (withdrawn
consent at order creation over HTTP), `test_callset_m3_policy_c` and `test_genome_analysis_endpoints::TestTheConsentGate`
(Policy C on the call-set and genome reads).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import uuid

import pytest

from clinical.data_access import (
    BcryptHasher,
    DataAccess,
    GenomeConsentWithheldError,
    NotFoundError,
)

DSN = os.getenv("CLINICAL_TEST_DSN")

APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"
PASSWORD = "correct horse battery staple"
PATIENT_COOKIE = "clinical_patient_session"
CLIENT_IP = "203.0.113.31"
T0 = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
SCOPE = "testing"
OTHER_SCOPE = "research"
RETRACTION_REASON = "characterization: a retraction after a consent withdrawal"

VCF_TEXT = (
    "##fileformat=VCFv4.2\n"
    "##assembly=GRCh38\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample1\n"
    "chr1\t1000\t.\tA\tT\t30\tPASS\t.\tGT\t0/1\n"
)
RUN_DOCUMENT = {"variants": [{"chrom": "1", "pos": 1000}], "run_complete": True}


# ─── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def clinical_dsn(monkeypatch):
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")

    from clinical.bootstrap import apply_schema, ensure_roles

    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    ensure_roles(connection, app_password=APP_PASSWORD, retention_password=RETENTION_PASSWORD)
    apply_schema(connection)
    connection.commit()
    connection.close()

    monkeypatch.setenv("CLINICAL_DSN", DSN)
    return DSN


@pytest.fixture(autouse=True)
def reset_the_login_throttle():
    from clinical.auth_endpoints import _LOGIN_THROTTLE

    _LOGIN_THROTTLE.reset()
    yield
    _LOGIN_THROTTLE.reset()


@pytest.fixture()
def conn(clinical_dsn):
    import psycopg

    connection = psycopg.connect(clinical_dsn, autocommit=False, connect_timeout=10)
    yield connection
    connection.close()


@pytest.fixture()
def dao(conn):
    return DataAccess(conn, password_hasher=BcryptHasher(rounds=4))


@pytest.fixture()
def client(clinical_dsn):
    from fastapi.testclient import TestClient

    from clinical.app import app

    return TestClient(app, client=(CLIENT_IP, 45678))


def _staff(dao, conn, org_id, email):
    """One account holding every role the organisation's work needs, so each stage below is reachable. Roles are
    additive and nothing here relaxes a gate: a stage that does not consult consent would not consult it for any
    role."""
    user_id = dao.create_user(org_id, email, PASSWORD)
    with conn.cursor() as cur:
        for role in ("Administrator", "Auditor", "Orderer", "Lab technician", "Interpreter"):
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (user_id, org_id, role, user_id, T0),
            )
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at, basis) "
            "VALUES (%s, %s, 'Approver', %s, %s, %s)",
            (user_id, org_id, user_id, T0, "consent characterization fixture: signs and releases what it interprets"),
        )
    conn.commit()
    return dao.login(email, org_id, PASSWORD)


@pytest.fixture()
def world(dao, conn):
    """Two organisations, each with one staff account, a catalogue test and one patient with a portal login. Org B
    exists so the tenant-isolation rows have a real foreign organisation to point at."""
    orgs = {}
    for tag, name in (("a", "Org A"), ("b", "Org B")):
        org_id = dao.create_organisation(name)
        staff = _staff(dao, conn, org_id, f"admin@{tag}.example")
        test_id = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (test_id, org_id, "Panel", "GRCh38", "active", T0),
            )
        conn.commit()
        patient = dao.create_patient(staff, f"Patient {tag.upper()}", dt.date(1990, 1, 1), "F")
        dao.enrol_patient_login(staff, patient, f"patient@{tag}.example", PASSWORD)
        orgs[tag] = {
            "org_id": org_id,
            "staff": staff,
            "test_id": test_id,
            "patient": patient,
            "patient_email": f"patient@{tag}.example",
        }
    conn.commit()
    return orgs


@pytest.fixture()
def vcf(tmp_path):
    path = tmp_path / "run.vcf"
    path.write_text(VCF_TEXT, encoding="utf-8")
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


# ─── driving the workflow, through the real writers only ─────────────────────


def _new_order(dao, org, patient=None, consent_id=None):
    """A draft order under a fresh (or the given) consent. Returns (order_id, consent_id)."""
    staff = org["staff"]
    patient = patient or org["patient"]
    if consent_id is None:
        consent_id = dao.record_consent(staff, patient, SCOPE)
    order_id = dao.create_order(
        staff,
        patient_id=patient,
        test_id=org["test_id"],
        required_scope=SCOPE,
        consent_id=consent_id,
        priority="routine",
        clinical_indication="consent characterization",
    )
    return order_id, consent_id


def _to_placed(dao, org):
    order_id, consent_id = _new_order(dao, org)
    dao.place_order(org["staff"], order_id)
    return order_id, consent_id


def _to_qc_passed(dao, org):
    order_id, consent_id = _to_placed(dao, org)
    sample_id = dao.receive_sample(org["staff"], order_id, "blood")
    dao.record_qc(org["staff"], sample_id, "passed")
    return order_id, consent_id, sample_id


def _record_result(dao, org, sample_id, vcf):
    system = dao._create_system_session(org["org_id"])
    return dao.record_pipeline_result(system, sample_id, vcf[0], vcf[1], RUN_DOCUMENT)


def _to_state(dao, org, vcf, upto):
    """Drive one order as far as `upto` ('draft' | 'under_review' | 'approved' | 'released'), the way test_w135 does."""
    order_id, consent_id, sample_id = _to_qc_passed(dao, org)
    result = _record_result(dao, org, sample_id, vcf)
    ctx = {"order_id": order_id, "consent_id": consent_id, "sample_id": sample_id, "result": result}
    report_id = result.report_id
    if upto == "draft":
        return ctx
    dao.record_sole_signatory_claim(
        org["staff"], interpretation_id=result.interpretation_id, report_id=report_id, reason="characterization"
    )
    dao.submit_for_review(org["staff"], report_id)
    if upto == "under_review":
        return ctx
    dao.approve_report(org["staff"], report_id)
    if upto == "approved":
        return ctx
    dao.release_report(org["staff"], report_id)
    return ctx


def _withdraw(dao, org, consent_id):
    dao.withdraw_consent(org["staff"], consent_id)


def _scalar(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    conn.commit()
    return None if row is None else row[0]


def _order_state(conn, org, order_id):
    return _scalar(conn, "SELECT state FROM orders WHERE org_id = %s AND order_id = %s", (org["org_id"], order_id))


def _report_state(conn, org, report_id):
    return _scalar(conn, "SELECT state FROM reports WHERE org_id = %s AND id = %s", (org["org_id"], report_id))


def _audit(conn, org):
    with conn.cursor() as cur:
        cur.execute("SELECT log_id, action, outcome FROM audit_log WHERE org_id = %s ORDER BY log_id", (org["org_id"],))
        rows = cur.fetchall()
    conn.commit()
    return rows


def _portal_cookie(client, org):
    from clinical.auth_endpoints import _LOGIN_THROTTLE

    _LOGIN_THROTTLE.reset()
    response = client.post(
        "/portal/login", json={"email": org["patient_email"], "org_id": str(org["org_id"]), "password": PASSWORD}
    )
    assert response.status_code == 204, response.text
    return response.headers.get_list("set-cookie")[0].split("=", 1)[1].split(";", 1)[0]


def _portal_reports(client, org):
    cookie = _portal_cookie(client, org)
    response = client.get("/portal/me/reports", cookies={PATIENT_COOKIE: cookie})
    assert response.status_code == 200, response.text
    single_ids = [r["report_id"] for r in response.json()]
    return cookie, response.json(), single_ids


def _rule3(conn, org, order_id):
    """Policy C's predicate, evaluated by the database against one order row."""
    return _scalar(
        conn,
        f"SELECT {DataAccess._CALLSET_CONSENT_OK_SQL} FROM orders o WHERE o.org_id = %s AND o.order_id = %s",  # noqa: S608
        (org["org_id"], order_id),
    )


def _rule1(dao, org, consent_id):
    """Rule 1: could a NEW order be created on this consent id? Returns 'ok' or the exception class name."""
    try:
        dao.create_order(
            org["staff"],
            patient_id=org["patient"],
            test_id=org["test_id"],
            required_scope=SCOPE,
            consent_id=consent_id,
            priority="routine",
            clinical_indication="rule 1 probe",
        )
    except (NotFoundError, ValueError) as exc:
        return type(exc).__name__
    return "ok"


def _rule2(dao, org, scope=SCOPE):
    return dao.check_consent(org["staff"], org["patient"], scope)


# ─── 1. the three rules side by side ─────────────────────────────────────────


class TestTheThreeConsentRulesAreNotTheSameRule:
    """
    RULE 1  `create_order`: the ONE consent id the caller names must be active (not withdrawn, not superseded), for
            this patient and organisation, with the order's scope.
    RULE 2  `check_consent` (via `validate_order_for_submission`): the LATEST NON-SUPERSEDED consent of the patient
            for the scope, by recorded_at; withdrawn -> `consent_withdrawn`; none -> `consent_missing`.
    RULE 3  Policy C (`_CALLSET_CONSENT_OK_SQL`): the ORDER'S OWN consent is not withdrawn AND the patient's latest
            consent for the scope, by (recorded_at, consent_id), is not withdrawn. `superseded_by` is ignored.

    Each row builds one consent history for a patient, places one order under the FIRST consent while it is active,
    changes the history, and records what each rule now says. THE ROWS WHERE THE RULES DISAGREE ARE THE POINT; they
    document the current implementation and do not say which rule is right.
    """

    def _history_active(self, dao, org, c1):
        return None

    def _history_withdrawn(self, dao, org, c1):
        _withdraw(dao, org, c1)

    def _history_withdrawn_then_newer_active(self, dao, org, c1):
        _withdraw(dao, org, c1)
        dao.record_consent(org["staff"], org["patient"], SCOPE)

    def _history_superseded_by_active(self, dao, org, c1):
        dao.record_consent(org["staff"], org["patient"], SCOPE, supersedes_id=c1)

    def _history_withdrawn_then_superseded_by_active(self, dao, org, c1):
        _withdraw(dao, org, c1)
        dao.record_consent(org["staff"], org["patient"], SCOPE, supersedes_id=c1)

    def _history_newer_consent_withdrawn_older_still_active(self, dao, org, c1):
        newer = dao.record_consent(org["staff"], org["patient"], SCOPE)
        _withdraw(dao, org, newer)

    # name -> (rule 1 on the ORIGINAL consent id, rule 2, rule 3 on the order placed under it)
    TABLE = {
        "active": ("ok", "consent_ok", True),
        "withdrawn": ("NotFoundError", "consent_withdrawn", False),
        "withdrawn_then_newer_active": ("NotFoundError", "consent_ok", False),
        "superseded_by_active": ("NotFoundError", "consent_ok", True),
        "withdrawn_then_superseded_by_active": ("NotFoundError", "consent_ok", False),
        "newer_consent_withdrawn_older_still_active": ("ok", "consent_withdrawn", False),
    }

    @pytest.mark.parametrize("history", sorted(TABLE))
    def test_each_rule_answers_a_consent_history_as_recorded(self, dao, conn, world, history):
        """Characterization: the exact (rule 1, rule 2, rule 3) answers for each history. Not a policy."""
        org = world["a"]
        order_id, c1 = _to_placed(dao, org)
        getattr(self, f"_history_{history}")(dao, org, c1)

        observed = (_rule1(dao, org, c1), _rule2(dao, org), _rule3(conn, org, order_id))
        assert observed == self.TABLE[history], history

    def test_the_rules_disagree_in_at_least_the_recorded_rows(self):
        """A guard on the table above: it must keep recording disagreement, not collapse into one rule by editing."""
        rule2_ok_rule3_refuses = [h for h, (_, r2, r3) in self.TABLE.items() if r2 == "consent_ok" and r3 is False]
        assert sorted(rule2_ok_rule3_refuses) == [
            "withdrawn_then_newer_active",
            "withdrawn_then_superseded_by_active",
        ]

    def test_a_scope_with_no_consent_is_missing_for_rule_2_and_a_mismatched_scope_is_refused_by_rule_1(
        self, dao, world
    ):
        org = world["a"]
        _, c1 = _to_placed(dao, org)
        assert _rule2(dao, org, OTHER_SCOPE) == "consent_missing"
        with pytest.raises(ValueError):
            dao.create_order(
                org["staff"],
                patient_id=org["patient"],
                test_id=org["test_id"],
                required_scope=OTHER_SCOPE,
                consent_id=c1,
                priority="routine",
                clinical_indication="scope mismatch",
            )

    def test_another_organisations_consent_is_invisible_to_rules_1_and_2(self, dao, world):
        a, b = world["a"], world["b"]
        _, consent_a = _to_placed(dao, a)

        # Rule 1: org B cannot back an order with org A's consent id, even for its own patient.
        assert _rule1(dao, b, consent_a) == "NotFoundError"
        # Rule 2: org B's session asking about org A's patient finds no consent.
        assert dao.check_consent(b["staff"], a["patient"], SCOPE) == "consent_missing"
        # Neither refusal revealed anything: org A's consent is untouched and still active.
        assert _rule2(dao, a) == "consent_ok"

    def test_rule_3_is_false_for_an_order_whose_consent_row_is_missing(self, dao, conn, world):
        """Policy C fails closed. The order's consent row cannot be removed through the application, so this row
        builds the broken state as the database owner (foreign keys lifted for this transaction only)."""
        org = world["a"]
        order_id, c1 = _to_placed(dao, org)
        assert _rule3(conn, org, order_id) is True
        with conn.cursor() as cur:
            cur.execute("SET LOCAL session_replication_role = replica")
            cur.execute("DELETE FROM consents WHERE org_id = %s AND consent_id = %s", (org["org_id"], c1))
        conn.commit()
        assert _rule3(conn, org, order_id) is False


# ─── 2. withdrawal at each stage ─────────────────────────────────────────────


class TestOrderCreationAfterWithdrawal:
    def test_a_withdrawn_consent_cannot_back_a_new_order_and_nothing_is_written(self, dao, conn, world):
        """Established behaviour (rule 1), at the data layer: the refusal is a plain NotFoundError, no order row is
        created, and the audit log has no `order_created` success for the attempt. The HTTP shape is pinned in
        test_w166_catalogue_and_orders."""
        org = world["a"]
        consent_id = dao.record_consent(org["staff"], org["patient"], SCOPE)
        _withdraw(dao, org, consent_id)
        before = _scalar(conn, "SELECT count(*) FROM orders WHERE org_id = %s", (org["org_id"],))
        created_before = [a for _, a, o in _audit(conn, org) if a == "order_created" and o == "success"]

        with pytest.raises(NotFoundError):
            _new_order(dao, org, consent_id=consent_id)

        assert _scalar(conn, "SELECT count(*) FROM orders WHERE org_id = %s", (org["org_id"],)) == before
        assert [a for _, a, o in _audit(conn, org) if a == "order_created" and o == "success"] == created_before


class TestStagesBetweenOrderCreationAndSubmissionAfterWithdrawal:
    """CURRENT IMPLEMENTATION ONLY. None of these stages consults consent, so each one proceeds after the consent
    behind the order was withdrawn. That is not a decision that it should."""

    def test_placement_after_withdrawal_is_currently_accepted(self, dao, conn, world):
        org = world["a"]
        order_id, consent_id = _new_order(dao, org)
        _withdraw(dao, org, consent_id)

        dao.place_order(org["staff"], order_id)

        assert _order_state(conn, org, order_id) == "placed"

    def test_cancellation_after_withdrawal_is_currently_accepted(self, dao, conn, world):
        org = world["a"]
        order_id, consent_id = _new_order(dao, org)
        _withdraw(dao, org, consent_id)

        dao.cancel_order(org["staff"], order_id)

        assert _order_state(conn, org, order_id) == "cancelled"

    def test_sample_receipt_after_withdrawal_is_currently_accepted(self, dao, conn, world):
        org = world["a"]
        order_id, consent_id = _to_placed(dao, org)
        _withdraw(dao, org, consent_id)

        sample_id = dao.receive_sample(org["staff"], order_id, "blood")

        assert (
            _scalar(
                conn, "SELECT count(*) FROM samples WHERE org_id = %s AND sample_id = %s", (org["org_id"], sample_id)
            )
            == 1
        )

    @pytest.mark.parametrize("verdict", ["passed", "failed"])
    def test_qc_after_withdrawal_is_currently_accepted(self, dao, conn, world, verdict):
        org = world["a"]
        order_id, consent_id = _to_placed(dao, org)
        sample_id = dao.receive_sample(org["staff"], order_id, "blood")
        _withdraw(dao, org, consent_id)

        dao.record_qc(org["staff"], sample_id, verdict, "characterization" if verdict == "failed" else None)

        assert (
            _scalar(
                conn, "SELECT qc_status FROM samples WHERE org_id = %s AND sample_id = %s", (org["org_id"], sample_id)
            )
            == verdict
        )


class TestTheSubmissionToResultWindowAtTheDataLayer:
    """
    THE WINDOW: `validate_order_for_submission` (the only post-order consent check on the pipeline path) runs BEFORE
    the engine; `record_pipeline_result` takes no consent argument and reads no consent. A withdrawal in between is
    therefore not seen by the recording. The worker-level version of this (the real submission worker, a stubbed
    engine that withdraws while it runs) is geper/api/tests/test_worker_consent_withdrawal_during_run.py.

    CURRENT IMPLEMENTATION ONLY. Whether the recording should refuse, hold or discard is undecided.
    """

    def test_result_recording_after_a_passed_validation_and_a_withdrawal_is_currently_accepted(
        self, dao, conn, world, vcf
    ):
        org = world["a"]
        order_id, consent_id, sample_id = _to_qc_passed(dao, org)
        order = dao.get_order(dao._create_system_session(org["org_id"]), order_id)
        system = dao._create_system_session(org["org_id"])

        passed, exception_id = dao.validate_order_for_submission(
            system, order_id, order["patient_id"], sample_id, vcf[0], "GRCh38", SCOPE, "system"
        )
        assert (passed, exception_id) == (True, None)

        _withdraw(dao, org, consent_id)
        result = dao.record_pipeline_result(system, sample_id, vcf[0], vcf[1], RUN_DOCUMENT)

        assert _report_state(conn, org, result.report_id) == "draft"
        assert _scalar(conn, "SELECT count(*) FROM interpretations WHERE org_id = %s", (org["org_id"],)) == 1
        # What the system now says about the same order. The validation that passed is no longer true...
        assert _rule2(dao, org) == "consent_withdrawn"
        assert _rule3(conn, org, order_id) is False
        # ...and a fresh validation (which a retry would run) refuses it.
        passed_again, _ = dao.validate_order_for_submission(
            system, order_id, order["patient_id"], sample_id, vcf[0], "GRCh38", SCOPE, "system"
        )
        assert passed_again is False
        # Policy C hides the recorded analysis from staff reads.
        with pytest.raises(GenomeConsentWithheldError):
            dao.list_patient_genome_analyses(org["staff"], org["patient"])

    def test_the_audit_log_orders_the_withdrawal_before_the_recording(self, dao, conn, world, vcf):
        org = world["a"]
        order_id, consent_id, sample_id = _to_qc_passed(dao, org)
        _withdraw(dao, org, consent_id)
        _record_result(dao, org, sample_id, vcf)

        rows = _audit(conn, org)
        actions = [a for _, a, o in rows if o == "success"]
        assert "consent_withdrawn" in actions and "pipeline_result_recorded" in actions
        assert actions.index("consent_withdrawn") < actions.index("pipeline_result_recorded")
        assert not [a for _, a, o in rows if o != "success" and a == "pipeline_result_recorded"]


class TestReviewAndReleaseAfterWithdrawal:
    """CURRENT IMPLEMENTATION ONLY: the review and release verbs do not consult consent."""

    def test_review_steps_after_withdrawal_are_currently_accepted(self, dao, conn, world, vcf):
        org = world["a"]
        ctx = _to_state(dao, org, vcf, "draft")
        report_id = ctx["result"].report_id
        _withdraw(dao, org, ctx["consent_id"])

        dao.record_sole_signatory_claim(
            org["staff"],
            interpretation_id=ctx["result"].interpretation_id,
            report_id=report_id,
            reason="characterization",
        )
        dao.submit_for_review(org["staff"], report_id)
        assert _report_state(conn, org, report_id) == "under_review"
        dao.approve_report(org["staff"], report_id)
        assert _report_state(conn, org, report_id) == "approved"

    def test_release_after_withdrawal_is_currently_accepted(self, dao, conn, world, vcf):
        org = world["a"]
        ctx = _to_state(dao, org, vcf, "approved")
        report_id = ctx["result"].report_id
        _withdraw(dao, org, ctx["consent_id"])

        dao.release_report(org["staff"], report_id)

        assert _report_state(conn, org, report_id) == "released"


class TestWhatEachSurfaceShowsAfterReleaseWithdrawalAndRetraction:
    """CURRENT IMPLEMENTATION ONLY. A released report is shown by the patient portal whatever the consent says;
    staff order and consent reads show the rows with the consent's status as a display; the genome read is withheld
    under Policy C."""

    def test_after_release_and_withdrawal_each_surface_behaves_as_recorded(self, client, dao, conn, world, vcf):
        org = world["a"]
        ctx = _to_state(dao, org, vcf, "released")
        report_id = ctx["result"].report_id
        state_before = _order_state(conn, org, ctx["order_id"])
        _withdraw(dao, org, ctx["consent_id"])

        # patient portal: still lists the released report, not retracted
        cookie, listed, ids = _portal_reports(client, org)
        assert ids == [str(report_id)]
        assert listed[0]["is_retracted"] is False
        single = client.get(f"/portal/me/reports/{report_id}", cookies={PATIENT_COOKIE: cookie})
        assert single.status_code == 200, single.text

        # staff order read: still readable, and the order is not changed by the withdrawal
        order = dao.read_order(org["staff"], ctx["order_id"])
        assert order is not None and order["state"] == state_before
        # staff consent read: the withdrawal is displayed
        assert dao.read_consent(org["staff"], ctx["consent_id"])["status"] == "withdrawn"
        # staff genome read: withheld by Policy C
        with pytest.raises(GenomeConsentWithheldError):
            dao.list_patient_genome_analyses(org["staff"], org["patient"])

    def test_the_patients_own_genome_read_after_release_and_withdrawal_is_currently_served(self, dao, conn, world, vcf):
        """`DataAccess.list_my_genome_analyses` applies the released-and-not-retracted rule and has no consent
        predicate, unlike the STAFF genome read above, which is withheld under Policy C. Tested at the data layer
        because its route (`GET /portal/me/genome/chromosomes`) is mounted only when the patient-genome flag is set
        (`genome_endpoints.patient_routes_enabled`), which is off by default. Current implementation only: whether the
        patient's own read should follow Policy C is undecided."""
        org = world["a"]
        ctx = _to_state(dao, org, vcf, "released")
        patient_session = dao.patient_login(org["patient_email"], org["org_id"], PASSWORD)
        before = dao.list_my_genome_analyses(patient_session)
        assert len(before) == 1

        _withdraw(dao, org, ctx["consent_id"])

        assert dao.list_my_genome_analyses(patient_session) == before

    def test_after_retraction_the_portal_still_shows_the_report_flagged(self, client, dao, conn, world, vcf):
        org = world["a"]
        ctx = _to_state(dao, org, vcf, "released")
        report_id = ctx["result"].report_id
        _withdraw(dao, org, ctx["consent_id"])

        dao.retract_report(org["staff"], report_id, RETRACTION_REASON)

        _, listed, ids = _portal_reports(client, org)
        assert ids == [str(report_id)]
        assert listed[0]["is_retracted"] is True
        assert RETRACTION_REASON not in str(listed)

    def test_another_organisations_withdrawal_changes_nothing_here(self, client, dao, conn, world, vcf):
        """Tenant isolation: org B withdrawing its own consent leaves org A's released report, portal and Policy C
        read exactly as they were."""
        a, b = world["a"], world["b"]
        ctx_a = _to_state(dao, a, vcf, "released")
        order_b, consent_b = _to_placed(dao, b)
        _withdraw(dao, b, consent_b)

        _, listed, ids = _portal_reports(client, a)
        assert ids == [str(ctx_a["result"].report_id)]
        assert dao.list_patient_genome_analyses(a["staff"], a["patient"])[0]["released_to_patient"] is True
        assert _rule2(dao, a) == "consent_ok"
        assert _rule3(conn, a, ctx_a["order_id"]) is True


class TestTheAuditTrailAroundAWithdrawal:
    def test_the_withdrawal_is_audited_with_the_consent_and_no_later_stage_records_a_consent_refusal(
        self, dao, conn, world, vcf
    ):
        org = world["a"]
        ctx = _to_state(dao, org, vcf, "draft")
        _withdraw(dao, org, ctx["consent_id"])
        dao.record_sole_signatory_claim(
            org["staff"],
            interpretation_id=ctx["result"].interpretation_id,
            report_id=ctx["result"].report_id,
            reason="characterization",
        )
        dao.submit_for_review(org["staff"], ctx["result"].report_id)

        rows = _audit(conn, org)
        withdrawal = [i for i, (_, a, o) in enumerate(rows) if a == "consent_withdrawn" and o == "success"]
        assert len(withdrawal) == 1
        later = rows[withdrawal[0] + 1 :]
        assert later, "stages after the withdrawal left no audit rows at all"
        assert all(o == "success" for _, _, o in later), later
        # a withdrawal of an already-withdrawn consent is a plain not-found, audited as a failure
        with pytest.raises(NotFoundError):
            _withdraw(dao, org, ctx["consent_id"])
        assert [o for _, a, o in _audit(conn, org) if a == "consent_withdrawn"].count("success") == 1
