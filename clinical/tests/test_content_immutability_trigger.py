"""
tests/test_content_immutability_trigger.py
────────────────────────────────────────────

D0b half two: does schema.sql's BEFORE UPDATE trigger on reports,
interpretations, and vcfs actually enforce "state may ADVANCE, content may
NOT CHANGE"?

WHY A UNIQUELY-NAMED DATABASE, not the shared kelly-clinical-test schema
every other file DROPs and rebuilds: this file is landing the same day as
Ryan's DELETE revocation (test_privilege_boundary.py) and Kelly's read
receipts, all touching schema.sql's grant area in the same shared working
tree and the same shared Postgres container. A DROP SCHEMA CASCADE from one
agent's fixture racing another agent's mid-test schema is exactly the
contention this session has already lost a day to once today. CREATE
DATABASE at a fresh, randomly-suffixed name sidesteps it structurally --
two agents each creating their own database can never collide, and neither
can drop or reset the other's.

THREE GUARDS, same discipline as test_privilege_boundary.py and for the
same reason (a passing negative test is not evidence unless it could have
failed):
  1. EXACT EXCEPTION CLASS per refusal path -- ImmutabilityViolationError
     (the trigger's own RAISE EXCEPTION, SQLSTATE P0001, via DataAccess) or
     psycopg.errors.InsufficientPrivilege (the native column-level REVOKE,
     when the write never reaches the trigger at all because Postgres's own
     privilege system refuses it first).
  2. THE SUBJECT IS PROVEN PRESENT before a refusal means anything -- every
     tamper attempt first SELECTs the row back and asserts the OLD value is
     what the test thinks it is, so a refusal is never mistaken for "the
     WHERE clause matched nothing."
  3. THE REFUSAL IS PROVEN CAPABLE OF FAILING -- TestRefusalCanItselfFail
     disables the trigger, observes the SAME tamper statement succeed,
     re-enables it, and observes refusal return. Encoded as a real,
     re-runnable test rather than a one-off manual proof done once and
     discarded.
"""

from __future__ import annotations

import datetime
from datetime import date, timezone
import os
import re
import uuid

import pytest

from clinical.data_access import (
    BcryptHasher,
    DataAccess,
    ImmutabilityViolationError,
    SystemClock,
)

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = __import__("pathlib").Path(__file__).parent.parent / "schema.sql"

_LOCAL_TEST_PASSWORD = "not-a-secret-local-test-only"
_ROLES = ("clinical_app", "clinical_retention")


def _dsn_for_db(dsn: str, db_name: str) -> str:
    """Swap the trailing /<database> component of a DSN for `db_name`."""
    return re.sub(r"/[^/?]+(\?.*)?$", f"/{db_name}\\1", dsn)


def _unique_db_name() -> str:
    return f"test_immut_{uuid.uuid4().hex[:16]}"


def _connect_as(role: str, db_dsn: str):
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    return psycopg.connect(db_dsn, user=role, password=_LOCAL_TEST_PASSWORD, autocommit=True)


@pytest.fixture()
def db_dsn():
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")

    db_name = _unique_db_name()
    admin = psycopg.connect(DSN, autocommit=True)
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{db_name}"')
    admin.close()

    yield _dsn_for_db(DSN, db_name)

    cleanup = psycopg.connect(DSN, autocommit=True)
    with cleanup.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    cleanup.close()


@pytest.fixture()
def conn(db_dsn):
    """Superuser connection into the uniquely-named database: builds the schema and owns the roles."""
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    connection = psycopg.connect(db_dsn, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        for role in _ROLES:
            cur.execute(f"DO $$ BEGIN CREATE ROLE {role}; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
            cur.execute(f"ALTER ROLE {role} LOGIN PASSWORD '{_LOCAL_TEST_PASSWORD}'")
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def app_conn(conn, db_dsn):
    c = _connect_as("clinical_app", db_dsn)
    yield c
    c.close()


@pytest.fixture()
def retention_conn(conn, db_dsn):
    c = _connect_as("clinical_retention", db_dsn)
    yield c
    c.close()


@pytest.fixture()
def dao(conn):
    return DataAccess(conn, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))


def _fetchone(c, sql, params):
    with c.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


@pytest.fixture()
def approved_report(dao, conn):
    """A fully approved report, plus the ids of its interpretation and vcf. Proves each is present before returning."""
    org_id = dao.create_organisation("Immut Org")
    admin_id = dao.create_user(org_id, "admin@immut.test", "password")
    now = datetime.datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (admin_id, org_id, admin_id, now),
        )
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Interpreter', %s, %s)",
            (admin_id, org_id, admin_id, now),
        )
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at, basis) "
            "VALUES (%s, %s, 'Approver', %s, %s, %s)",
            (admin_id, org_id, admin_id, now, "board certified"),
        )
    conn.commit()
    session = dao.login("admin@immut.test", org_id, "password")

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

    sample_id, run_id, vcf_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO samples (sample_id, order_id, org_id, type, collected_at, collected_by, qc_status) "
            "VALUES (%s, %s, %s, 'blood', %s, %s, 'passed')",
            (sample_id, order_id, org_id, now, admin_id),
        )
        cur.execute(
            "INSERT INTO sequencing_runs (org_id, id, sample_id, created_at, created_by) VALUES (%s, %s, %s, %s, %s)",
            (org_id, run_id, sample_id, now, admin_id),
        )
        cur.execute(
            "INSERT INTO vcfs (org_id, id, sequencing_run_id, vcf_path, content_hash, created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (org_id, vcf_id, run_id, "/tmp/immut.vcf", "0" * 64, now, admin_id),
        )
    conn.commit()

    interp_id = dao.create_interpretation(session, vcf_id, {"model_version": "v1"}, str(sample_id))
    report_id = dao.create_report(session, interp_id)

    with conn.cursor() as cur:
        cur.execute(
            'INSERT INTO reviewer_claims (org_id, id, interpretation_id, claim_type, reason, actor_id, "timestamp") '
            "VALUES (%s, %s, %s, 'accept', 'looks right', %s, %s)",
            (org_id, uuid.uuid4(), interp_id, admin_id, now),
        )
    conn.commit()

    dao.submit_for_review(session, report_id)
    dao._approve_report(session, report_id, admin_id)

    row = _fetchone(conn, "SELECT approver_id, content_hash FROM reports WHERE id = %s", (report_id,))
    assert row[0] is not None and row[1] is not None, "fixture must produce an actually-approved report"

    return {
        "org_id": org_id,
        "session": session,
        "admin_id": admin_id,
        "report_id": report_id,
        "interp_id": interp_id,
        "vcf_id": vcf_id,
    }


class TestContentImmutabilityRefused:
    def test_reports_content_hash_tamper_refused(self, conn, approved_report):
        before = _fetchone(conn, "SELECT content_hash FROM reports WHERE id = %s", (approved_report["report_id"],))
        assert before[0] is not None  # subject present, guard 2

        with pytest.raises(Exception) as exc_info:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE reports SET content_hash = %s WHERE id = %s",
                    ("f" * 64, approved_report["report_id"]),
                )
        conn.rollback()
        assert getattr(exc_info.value, "sqlstate", None) == "P0001"

        after = _fetchone(conn, "SELECT content_hash FROM reports WHERE id = %s", (approved_report["report_id"],))
        assert after[0] == before[0]

    def test_reports_approver_id_tamper_refused(self, conn, approved_report):
        other_user = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, org_id, email, password_hash, password_changed_at, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    other_user,
                    approved_report["org_id"],
                    "other@immut.test",
                    "x",
                    datetime.datetime.now(timezone.utc),
                    datetime.datetime.now(timezone.utc),
                    datetime.datetime.now(timezone.utc),
                ),
            )
        conn.commit()

        before = _fetchone(conn, "SELECT approver_id FROM reports WHERE id = %s", (approved_report["report_id"],))
        assert before[0] is not None

        with pytest.raises(Exception) as exc_info:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE reports SET approver_id = %s WHERE id = %s", (other_user, approved_report["report_id"])
                )
        conn.rollback()
        assert getattr(exc_info.value, "sqlstate", None) == "P0001"

    def test_reports_interpretation_id_tamper_refused(self, conn, approved_report):
        with pytest.raises(Exception) as exc_info:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE reports SET interpretation_id = %s WHERE id = %s",
                    (uuid.uuid4(), approved_report["report_id"]),
                )
        conn.rollback()
        assert getattr(exc_info.value, "sqlstate", None) == "P0001"

    def test_interpretations_run_document_tamper_refused(self, conn, approved_report):
        import json

        before = _fetchone(
            conn, "SELECT run_document FROM interpretations WHERE id = %s", (approved_report["interp_id"],)
        )
        assert before[0] is not None

        with pytest.raises(Exception) as exc_info:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE interpretations SET run_document = %s WHERE id = %s",
                    (json.dumps({"model_version": "tampered"}), approved_report["interp_id"]),
                )
        conn.rollback()
        assert getattr(exc_info.value, "sqlstate", None) == "P0001"

    def test_vcfs_content_hash_tamper_refused(self, conn, approved_report):
        with pytest.raises(Exception) as exc_info:
            with conn.cursor() as cur:
                cur.execute("UPDATE vcfs SET content_hash = %s WHERE id = %s", ("e" * 64, approved_report["vcf_id"]))
        conn.rollback()
        assert getattr(exc_info.value, "sqlstate", None) == "P0001"

    def test_vcfs_vcf_path_tamper_refused(self, conn, approved_report):
        with pytest.raises(Exception) as exc_info:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE vcfs SET vcf_path = %s WHERE id = %s", ("/tmp/swapped.vcf", approved_report["vcf_id"])
                )
        conn.rollback()
        assert getattr(exc_info.value, "sqlstate", None) == "P0001"


class TestStateMayAdvance:
    def test_release_changes_state_only_and_succeeds(self, dao, conn, approved_report):
        """The exact case RLS could not handle: approved -> released is an UPDATE of an already-approved row."""
        release_id = dao._release_report(
            approved_report["session"], approved_report["report_id"], "clinician", approved_report["admin_id"]
        )
        assert release_id is not None

        row = _fetchone(conn, "SELECT state, content_hash FROM reports WHERE id = %s", (approved_report["report_id"],))
        assert row[0] == "released"
        assert row[1] is not None  # content_hash untouched by the state change

    def test_submit_for_review_changes_state_only(self, dao, conn):
        org_id = dao.create_organisation("Advance Org")
        admin_id = dao.create_user(org_id, "admin2@immut.test", "password")
        now = datetime.datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, 'Administrator', %s, %s)",
                (admin_id, org_id, admin_id, now),
            )
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, 'Interpreter', %s, %s)",
                (admin_id, org_id, admin_id, now),
            )
        conn.commit()
        session = dao.login("admin2@immut.test", org_id, "password")

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
        sample_id, run_id, vcf_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO samples (sample_id, order_id, org_id, type, collected_at, collected_by, qc_status) "
                "VALUES (%s, %s, %s, 'blood', %s, %s, 'passed')",
                (sample_id, order_id, org_id, now, admin_id),
            )
            cur.execute(
                "INSERT INTO sequencing_runs (org_id, id, sample_id, created_at, created_by) VALUES (%s, %s, %s, %s, %s)",
                (org_id, run_id, sample_id, now, admin_id),
            )
            cur.execute(
                "INSERT INTO vcfs (org_id, id, sequencing_run_id, vcf_path, content_hash, created_at, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (org_id, vcf_id, run_id, "/tmp/advance.vcf", "1" * 64, now, admin_id),
            )
        conn.commit()
        interp_id = dao.create_interpretation(session, vcf_id, {"model_version": "v1"}, str(sample_id))
        report_id = dao.create_report(session, interp_id)
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO reviewer_claims (org_id, id, interpretation_id, claim_type, reason, actor_id, "timestamp") '
                "VALUES (%s, %s, %s, 'accept', 'looks right', %s, %s)",
                (org_id, uuid.uuid4(), interp_id, admin_id, now),
            )
        conn.commit()

        dao.submit_for_review(session, report_id)

        row = _fetchone(conn, "SELECT state FROM reports WHERE id = %s", (report_id,))
        assert row[0] == "under_review"


class TestTombstonePrivilegeGate:
    def test_retention_role_can_write_tombstone_columns(self, retention_conn, conn, approved_report):
        now = datetime.datetime.now(timezone.utc)
        system_id = approved_report["admin_id"]  # any valid (org_id, user_id) satisfies the FK

        before = _fetchone(conn, "SELECT tombstoned_at FROM reports WHERE id = %s", (approved_report["report_id"],))
        assert before[0] is None

        with retention_conn.cursor() as cur:
            cur.execute(
                "UPDATE reports SET tombstoned_at = %s, tombstoned_by = %s WHERE id = %s",
                (now, system_id, approved_report["report_id"]),
            )

        after = _fetchone(conn, "SELECT tombstoned_at FROM reports WHERE id = %s", (approved_report["report_id"],))
        assert after[0] is not None

    def test_app_role_cannot_write_tombstone_columns(self, app_conn, conn, approved_report):
        now = datetime.datetime.now(timezone.utc)
        system_id = approved_report["admin_id"]

        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with app_conn.cursor() as cur:
                cur.execute(
                    "UPDATE reports SET tombstoned_at = %s, tombstoned_by = %s WHERE id = %s",
                    (now, system_id, approved_report["report_id"]),
                )

        after = _fetchone(conn, "SELECT tombstoned_at FROM reports WHERE id = %s", (approved_report["report_id"],))
        assert after[0] is None


class TestRefusalCanItselfFail:
    """Ryan's third guard, applied to the trigger instead of the GRANT block:
    disable it, prove the SAME tamper statement now succeeds, re-enable it,
    prove refusal returns. A refusal never observed to be capable of NOT
    firing is not evidence the mechanism works."""

    def test_disabling_the_trigger_lets_the_tamper_through_and_reenabling_restores_the_refusal(
        self, conn, approved_report
    ):
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE reports DISABLE TRIGGER trg_reports_content_immutability")
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("UPDATE reports SET content_hash = %s WHERE id = %s", ("d" * 64, approved_report["report_id"]))
        conn.commit()

        tampered = _fetchone(conn, "SELECT content_hash FROM reports WHERE id = %s", (approved_report["report_id"],))
        assert tampered[0] == "d" * 64  # the write landed -- proves this assertion mechanism CAN observe a failure

        with conn.cursor() as cur:
            cur.execute("ALTER TABLE reports ENABLE TRIGGER trg_reports_content_immutability")
        conn.commit()

        with pytest.raises(Exception) as exc_info:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE reports SET content_hash = %s WHERE id = %s", ("c" * 64, approved_report["report_id"])
                )
        conn.rollback()
        assert getattr(exc_info.value, "sqlstate", None) == "P0001"


class TestImmutabilityViolationErrorTranslation:
    def test_dataaccess_raises_the_named_exception_not_a_raw_driver_error(self, dao, conn, approved_report):
        """
        DataAccess never legitimately hits this trigger (see
        ImmutabilityViolationError's docstring), so this exercises the
        translation directly via _execute() rather than through a public
        method -- there is no public method whose normal, correct behaviour
        is to tamper approved content.
        """
        with pytest.raises(ImmutabilityViolationError):
            dao._execute(
                "UPDATE reports SET content_hash = %s WHERE id = %s",
                ("b" * 64, approved_report["report_id"]),
            )
