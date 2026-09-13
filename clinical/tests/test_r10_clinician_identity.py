"""
clinical/tests/test_r10_clinician_identity.py
──────────────────────────────────────────────

R10 (human ruling 2026-09-13): a clinical report shows the signing
clinician's NAME, REGISTRATION NUMBER and HOSPITAL.

Before this change the user record held an email address and nothing else, so
those three values were typed in by hand at sign-off time
(geper/review/signoff.py::approve) and nothing tied them to the account that
signed. This suite is about the half that lives in the database: the three
fields on the user record, and the read that turns an APPROVED REPORT into the
identity it may print.

WHAT IS TESTED AS BEHAVIOUR, not as schema:

  - a report approved by a user carrying all three fields yields exactly those
    three values, read from the user record (not from anything the caller
    supplied);
  - a user missing a registration number is REFUSED, and the refusal names the
    missing field. Refusal rather than a "Reg. No. not provided" marking: the
    ruling is that the report SHOWS the registration number, so printing a
    signature without it produces a document that asserts a qualified
    signatory while withholding the one credential that evidences the
    qualification. The remedy is one administrator action
    (set_clinician_identity), so refusing costs a correction and marking costs
    a defective released report;
  - the approval path itself is UNCHANGED for a user with no identity -- a
    legacy account can still approve, exactly as before. The new gate is at
    the point the identity is read for printing, not at approval, so no
    existing report or workflow changes behaviour;
  - an unapproved report has no signing identity at all, rather than a blank
    or fabricated one;
  - every read is org-scoped: org B cannot read org A's signing identity, and
    a cross-org report id is NotFound rather than a leak.
"""

from __future__ import annotations

import datetime
from datetime import date, timezone
import json
import os
import pathlib
import uuid

import pytest

from clinical.data_access import (
    AuthorizationError,
    BcryptHasher,
    DataAccess,
    IncompleteClinicianIdentityError,
    NotFoundError,
    SystemClock,
)

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = pathlib.Path(__file__).parent.parent / "schema.sql"

_CREATE_ROLE = "DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
_CREATE_ROLE_RETENTION = (
    "DO $$ BEGIN CREATE ROLE clinical_retention; EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
)

NAME = "Dr. Rajesh Sharma"
REG = "MCI-12345"
HOSPITAL = "AIIMS Delhi"


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


def _approver_session(dao, admin_session, org_id, email, **identity):
    user_id = dao.create_user(org_id, email, "password", **identity)
    dao.assign_role(admin_session, user_id, "Approver", basis="Board-certified clinical geneticist, licence #A-001")
    return dao.login(email, org_id, "password")


@pytest.fixture
def approver_a(dao, session_a, org_a):
    """An Approver whose user record carries all three signing fields."""
    return _approver_session(
        dao,
        session_a,
        org_a,
        "approver@org-a.test",
        full_name=NAME,
        registration_number=REG,
        hospital=HOSPITAL,
    )


@pytest.fixture
def approver_b(dao, session_b, org_b):
    return _approver_session(
        dao,
        session_b,
        org_b,
        "approver@org-b.test",
        full_name="Dr. B",
        registration_number="MCI-99999",
        hospital="Other Hospital",
    )


@pytest.fixture
def approver_without_reg_a(dao, session_a, org_a):
    """Name and hospital recorded, registration number never captured."""
    return _approver_session(
        dao,
        session_a,
        org_a,
        "noreg@org-a.test",
        full_name=NAME,
        hospital=HOSPITAL,
    )


@pytest.fixture
def legacy_approver_a(dao, session_a, org_a):
    """A pre-R10 account: email and password, no signing identity at all."""
    return _approver_session(dao, session_a, org_a, "legacy@org-a.test")


def _make_interpretation(dao, conn, session):
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
            (org_id, interp_id, vcf_id, json.dumps({"variants": []}), "sub-" + str(interp_id), now, session.user_id),
        )
    conn.commit()
    return interp_id


def _report_under_review(dao, conn, session):
    interp_id = _make_interpretation(dao, conn, session)
    report_id = dao.create_report(session, interp_id)
    with conn.cursor() as cur:
        cur.execute("UPDATE reports SET state = 'under_review' WHERE id = %s", (report_id,))
    conn.commit()
    return report_id


# ───────────────────────────────────────────────────────────────────────────
# The report shows the user record's identity
# ───────────────────────────────────────────────────────────────────────────


class TestSigningIdentityComesFromTheUserRecord:
    def test_approved_report_yields_the_approvers_three_fields(self, dao, conn, session_a, approver_a):
        report_id = _report_under_review(dao, conn, session_a)
        dao._approve_report(approver_a, report_id, actor_id=approver_a.user_id)

        identity = dao.signing_identity_for_report(approver_a, report_id)

        assert identity["full_name"] == NAME
        assert identity["registration_number"] == REG
        assert identity["hospital"] == HOSPITAL
        assert identity["user_id"] == approver_a.user_id

    def test_identity_follows_a_corrected_user_record(self, dao, conn, session_a, approver_a):
        """
        The point of sourcing from the record rather than a typed-in string:
        correcting the record corrects what the report may print. A typed-in
        value frozen at sign-off could not be corrected at all without
        re-signing.
        """
        report_id = _report_under_review(dao, conn, session_a)
        dao._approve_report(approver_a, report_id, actor_id=approver_a.user_id)

        dao.set_clinician_identity(
            session_a,
            approver_a.user_id,
            full_name=NAME,
            registration_number="MCI-54321",
            hospital=HOSPITAL,
        )

        identity = dao.signing_identity_for_report(approver_a, report_id)
        assert identity["registration_number"] == "MCI-54321"

    def test_create_user_without_identity_records_none_not_empty_strings(self, dao, session_a, org_a):
        """A blank string would print as a signature with an empty credential."""
        user_id = dao.create_user(org_a, "plain@org-a.test", "password")
        record = dao.get_user(session_a, user_id)
        assert record.full_name is None
        assert record.registration_number is None
        assert record.hospital is None


# ───────────────────────────────────────────────────────────────────────────
# A missing registration number is refused
# ───────────────────────────────────────────────────────────────────────────


class TestIncompleteIdentityIsRefused:
    def test_missing_registration_number_is_refused_and_named(self, dao, conn, session_a, approver_without_reg_a):
        report_id = _report_under_review(dao, conn, session_a)
        dao._approve_report(approver_without_reg_a, report_id, actor_id=approver_without_reg_a.user_id)

        with pytest.raises(IncompleteClinicianIdentityError) as exc:
            dao.signing_identity_for_report(approver_without_reg_a, report_id)
        assert "registration_number" in str(exc.value)
        # The fields that ARE present must not be named as missing.
        assert "full_name" not in str(exc.value)

    def test_legacy_account_with_no_identity_names_all_three(self, dao, conn, session_a, legacy_approver_a):
        report_id = _report_under_review(dao, conn, session_a)
        dao._approve_report(legacy_approver_a, report_id, actor_id=legacy_approver_a.user_id)

        with pytest.raises(IncompleteClinicianIdentityError) as exc:
            dao.signing_identity_for_report(legacy_approver_a, report_id)
        message = str(exc.value)
        for field in ("full_name", "registration_number", "hospital"):
            assert field in message

    def test_approval_itself_still_succeeds_for_an_account_with_no_identity(
        self, dao, conn, session_a, legacy_approver_a
    ):
        """
        CONTROL: the new gate is at the point of use, not at approval.
        Existing behaviour for legacy accounts and their reports is unchanged
        -- approval records the same three facts it always did.
        """
        report_id = _report_under_review(dao, conn, session_a)
        dao._approve_report(legacy_approver_a, report_id, actor_id=legacy_approver_a.user_id)

        with conn.cursor() as cur:
            cur.execute("SELECT state, approver_id, approved_at, content_hash FROM reports WHERE id = %s", (report_id,))
            state, approver_id, approved_at, content_hash = cur.fetchone()
        assert state == "approved"
        assert approver_id == legacy_approver_a.user_id
        assert approved_at is not None
        assert content_hash is not None

    def test_unapproved_report_has_no_signing_identity(self, dao, conn, session_a, approver_a):
        """
        CONTROL: not-yet-signed is a different answer from signed-by-someone.
        A blank identity here would be a report claiming an unnamed signature.
        """
        report_id = _report_under_review(dao, conn, session_a)
        with pytest.raises(ValueError) as exc:
            dao.signing_identity_for_report(approver_a, report_id)
        assert "not been approved" in str(exc.value)


# ───────────────────────────────────────────────────────────────────────────
# Administration of the three fields
# ───────────────────────────────────────────────────────────────────────────


class TestSetClinicianIdentity:
    def test_administrator_can_record_an_identity_after_the_fact(self, dao, session_a, legacy_approver_a, conn):
        dao.set_clinician_identity(
            session_a,
            legacy_approver_a.user_id,
            full_name=NAME,
            registration_number=REG,
            hospital=HOSPITAL,
        )
        report_id = _report_under_review(dao, conn, session_a)
        dao._approve_report(legacy_approver_a, report_id, actor_id=legacy_approver_a.user_id)
        identity = dao.signing_identity_for_report(legacy_approver_a, report_id)
        assert (identity["full_name"], identity["registration_number"], identity["hospital"]) == (NAME, REG, HOSPITAL)

    def test_non_administrator_may_not_set_an_identity(self, dao, approver_a, legacy_approver_a):
        """An Approver editing their own printed credentials would make the record self-asserted."""
        with pytest.raises(AuthorizationError):
            dao.set_clinician_identity(
                approver_a,
                legacy_approver_a.user_id,
                full_name=NAME,
                registration_number=REG,
                hospital=HOSPITAL,
            )

    def test_blank_values_are_refused(self, dao, session_a, legacy_approver_a):
        with pytest.raises(ValueError):
            dao.set_clinician_identity(
                session_a,
                legacy_approver_a.user_id,
                full_name=NAME,
                registration_number="   ",
                hospital=HOSPITAL,
            )

    def test_two_users_in_one_org_may_not_share_a_registration_number(self, dao, session_a, org_a, legacy_approver_a):
        dao.set_clinician_identity(
            session_a, legacy_approver_a.user_id, full_name=NAME, registration_number=REG, hospital=HOSPITAL
        )
        other = dao.create_user(org_a, "other@org-a.test", "password")
        with pytest.raises(Exception):
            dao.set_clinician_identity(
                session_a, other, full_name="Dr. Other", registration_number=REG, hospital=HOSPITAL
            )

    def test_the_same_registration_number_may_exist_in_another_org(self, dao, session_a, session_b, org_b, approver_a):
        """CONTROL for the uniqueness rule: it is org-scoped, not global."""
        other = dao.create_user(org_b, "same-reg@org-b.test", "password")
        dao.set_clinician_identity(session_b, other, full_name=NAME, registration_number=REG, hospital=HOSPITAL)
        record = dao.get_user(session_b, other)
        assert record.registration_number == REG


# ───────────────────────────────────────────────────────────────────────────
# Organisation scoping
# ───────────────────────────────────────────────────────────────────────────


class TestOrgScoping:
    def test_other_org_cannot_read_a_reports_signing_identity(
        self, dao, conn, session_a, session_b, approver_a, approver_b
    ):
        report_id = _report_under_review(dao, conn, session_a)
        dao._approve_report(approver_a, report_id, actor_id=approver_a.user_id)

        with pytest.raises(NotFoundError):
            dao.signing_identity_for_report(approver_b, report_id)

    def test_other_org_cannot_set_an_identity_on_this_orgs_user(self, dao, session_b, legacy_approver_a):
        with pytest.raises(NotFoundError):
            dao.set_clinician_identity(
                session_b,
                legacy_approver_a.user_id,
                full_name=NAME,
                registration_number=REG,
                hospital=HOSPITAL,
            )
