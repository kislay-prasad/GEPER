"""
tests/test_phase_5d_orchestration.py
────────────────────────────────────
Tests for Phase 5d exception wiring into failure paths.
Verifies that check failures trigger create_or_reopen_exception.
"""

import datetime
import os
import pytest
import uuid

from clinical.data_access import DataAccess, BcryptHasher, SystemClock
from clinical.models.exception import (
    ExceptionCategory,
    ExceptionReasonCode,
    ExceptionStatus,
)

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = __import__("pathlib").Path(__file__).parent.parent / "schema.sql"


@pytest.fixture()
def conn():
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")
    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute("DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def dao(conn):
    """DataAccess instance for testing."""
    return DataAccess(conn, clock=SystemClock(), password_hasher=BcryptHasher(rounds=4))


@pytest.fixture
def org_a(dao):
    """Create organisation A."""
    return dao.create_organisation("Org A")


@pytest.fixture
def session_admin(dao, org_a, conn):
    """Create an admin user and session in Org A."""
    user_id = dao.create_user(org_a, "admin@org-a.test", "password")
    # Insert admin role directly to break bootstrap cycle
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org_a, user_id, datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)),
        )
    conn.commit()
    return dao.login("admin@org-a.test", org_a, "password")


@pytest.fixture
def test_catalogue(dao, session_admin, conn):
    """Create a test in the catalogue."""
    test_id = uuid.uuid4()
    now = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, session_admin.org_id, "Test Gene Panel", "GRCh38", "active", now),
        )
    conn.commit()
    return test_id


@pytest.fixture
def patient_and_order(dao, session_admin, test_catalogue, conn):
    """Create a patient and order for testing."""
    patient_id = dao.create_patient(session_admin, "Test Patient", datetime.date(1990, 1, 1), "M")
    consent_id = dao.record_consent(session_admin, patient_id, "testing")
    order_id = dao.create_order(
        session_admin,
        patient_id=patient_id,
        test_id=test_catalogue,
        required_scope="testing",
        consent_id=consent_id,
        priority="routine",
        clinical_indication="Test indication",
    )
    return patient_id, order_id


@pytest.fixture
def sample_with_qc(dao, session_admin, patient_and_order, conn):
    """Create a sample with passing QC."""
    _, order_id = patient_and_order
    sample_id = dao.receive_sample(session_admin, order_id, "blood")
    dao.record_qc(session_admin, sample_id, "passed")
    return sample_id


class TestValidateOrderForSubmission:
    """Test the orchestration layer validate_order_for_submission."""

    def test_all_checks_pass_returns_success(
        self, dao, session_admin, patient_and_order, sample_with_qc, tmp_path, conn
    ):
        """When all 5a checks pass, orchestration returns (True, None)."""
        patient_id, order_id = patient_and_order
        sample_id = sample_with_qc

        # Create a valid VCF file for testing
        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text(
            "##fileformat=VCFv4.2\n"
            "##assembly=GRCh38\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tsample1\n"
            "chr1\t1000\t.\tA\tT\t30\tPASS\t.\t0/1\n"
        )

        # All checks should pass
        success, exc_id = dao.validate_order_for_submission(
            session_admin,
            order_id,
            patient_id,
            sample_id,
            str(vcf_file),
            "GRCh38",
            "testing",
            "system",
        )

        assert success is True
        assert exc_id is None

    def test_missing_consent_creates_exception(
        self, dao, session_admin, patient_and_order, sample_with_qc, tmp_path, conn
    ):
        """When consent is missing, orchestration creates exception and returns (False, exc_id)."""
        patient_id, order_id = patient_and_order
        sample_id = sample_with_qc

        # Create a valid VCF file
        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text(
            "##fileformat=VCFv4.2\n"
            "##assembly=GRCh38\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tsample1\n"
            "chr1\t1000\t.\tA\tT\t30\tPASS\t.\t0/1\n"
        )

        # Call with scope that has no consent
        success, exc_id = dao.validate_order_for_submission(
            session_admin,
            order_id,
            patient_id,
            sample_id,
            str(vcf_file),
            "GRCh38",
            "no_such_scope",  # Missing consent for this scope
            "system",
        )

        assert success is False
        assert exc_id is not None

        # Verify exception was created
        exceptions = dao.get_exceptions_for_order(session_admin, order_id)
        assert len(exceptions) == 1
        exc = exceptions[0]
        assert exc.reason_code == ExceptionReasonCode.CONSENT_MISSING.value
        assert exc.category == ExceptionCategory.PRECONDITION_FAILURE.value
        assert exc.owner == "orderer"
        assert exc.status == ExceptionStatus.OPEN.value

    def test_missing_vcf_file_creates_exception(self, dao, session_admin, patient_and_order, sample_with_qc, conn):
        """When VCF file is missing, orchestration creates exception."""
        patient_id, order_id = patient_and_order
        sample_id = sample_with_qc

        # Use non-existent VCF file
        vcf_path = "/nonexistent/path/to/file.vcf"

        success, exc_id = dao.validate_order_for_submission(
            session_admin,
            order_id,
            patient_id,
            sample_id,
            vcf_path,
            "GRCh38",
            "testing",
            "system",
        )

        assert success is False
        assert exc_id is not None

        # Verify exception was created
        exceptions = dao.get_exceptions_for_order(session_admin, order_id)
        assert len(exceptions) == 1
        exc = exceptions[0]
        assert exc.reason_code == ExceptionReasonCode.VCF_MISSING.value
        assert exc.category == ExceptionCategory.VALIDATION_FAILURE.value
        assert exc.owner == "lab_operator"

    def test_failed_qc_creates_exception(self, dao, session_admin, patient_and_order, test_catalogue, tmp_path, conn):
        """When QC fails, orchestration creates exception."""
        patient_id, order_id = patient_and_order

        # Create a sample with failed QC
        sample_id = dao.receive_sample(session_admin, order_id, "blood")
        dao.record_qc(session_admin, sample_id, "failed")

        # Create a valid VCF file
        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text(
            "##fileformat=VCFv4.2\n"
            "##assembly=GRCh38\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tsample1\n"
            "chr1\t1000\t.\tA\tT\t30\tPASS\t.\t0/1\n"
        )

        success, exc_id = dao.validate_order_for_submission(
            session_admin,
            order_id,
            patient_id,
            sample_id,
            str(vcf_file),
            "GRCh38",
            "testing",
            "system",
        )

        assert success is False
        assert exc_id is not None

        # Verify exception was created
        exceptions = dao.get_exceptions_for_order(session_admin, order_id)
        assert len(exceptions) == 1
        exc = exceptions[0]
        assert exc.reason_code == ExceptionReasonCode.QC_FAILED.value
        assert exc.category == ExceptionCategory.VALIDATION_FAILURE.value

    def test_missing_order_indication_creates_exception(
        self, dao, session_admin, patient_and_order, sample_with_qc, test_catalogue, tmp_path, conn
    ):
        """When order has no clinical indication, orchestration creates exception."""
        patient_id, _ = patient_and_order
        sample_id = sample_with_qc

        # Create order without clinical_indication
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_catalogue,
            required_scope="testing",
            consent_id=uuid.uuid4(),
            priority="routine",
            clinical_indication=None,  # Missing indication
        )

        # Create a valid VCF file
        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text(
            "##fileformat=VCFv4.2\n"
            "##assembly=GRCh38\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tsample1\n"
            "chr1\t1000\t.\tA\tT\t30\tPASS\t.\t0/1\n"
        )

        success, exc_id = dao.validate_order_for_submission(
            session_admin,
            order_id,
            patient_id,
            sample_id,
            str(vcf_file),
            "GRCh38",
            "testing",
            "system",
        )

        assert success is False
        assert exc_id is not None

        # Verify exception was created for indication_missing
        exceptions = dao.get_exceptions_for_order(session_admin, order_id)
        assert len(exceptions) == 1
        exc = exceptions[0]
        assert exc.reason_code == ExceptionReasonCode.INDICATION_MISSING.value
