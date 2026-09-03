"""
Clinical domain tests — Phase 3.

Patients, consents, orders, samples, tests.
"""

import datetime
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
