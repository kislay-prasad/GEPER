"""
clinical/tests/test_record_pipeline_result.py
─────────────────────────────────────────────
D0 ("build the link", 2026-09-12): a completed pipeline run becomes
clinical records -- sequencing run, VCF, interpretation, draft report -- in
ONE transaction, and a retry can find what an earlier attempt recorded.

Why one method rather than the four existing create_* calls in a row: each
of those is @auditable, and @auditable COMMITS on return. Four calls are
four commits, so a failure at the report left an interpretation with no
report (and a VCF with no interpretation) behind. The atomicity tests below
inject that exact failure and count rows in every table.

All state is built through the real methods (create_patient, create_order,
receive_sample, record_qc, ...); raw SQL appears only to READ the result and,
in one test, to set up an approved report for the amendment case, which is
how the existing amendment tests do it.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid

import pytest

from clinical.data_access import (
    BcryptHasher,
    DataAccess,
    DuplicateInterpretationError,
    NotFoundError,
    SystemClock,
    VcfChangedError,
    interpretation_submission_key,
)
from clinical.tests.db_guard import reset_schema

DSN = os.getenv("CLINICAL_TEST_DSN")

VCF_TEXT = (
    "##fileformat=VCFv4.2\n"
    "##assembly=GRCh38\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample1\n"
    "chr1\t1000\t.\tA\tT\t30\tPASS\t.\tGT\t0/1\n"
)
RUN_DOCUMENT = {"variants": [{"chrom": "1", "pos": 1000}], "run_complete": True}
COUNTED_TABLES = ("sequencing_runs", "vcfs", "interpretations", "reports")


@pytest.fixture()
def conn():
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")
    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    reset_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def dao(conn):
    return DataAccess(conn, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))


def _admin(dao, conn, org_id, email):
    user_id = dao.create_user(org_id, email, "password")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org_id, user_id, datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)),
        )
    conn.commit()
    return dao.login(email, org_id, "password")


def _sample(dao, conn, admin):
    """patient -> consent -> order -> placed -> sample received -> QC passed,
    all through the real methods. Returns (order_id, sample_id)."""
    test_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                test_id,
                admin.org_id,
                "Panel",
                "GRCh38",
                "active",
                datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc),
            ),
        )
    conn.commit()
    patient_id = dao.create_patient(admin, "Test Patient", datetime.date(1990, 1, 1), "M")
    consent_id = dao.record_consent(admin, patient_id, "testing")
    order_id = dao.create_order(
        admin,
        patient_id=patient_id,
        test_id=test_id,
        required_scope="testing",
        consent_id=consent_id,
        priority="routine",
        clinical_indication="Test indication",
    )
    dao.place_order(admin, order_id)
    sample_id = dao.receive_sample(admin, order_id, "blood")
    dao.record_qc(admin, sample_id, "passed")
    return order_id, sample_id


@pytest.fixture
def org_a(dao, conn):
    org_id = dao.create_organisation("Org A")
    admin = _admin(dao, conn, org_id, "admin@a.test")
    order_id, sample_id = _sample(dao, conn, admin)
    return {"org_id": org_id, "admin": admin, "order_id": order_id, "sample_id": sample_id}


@pytest.fixture
def org_b(dao, conn):
    org_id = dao.create_organisation("Org B")
    admin = _admin(dao, conn, org_id, "admin@b.test")
    order_id, sample_id = _sample(dao, conn, admin)
    return {"org_id": org_id, "admin": admin, "order_id": order_id, "sample_id": sample_id}


@pytest.fixture
def vcf(tmp_path):
    path = tmp_path / "run.vcf"
    path.write_text(VCF_TEXT, encoding="utf-8")
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


def _counts(conn):
    with conn.cursor() as cur:
        out = {}
        for table in COUNTED_TABLES:
            cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 -- fixed table names above
            out[table] = cur.fetchone()[0]
    conn.commit()
    return out


def _audit_actions(conn, org_id):
    with conn.cursor() as cur:
        cur.execute("SELECT action, outcome FROM audit_log WHERE org_id = %s ORDER BY log_id", (org_id,))
        rows = cur.fetchall()
    conn.commit()
    return rows


class TestRecordPipelineResult:
    def test_records_run_vcf_interpretation_and_draft_report(self, dao, conn, org_a, vcf):
        vcf_path, vcf_hash = vcf
        system = dao._create_system_session(org_a["org_id"])

        result = dao.record_pipeline_result(system, org_a["sample_id"], vcf_path, vcf_hash, RUN_DOCUMENT)

        assert _counts(conn) == {t: 1 for t in COUNTED_TABLES}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT i.vcf_id, i.run_document, i.submission_key, i.created_by, i.retention_days, "
                "       v.content_hash, v.vcf_path, v.retention_days, v.sequencing_run_id, "
                "       r.id, r.state, r.retention_days, s.sample_id "
                "FROM interpretations i "
                "JOIN vcfs v ON v.org_id = i.org_id AND v.id = i.vcf_id "
                "JOIN reports r ON r.org_id = i.org_id AND r.interpretation_id = i.id "
                "JOIN sequencing_runs s ON s.org_id = v.org_id AND s.id = v.sequencing_run_id "
                "WHERE i.org_id = %s AND i.id = %s",
                (org_a["org_id"], result.interpretation_id),
            )
            row = cur.fetchone()
        conn.commit()
        (
            vcf_id,
            run_document,
            submission_key,
            created_by,
            interp_retention,
            content_hash,
            stored_path,
            vcf_retention,
            run_id,
            report_id,
            state,
            report_retention,
            sample_id,
        ) = row
        run_document = json.loads(run_document) if isinstance(run_document, str) else run_document

        assert vcf_id == result.vcf_id
        assert run_id == result.sequencing_run_id
        assert report_id == result.report_id
        assert sample_id == org_a["sample_id"]
        assert run_document == RUN_DOCUMENT
        assert content_hash == vcf_hash
        assert stored_path == vcf_path
        assert submission_key == f"{vcf_hash}|{org_a['sample_id']}" == result.submission_key
        assert created_by == system.user_id
        assert state == "draft"
        # Retention: resolved once at the VCF and inherited downward (ruling D2).
        assert vcf_retention is not None and interp_retention == vcf_retention == report_retention

        actions = [a for a, outcome in _audit_actions(conn, org_a["org_id"]) if outcome == "success"]
        for action in (
            "sequencing_run_created",
            "vcf_created",
            "interpretation_created",
            "report_created",
            "pipeline_result_recorded",
        ):
            assert action in actions, actions

    def test_submission_key_matches_the_two_existing_formulas(self, dao, org_a, vcf):
        vcf_path, vcf_hash = vcf
        with open(vcf_path, "rb") as fh:
            content = fh.read()
        key = interpretation_submission_key(vcf_hash, org_a["sample_id"])
        assert key == dao.derive_submission_key(content, org_a["sample_id"])
        assert key == f"{vcf_hash}|{org_a['sample_id']}"

    def test_vcf_changed_since_the_run_is_refused_and_nothing_is_written(self, dao, conn, org_a, vcf):
        vcf_path, _ = vcf
        system = dao._create_system_session(org_a["org_id"])
        with pytest.raises(VcfChangedError):
            dao.record_pipeline_result(system, org_a["sample_id"], vcf_path, "0" * 64, RUN_DOCUMENT)
        assert _counts(conn) == {t: 0 for t in COUNTED_TABLES}

    def test_failure_at_the_report_rolls_back_every_row(self, dao, conn, org_a, vcf, monkeypatch):
        """The exact partial state four separate commits used to leave."""
        vcf_path, vcf_hash = vcf
        system = dao._create_system_session(org_a["org_id"])
        real_execute = dao._execute

        def failing_execute(sql, params=()):
            if sql.lstrip().upper().startswith("INSERT INTO REPORTS"):
                raise RuntimeError("injected: report insert failed")
            return real_execute(sql, params)

        monkeypatch.setattr(dao, "_execute", failing_execute)
        with pytest.raises(RuntimeError, match="injected"):
            dao.record_pipeline_result(system, org_a["sample_id"], vcf_path, vcf_hash, RUN_DOCUMENT)
        monkeypatch.undo()

        assert _counts(conn) == {t: 0 for t in COUNTED_TABLES}
        # The attempt itself is on the record, as a failure, with none of the
        # per-resource "created" rows that would claim otherwise.
        rows = _audit_actions(conn, org_a["org_id"])
        assert ("pipeline_result_recorded", "error") in rows
        assert not any(a in ("vcf_created", "interpretation_created") for a, _ in rows)

    def test_same_vcf_and_sample_twice_is_a_duplicate_not_a_second_interpretation(self, dao, conn, org_a, vcf):
        vcf_path, vcf_hash = vcf
        system = dao._create_system_session(org_a["org_id"])
        first = dao.record_pipeline_result(system, org_a["sample_id"], vcf_path, vcf_hash, RUN_DOCUMENT)
        with pytest.raises(DuplicateInterpretationError) as info:
            dao.record_pipeline_result(system, org_a["sample_id"], vcf_path, vcf_hash, {"variants": []})
        assert info.value.submission_key == first.submission_key
        assert _counts(conn) == {t: 1 for t in COUNTED_TABLES}

    def test_another_organisations_sample_is_refused_and_nothing_is_written(self, dao, conn, org_a, org_b, vcf):
        vcf_path, vcf_hash = vcf
        system_b = dao._create_system_session(org_b["org_id"])
        with pytest.raises(NotFoundError):
            dao.record_pipeline_result(system_b, org_a["sample_id"], vcf_path, vcf_hash, RUN_DOCUMENT)
        assert _counts(conn) == {t: 0 for t in COUNTED_TABLES}


class TestFindInterpretationBySubmissionKey:
    def test_absent_until_recorded_then_found(self, dao, org_a, vcf):
        vcf_path, vcf_hash = vcf
        system = dao._create_system_session(org_a["org_id"])
        key = interpretation_submission_key(vcf_hash, org_a["sample_id"])
        assert dao.find_interpretation_by_submission_key(system, key) is None

        recorded = dao.record_pipeline_result(system, org_a["sample_id"], vcf_path, vcf_hash, RUN_DOCUMENT)
        found = dao.find_interpretation_by_submission_key(system, key)
        assert found == {
            "interpretation_id": recorded.interpretation_id,
            "report_id": recorded.report_id,
            "vcf_id": recorded.vcf_id,
        }

    def test_org_scoped(self, dao, org_a, org_b, vcf):
        """Org B asking for Org A's key finds nothing, even though the key
        string is known to it."""
        vcf_path, vcf_hash = vcf
        system_a = dao._create_system_session(org_a["org_id"])
        recorded = dao.record_pipeline_result(system_a, org_a["sample_id"], vcf_path, vcf_hash, RUN_DOCUMENT)
        system_b = dao._create_system_session(org_b["org_id"])
        assert dao.find_interpretation_by_submission_key(system_b, recorded.submission_key) is None

    def test_report_is_the_original_not_a_later_amendment(self, dao, conn, org_a, vcf):
        vcf_path, vcf_hash = vcf
        system = dao._create_system_session(org_a["org_id"])
        recorded = dao.record_pipeline_result(system, org_a["sample_id"], vcf_path, vcf_hash, RUN_DOCUMENT)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reports SET state = 'approved', approver_id = %s, approved_at = now(), content_hash = %s "
                "WHERE id = %s",
                (org_a["admin"].user_id, "0" * 64, recorded.report_id),
            )
        conn.commit()
        amendment_report_id, _ = dao.create_amendment(org_a["admin"], recorded.report_id, "Corrected classification")

        found = dao.find_interpretation_by_submission_key(system, recorded.submission_key)
        assert found["report_id"] == recorded.report_id != amendment_report_id
