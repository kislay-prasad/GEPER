"""
clinical/tests/test_patient_identity.py
────────────────────────────────────────
w130 — the patient identity foundation.

WHAT THIS SUITE EXISTS TO CATCH, stated first because everything else is
secondary to it:

    A patient-facing read that binds the patient identifier from a CALLER
    ARGUMENT rather than from the authenticated session.

That version works. It passes a cross-organisation test, it passes a
"patient A sees their own data" test, it passes a not-found test, and it
passes a test that hands it the session's own patient id. The only test it
fails is one that hands it a DIFFERENT, REAL, SAME-ORGANISATION patient's id
and asserts the answer is about the SESSION's patient. That test is
`TestPatientIdentityComesFromTheSession` below and it is the reason this file
exists.

RUNNING THESE. Like clinical/tests/test_identity.py they need a real
PostgreSQL -- most of what they assert is enforced BY Postgres (the composite
foreign keys, the one-login-per-patient uniqueness, the termination CHECK).

    $env:CLINICAL_TEST_DSN="postgresql://...";  $env:CLINICAL_TEST_DB_DISPOSABLE="1"
    py -3.12 -m pytest clinical/tests/test_patient_identity.py

THE FOUR-CELL MATRIX this file is built around is NOT the tenancy matrix:

    right patient + right organisation  -> allowed
    wrong patient + right organisation  -> refused      <- the new axis
    right patient + wrong organisation  -> refused
    wrong patient + wrong organisation  -> refused
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import uuid

import pytest

from clinical.data_access import (
    GENERIC_AUTH_ERROR,
    MAX_FAILED_LOGIN_ATTEMPTS,
    ORG_SCOPED_TABLES,
    PATIENT_NOT_FOUND,
    AuthenticationError,
    AuthorizationError,
    BcryptHasher,
    DataAccess,
    NotFoundError,
    PatientAlreadyEnrolledError,
    PatientSession,
    Session,
    SessionExpired,
)

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"

requires_db = pytest.mark.skipif(
    not DSN, reason="CLINICAL_TEST_DSN not set -- database tests skipped (skip is not pass)"
)

PASSWORD = "a sufficiently long patient password"
OTHER_PASSWORD = "a different sufficiently long password"


class FakeClock:
    def __init__(self, start: dt.datetime | None = None) -> None:
        self._now = start or dt.datetime(2026, 9, 3, 9, 0, 0, tzinfo=dt.timezone.utc)

    def now(self) -> dt.datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now = self._now + dt.timedelta(**kwargs)


# ─── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def conn():
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")
    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute("DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
        cur.execute("DO $$ BEGIN CREATE ROLE clinical_retention; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def dao(conn, clock):
    return DataAccess(conn, clock=clock, password_hasher=BcryptHasher(rounds=4))


@pytest.fixture()
def org_a(dao):
    return dao.create_organisation("Org A")


@pytest.fixture()
def org_b(dao):
    return dao.create_organisation("Org B")


def _staff_session(dao, conn, org_id, email, *, administrator=True):
    """A staff principal with the roles it needs, inserted directly to break
    the bootstrap cycle exactly as test_identity.py's admin_session does."""
    user_id = dao.create_user(org_id, email, "correct horse battery staple")
    if administrator:
        with conn.cursor() as cur:
            for role in ("Administrator", "Orderer"):
                cur.execute(
                    "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (user_id, org_id, role, user_id, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)),
                )
    return dao.login(email, org_id, "correct horse battery staple")


def _place_order(dao, conn, staff_session, patient_id, scope="genomic-testing"):
    """A REAL order for a patient, placed through the real create_order.

    The `tests` row is inserted directly because DataAccess has no creator for
    it -- the same thing clinical/tests/test_clinical_domain.py does.
    """
    test_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                test_id,
                staff_session.org_id,
                f"Whole genome {test_id}",
                "GRCh38",
                "active",
                dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
            ),
        )
    consent_id = dao.record_consent(staff_session, patient_id, scope)
    return dao.create_order(staff_session, patient_id, test_id, scope, consent_id)


@pytest.fixture()
def staff_a(dao, conn, org_a):
    return _staff_session(dao, conn, org_a, "admin@a.example")


@pytest.fixture()
def staff_b(dao, conn, org_b):
    return _staff_session(dao, conn, org_b, "admin@b.example")


@pytest.fixture()
def patient_a(dao, staff_a):
    """Alice. The patient whose session every positive assertion is made from."""
    return dao.create_patient(staff_a, "Alice Anderson", dt.date(1980, 1, 1), "F")


@pytest.fixture()
def patient_a2(dao, staff_a):
    """Bob. A DIFFERENT, REAL patient in the SAME organisation as Alice.

    This fixture is the whole point of the suite. A cross-organisation victim
    proves nothing about the patient axis, because the org predicate already
    refuses it. Bob is refused only if the patient axis is real.
    """
    return dao.create_patient(staff_a, "Bob Brown", dt.date(1975, 6, 15), "M")


@pytest.fixture()
def patient_b(dao, staff_b):
    """Carol, in the OTHER organisation."""
    return dao.create_patient(staff_b, "Carol Clark", dt.date(1990, 3, 3), "F")


@pytest.fixture()
def alice_session(dao, staff_a, patient_a, org_a):
    """A REAL patient session, obtained through the REAL authentication path.

    Not hand-built. A PatientSession constructed in the test would prove
    nothing about whether patient_login puts the right patient on it, and the
    whole suite hangs off that being true (Standing Rule 2a).
    """
    dao.enrol_patient_login(staff_a, patient_a, "alice@portal.example", PASSWORD)
    return dao.patient_login("alice@portal.example", org_a, PASSWORD, ip_address="10.0.0.9")


@pytest.fixture()
def bob_session(dao, staff_a, patient_a2, org_a):
    """Bob's real session -- the same organisation as Alice, a different patient."""
    dao.enrol_patient_login(staff_a, patient_a2, "bob@portal.example", OTHER_PASSWORD)
    return dao.patient_login("bob@portal.example", org_a, OTHER_PASSWORD)


# ─── the schema: the two tables, and what Postgres refuses ───────────────────


@requires_db
class TestTheTwoTablesAreStructural:
    """
    The guarantees here are enforced by Postgres, not by Python, which is why
    each one is proved by attempting the write and watching it be refused
    rather than by reading the schema text.
    """

    def test_a_credential_cannot_name_a_patient_in_another_organisation(self, conn, org_a, patient_b, staff_a):
        """
        The composite FK, exercised. A patient_logins row in org_a naming
        Carol -- a patient who really exists, in org_b -- must be
        inexpressible, not merely unwritten.
        """
        psycopg = pytest.importorskip("psycopg")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO patient_logins (patient_login_id, patient_id, org_id, email, "
                    "password_hash, password_changed_at, enrolled_by, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        uuid.uuid4(),
                        patient_b,  # a real patient -- in the WRONG organisation
                        org_a,
                        "carol@a.example",
                        "$2b$04$notarealhash",
                        dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc),
                        staff_a.user_id,
                        dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc),
                        dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc),
                    ),
                )
        conn.rollback()

    def test_the_positive_twin_the_same_insert_for_the_right_organisation_succeeds(
        self, conn, org_a, patient_a, staff_a
    ):
        """
        Rule 4's positive twin. Without it, the refusal above is satisfied by
        an INSERT that is broken for some entirely different reason -- a typo
        in a column name refuses the row just as convincingly.
        """
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO patient_logins (patient_login_id, patient_id, org_id, email, "
                "password_hash, password_changed_at, enrolled_by, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    uuid.uuid4(),
                    patient_a,
                    org_a,
                    "alice@a.example",
                    "$2b$04$notarealhash",
                    dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc),
                    staff_a.user_id,
                    dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc),
                    dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc),
                ),
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM patient_logins WHERE org_id = %s AND patient_id = %s", (org_a, patient_a))
            assert cur.fetchone()[0] == "alice@a.example"

    def test_a_patient_cannot_hold_a_role_assignment(self, conn, org_a, patient_a, staff_a):
        """
        "Patients are not staff roles", proved structurally rather than
        asserted. role_assignments.user_id is a composite FK to `users`, so a
        patient_id in it is refused by the database. No eighth role value was
        added and none could help: the refusal is about the FK, not the CHECK.
        """
        psycopg = pytest.importorskip("psycopg")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                    "VALUES (%s, %s, 'Administrator', %s, %s)",
                    (patient_a, org_a, staff_a.user_id, dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc)),
                )
        conn.rollback()

    def test_a_patient_cannot_be_given_a_staff_session_row(self, conn, org_a, patient_a):
        """
        The other half of the principal separation, and the cheapest one to
        prove without ambiguity: `sessions` carries exactly one foreign key, so
        a refusal here can only be sessions_user_fk and cannot be some other
        constraint firing first.

        A patient id in `sessions` would be a patient holding a STAFF session,
        which is decision 8 inverted. The database refuses it.
        """
        psycopg = pytest.importorskip("psycopg")
        now = dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sessions (session_id, user_id, org_id, created_at, expires_at, "
                    "last_activity_at) VALUES (%s, %s, %s, %s, %s, %s)",
                    (uuid.uuid4(), patient_a, org_a, now, now + dt.timedelta(hours=24), now),
                )
        conn.rollback()

    def test_the_positive_twin_a_real_staff_id_in_sessions_is_accepted(self, conn, org_a, staff_a):
        """Rule 4's twin: the same INSERT with a staff principal succeeds, so
        the refusal above is about WHO, not about a malformed statement."""
        now = dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (session_id, user_id, org_id, created_at, expires_at, "
                "last_activity_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (uuid.uuid4(), staff_a.user_id, org_a, now, now + dt.timedelta(hours=24), now),
            )
        conn.commit()


@requires_db
class TestStaffEnrolment:
    """
    Decision 4: a staff principal provisions the portal identity and links it
    to exactly one patient record. There is no self-registration route, and
    the tests below exercise the real method rather than reading rows.
    """

    def test_a_staff_administrator_enrols_a_patient(self, dao, staff_a, patient_a, conn):
        login_id = dao.enrol_patient_login(staff_a, patient_a, "alice@portal.example", PASSWORD)
        assert isinstance(login_id, uuid.UUID)

        # Read back through SQL only to check the columns the METHOD is
        # responsible for filling; the behaviour itself is proved by the login
        # tests, which call patient_login().
        with conn.cursor() as cur:
            cur.execute(
                "SELECT patient_id, org_id, enrolled_by, password_hash FROM patient_logins WHERE patient_login_id = %s",
                (login_id,),
            )
            patient_id, org_id, enrolled_by, password_hash = cur.fetchone()
        assert patient_id == patient_a
        assert org_id == staff_a.org_id
        assert enrolled_by == staff_a.user_id, "the enroller is the acting staff principal, recorded on the row"
        assert password_hash.startswith("$2"), "the same bcrypt format users.password_hash holds"
        assert PASSWORD not in password_hash

    def test_enrolment_requires_the_administrator_role(self, dao, conn, org_a, patient_a):
        """
        A roleless staff session -- the ordinary state, since create_user
        assigns no role -- cannot issue a patient credential.
        """
        roleless = _staff_session(dao, conn, org_a, "nobody@a.example", administrator=False)
        with pytest.raises(AuthorizationError):
            dao.enrol_patient_login(roleless, patient_a, "alice@portal.example", PASSWORD)

    def test_the_positive_twin_the_same_call_with_the_role_succeeds(self, dao, staff_a, patient_a):
        """Rule 4: without this, the refusal above is satisfied by an enrolment
        that is broken for every caller."""
        assert dao.enrol_patient_login(staff_a, patient_a, "alice@portal.example", PASSWORD)

    def test_staff_cannot_enrol_a_patient_from_another_organisation(self, dao, staff_a, patient_b):
        """
        wrong patient + wrong organisation. patient_b is REAL -- it exists, in
        org_b -- and org_a's administrator still cannot reach it.
        """
        with pytest.raises(NotFoundError):
            dao.enrol_patient_login(staff_a, patient_b, "carol@portal.example", PASSWORD)

    def test_a_cross_org_patient_and_an_invented_one_refuse_identically(self, dao, staff_a, patient_b):
        """
        Standing Rule 2b applied at the method boundary: the MESSAGE, not the
        exception type. If a real-but-elsewhere patient refused differently
        from a nonexistent one, the difference confirms the record exists.
        """
        with pytest.raises(NotFoundError) as real_elsewhere:
            dao.enrol_patient_login(staff_a, patient_b, "carol@portal.example", PASSWORD)
        with pytest.raises(NotFoundError) as invented:
            dao.enrol_patient_login(staff_a, uuid.uuid4(), "nobody@portal.example", PASSWORD)

        assert str(real_elsewhere.value) == str(invented.value)
        assert str(real_elsewhere.value) == PATIENT_NOT_FOUND
        assert str(patient_b) not in str(real_elsewhere.value)

    def test_exactly_one_patient_record_per_credential(self, dao, staff_a, patient_a):
        """Decision 4: linked to EXACTLY ONE patient record. A second
        enrolment for the same patient is refused, not silently accepted as a
        second credential."""
        dao.enrol_patient_login(staff_a, patient_a, "alice@portal.example", PASSWORD)
        with pytest.raises(PatientAlreadyEnrolledError):
            dao.enrol_patient_login(staff_a, patient_a, "alice.again@portal.example", PASSWORD)

    def test_enrolment_is_atomic_a_failure_in_the_write_leaves_nothing(self, dao, staff_a, patient_a, patient_a2, conn):
        """
        THE TEST THE BRIEF ASKED FOR, and it is the one a composed
        implementation fails.

        @auditable commits after each audit row. So an enrolment assembled out
        of two decorated methods -- verify, then insert -- leaves the verify
        step's audit row committed when the insert fails, and the audit log
        then records a verification for an enrolment that never happened.

        The failure is forced at the LAST step: the email is already taken in
        this organisation, so every precondition passes and the INSERT is what
        breaks. Nothing may survive it: no credential row for Bob, and no
        audit row of any kind attributing enrolment work to Bob.
        """
        dao.enrol_patient_login(staff_a, patient_a, "taken@portal.example", PASSWORD)

        with pytest.raises(Exception):
            dao.enrol_patient_login(staff_a, patient_a2, "taken@portal.example", OTHER_PASSWORD)
        conn.rollback()

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM patient_logins WHERE patient_id = %s", (patient_a2,))
            assert cur.fetchone()[0] == 0, "a credential row survived a failed enrolment"

            # ENROLMENT rows only. `patient_created` is a legitimate success
            # row for Bob -- the fixture really did create him -- and matching
            # it would make this assertion fire for a reason that has nothing
            # to do with atomicity.
            cur.execute(
                "SELECT action, outcome, resource_id, details FROM audit_log "
                "WHERE outcome = 'success' AND action LIKE %s "
                "AND (resource_id = %s OR details::text LIKE %s)",
                ("%enrol%", str(patient_a2), f"%{patient_a2}%"),
            )
            survivors = cur.fetchall()
        assert not survivors, (
            "a SUCCESS-outcome audit row survived a failed enrolment, attributing work to a patient "
            f"who was never enrolled: {survivors}"
        )

    def test_the_positive_twin_a_successful_enrolment_does_leave_both(self, dao, staff_a, patient_a2, conn):
        """
        Rule 4's twin for the absence assertion above. An implementation that
        wrote NOTHING, ever, would satisfy the atomicity test perfectly. This
        one fails unless the successful path really does leave both the
        credential row and the success audit row.
        """
        login_id = dao.enrol_patient_login(staff_a, patient_a2, "bob@portal.example", PASSWORD)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM patient_logins WHERE patient_id = %s", (patient_a2,))
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT count(*) FROM audit_log WHERE outcome = 'success' AND action = 'patient_login_enrolled' "
                "AND resource_id = %s",
                (str(login_id),),
            )
            assert cur.fetchone()[0] == 1


@requires_db
class TestPatientAuthentication:
    """
    Decision 2: reuse the existing mechanism. These exercise the REAL
    patient_login, not a constructed session.
    """

    def test_a_patient_authenticates_and_the_session_names_their_patient_record(self, dao, alice_session, patient_a):
        assert isinstance(alice_session, PatientSession)
        assert alice_session.patient_id == patient_a
        assert not isinstance(alice_session, Session), "a PatientSession must not be a Session"
        assert not hasattr(alice_session, "user_id"), (
            "a PatientSession must carry no user_id: roughly thirty statements write session.user_id "
            "into a staff actor column, and the absent attribute is the runtime backstop"
        )

    def test_every_authentication_failure_is_the_same_sentence(self, dao, org_a, org_b, staff_a, patient_a):
        """
        Standing Rule 2b at the method boundary. Asserted on equality with the
        constant, not on the absence of a word: "Invalid account credentials"
        would pass an absence check while a genuinely distinguishing message
        could too.
        """
        dao.enrol_patient_login(staff_a, patient_a, "alice@portal.example", PASSWORD)

        messages = []
        for email, org, password in [
            ("alice@portal.example", org_a, "the wrong password entirely"),
            ("nobody@portal.example", org_a, PASSWORD),
            ("alice@portal.example", org_b, PASSWORD),
            ("alice@portal.example", uuid.uuid4(), PASSWORD),
        ]:
            with pytest.raises(AuthenticationError) as caught:
                dao.patient_login(email, org, password)
            messages.append(str(caught.value))

        assert messages == [GENERIC_AUTH_ERROR] * 4, messages

    def test_a_patient_credential_does_not_authenticate_in_another_organisation(
        self, dao, staff_a, patient_a, org_a, org_b
    ):
        """
        right patient + wrong organisation. The credential is real and the
        password is correct; only the organisation is wrong.
        """
        dao.enrol_patient_login(staff_a, patient_a, "alice@portal.example", PASSWORD)
        with pytest.raises(AuthenticationError):
            dao.patient_login("alice@portal.example", org_b, PASSWORD)

    def test_the_positive_twin_the_same_credential_in_its_own_organisation_works(self, dao, staff_a, patient_a, org_a):
        dao.enrol_patient_login(staff_a, patient_a, "alice@portal.example", PASSWORD)
        assert dao.patient_login("alice@portal.example", org_a, PASSWORD).patient_id == patient_a

    def test_a_patient_credential_is_not_a_staff_credential(self, dao, staff_a, patient_a, org_a):
        """
        The two principal tables are separate, so login() cannot find a
        patient and patient_login() cannot find a staff user. Proved in both
        directions, because one direction alone would be satisfied by an
        implementation that simply refuses everything.
        """
        dao.enrol_patient_login(staff_a, patient_a, "alice@portal.example", PASSWORD)

        with pytest.raises(AuthenticationError):
            dao.login("alice@portal.example", org_a, PASSWORD)
        with pytest.raises(AuthenticationError):
            dao.patient_login("admin@a.example", org_a, "correct horse battery staple")

    def test_the_staff_lockout_policy_applies_unchanged(self, dao, staff_a, patient_a, org_a, conn):
        """
        Decision 2: the existing lockout policy is reused, not re-derived.
        MAX_FAILED_LOGIN_ATTEMPTS failures lock the credential, and the
        CORRECT password is then refused -- which is what distinguishes a
        lockout from a simple wrong-password refusal.
        """
        dao.enrol_patient_login(staff_a, patient_a, "alice@portal.example", PASSWORD)
        for _ in range(MAX_FAILED_LOGIN_ATTEMPTS):
            with pytest.raises(AuthenticationError):
                dao.patient_login("alice@portal.example", org_a, "wrong")

        with pytest.raises(AuthenticationError):
            dao.patient_login("alice@portal.example", org_a, PASSWORD)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT failed_login_attempts, locked_until FROM patient_logins WHERE org_id = %s AND email = %s",
                (org_a, "alice@portal.example"),
            )
            attempts, locked_until = cur.fetchone()
        assert attempts >= MAX_FAILED_LOGIN_ATTEMPTS
        assert locked_until is not None

    def test_one_patients_failures_do_not_lock_another_patients_credential(
        self, dao, staff_a, patient_a, patient_a2, org_a
    ):
        """
        The counter is per credential. Same organisation, so this is the
        patient axis and not the tenancy one.
        """
        dao.enrol_patient_login(staff_a, patient_a, "alice@portal.example", PASSWORD)
        dao.enrol_patient_login(staff_a, patient_a2, "bob@portal.example", OTHER_PASSWORD)
        for _ in range(MAX_FAILED_LOGIN_ATTEMPTS):
            with pytest.raises(AuthenticationError):
                dao.patient_login("alice@portal.example", org_a, "wrong")

        assert dao.patient_login("bob@portal.example", org_a, OTHER_PASSWORD).patient_id == patient_a2


@requires_db
class TestPatientSessionPolicy:
    """The session policy is the staff policy, reused. Same windows, same
    inclusive idle boundary, same server-side termination."""

    def test_a_valid_session_validates_and_carries_the_patient(self, dao, alice_session, patient_a):
        revalidated = dao.patient_session_valid(alice_session.session_id)
        assert revalidated.patient_id == patient_a
        assert revalidated.org_id == alice_session.org_id

    def test_the_idle_window_expires_at_exactly_the_timeout(self, dao, clock, alice_session):
        clock.advance(minutes=30)
        with pytest.raises(SessionExpired):
            dao.patient_session_valid(alice_session.session_id)

    def test_the_positive_twin_one_minute_short_of_the_timeout_still_validates(self, dao, clock, alice_session):
        clock.advance(minutes=29)
        assert dao.patient_session_valid(alice_session.session_id).session_id == alice_session.session_id

    def test_activity_never_extends_the_absolute_cap(self, dao, clock, alice_session):
        # 29-minute steps keep the idle window alive indefinitely, so the only
        # thing that can end this loop is SESSION_ABSOLUTE_HOURS. 60 steps is
        # 29 hours, comfortably past the 24-hour cap; 48 steps (23.2 hours) is
        # not, and the first version of this test failed for that reason
        # rather than for a fault in the code.
        for _ in range(60):
            clock.advance(minutes=29)
            try:
                dao.patient_session_valid(alice_session.session_id)
            except SessionExpired:
                break
        else:
            pytest.fail("the absolute cap never fired -- activity extended it")

    def test_a_staff_session_id_is_not_a_patient_session_id(self, dao, staff_a):
        """
        The two id spaces are DIFFERENT TABLES, which is why _get_session did
        not need widening and must not be widened. A perfectly valid staff
        session id is simply not found here.
        """
        with pytest.raises(SessionExpired):
            dao.patient_session_valid(staff_a.session_id)

    def test_a_patient_session_id_is_not_a_staff_session_id(self, dao, alice_session):
        """The other direction. Decision 7's structural half: a patient session
        id presented to the staff validator finds nothing."""
        with pytest.raises(SessionExpired):
            dao.session_valid(alice_session.session_id)

    def test_the_positive_twin_each_id_validates_in_its_own_space(self, dao, staff_a, alice_session):
        """Without this, both refusals above are satisfied by a
        patient_session_valid that refuses every id ever presented to it."""
        assert dao.session_valid(staff_a.session_id).user_id == staff_a.user_id
        assert dao.patient_session_valid(alice_session.session_id).patient_id == alice_session.patient_id


@requires_db
class TestAPatientSessionIsRefusedByStaffOperations:
    """
    DECISION 7, and it is enforced rather than annotated.

    Python annotations are not checked at run time and a PatientSession
    carries org_id, so it duck-types into any staff method that touches only
    session.org_id -- which is most of the twenty-nine ungated readers. The
    refusal therefore lives in the @auditable decorator that every public
    DataAccess method already carries, and fires whether or not the method is
    audited.
    """

    def test_a_patient_session_cannot_read_a_patient_through_the_staff_reader(self, dao, alice_session, patient_a2):
        """
        get_patient is auditable=False and binds only session.org_id. It is
        the single most duck-typeable method in the file, and handing it a
        patient session must be refused rather than answered.

        Note the argument: BOB, not Alice. If this were refused only for
        another patient it would be the patient axis, not decision 7.
        """
        with pytest.raises(AuthorizationError):
            dao.get_patient(alice_session, patient_a2)

    def test_it_is_refused_for_the_patients_own_record_too(self, dao, alice_session, patient_a):
        """
        Decision 7 is about the OPERATION, not about the row. A staff method
        refuses a patient session even when the row being asked for is the
        session's own patient -- otherwise the refusal is really the patient
        axis wearing decision 7's name.
        """
        with pytest.raises(AuthorizationError):
            dao.get_patient(alice_session, patient_a)

    def test_the_positive_twin_a_staff_session_reads_the_same_record(self, dao, staff_a, patient_a):
        """Rule 4: without this, the refusals above are satisfied by a
        get_patient that refuses everybody."""
        assert dao.get_patient(staff_a, patient_a)["patient_id"] == patient_a

    def test_a_patient_session_cannot_perform_a_staff_write(self, dao, alice_session):
        with pytest.raises(AuthorizationError):
            dao.create_patient(alice_session, "Injected Patient", dt.date(2000, 1, 1), "U")

    def test_a_patient_session_cannot_enrol_another_patient(self, dao, alice_session, patient_a2):
        """The enrolment method itself is staff-only. A patient session reaching
        it would be a patient issuing credentials."""
        with pytest.raises(AuthorizationError):
            dao.enrol_patient_login(alice_session, patient_a2, "hijack@portal.example", PASSWORD)


def _my_record(dao, session, supplied_patient_id):
    """
    Ask for the session's patient record while SUPPLYING a patient id.

    Written to work against both implementations on purpose. The wrong version
    takes the id and binds it; the right version has no such parameter at all
    and raises TypeError, so the call falls back to the argument-free form.
    Either way the QUESTION is the same -- "can a supplied id change the
    answer?" -- and only one of the two implementations answers it correctly.
    """
    try:
        return dao.get_my_patient_record(session, supplied_patient_id)
    except TypeError:
        return dao.get_my_patient_record(session)


@requires_db
class TestPatientIdentityComesFromTheSession:
    """
    DECISION 6, AND THE REASON THIS WAVE EXISTS.

    THE TEST THAT CATCHES IT IS THE MOST EASILY FAKED TEST HERE. Passing a
    patient id that MATCHES the session proves nothing -- both the right and
    the wrong implementation return the right record for it. Passing a
    cross-ORGANISATION id proves nothing either -- the org predicate refuses
    that in both implementations too.

    It must pass a DIFFERENT, REAL, SAME-ORGANISATION patient's id, and assert
    that the answer is about the SESSION's patient. Bob is that patient: he is
    created by the same staff session in the same organisation as Alice, he has
    a real row, and he is reachable by any staff session in org_a.
    """

    def test_a_supplied_patient_id_cannot_override_the_session(self, dao, alice_session, patient_a, patient_a2, conn):
        """
        Alice's session, Bob's real id. The answer must be ALICE.

        The guards below make this test unfakeable by accident: Bob must
        really exist, must be in the same organisation, and must not be Alice.
        Without them a fixture change could quietly turn this into a test that
        passes the session's own id back to itself.
        """
        assert patient_a != patient_a2
        with conn.cursor() as cur:
            cur.execute("SELECT org_id, name FROM patients WHERE patient_id = %s", (patient_a2,))
            bob_org, bob_name = cur.fetchone()
        assert bob_org == alice_session.org_id, "Bob must be in the SAME organisation or this proves nothing"
        assert bob_name == "Bob Brown"

        record = _my_record(dao, alice_session, patient_a2)

        assert record["patient_id"] == patient_a, (
            "the supplied patient id overrode the session -- the patient is being bound from the "
            "caller's argument, which is the one failure this wave exists to prevent"
        )
        assert record["name"] == "Alice Anderson"

    def test_the_method_takes_no_patient_identifier_at_all(self, dao):
        """
        The structural form of the same rule, and the stronger one: if the
        parameter does not exist, the class of bug where it is passed through
        unvalidated cannot exist.

        Derived from the DECLARED signature rather than from a hand-written
        list of method names, so a portal method added later is covered the day
        it is written -- the same reasoning that made the org coverage guard
        derive its census from schema.sql.

        READ FROM THE AST, AND THAT IS NOT A STYLE CHOICE. The first version of
        this test used inspect.signature, and it PASSED AGAINST THE WRONG
        IMPLEMENTATION -- `auditable` does not apply functools.wraps, so its
        wrapper reports `(self, *args, **kwargs)` and every parameter name is
        invisible at run time. A cannot-fail test in exactly the shape the
        Phase 5d incident recorded. The AST reads what is written.
        """
        import ast

        source = (pathlib.Path(__file__).resolve().parent.parent / "data_access.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        portal = [
            node
            for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DataAccess"]
            for node in cls.body
            if isinstance(node, ast.FunctionDef)
            and (node.name.startswith("get_my_") or node.name.startswith("list_my_"))
        ]
        assert portal, "no portal methods found -- this guard would pass vacuously"

        offenders = []
        for node in portal:
            names = [a.arg for a in node.args.args] + [a.arg for a in node.args.kwonlyargs]
            leaked = [n for n in names if "patient" in n and n not in ("self", "patient_session")]
            if leaked:
                offenders.append(f"{node.name}{tuple(leaked)}")
        assert not offenders, (
            "patient-facing method(s) taking a patient identifier as an argument: "
            f"{offenders}. A patient-facing method must bind the patient from the session."
        )

    def test_bob_reaches_his_own_record_from_his_own_session(self, dao, bob_session, patient_a2):
        """
        Rule 4's positive twin, and it is load-bearing here. Without it, a
        get_my_patient_record that ALWAYS returned Alice -- or that always
        returned nothing -- would satisfy the test above perfectly.
        """
        assert _my_record(dao, bob_session, patient_a2)["patient_id"] == patient_a2

    def test_a_supplied_id_cannot_reach_across_organisations_either(self, dao, alice_session, patient_a, patient_b):
        """
        right patient + wrong organisation, and wrong patient + wrong
        organisation collapse to the same thing under the portal surface: the
        answer follows the session in both axes at once.
        """
        record = _my_record(dao, alice_session, patient_b)
        assert record["patient_id"] == patient_a

    def test_the_orders_list_follows_the_session_and_not_the_argument(
        self, dao, conn, staff_a, alice_session, bob_session, patient_a, patient_a2
    ):
        """
        The same rule on a JOINED read, with REAL orders on both sides.

        The patient axis downstream is only available by anchoring on
        `patients`: only four tables carry a patient_id column and `orders` is
        one of them, but samples, runs, VCFs, interpretations and reports are
        not.

        THE ASSERTION IS ON CONTENT, NOT ON A COUNT (Rule 4). Alice must see
        HER order id and must NOT see Bob's -- and Bob's must be a real order
        that a staff session in the same organisation really can see, which is
        what the last assertion checks. A count-only test is satisfied by a
        query that returns one wrong row.
        """
        alice_order = _place_order(dao, conn, staff_a, patient_a)
        bob_order = _place_order(dao, conn, staff_a, patient_a2)
        assert alice_order != bob_order

        def ids(session, supplied):
            try:
                rows = dao.list_my_test_orders(session, supplied)
            except TypeError:
                rows = dao.list_my_test_orders(session)
            return {r["order_id"] for r in rows}

        # Alice's session, asking with BOB'S id.
        alice_view = ids(alice_session, patient_a2)
        assert alice_order in alice_view
        assert bob_order not in alice_view, (
            "Bob's order reached Alice's portal view -- the supplied patient id overrode the session"
        )

        # The positive twin, twice over: Bob really does see his own order,
        # and a staff session really can see both, so "absent" above means
        # "scoped out" rather than "never written".
        bob_view = ids(bob_session, patient_a2)
        assert bob_view == {bob_order}
        assert dao.get_order(staff_a, bob_order)["order_id"] == bob_order

    def test_every_portal_statement_binds_the_patient_from_the_session(self):
        """
        THE DERIVED GUARD FOR THE NEW AXIS (the design's Q8 rule 4).

        A hand-written tuple of method names here would reproduce w129's GAP-3
        on the patient axis: the guard falls silent about the one method
        somebody forgot to name. So the surface is derived from the class, and
        every SQL statement a portal method issues must bind
        patient_session.patient_id.

        WIDENED IN w135, AND WIDENING IT MADE IT STRONGER RATHER THAN WEAKER.
        Until then every portal method carried its SQL as a literal in its own
        body, so walking the function was enough. w135's two report readers
        share ONE statement held in a class attribute -- deliberately, because
        two copies of a six-table join is two places for an org predicate to be
        dropped from -- and against that shape the original walk found NO SQL
        in either method and would have passed them both **vacuously**. It did
        not fail silently only because a docstring happened to contain the
        substring it scans for; that was luck, not coverage.

        So the guard now RESOLVES the class-level statements a portal method
        references, and asserts that every portal method yields at least one
        SQL statement. A future reader who moves a statement out of a method
        cannot thereby remove it from this check.

        Docstrings are excluded before scanning. They discuss SQL by nature --
        the very method that exposed this explains why a write grant on
        `reports.state` matters -- and a paragraph ABOUT a statement is not a
        statement.
        """
        import ast

        source = (pathlib.Path(__file__).resolve().parent.parent / "data_access.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DataAccess"]
        portal = [
            node
            for cls in classes
            for node in cls.body
            if isinstance(node, ast.FunctionDef)
            and (node.name.startswith("get_my_") or node.name.startswith("list_my_"))
        ]
        assert portal, "no portal methods found -- this guard would pass vacuously"

        # Class-level SQL held as a shared constant, by attribute name.
        shared = {}
        for cls in classes:
            for node in cls.body:
                if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Constant, ast.BinOp, ast.JoinedStr)):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            shared[target.id] = node.value

        def sql_strings(node):
            """Every SQL-looking string constant under `node`, docstrings excluded."""
            skip = set()
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    first = sub.body[0] if sub.body else None
                    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                        skip.add(id(first.value))
            return [
                " ".join(c.value.split())
                for c in ast.walk(node)
                if isinstance(c, ast.Constant)
                and isinstance(c.value, str)
                and id(c) not in skip
                and any(kw in c.value for kw in ("SELECT ", "UPDATE ", "INSERT INTO", "DELETE FROM"))
            ]

        offenders = []
        for node in portal:
            body = ast.unparse(node)
            statements = sql_strings(node)
            for name, value in shared.items():
                if name in body:
                    statements += sql_strings(value)
            if not statements:
                offenders.append(f"{node.name}: issues no SQL this guard can see -- it would pass vacuously")
            for sql in statements:
                if "patient_id" not in sql:
                    offenders.append(f"{node.name}: statement does not mention patient_id: {sql}")
            if "patient_session.patient_id" not in body:
                offenders.append(f"{node.name}: never binds patient_session.patient_id")
        assert not offenders, "portal method(s) not bound to the session's patient:\n  " + "\n  ".join(offenders)


@requires_db
class TestAStaffSessionIsNotAPatientSession:
    """DECISION 8: patient access is explicit. A staff session does not become
    a patient session by being handed to a portal method."""

    def test_a_staff_session_is_refused_by_the_portal_surface(self, dao, staff_a, patient_a):
        with pytest.raises(AuthorizationError):
            _my_record(dao, staff_a, patient_a)

    def test_a_staff_session_is_refused_by_the_portal_orders_list(self, dao, staff_a, patient_a):
        with pytest.raises(AuthorizationError):
            try:
                dao.list_my_test_orders(staff_a, patient_a)
            except TypeError:
                dao.list_my_test_orders(staff_a)

    def test_the_positive_twin_a_patient_session_is_accepted(self, dao, alice_session, patient_a):
        assert _my_record(dao, alice_session, patient_a)["patient_id"] == patient_a


class TestTheGuardRailsCoverTheNewTables:
    """Source-reading; runs without a database."""

    def test_both_tables_are_declared_org_scoped(self):
        """
        Not a restatement of the derived guard, which already fails if a name
        is missing. This pins the DIRECTION: neither table may be moved to the
        exemption list later, and the exemption interlock refuses it anyway
        because both carry org_id.
        """
        assert "patient_logins" in ORG_SCOPED_TABLES
        assert "patient_sessions" in ORG_SCOPED_TABLES

    def test_the_patient_login_lookup_binds_org_id(self):
        """
        The statement the design named specifically. An email is unique only
        WITHIN an organisation, so a login lookup keyed on the address alone
        would authenticate one tenant's patient against another tenant's
        credential.
        """
        source = (pathlib.Path(__file__).resolve().parent.parent / "data_access.py").read_text(encoding="utf-8")
        assert "FROM patient_logins WHERE org_id = %s AND email = %s" in " ".join(source.split())

    def test_no_new_system_session_call_site_was_added(self):
        """
        The brief's hard constraint, restated where this wave can see it. The
        four sites are pinned by name in test_identity.py and that guard scans
        the tree; this asserts the same thing about the modules w130 adds, so
        the failure names w130 rather than arriving as a count mismatch.
        """
        clinical = pathlib.Path(__file__).resolve().parent.parent
        for name in ("data_access.py", "endpoints.py", "app.py"):
            source = (clinical / name).read_text(encoding="utf-8")
            w130_region = source.split("w130")
            for chunk in w130_region[1:]:
                assert "_create_system_session" not in chunk[:4000], (
                    f"a _create_system_session call site appears in w130 code in {name}"
                )

    def test_nothing_clinical_may_name_a_patient_principal_as_an_actor(self):
        """
        A CENSUS, not a sample (Standing Rule 3). The reason the principal is a
        separate table is that `REFERENCES users` occurs 22 times in
        schema.sql, nineteen of them clinical-record actor columns -- so a
        patient in `users` would be a database-legal report approver.

        That argument is only worth anything while patient_logins is referenced
        by NOTHING except the patient session table. This counts it. A future
        `reports.patient_approver_id`, or any other actor column pointed at the
        patient principal, fails here.
        """
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        referencing = [line.strip() for line in schema.splitlines() if "REFERENCES patient_logins" in line]
        assert len(referencing) == 1, f"patient_logins is referenced {len(referencing)} times: {referencing}"
        assert "patient_logins (patient_login_id, org_id)" in referencing[0]

    def test_no_patient_role_was_added(self):
        """
        Decision 1, asserted against both places a role could have been added.
        """
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        assert "'Patient'" not in schema
        source = (pathlib.Path(__file__).resolve().parent.parent / "data_access.py").read_text(encoding="utf-8")
        assert '"Patient"' not in source
        assert "role_assignments_role_known CHECK" in schema
        # The seven staff roles, unchanged.
        assert "role IN ('Orderer', 'Lab technician', 'Interpreter'," in schema
