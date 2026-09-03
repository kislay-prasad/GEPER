"""
Clinical domain tests — Phase 3.

Patients, consents, orders, samples, tests.
"""

import datetime
import json
import os
import uuid

import pytest

from clinical.data_access import DataAccess, BcryptHasher, SystemClock

# Use fixtures from test_identity via parametrization or copy essentials here
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


class TestConsentRecording:
    """Consent recording and management."""

    def test_record_consent_basic(self, dao, session_admin):
        """Record a consent for a patient."""
        patient_id = dao.create_patient(session_admin, "Test", datetime.date(1990, 1, 1), "M")
        consent_id = dao.record_consent(session_admin, patient_id, "testing")
        assert isinstance(consent_id, uuid.UUID)

    def test_record_consent_supersession(self, dao, session_admin):
        """Recording a new consent can supersede an old one."""
        patient_id = dao.create_patient(session_admin, "Test", datetime.date(1990, 1, 1), "M")
        consent1 = dao.record_consent(session_admin, patient_id, "testing")
        consent2 = dao.record_consent(session_admin, patient_id, "testing", supersedes_id=consent1)

        # Verify the chain: consent1 points to consent2 as its supersession
        original = dao.get_consent(session_admin, consent1)
        assert original["superseded_by"] == consent2

    def test_record_consent_audit(self, dao, session_admin, conn):
        """Consent recording is audited."""
        patient_id = dao.create_patient(session_admin, "Test", datetime.date(1990, 1, 1), "M")
        dao.record_consent(session_admin, patient_id, "research")

        cur = conn.cursor()
        cur.execute(
            "SELECT action, resource_type, outcome FROM audit_log WHERE action = 'consent_recorded' AND org_id = %s",
            (session_admin.org_id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "consent_recorded"
        assert row[1] == "consent"
        assert row[2] == "success"
        cur.close()


class TestPatientCreation:
    """Patient record creation."""

    def test_create_patient_basic(self, dao, session_admin):
        """Create a patient with name, DOB, and sex."""
        patient_id = dao.create_patient(
            session_admin,
            name="Alice Test",
            dob=datetime.date(1990, 5, 15),
            sex="F",
        )
        assert isinstance(patient_id, uuid.UUID)

    def test_create_patient_sex_validation(self, dao, session_admin):
        """Sex must be one of M/F/O/U."""
        with pytest.raises(ValueError, match="Sex must be one of"):
            dao.create_patient(
                session_admin,
                name="Bob",
                dob=datetime.date(1985, 1, 1),
                sex="X",
            )

    def test_create_patient_audit_entry(self, dao, session_admin, conn):
        """Patient creation is audited."""
        dao.create_patient(
            session_admin,
            name="Charlie",
            dob=datetime.date(1992, 3, 20),
            sex="M",
        )

        # Verify audit entry exists
        cur = conn.cursor()
        cur.execute(
            "SELECT action, resource_type, outcome, details FROM audit_log "
            "WHERE action = 'patient_created' AND org_id = %s",
            (session_admin.org_id,),
        )
        row = cur.fetchone()
        assert row is not None
        action, resource_type, outcome, details = row
        assert action == "patient_created"
        assert resource_type == "patient"
        assert outcome == "success"
        assert details["name"] == "Charlie"
        assert details["sex"] == "M"
        cur.close()


@pytest.fixture
def test_catalogue(dao, session_admin, conn):
    """Create a test in the catalogue."""
    test_id = uuid.uuid4()
    now = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, status, created_at) VALUES (%s, %s, %s, %s, %s)",
            (test_id, session_admin.org_id, "Test Gene Panel", "active", now),
        )
    conn.commit()
    return test_id


@pytest.fixture
def patient_with_consent(dao, session_admin, test_catalogue):
    """Create a patient with active consent."""
    patient_id = dao.create_patient(session_admin, "Patient One", datetime.date(1985, 6, 15), "F")
    consent_id = dao.record_consent(session_admin, patient_id, "testing")
    return patient_id, consent_id, test_catalogue


class TestOrderManagement:
    """Order creation and management."""

    def test_create_order_basic(self, dao, session_admin, patient_with_consent):
        """Create an order for a patient."""
        patient_id, consent_id, test_id = patient_with_consent
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
            priority="routine",
        )
        assert isinstance(order_id, uuid.UUID)

    def test_create_order_missing_patient(self, dao, session_admin, patient_with_consent):
        """Cannot create order for nonexistent patient."""
        _, consent_id, test_id = patient_with_consent
        fake_patient = uuid.uuid4()
        with pytest.raises(Exception, match="not found"):
            dao.create_order(
                session_admin,
                patient_id=fake_patient,
                test_id=test_id,
                required_scope="testing",
                consent_id=consent_id,
            )

    def test_create_order_missing_test(self, dao, session_admin, patient_with_consent):
        """Cannot create order for nonexistent test."""
        patient_id, consent_id, _ = patient_with_consent
        fake_test = uuid.uuid4()
        with pytest.raises(Exception, match="not found"):
            dao.create_order(
                session_admin,
                patient_id=patient_id,
                test_id=fake_test,
                required_scope="testing",
                consent_id=consent_id,
            )

    def test_create_order_missing_consent(self, dao, session_admin, patient_with_consent):
        """Cannot create order without valid consent."""
        patient_id, _, test_id = patient_with_consent
        fake_consent = uuid.uuid4()
        with pytest.raises(Exception, match="not found"):
            dao.create_order(
                session_admin,
                patient_id=patient_id,
                test_id=test_id,
                required_scope="testing",
                consent_id=fake_consent,
            )

    def test_create_order_scope_mismatch(self, dao, session_admin, patient_with_consent):
        """Order requires scope must match consent scope."""
        patient_id, consent_id, test_id = patient_with_consent
        with pytest.raises(ValueError, match="scope"):
            dao.create_order(
                session_admin,
                patient_id=patient_id,
                test_id=test_id,
                required_scope="research",  # Mismatch
                consent_id=consent_id,
            )

    def test_create_order_invalid_priority(self, dao, session_admin, patient_with_consent):
        """Priority must be routine or urgent."""
        patient_id, consent_id, test_id = patient_with_consent
        with pytest.raises(ValueError, match="Priority"):
            dao.create_order(
                session_admin,
                patient_id=patient_id,
                test_id=test_id,
                required_scope="testing",
                consent_id=consent_id,
                priority="STAT",
            )

    def test_place_order_basic(self, dao, session_admin, patient_with_consent):
        """Place an order, transitioning from draft to placed."""
        patient_id, consent_id, test_id = patient_with_consent
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
        )
        dao.place_order(session_admin, order_id)

        order = dao.get_order(session_admin, order_id)
        assert order["state"] == "placed"
        assert order["placed_at"] is not None

    def test_place_order_not_draft(self, dao, session_admin, patient_with_consent):
        """Cannot place an order that is not draft."""
        patient_id, consent_id, test_id = patient_with_consent
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
        )
        dao.place_order(session_admin, order_id)

        with pytest.raises(ValueError, match="cannot place non-draft"):
            dao.place_order(session_admin, order_id)

    def test_cancel_order_basic(self, dao, session_admin, patient_with_consent):
        """Cancel an order."""
        patient_id, consent_id, test_id = patient_with_consent
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
        )
        dao.cancel_order(session_admin, order_id)

        order = dao.get_order(session_admin, order_id)
        assert order["state"] == "cancelled"
        assert order["cancelled_at"] is not None

    def test_cancel_order_placed(self, dao, session_admin, patient_with_consent):
        """Can cancel a placed order."""
        patient_id, consent_id, test_id = patient_with_consent
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
        )
        dao.place_order(session_admin, order_id)
        dao.cancel_order(session_admin, order_id)

        order = dao.get_order(session_admin, order_id)
        assert order["state"] == "cancelled"

    def test_cancel_order_reported_fails(self, dao, session_admin, patient_with_consent, conn):
        """Cannot cancel a reported order."""
        patient_id, consent_id, test_id = patient_with_consent
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
        )

        # Manually update to reported state
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET state = %s WHERE order_id = %s AND org_id = %s",
                ("reported", order_id, session_admin.org_id),
            )
        conn.commit()

        with pytest.raises(ValueError, match="Cannot cancel"):
            dao.cancel_order(session_admin, order_id)

    def test_order_audit_created(self, dao, session_admin, patient_with_consent, conn):
        """Order creation is audited."""
        patient_id, consent_id, test_id = patient_with_consent
        dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
        )

        cur = conn.cursor()
        cur.execute(
            "SELECT action FROM audit_log WHERE action = %s AND org_id = %s",
            ("order_created", session_admin.org_id),
        )
        row = cur.fetchone()
        assert row is not None
        cur.close()

    def test_order_audit_placed(self, dao, session_admin, patient_with_consent, conn):
        """Order placement is audited."""
        patient_id, consent_id, test_id = patient_with_consent
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
        )
        dao.place_order(session_admin, order_id)

        cur = conn.cursor()
        cur.execute(
            "SELECT action FROM audit_log WHERE action = %s AND org_id = %s",
            ("order_placed", session_admin.org_id),
        )
        row = cur.fetchone()
        assert row is not None
        cur.close()


class TestSampleManagement:
    """Sample receiving and QC recording."""

    def test_receive_sample_basic(self, dao, session_admin, patient_with_consent):
        """Receive a sample for an order."""
        patient_id, consent_id, test_id = patient_with_consent
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
        )
        dao.place_order(session_admin, order_id)

        sample_id = dao.receive_sample(
            session_admin,
            order_id=order_id,
            sample_type="blood",
        )
        assert isinstance(sample_id, uuid.UUID)

    def test_receive_sample_invalid_type(self, dao, session_admin, patient_with_consent):
        """Sample type must be valid."""
        patient_id, consent_id, test_id = patient_with_consent
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
        )
        dao.place_order(session_admin, order_id)

        with pytest.raises(ValueError, match="Sample type"):
            dao.receive_sample(
                session_admin,
                order_id=order_id,
                sample_type="urine",
            )

    def test_receive_sample_not_placed(self, dao, session_admin, patient_with_consent):
        """Cannot receive sample for draft order."""
        patient_id, consent_id, test_id = patient_with_consent
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
        )

        with pytest.raises(ValueError, match="state"):
            dao.receive_sample(
                session_admin,
                order_id=order_id,
                sample_type="blood",
            )

    def test_record_qc_pass(self, dao, session_admin, patient_with_consent):
        """Record QC pass for a sample."""
        patient_id, consent_id, test_id = patient_with_consent
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
        )
        dao.place_order(session_admin, order_id)
        sample_id = dao.receive_sample(
            session_admin,
            order_id=order_id,
            sample_type="blood",
        )

        dao.record_qc(session_admin, sample_id=sample_id, qc_status="passed")

        sample = dao.get_sample(session_admin, sample_id)
        assert sample["qc_status"] == "passed"

    def test_record_qc_fail(self, dao, session_admin, patient_with_consent):
        """Record QC fail for a sample."""
        patient_id, consent_id, test_id = patient_with_consent
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
        )
        dao.place_order(session_admin, order_id)
        sample_id = dao.receive_sample(
            session_admin,
            order_id=order_id,
            sample_type="blood",
        )

        dao.record_qc(
            session_admin,
            sample_id=sample_id,
            qc_status="failed",
            qc_reason="Hemolysis detected",
        )

        sample = dao.get_sample(session_admin, sample_id)
        assert sample["qc_status"] == "failed"
        assert sample["qc_reason"] == "Hemolysis detected"

    def test_record_qc_invalid_status(self, dao, session_admin, patient_with_consent):
        """QC status must be passed or failed."""
        patient_id, consent_id, test_id = patient_with_consent
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
        )
        dao.place_order(session_admin, order_id)
        sample_id = dao.receive_sample(
            session_admin,
            order_id=order_id,
            sample_type="blood",
        )

        with pytest.raises(ValueError, match="QC status"):
            dao.record_qc(
                session_admin,
                sample_id=sample_id,
                qc_status="pending",
            )


class TestConsentWithdrawal:
    """Consent withdrawal."""

    def test_withdraw_consent_basic(self, dao, session_admin):
        """Withdraw an active consent."""
        patient_id = dao.create_patient(session_admin, "Patient", datetime.date(1988, 3, 10), "M")
        consent_id = dao.record_consent(session_admin, patient_id, "testing")

        dao.withdraw_consent(session_admin, consent_id)

        consent = dao.get_consent(session_admin, consent_id)
        assert consent["withdrawn_at"] is not None
        assert consent["withdrawn_by"] is not None

    def test_withdraw_consent_missing(self, dao, session_admin):
        """Cannot withdraw nonexistent consent."""
        fake_consent = uuid.uuid4()
        with pytest.raises(Exception, match="not found"):
            dao.withdraw_consent(session_admin, fake_consent)

    def test_withdraw_consent_audit(self, dao, session_admin, conn):
        """Consent withdrawal is audited."""
        patient_id = dao.create_patient(session_admin, "Patient", datetime.date(1988, 3, 10), "M")
        consent_id = dao.record_consent(session_admin, patient_id, "testing")

        dao.withdraw_consent(session_admin, consent_id)

        cur = conn.cursor()
        cur.execute(
            "SELECT action FROM audit_log WHERE action = %s AND org_id = %s",
            ("consent_withdrawn", session_admin.org_id),
        )
        row = cur.fetchone()
        assert row is not None
        cur.close()


class TestReadMethods:
    """Read-only methods for retrieval."""

    def test_get_patient_basic(self, dao, session_admin):
        """Retrieve a patient record."""
        patient_id = dao.create_patient(
            session_admin,
            name="David",
            dob=datetime.date(1995, 12, 25),
            sex="M",
        )

        patient = dao.get_patient(session_admin, patient_id)
        assert patient is not None
        assert patient["patient_id"] == patient_id
        assert patient["name"] == "David"
        assert patient["dob"] == datetime.date(1995, 12, 25)
        assert patient["sex"] == "M"

    def test_get_patient_missing(self, dao, session_admin):
        """Nonexistent patient returns None."""
        fake_patient = uuid.uuid4()
        patient = dao.get_patient(session_admin, fake_patient)
        assert patient is None

    def test_get_order_basic(self, dao, session_admin, patient_with_consent):
        """Retrieve an order record."""
        patient_id, consent_id, test_id = patient_with_consent
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
            priority="urgent",
            clinical_indication="Suspected genetic disorder",
        )

        order = dao.get_order(session_admin, order_id)
        assert order is not None
        assert order["order_id"] == order_id
        assert order["patient_id"] == patient_id
        assert order["test_id"] == test_id
        assert order["priority"] == "urgent"
        assert order["clinical_indication"] == "Suspected genetic disorder"
        assert order["state"] == "draft"

    def test_get_order_missing(self, dao, session_admin):
        """Nonexistent order returns None."""
        fake_order = uuid.uuid4()
        order = dao.get_order(session_admin, fake_order)
        assert order is None

    def test_get_consent_basic(self, dao, session_admin):
        """Retrieve a consent record."""
        patient_id = dao.create_patient(session_admin, "Patient", datetime.date(1988, 3, 10), "M")
        consent_id = dao.record_consent(session_admin, patient_id, "research")

        consent = dao.get_consent(session_admin, consent_id)
        assert consent is not None
        assert consent["consent_id"] == consent_id
        assert consent["patient_id"] == patient_id
        assert consent["scope"] == "research"
        assert consent["withdrawn_at"] is None

    def test_get_consent_missing(self, dao, session_admin):
        """Nonexistent consent returns None."""
        fake_consent = uuid.uuid4()
        consent = dao.get_consent(session_admin, fake_consent)
        assert consent is None

    def test_get_sample_basic(self, dao, session_admin, patient_with_consent):
        """Retrieve a sample record."""
        patient_id, consent_id, test_id = patient_with_consent
        order_id = dao.create_order(
            session_admin,
            patient_id=patient_id,
            test_id=test_id,
            required_scope="testing",
            consent_id=consent_id,
        )
        dao.place_order(session_admin, order_id)
        sample_id = dao.receive_sample(
            session_admin,
            order_id=order_id,
            sample_type="saliva",
            condition_on_receipt="Good condition",
        )

        sample = dao.get_sample(session_admin, sample_id)
        assert sample is not None
        assert sample["sample_id"] == sample_id
        assert sample["order_id"] == order_id
        assert sample["type"] == "saliva"
        assert sample["condition_on_receipt"] == "Good condition"
        assert sample["qc_status"] == "pending"

    def test_get_sample_missing(self, dao, session_admin):
        """Nonexistent sample returns None."""
        fake_sample = uuid.uuid4()
        sample = dao.get_sample(session_admin, fake_sample)
        assert sample is None


# ─── Phase 4a: Lineage foundation tests ───────────────────────────────────────


@pytest.fixture
def sample_for_lineage(dao, session_admin, patient_with_consent):
    """Create a complete lineage chain up to a sample for testing."""
    patient_id, consent_id, test_id = patient_with_consent
    order_id = dao.create_order(
        session_admin,
        patient_id=patient_id,
        test_id=test_id,
        required_scope="testing",
        consent_id=consent_id,
    )
    dao.place_order(session_admin, order_id)
    sample_id = dao.receive_sample(
        session_admin,
        order_id=order_id,
        sample_type="dna",
    )
    return sample_id


class TestSequencingRunCreation:
    """Sequencing run creation tests."""

    def test_create_sequencing_run_basic(self, dao, session_admin, sample_for_lineage):
        """Create a sequencing run for a sample."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)
        assert isinstance(run_id, uuid.UUID)

    def test_create_sequencing_run_invalid_sample(self, dao, session_admin):
        """Creating a run for nonexistent sample raises NotFoundError."""
        fake_sample = uuid.uuid4()
        from clinical.data_access import NotFoundError

        with pytest.raises(NotFoundError):
            dao.create_sequencing_run(session_admin, fake_sample)

    def test_create_sequencing_run_audit(self, dao, session_admin, sample_for_lineage, conn):
        """Sequencing run creation is audited."""
        sample_id = sample_for_lineage
        dao.create_sequencing_run(session_admin, sample_id)

        cur = conn.cursor()
        cur.execute(
            "SELECT action, resource_type, outcome FROM audit_log "
            "WHERE action = 'sequencing_run_created' AND org_id = %s",
            (session_admin.org_id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "sequencing_run_created"
        assert row[1] == "sequencing_run"
        assert row[2] == "success"
        cur.close()


class TestVcfCreation:
    """VCF (variant call format) creation tests."""

    def test_create_vcf_basic(self, dao, session_admin, sample_for_lineage, tmp_path):
        """Create a VCF record with a real temp file."""

        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        # Create a temporary VCF file
        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\n")
        vcf_path = str(vcf_file)

        vcf_id = dao.create_vcf(session_admin, run_id, vcf_path)
        assert isinstance(vcf_id, uuid.UUID)

        # Verify hash was computed
        vcf = dao.get_vcf(session_admin, vcf_id)
        assert vcf is not None
        assert len(vcf["content_hash"]) == 64  # SHA-256 hex is 64 chars

    def test_create_vcf_file_not_found(self, dao, session_admin, sample_for_lineage):
        """Creating a VCF for nonexistent file raises ValueError."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        with pytest.raises(ValueError, match="VCF file not found"):
            dao.create_vcf(session_admin, run_id, "/nonexistent/path/file.vcf")

    def test_create_vcf_invalid_sequencing_run(self, dao, session_admin, tmp_path):
        """Creating a VCF for nonexistent sequencing run raises error."""
        # Create a temp VCF file
        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\n")

        invalid_run_id = uuid.uuid4()
        with pytest.raises(Exception):  # NotFoundError or similar
            dao.create_vcf(session_admin, invalid_run_id, str(vcf_file))

    def test_create_vcf_hash_computed(self, dao, session_admin, sample_for_lineage, tmp_path):
        """VCF hash is computed correctly and matches file content."""
        import hashlib

        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        # Create a temp VCF file with known content (use binary mode to avoid line-ending conversion)
        vcf_file = tmp_path / "test_hash.vcf"
        content = b"##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\n"
        vcf_file.write_bytes(content)

        # Compute expected hash
        expected_hash = hashlib.sha256(content).hexdigest()

        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))
        vcf = dao.get_vcf(session_admin, vcf_id)
        assert vcf["content_hash"] == expected_hash

    def test_create_vcf_audit(self, dao, session_admin, sample_for_lineage, tmp_path, conn):
        """VCF creation is audited."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test_audit.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")

        dao.create_vcf(session_admin, run_id, str(vcf_file))

        cur = conn.cursor()
        cur.execute(
            "SELECT action, resource_type, outcome FROM audit_log WHERE action = 'vcf_created' AND org_id = %s",
            (session_admin.org_id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "vcf_created"
        assert row[1] == "vcf"
        assert row[2] == "success"
        cur.close()


class TestInterpretationCreation:
    """Interpretation creation tests."""

    def test_create_interpretation_basic(self, dao, session_admin, sample_for_lineage, tmp_path):
        """Create an interpretation for a VCF."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test_interp.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        run_document = {
            "model_version": "1.0",
            "database_version": "2024.01",
            "source_health": True,
            "run_complete": True,
        }

        interp_id = dao.create_interpretation(
            session_admin,
            vcf_id,
            run_document,
            str(sample_id),
        )
        assert isinstance(interp_id, uuid.UUID)

    def test_create_interpretation_invalid_vcf(self, dao, session_admin):
        """Creating interpretation for nonexistent VCF raises NotFoundError."""
        from clinical.data_access import NotFoundError

        fake_vcf = uuid.uuid4()
        run_document = {"model_version": "1.0"}

        with pytest.raises(NotFoundError):
            dao.create_interpretation(
                session_admin,
                fake_vcf,
                run_document,
                "sample123",
            )

    def test_create_interpretation_sample_mismatch(self, dao, session_admin, sample_for_lineage, tmp_path):
        """Mismatched platform_sample_id raises ValueError."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test_mismatch.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        run_document = {"model_version": "1.0"}
        wrong_sample_id = str(uuid.uuid4())  # Different UUID

        with pytest.raises(ValueError, match="Platform sample ID mismatch"):
            dao.create_interpretation(
                session_admin,
                vcf_id,
                run_document,
                wrong_sample_id,
            )

    def test_create_interpretation_submission_key_unique(self, dao, session_admin, sample_for_lineage, tmp_path, conn):
        """Duplicate submission_key raises database constraint error."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test_dup.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        run_document = {"model_version": "1.0"}

        # Create first interpretation
        interp_id_1 = dao.create_interpretation(
            session_admin,
            vcf_id,
            run_document,
            str(sample_id),
        )
        assert isinstance(interp_id_1, uuid.UUID)

        # Try to create a second interpretation with same submission_key
        # This should raise a database constraint error (unique violation)
        import psycopg

        with pytest.raises(psycopg.IntegrityError):
            dao.create_interpretation(
                session_admin,
                vcf_id,
                run_document,
                str(sample_id),
            )

    def test_create_interpretation_audit(self, dao, session_admin, sample_for_lineage, tmp_path, conn):
        """Interpretation creation is audited."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test_interp_audit.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        run_document = {"model_version": "1.0"}
        dao.create_interpretation(
            session_admin,
            vcf_id,
            run_document,
            str(sample_id),
        )

        cur = conn.cursor()
        cur.execute(
            "SELECT action, resource_type, outcome FROM audit_log "
            "WHERE action = 'interpretation_created' AND org_id = %s",
            (session_admin.org_id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "interpretation_created"
        assert row[1] == "interpretation"
        assert row[2] == "success"
        cur.close()


class TestReportCreation:
    """Report creation tests."""

    def test_create_report_basic(self, dao, session_admin, sample_for_lineage, tmp_path):
        """Create a report linked to an interpretation."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test_report.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        run_document = {"model_version": "1.0"}
        interp_id = dao.create_interpretation(
            session_admin,
            vcf_id,
            run_document,
            str(sample_id),
        )

        report_id = dao.create_report(session_admin, interp_id)
        assert isinstance(report_id, uuid.UUID)

    def test_create_report_invalid_interpretation(self, dao, session_admin):
        """Creating report for nonexistent interpretation raises NotFoundError."""
        from clinical.data_access import NotFoundError

        fake_interp = uuid.uuid4()

        with pytest.raises(NotFoundError):
            dao.create_report(session_admin, fake_interp)

    def test_create_report_audit(self, dao, session_admin, sample_for_lineage, tmp_path, conn):
        """Report creation is audited."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test_report_audit.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        run_document = {"model_version": "1.0"}
        interp_id = dao.create_interpretation(
            session_admin,
            vcf_id,
            run_document,
            str(sample_id),
        )

        dao.create_report(session_admin, interp_id)

        cur = conn.cursor()
        cur.execute(
            "SELECT action, resource_type, outcome FROM audit_log WHERE action = 'report_created' AND org_id = %s",
            (session_admin.org_id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "report_created"
        assert row[1] == "report"
        assert row[2] == "success"
        cur.close()


class TestOrgIsolationLineage:
    """Organisation isolation for lineage tables."""

    def test_sequencing_run_cross_org_isolation(self, dao, session_admin, sample_for_lineage, org_a, conn):
        """Sequencing run query for another org returns nothing."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        # Create second org and session
        org_b = dao.create_organisation("Org B")
        user_id = dao.create_user(org_b, "admin@org-b.test", "password")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, 'Administrator', %s, %s)",
                (user_id, org_b, user_id, datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)),
            )
        conn.commit()
        session_b = dao.login("admin@org-b.test", org_b, "password")

        # Org B cannot read Org A's run
        result = dao.get_sequencing_run(session_b, run_id)
        assert result is None

    def test_vcf_cross_org_isolation(self, dao, session_admin, sample_for_lineage, org_a, tmp_path, conn):
        """VCF query for another org returns nothing."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test_isolation.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        # Create second org and session
        org_b = dao.create_organisation("Org B")
        user_id = dao.create_user(org_b, "admin@org-b.test", "password")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, 'Administrator', %s, %s)",
                (user_id, org_b, user_id, datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)),
            )
        conn.commit()
        session_b = dao.login("admin@org-b.test", org_b, "password")

        # Org B cannot read Org A's VCF
        result = dao.get_vcf(session_b, vcf_id)
        assert result is None

    def test_interpretation_cross_org_isolation(self, dao, session_admin, sample_for_lineage, org_a, tmp_path, conn):
        """Interpretation query for another org returns nothing."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test_iso_interp.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        run_document = {"model_version": "1.0"}
        interp_id = dao.create_interpretation(
            session_admin,
            vcf_id,
            run_document,
            str(sample_id),
        )

        # Create second org and session
        org_b = dao.create_organisation("Org B")
        user_id = dao.create_user(org_b, "admin@org-b.test", "password")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, 'Administrator', %s, %s)",
                (user_id, org_b, user_id, datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)),
            )
        conn.commit()
        session_b = dao.login("admin@org-b.test", org_b, "password")

        # Org B cannot read Org A's interpretation
        result = dao.get_interpretation(session_b, interp_id)
        assert result is None

    def test_report_cross_org_isolation(self, dao, session_admin, sample_for_lineage, org_a, tmp_path, conn):
        """Report query for another org returns nothing."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test_iso_report.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        run_document = {"model_version": "1.0"}
        interp_id = dao.create_interpretation(
            session_admin,
            vcf_id,
            run_document,
            str(sample_id),
        )

        report_id = dao.create_report(session_admin, interp_id)

        # Create second org and session
        org_b = dao.create_organisation("Org B")
        user_id = dao.create_user(org_b, "admin@org-b.test", "password")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, 'Administrator', %s, %s)",
                (user_id, org_b, user_id, datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)),
            )
        conn.commit()
        session_b = dao.login("admin@org-b.test", org_b, "password")

        # Org B cannot read Org A's report
        result = dao.get_report(session_b, report_id)
        assert result is None


class TestLineageReadMethods:
    """Read method tests for lineage (retrieval and missing records)."""

    def test_get_sequencing_run_missing(self, dao, session_admin):
        """Reading nonexistent run returns None."""
        fake_run = uuid.uuid4()
        result = dao.get_sequencing_run(session_admin, fake_run)
        assert result is None

    def test_get_vcf_missing(self, dao, session_admin):
        """Reading nonexistent VCF returns None."""
        fake_vcf = uuid.uuid4()
        result = dao.get_vcf(session_admin, fake_vcf)
        assert result is None

    def test_get_interpretation_missing(self, dao, session_admin):
        """Reading nonexistent interpretation returns None."""
        fake_interp = uuid.uuid4()
        result = dao.get_interpretation(session_admin, fake_interp)
        assert result is None

    def test_get_report_missing(self, dao, session_admin):
        """Reading nonexistent report returns None."""
        fake_report = uuid.uuid4()
        result = dao.get_report(session_admin, fake_report)
        assert result is None

    def test_get_sequencing_run_retrieves_all_fields(self, dao, session_admin, sample_for_lineage):
        """Sequencing run retrieval returns all fields."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        result = dao.get_sequencing_run(session_admin, run_id)
        assert result is not None
        assert result["id"] == run_id
        assert result["sample_id"] == sample_id
        assert result["created_by"] == session_admin.user_id
        assert result["created_at"] is not None

    def test_get_vcf_retrieves_all_fields(self, dao, session_admin, sample_for_lineage, tmp_path):
        """VCF retrieval returns all fields."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test_fields.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_path = str(vcf_file)
        vcf_id = dao.create_vcf(session_admin, run_id, vcf_path)

        result = dao.get_vcf(session_admin, vcf_id)
        assert result is not None
        assert result["id"] == vcf_id
        assert result["sequencing_run_id"] == run_id
        assert result["vcf_path"] == vcf_path
        assert len(result["content_hash"]) == 64
        assert result["created_by"] == session_admin.user_id
        assert result["created_at"] is not None

    def test_get_interpretation_retrieves_all_fields(self, dao, session_admin, sample_for_lineage, tmp_path):
        """Interpretation retrieval returns all fields."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test_interp_fields.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        run_document = {
            "model_version": "1.0",
            "database_version": "2024.01",
        }
        interp_id = dao.create_interpretation(
            session_admin,
            vcf_id,
            run_document,
            str(sample_id),
        )

        result = dao.get_interpretation(session_admin, interp_id)
        assert result is not None
        assert result["id"] == interp_id
        assert result["vcf_id"] == vcf_id
        assert result["run_document"] == run_document
        assert "|" in result["submission_key"]  # hash|sample_id format
        assert result["created_by"] == session_admin.user_id
        assert result["created_at"] is not None

    def test_get_report_retrieves_all_fields(self, dao, session_admin, sample_for_lineage, tmp_path):
        """Report retrieval returns all fields."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test_report_fields.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        run_document = {"model_version": "1.0"}
        interp_id = dao.create_interpretation(
            session_admin,
            vcf_id,
            run_document,
            str(sample_id),
        )

        report_id = dao.create_report(session_admin, interp_id)

        result = dao.get_report(session_admin, report_id)
        assert result is not None
        assert result["id"] == report_id
        assert result["interpretation_id"] == interp_id
        assert result["created_by"] == session_admin.user_id
        assert result["created_at"] is not None


# ─── Phase 4b: Lineage query tests ──────────────────────────────────────────


class TestLineageQueries:
    """Tests for Phase 4b lineage query methods: trace_report_ancestors and find_reports_by_interpretation_criteria."""

    # ── trace_report_ancestors tests ─────────────────────────────────────────

    def test_trace_ancestors_complete_chain(self, dao, session_admin, sample_for_lineage, tmp_path):
        """Trace ancestors returns complete chain from report back to patient."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        run_document = {"model_version": "v8"}
        interp_id = dao.create_interpretation(session_admin, vcf_id, run_document, str(sample_id))

        report_id = dao.create_report(session_admin, interp_id)

        # Trace ancestors
        chain = dao.trace_report_ancestors(session_admin, report_id)

        # Verify chain length: report, interpretation, vcf, sequencing_run, sample, order, patient
        assert len(chain) == 7
        assert chain[0]["table"] == "report"
        assert chain[1]["table"] == "interpretation"
        assert chain[2]["table"] == "vcf"
        assert chain[3]["table"] == "sequencing_run"
        assert chain[4]["table"] == "sample"
        assert chain[5]["table"] == "order"
        assert chain[6]["table"] == "patient"

    def test_trace_ancestors_missing_report(self, dao, session_admin):
        """Trace ancestors raises NotFoundError for nonexistent report."""
        from clinical.data_access import NotFoundError

        fake_report = uuid.uuid4()
        with pytest.raises(NotFoundError):
            dao.trace_report_ancestors(session_admin, fake_report)

    def test_trace_ancestors_cross_org_not_found(self, dao, conn):
        """Trace ancestors does not see reports from other organisations."""
        from clinical.data_access import NotFoundError

        # Create two orgs
        org_a = dao.create_organisation("Org A")
        org_b = dao.create_organisation("Org B")

        # Create users and sessions for both orgs
        user_a = dao.create_user(org_a, "user_a@test.local", "pass")
        user_b = dao.create_user(org_b, "user_b@test.local", "pass")

        # Setup admin roles
        now = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, 'Administrator', %s, %s)",
                (user_a, org_a, user_a, now),
            )
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, 'Administrator', %s, %s)",
                (user_b, org_b, user_b, now),
            )
        conn.commit()

        session_a = dao.login("user_a@test.local", org_a, "pass")
        session_b = dao.login("user_b@test.local", org_b, "pass")

        # Create a full lineage in org_b
        patient_id = dao.create_patient(session_b, "Patient", datetime.date(1990, 1, 1), "M")
        consent_id = dao.record_consent(session_b, patient_id, "testing")

        test_id = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tests (test_id, org_id, name, status, created_at) VALUES (%s, %s, %s, %s, %s)",
                (test_id, org_b, "Test", "active", now),
            )
        conn.commit()

        order_id = dao.create_order(session_b, patient_id, test_id, "testing", consent_id)
        dao.place_order(session_b, order_id)
        sample_id = dao.receive_sample(session_b, order_id, "dna")
        run_id = dao.create_sequencing_run(session_b, sample_id)

        # Create a temporary VCF file for org_b
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".vcf", delete=False, mode="w") as f:
            f.write("##fileformat=VCFv4.2\n")
            vcf_path = f.name

        try:
            vcf_id = dao.create_vcf(session_b, run_id, vcf_path)
            interp_id = dao.create_interpretation(session_b, vcf_id, {"model_version": "v1"}, str(sample_id))
            report_id = dao.create_report(session_b, interp_id)

            # Try to trace from org_a's session — should not see org_b's report
            with pytest.raises(NotFoundError):
                dao.trace_report_ancestors(session_a, report_id)
        finally:
            import os

            os.unlink(vcf_path)

    def test_trace_ancestors_audit(self, dao, session_admin, sample_for_lineage, tmp_path, conn):
        """Trace ancestors is audited with chain_length and patient_id in details."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        run_document = {"model_version": "v8"}
        interp_id = dao.create_interpretation(session_admin, vcf_id, run_document, str(sample_id))
        report_id = dao.create_report(session_admin, interp_id)

        # Clear audit log to see only the trace call
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_log WHERE action = 'read_lineage'")
        conn.commit()

        chain = dao.trace_report_ancestors(session_admin, report_id)

        # Verify audit entry
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, resource_type, outcome, details FROM audit_log "
                "WHERE action = 'read_lineage' AND org_id = %s",
                (session_admin.org_id,),
            )
            row = cur.fetchone()

        assert row is not None
        action, resource_type, outcome, details_json = row
        assert action == "read_lineage"
        assert resource_type == "report"
        assert outcome == "success"

        details = json.loads(details_json) if isinstance(details_json, str) else details_json
        assert details["chain_length"] == 7
        assert "final_patient_id" in details
        assert details["final_patient_id"] == str(chain[-1]["resource_id"])

    # ── find_reports_by_interpretation_criteria tests ────────────────────────

    def test_find_empty_criteria_raises(self, dao, session_admin):
        """find_reports_by_interpretation_criteria raises ValueError for empty criteria."""
        with pytest.raises(ValueError, match="At least one criterion required"):
            dao.find_reports_by_interpretation_criteria(session_admin, {})

    def test_find_model_version_filter(self, dao, session_admin, sample_for_lineage, tmp_path):
        """Filter by model_version returns only matching interpretations."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\nchr1\t100\t.\tA\tT\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        # Create two interpretations with different model versions
        interp_v1_id = dao.create_interpretation(session_admin, vcf_id, {"model_version": "v1"}, str(sample_id))
        report_v1_id = dao.create_report(session_admin, interp_v1_id)

        # Create second VCF for second interpretation (unique content)
        vcf_file2 = tmp_path / "test2.vcf"
        vcf_file2.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\nchr1\t200\t.\tG\tC\n")
        vcf_id2 = dao.create_vcf(session_admin, run_id, str(vcf_file2))

        interp_v2_id = dao.create_interpretation(session_admin, vcf_id2, {"model_version": "v2"}, str(sample_id))
        dao.create_report(session_admin, interp_v2_id)

        # Query for v1 only
        results = dao.find_reports_by_interpretation_criteria(session_admin, {"model_version": "v1"})

        assert len(results) == 1
        assert results[0]["report_id"] == report_v1_id
        assert results[0]["run_document"]["model_version"] == "v1"

    def test_find_database_version_filter(self, dao, session_admin, sample_for_lineage, tmp_path):
        """Filter by database_version returns only matching interpretations."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\n1\t100\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        # Create interpretations with different database versions
        interp_db1_id = dao.create_interpretation(
            session_admin, vcf_id, {"database_version": "2024.01"}, str(sample_id)
        )
        dao.create_report(session_admin, interp_db1_id)

        vcf_file2 = tmp_path / "test2.vcf"
        vcf_file2.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\n1\t200\n")
        vcf_id2 = dao.create_vcf(session_admin, run_id, str(vcf_file2))

        interp_db2_id = dao.create_interpretation(
            session_admin, vcf_id2, {"database_version": "2025.01"}, str(sample_id)
        )
        dao.create_report(session_admin, interp_db2_id)

        # Query for 2024.01 only
        results = dao.find_reports_by_interpretation_criteria(session_admin, {"database_version": "2024.01"})

        assert len(results) == 1
        assert results[0]["run_document"]["database_version"] == "2024.01"

    def test_find_source_health_filter(self, dao, session_admin, sample_for_lineage, tmp_path):
        """Filter by source_health (service + status) returns matching interpretations."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        # Create interpretation with databases info including Ensembl status
        run_doc_health = {
            "model_version": "v1",
            "databases": {
                "Ensembl": "failed",
                "ClinVar": "available",
            },
        }
        interp_id = dao.create_interpretation(session_admin, vcf_id, run_doc_health, str(sample_id))
        report_id = dao.create_report(session_admin, interp_id)

        # Query for failed Ensembl
        results = dao.find_reports_by_interpretation_criteria(
            session_admin,
            {"source_health": {"service": "Ensembl", "status": "failed"}},
        )

        assert len(results) == 1
        assert results[0]["report_id"] == report_id
        assert results[0]["run_document"]["databases"]["Ensembl"] == "failed"

    def test_find_date_range_filter(self, dao, session_admin, sample_for_lineage, tmp_path):
        """Filter by date_range returns interpretations within the range."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        # Create an interpretation
        interp_id = dao.create_interpretation(session_admin, vcf_id, {"model_version": "v1"}, str(sample_id))
        report_id = dao.create_report(session_admin, interp_id)

        # Query with a date range that includes today
        results = dao.find_reports_by_interpretation_criteria(
            session_admin,
            {
                "date_range": {
                    "start": "2026-09-01",
                    "end": "2026-09-04",
                }
            },
        )

        assert len(results) >= 1
        found_report = next((r for r in results if r["report_id"] == report_id), None)
        assert found_report is not None

    def test_find_multiple_criteria_and_logic(self, dao, session_admin, sample_for_lineage, tmp_path):
        """Multiple criteria use AND logic: all must match."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        # Create interpretation matching both criteria
        run_doc = {
            "model_version": "v1",
            "database_version": "2024.01",
        }
        interp_both_id = dao.create_interpretation(session_admin, vcf_id, run_doc, str(sample_id))
        report_both_id = dao.create_report(session_admin, interp_both_id)

        # Create interpretation matching only model_version (unique VCF content)
        vcf_file2 = tmp_path / "test2.vcf"
        vcf_file2.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\nchr2\t300\n")
        vcf_id2 = dao.create_vcf(session_admin, run_id, str(vcf_file2))

        run_doc_partial = {"model_version": "v1", "database_version": "2025.01"}
        interp_partial_id = dao.create_interpretation(session_admin, vcf_id2, run_doc_partial, str(sample_id))
        dao.create_report(session_admin, interp_partial_id)

        # Query with both criteria
        results = dao.find_reports_by_interpretation_criteria(
            session_admin,
            {
                "model_version": "v1",
                "database_version": "2024.01",
            },
        )

        # Should get only the one matching both
        assert len(results) == 1
        assert results[0]["report_id"] == report_both_id

    def test_find_no_results(self, dao, session_admin, sample_for_lineage, tmp_path):
        """Query with criteria that match nothing returns empty list."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        # Create interpretation with v1
        interp_id = dao.create_interpretation(session_admin, vcf_id, {"model_version": "v1"}, str(sample_id))
        dao.create_report(session_admin, interp_id)

        # Query for v99 (doesn't exist)
        results = dao.find_reports_by_interpretation_criteria(session_admin, {"model_version": "v99"})

        assert results == []

    def test_find_returns_patient_id(self, dao, session_admin, sample_for_lineage, tmp_path):
        """Query results include patient_id via chain walk."""
        # sample_for_lineage is created via patient → order → sample
        sample_id = sample_for_lineage

        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        interp_id = dao.create_interpretation(session_admin, vcf_id, {"model_version": "v1"}, str(sample_id))
        report_id = dao.create_report(session_admin, interp_id)

        results = dao.find_reports_by_interpretation_criteria(session_admin, {"model_version": "v1"})

        assert len(results) == 1
        assert results[0]["report_id"] == report_id
        assert results[0]["sample_id"] == sample_id
        assert results[0]["patient_id"] is not None
        assert isinstance(results[0]["patient_id"], uuid.UUID)

    def test_find_audit(self, dao, session_admin, sample_for_lineage, tmp_path, conn):
        """Query is audited with criteria and result_count in details."""
        sample_id = sample_for_lineage
        run_id = dao.create_sequencing_run(session_admin, sample_id)

        vcf_file = tmp_path / "test.vcf"
        vcf_file.write_text("##fileformat=VCFv4.2\n")
        vcf_id = dao.create_vcf(session_admin, run_id, str(vcf_file))

        interp_id = dao.create_interpretation(session_admin, vcf_id, {"model_version": "v1"}, str(sample_id))
        dao.create_report(session_admin, interp_id)

        # Clear audit log to see only this query
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_log WHERE action = 'query_affected_reports'")
        conn.commit()

        dao.find_reports_by_interpretation_criteria(session_admin, {"model_version": "v1"})

        # Verify audit entry
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, resource_type, outcome, details FROM audit_log "
                "WHERE action = 'query_affected_reports' AND org_id = %s",
                (session_admin.org_id,),
            )
            row = cur.fetchone()

        assert row is not None
        action, resource_type, outcome, details_json = row
        assert action == "query_affected_reports"
        assert resource_type == "interpretation"
        assert outcome == "success"

        details = json.loads(details_json) if isinstance(details_json, str) else details_json
        assert "criteria" in details
        assert details["result_count"] == 1

    # ── Org isolation tests ──────────────────────────────────────────────────

    def test_lineage_queries_cross_org_isolation(self, dao, conn):
        """Lineage queries respect organisation boundaries."""
        from clinical.data_access import NotFoundError

        # Create two organisations
        org_a = dao.create_organisation("Org A")
        org_b = dao.create_organisation("Org B")

        # Create users and setup sessions
        user_a = dao.create_user(org_a, "user_a@test.local", "pass")
        user_b = dao.create_user(org_b, "user_b@test.local", "pass")

        now = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, 'Administrator', %s, %s)",
                (user_a, org_a, user_a, now),
            )
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, 'Administrator', %s, %s)",
                (user_b, org_b, user_b, now),
            )
        conn.commit()

        session_a = dao.login("user_a@test.local", org_a, "pass")
        session_b = dao.login("user_b@test.local", org_b, "pass")

        # Create full lineage in org_b
        patient_id_b = dao.create_patient(session_b, "Patient B", datetime.date(1990, 1, 1), "M")
        consent_id_b = dao.record_consent(session_b, patient_id_b, "testing")

        test_id = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tests (test_id, org_id, name, status, created_at) VALUES (%s, %s, %s, %s, %s)",
                (test_id, org_b, "Test", "active", now),
            )
        conn.commit()

        order_id_b = dao.create_order(session_b, patient_id_b, test_id, "testing", consent_id_b)
        dao.place_order(session_b, order_id_b)
        sample_id_b = dao.receive_sample(session_b, order_id_b, "dna")
        run_id_b = dao.create_sequencing_run(session_b, sample_id_b)

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".vcf", delete=False, mode="w") as f:
            f.write("##fileformat=VCFv4.2\n")
            vcf_path = f.name

        try:
            vcf_id_b = dao.create_vcf(session_b, run_id_b, vcf_path)
            interp_id_b = dao.create_interpretation(session_b, vcf_id_b, {"model_version": "v1"}, str(sample_id_b))
            report_id_b = dao.create_report(session_b, interp_id_b)

            # Try to query from org_a
            # trace_report_ancestors should not find org_b's report
            with pytest.raises(NotFoundError):
                dao.trace_report_ancestors(session_a, report_id_b)

            # find_reports_by_interpretation_criteria should return empty for org_a (no data in org_a)
            results = dao.find_reports_by_interpretation_criteria(session_a, {"model_version": "v1"})
            assert results == []

            # But org_b should see their own data
            results_b = dao.find_reports_by_interpretation_criteria(session_b, {"model_version": "v1"})
            assert len(results_b) == 1
            assert results_b[0]["report_id"] == report_id_b

        finally:
            import os

            os.unlink(vcf_path)
