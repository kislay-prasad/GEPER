"""
clinical/tests/test_w120_audit_resource_id.py
─────────────────────────────────────────────

THE DEFECT THIS CLOSES. `@auditable` took the audit row's `resource_id` from
the decorated method's RETURN VALUE:

    resource_id=str(result) if result else "<none>"

That is right for the creators -- create_report returns the report id -- and
silently wrong for every method declared `-> None`. `_approve_report` is one
of those. So the audit trail recorded, for the single event that makes a
report releasable:

    action='report_approved'  resource_type='report'  resource_id='<none>'

i.e. "a report was approved" without saying WHICH report. An audit that
cannot name its subject is not an audit; under ISO 15189 / spec 13.3 the
approval record is the thing an inspector reads first.

The same shape hit every `-> None` auditable method, and `-> bool`
verify_report_integrity as well -- `str(True)` and, for a MISMATCH, the
falsy `False` collapsing to `<none>`: the nonconformance case, the one that
matters most, named no report at all.

WHY THE EXISTING TESTS DID NOT CATCH IT. test_w117_sole_signatory.py:410
asserts `_audit_actions(conn, "report_approved") == 1` -- a COUNT of rows,
not a reading of one. A row full of `<none>` counts exactly the same as a
row naming the report, so the assertion holds while the record is useless.
Every test here therefore reads the recorded resource_id and compares it to
the identifier of the resource actually acted on.

The structural test at the bottom generalises it: a method that cannot
return an identifier must say where its identifier comes from, or be on a
short, reasoned exemption list.
"""

from __future__ import annotations

import ast
import datetime
import pathlib
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
SCHEMA_PATH = pathlib.Path(__file__).parent.parent / "schema.sql"

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


def _session_with_roles(dao, admin_session, org_id, email, *roles):
    user_id = dao.create_user(org_id, email, "password")
    for role in roles:
        dao.assign_role(admin_session, user_id, role, basis="w120 test fixture role grant")
    return dao.login(email, org_id, "password")


@pytest.fixture
def signatory_a(dao, session_a, org_a):
    """One person who both submits and approves: the shortest route to an approval."""
    return _session_with_roles(
        dao, session_a, org_a, "signatory@org-a.test", "Interpreter", "Approver", "Orderer", "Lab technician"
    )


NOW = datetime.datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def order_chain(dao, conn, signatory_a):
    """
    A real order -> sample -> run -> vcf -> interpretation chain, returned as
    a dict of the ids, so each test can name the resource it expects the
    audit row to record.
    """
    session = signatory_a
    org_id = session.org_id
    test_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, org_id, "Panel", "GRCh38", "active", NOW),
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
            "VALUES (%s, %s, %s, 'blood', %s, %s, 'pending')",
            (sample_id, order_id, org_id, NOW, session.user_id),
        )
        cur.execute(
            "INSERT INTO sequencing_runs (org_id, id, sample_id, created_at, created_by) VALUES (%s, %s, %s, %s, %s)",
            (org_id, run_id, sample_id, NOW, session.user_id),
        )
        cur.execute(
            "INSERT INTO vcfs (org_id, id, sequencing_run_id, vcf_path, content_hash, created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (org_id, vcf_id, run_id, "/tmp/x.vcf", "0" * 64, NOW, session.user_id),
        )
        cur.execute(
            "INSERT INTO interpretations "
            "(org_id, id, vcf_id, run_document, submission_key, created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (org_id, interp_id, vcf_id, json.dumps({"variants": []}), "sub-" + str(interp_id), NOW, session.user_id),
        )
    conn.commit()
    return {
        "patient_id": patient_id,
        "consent_id": consent_id,
        "order_id": order_id,
        "sample_id": sample_id,
        "interpretation_id": interp_id,
    }


@pytest.fixture
def report_a(dao, signatory_a, order_chain):
    return dao.create_report(signatory_a, order_chain["interpretation_id"])


def _resource_ids(conn, action):
    """The RECORDED resource ids for an action -- the content, not the count."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT resource_id FROM audit_log WHERE action = %s AND outcome = 'success' ORDER BY log_id",
            (action,),
        )
        rows = [r[0] for r in cur.fetchall()]
    conn.commit()
    return rows


# ───────────────────────────────────────────────────────────────────────────
# The headline: the approval record must name the report
# ───────────────────────────────────────────────────────────────────────────


class TestTheApprovalRecordNamesTheReport:
    def test_report_approved_records_the_report_id(self, dao, conn, signatory_a, order_chain, report_a):
        dao.record_sole_signatory_claim(
            signatory_a,
            interpretation_id=order_chain["interpretation_id"],
            report_id=report_a,
            reason="Sole signatory concurrence.",
        )
        dao.submit_for_review(signatory_a, report_a)
        dao.approve_report(signatory_a, report_a)

        assert _resource_ids(conn, "report_approved") == [str(report_a)]

    def test_report_submitted_for_review_records_the_report_id(self, dao, conn, signatory_a, order_chain, report_a):
        dao.record_sole_signatory_claim(
            signatory_a,
            interpretation_id=order_chain["interpretation_id"],
            report_id=report_a,
            reason="Sole signatory concurrence.",
        )
        dao.submit_for_review(signatory_a, report_a)

        assert _resource_ids(conn, "report_submitted_for_review") == [str(report_a)]

    def test_require_release_records_the_report_id(self, dao, conn, signatory_a, order_chain, report_a):
        dao.record_sole_signatory_claim(
            signatory_a,
            interpretation_id=order_chain["interpretation_id"],
            report_id=report_a,
            reason="Sole signatory concurrence.",
        )
        dao.submit_for_review(signatory_a, report_a)
        dao.approve_report(signatory_a, report_a)
        dao.require_release(signatory_a, report_a)

        assert _resource_ids(conn, "require_release") == [str(report_a)]

    def test_report_integrity_verified_records_the_report_id_not_the_verdict(
        self, dao, conn, signatory_a, order_chain, report_a
    ):
        """
        `-> bool`: str(True) is not an identifier, and a MISMATCH returns the
        falsy False, which the old expression collapsed to '<none>' -- the
        nonconformance case named no report at all.
        """
        dao.record_sole_signatory_claim(
            signatory_a,
            interpretation_id=order_chain["interpretation_id"],
            report_id=report_a,
            reason="Sole signatory concurrence.",
        )
        dao.submit_for_review(signatory_a, report_a)
        dao.approve_report(signatory_a, report_a)
        assert dao.verify_report_integrity(signatory_a, report_a) is True

        assert _resource_ids(conn, "report_integrity_verified") == [str(report_a)]


# ───────────────────────────────────────────────────────────────────────────
# The same defect, everywhere else it landed
# ───────────────────────────────────────────────────────────────────────────


class TestTheOtherVoidReturningMethods:
    def test_order_placed_records_the_order_id(self, dao, conn, signatory_a, order_chain):
        dao.place_order(signatory_a, order_chain["order_id"])
        assert _resource_ids(conn, "order_placed") == [str(order_chain["order_id"])]

    def test_order_cancelled_records_the_order_id(self, dao, conn, signatory_a, order_chain):
        dao.cancel_order(signatory_a, order_chain["order_id"])
        assert _resource_ids(conn, "order_cancelled") == [str(order_chain["order_id"])]

    def test_qc_recorded_records_the_sample_id(self, dao, conn, signatory_a, order_chain):
        dao.record_qc(signatory_a, order_chain["sample_id"], "passed")
        assert _resource_ids(conn, "qc_recorded") == [str(order_chain["sample_id"])]

    def test_consent_withdrawn_records_the_consent_id(self, dao, conn, signatory_a, order_chain):
        dao.withdraw_consent(signatory_a, order_chain["consent_id"])
        assert _resource_ids(conn, "consent_withdrawn") == [str(order_chain["consent_id"])]

    def test_user_disabled_records_the_target_user_id(self, dao, conn, session_a, signatory_a):
        dao.set_user_disabled(session_a, signatory_a.user_id, True)
        assert _resource_ids(conn, "user_disabled") == [str(signatory_a.user_id)]

    def test_clinician_identity_set_records_the_target_user_id(self, dao, conn, session_a, signatory_a):
        dao.set_clinician_identity(
            session_a,
            signatory_a.user_id,
            full_name="Dr A",
            registration_number="GMC1234567",
            hospital="St Elsewhere",
        )
        assert _resource_ids(conn, "clinician_identity_set") == [str(signatory_a.user_id)]

    def test_session_terminated_records_the_terminated_session_id(self, dao, conn, session_a, signatory_a):
        dao.terminate_session(session_a, signatory_a.session_id)
        assert _resource_ids(conn, "session_terminated") == [str(signatory_a.session_id)]

    def test_retention_policy_set_records_the_artefact_class(self, dao, conn, session_a):
        """
        A retention policy has no surrogate id: (org, artefact_class) IS its
        identity, so that is what the record must name.
        """
        dao.set_retention_policy(session_a, "vcf", 3650)
        assert _resource_ids(conn, "retention_policy_set") == ["vcf"]


# ───────────────────────────────────────────────────────────────────────────
# The methods that DO return an id must be untouched
# ───────────────────────────────────────────────────────────────────────────


class TestReturnedIdentifiersAreStillRecorded:
    def test_create_report_still_records_its_own_returned_id(self, dao, conn, signatory_a, report_a):
        assert _resource_ids(conn, "report_created") == [str(report_a)]

    def test_create_patient_still_records_its_own_returned_id(self, dao, conn, signatory_a, order_chain):
        assert _resource_ids(conn, "patient_created") == [str(order_chain["patient_id"])]


# ───────────────────────────────────────────────────────────────────────────
# Structural: no method may silently have nothing to name
# ───────────────────────────────────────────────────────────────────────────


# Deliberately exempt, each for a stated reason. Shrinking this list is fine;
# growing it needs a reason as specific as these.
RESOURCE_ID_EXEMPT = {
    # A login that failed created no session, so there is no session id to
    # name. The submitted email is recorded in `details`, where it belongs --
    # an unauthenticated identifier is evidence, not a resource id.
    "log_failed_login",
    # A sweep, not an act on one resource: one run touches every due
    # exception. The per-exception records are written by the methods it
    # calls; naming any single exception here would be a false narrowing.
    "retry_scheduler",
}


def test_every_auditable_method_that_cannot_return_an_id_declares_where_its_id_comes_from():
    """
    THE GENERAL FORM OF THE DEFECT. `resource_id=str(result)` can only work
    for a method that returns an identifier. A method annotated `-> None` or
    `-> bool` never does, so unless it declares resource_id_param its audit
    rows record '<none>' (or 'True') forever, and nothing fails.
    """
    source = (pathlib.Path(__file__).resolve().parent.parent / "data_access.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = []
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DataAccess"]:
        for node in cls.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "auditable"):
                    continue
                kwargs = {k.arg: k.value for k in dec.keywords}
                if isinstance(kwargs.get("auditable"), ast.Constant) and kwargs["auditable"].value is False:
                    continue
                returns = ast.unparse(node.returns) if node.returns else None
                if returns not in ("None", "bool"):
                    continue
                if "resource_id_param" in kwargs:
                    continue
                if node.name in RESOURCE_ID_EXEMPT:
                    continue
                offenders.append(f"{node.name} (line {node.lineno}, returns {returns})")

    assert not offenders, (
        "auditable method(s) that can never return an identifier and do not declare "
        f"resource_id_param: {offenders}. Their audit rows record resource_id='<none>' "
        "-- an audit entry that cannot name its subject. Add "
        "resource_id_param='<the parameter naming the resource>', or add the method to "
        "RESOURCE_ID_EXEMPT with a reason."
    )


def test_declared_resource_id_param_names_a_real_parameter():
    """A typo'd parameter name would fall back to '<none>' silently."""
    source = (pathlib.Path(__file__).resolve().parent.parent / "data_access.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    bad = []
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DataAccess"]:
        for node in cls.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "auditable"):
                    continue
                for kw in dec.keywords:
                    if kw.arg != "resource_id_param":
                        continue
                    name = kw.value.value
                    params = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
                    if name not in params:
                        bad.append(f"{node.name}: resource_id_param={name!r} is not a parameter of it")

    assert not bad, bad
