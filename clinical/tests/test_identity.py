"""
clinical/tests/test_identity.py
────────────────────────────────
Phase 1 identity suite.

RUNNING THESE. They need a real PostgreSQL database, because most of what
they assert is enforced BY Postgres -- composite foreign keys, the CHECK on
Approver's basis, REVOKE on audit_log. A sqlite stand-in would run faster and
prove none of it.

    export CLINICAL_TEST_DSN=postgresql://user:pw@localhost:5432/clinical_test
    pytest clinical/tests/test_identity.py

Without that variable every database test SKIPS. A skip is not a pass: if the
suite reports "skipped" then nothing here has been verified, and it should be
read as no evidence rather than as green. TestStructuralIsolation is the one
class that runs anywhere, because it reads source rather than rows.
"""

from __future__ import annotations

import ast
import datetime as dt
import os
import pathlib
import uuid

import pytest

from clinical.data_access import (
    GENERIC_AUTH_ERROR,
    MAX_FAILED_LOGIN_ATTEMPTS,
    ORG_SCOPED_TABLES,
    AuthenticationError,
    AuthorizationError,
    BcryptHasher,
    DataAccess,
    NotFoundError,
    SessionExpired,
)

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"

requires_db = pytest.mark.skipif(
    not DSN, reason="CLINICAL_TEST_DSN not set -- database tests skipped (skip is not pass)"
)


# ─── test doubles ────────────────────────────────────────────────────────────


class FakeClock:
    """
    The single clock. Passed into DataAccess, so production reads exactly the
    time the test sets -- no `datetime.now()` anywhere in the layer to drift
    against it. Without that, an advancing mock and a wall-clock read compare
    two different clocks and the assertion passes or fails for the wrong
    reason.
    """

    def __init__(self, start: dt.datetime | None = None) -> None:
        self._now = start or dt.datetime(2026, 9, 3, 9, 0, 0, tzinfo=dt.timezone.utc)

    def now(self) -> dt.datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now = self._now + dt.timedelta(**kwargs)


class ReversibleCipher:
    """Stand-in for the production secret cipher. Reversible, never secure."""

    def encrypt(self, plaintext: str) -> str:
        return "enc:" + plaintext

    def decrypt(self, ciphertext: str) -> str:
        assert ciphertext.startswith("enc:")
        return ciphertext[4:]


class FixedTotpVerifier:
    """Accepts one known code, so the suite does not depend on pyotp or on time steps."""

    VALID = "424242"

    def verify(self, secret: str, code: str, at: dt.datetime) -> bool:
        return secret == "SECRET" and code == self.VALID


# ─── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def conn():
    psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
    if not DSN:
        pytest.skip("CLINICAL_TEST_DSN not set")
    # connect_timeout is not tuning. Without it, a DSN pointing at nothing
    # makes this fixture BLOCK rather than raise -- measured: the run hung past
    # two minutes against a closed port. In CI that is a job that sits until the
    # workflow timeout and reports neither pass nor fail, which is the same
    # silent non-result the fail-not-skip guard exists to prevent. Ten seconds
    # is long enough for a service container that is still starting and short
    # enough that "the database is not there" arrives as an error.
    connection = psycopg.connect(DSN, autocommit=False, connect_timeout=10)
    with connection.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute("DO $$ BEGIN CREATE ROLE clinical_app; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture()
def clock():
    return FakeClock()


@pytest.fixture()
def dao(conn, clock):
    return DataAccess(
        conn,
        clock=clock,
        # Low cost so the suite stays fast while remaining real bcrypt --
        # the algorithm under test is the one that ships.
        password_hasher=BcryptHasher(rounds=4),
        totp_verifier=FixedTotpVerifier(),
        secret_cipher=ReversibleCipher(),
    )


@pytest.fixture()
def org_a(dao):
    return dao.create_organisation("Org A")


@pytest.fixture()
def org_b(dao):
    return dao.create_organisation("Org B")


@pytest.fixture()
def admin_session(dao, org_a, conn):
    """An administrator in org_a, with the role inserted directly to break the
    bootstrap cycle (assign_role itself requires an Administrator)."""
    admin_id = dao.create_user(org_a, "admin@a.example", "correct horse battery staple")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (admin_id, org_a, admin_id, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)),
        )
    return dao.login("admin@a.example", org_a, "correct horse battery staple", ip_address="10.0.0.1")


# ─── organisation isolation ──────────────────────────────────────────────────


@requires_db
class TestOrgIsolation:
    def test_cross_org_read_by_id_is_not_found(self, dao, org_a, org_b, admin_session):
        """
        The real cross-org attempt: a valid org_a session, holding a REAL
        org_b user id, asking for that record by id. Not a wrong function
        call -- an actual read of a record that exists, from the wrong side of
        the boundary.
        """
        victim = dao.create_user(org_b, "clinician@b.example", "a sufficiently long password")

        with pytest.raises(NotFoundError):
            dao.get_user(admin_session, victim)

    def test_cross_org_miss_is_indistinguishable_from_absent(self, dao, org_b, admin_session):
        """
        A real id in another organisation and an invented id must produce the
        same error. If they differed, the difference would confirm the record
        exists somewhere -- existence is itself information.
        """
        real_elsewhere = dao.create_user(org_b, "someone@b.example", "a sufficiently long password")
        invented = uuid.uuid4()

        with pytest.raises(NotFoundError) as real_exc:
            dao.get_user(admin_session, real_elsewhere)
        with pytest.raises(NotFoundError) as invented_exc:
            dao.get_user(admin_session, invented)

        assert type(real_exc.value) is type(invented_exc.value)
        # Only the echoed id differs; the shape of the message does not.
        assert str(real_exc.value).replace(str(real_elsewhere), "X") == str(invented_exc.value).replace(
            str(invented), "X"
        )

    def test_cross_org_role_read_returns_nothing(self, dao, org_b, admin_session):
        victim = dao.create_user(org_b, "roles@b.example", "a sufficiently long password")
        assert dao.get_user_roles(admin_session, victim) == []

    def test_composite_fk_blocks_cross_org_role_row(self, dao, conn, org_a, org_b):
        """
        The schema half of the isolation: even bypassing this layer entirely
        and inserting straight into the table, a role in org_a cannot name a
        user who belongs to org_b.
        """
        psycopg = pytest.importorskip("psycopg")
        victim = dao.create_user(org_b, "fk@b.example", "a sufficiently long password")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                    "VALUES (%s, %s, 'Interpreter', %s, %s)",
                    (victim, org_a, victim, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)),
                )


# ─── authentication failure is uniform ───────────────────────────────────────


@requires_db
class TestAuthFailureIsUniform:
    def test_disabled_user_auth_indistinguishable(self, dao, org_a, admin_session):
        """
        Asserts EQUALITY against the generic message, not the absence of a
        word. An absence check passes for "Invalid account credentials" --
        which is fine -- and would therefore not break if someone later
        shipped a message that is fine in wording but distinguishing in
        effect. Equality breaks the moment any path says something different.
        """
        target = dao.create_user(org_a, "off@a.example", "a sufficiently long password")
        dao.set_user_disabled(admin_session, target, True)

        with pytest.raises(AuthenticationError) as disabled_exc:
            dao.login("off@a.example", org_a, "a sufficiently long password")
        with pytest.raises(AuthenticationError) as wrong_pw_exc:
            dao.login("off@a.example", org_a, "definitely not the password")

        assert str(disabled_exc.value) == GENERIC_AUTH_ERROR
        assert str(wrong_pw_exc.value) == GENERIC_AUTH_ERROR

    def test_unknown_user_matches_wrong_password(self, dao, org_a):
        dao.create_user(org_a, "real@a.example", "a sufficiently long password")

        with pytest.raises(AuthenticationError) as unknown:
            dao.login("nobody@a.example", org_a, "a sufficiently long password")
        with pytest.raises(AuthenticationError) as wrong:
            dao.login("real@a.example", org_a, "wrong wrong wrong wrong")

        assert str(unknown.value) == str(wrong.value) == GENERIC_AUTH_ERROR


# ─── lockout ─────────────────────────────────────────────────────────────────


@requires_db
class TestLockout:
    def test_lockout_enforcement(self, dao, clock, org_a):
        """
        No sleep, and no second clock. `clock` is the same object DataAccess
        was constructed with, so advancing it advances production's view of
        time as well -- the locked_until comparison inside login() reads this
        clock, not the wall clock.
        """
        dao.create_user(org_a, "locked@a.example", "a sufficiently long password")

        for _ in range(MAX_FAILED_LOGIN_ATTEMPTS):
            with pytest.raises(AuthenticationError):
                dao.login("locked@a.example", org_a, "wrong password entirely")

        # Locked: the CORRECT password is now refused.
        with pytest.raises(AuthenticationError) as locked:
            dao.login("locked@a.example", org_a, "a sufficiently long password")
        assert str(locked.value) == GENERIC_AUTH_ERROR

        # Still locked one minute before the window closes.
        clock.advance(minutes=29)
        with pytest.raises(AuthenticationError):
            dao.login("locked@a.example", org_a, "a sufficiently long password")

        # Open after it.
        clock.advance(minutes=2)
        session = dao.login("locked@a.example", org_a, "a sufficiently long password")
        assert session.org_id == org_a

    def test_successful_login_resets_only_that_users_counter(self, dao, clock, conn, org_a):
        """
        The counter is per-user, so an attacker holding one valid credential
        cannot keep a second account permanently unlocked by logging in as
        themselves.
        """
        dao.create_user(org_a, "victim@a.example", "a sufficiently long password")
        dao.create_user(org_a, "attacker@a.example", "another sufficiently long one")

        for _ in range(3):
            with pytest.raises(AuthenticationError):
                dao.login("victim@a.example", org_a, "wrong")

        dao.login("attacker@a.example", org_a, "another sufficiently long one")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT failed_login_attempts FROM users WHERE org_id = %s AND email = %s",
                (org_a, "victim@a.example"),
            )
            assert cur.fetchone()[0] == 3, "another user's login cleared this user's counter"

        dao.login("victim@a.example", org_a, "a sufficiently long password")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT failed_login_attempts FROM users WHERE org_id = %s AND email = %s",
                (org_a, "victim@a.example"),
            )
            assert cur.fetchone()[0] == 0


# ─── audit ───────────────────────────────────────────────────────────────────


@requires_db
class TestAudit:
    def test_lockout_audit_distinguishes_sources(self, dao, conn, org_a):
        """
        Proves ORDER and GROUPING, not merely presence: five failures from one
        address and one from another must be readable as that, in sequence.
        A count alone would not tell a distributed attempt from a single
        source.
        """
        dao.create_user(org_a, "watched@a.example", "a sufficiently long password")

        for _ in range(5):
            with pytest.raises(AuthenticationError):
                dao.login("watched@a.example", org_a, "wrong", ip_address="203.0.113.7")
        with pytest.raises(AuthenticationError):
            dao.login("watched@a.example", org_a, "wrong", ip_address="198.51.100.2")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT ip_address FROM audit_log WHERE action = 'login_failed' AND org_id = %s "
                'ORDER BY "timestamp", log_id',
                (org_a,),
            )
            addresses = [str(r[0]) for r in cur.fetchall()]

        assert addresses == ["203.0.113.7"] * 5 + ["198.51.100.2"]

    def test_failed_login_against_unknown_org_is_logged_with_null_org(self, dao, conn):
        """
        The event most worth recording is the one an FK would have rejected.
        """
        with pytest.raises(AuthenticationError):
            dao.login("probe@nowhere.example", uuid.uuid4(), "guess", ip_address="203.0.113.9")

        with conn.cursor() as cur:
            cur.execute("SELECT org_id, details FROM audit_log WHERE action = 'login_failed' AND org_id IS NULL")
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] is None
        assert rows[0][1]["reason"] == "unknown_organisation"
        assert rows[0][1]["email"] == "probe@nowhere.example"

    def test_audit_log_rejects_update_and_delete_for_app_role(self, conn):
        """
        Append-only is a privilege, not a comment. Asserted as the application
        role, since the owner can always do anything.
        """
        psycopg = pytest.importorskip("psycopg")
        with conn.cursor() as cur:
            cur.execute("SET ROLE clinical_app")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE audit_log SET action = 'tampered'")
            # Transaction entered error state; must rollback before continuing
            conn.rollback()
            cur.execute("RESET ROLE")

        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SET ROLE clinical_app")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("DELETE FROM audit_log")
            # Transaction entered error state; must rollback before continuing
            conn.rollback()
            cur.execute("RESET ROLE")

        conn.commit()


# ─── sessions ────────────────────────────────────────────────────────────────


@requires_db
class TestSessions:
    def test_session_revocation(self, dao, org_a, admin_session):
        dao.create_user(org_a, "user@a.example", "a sufficiently long password")
        victim = dao.login("user@a.example", org_a, "a sufficiently long password")

        assert dao.session_valid(victim.session_id).user_id == victim.user_id

        dao.terminate_session(admin_session, victim.session_id)

        with pytest.raises(SessionExpired):
            dao.session_valid(victim.session_id)

    def test_idle_timeout_expires_and_activity_refreshes_it(self, dao, clock, org_a):
        dao.create_user(org_a, "idle@a.example", "a sufficiently long password")
        session = dao.login("idle@a.example", org_a, "a sufficiently long password")

        # Activity inside the window keeps it alive, repeatedly.
        for _ in range(4):
            clock.advance(minutes=20)
            assert dao.session_valid(session.session_id).session_id == session.session_id

        # Silence past the window ends it.
        clock.advance(minutes=31)
        with pytest.raises(SessionExpired):
            dao.session_valid(session.session_id)

    def test_absolute_cap_is_not_extended_by_activity(self, dao, clock, conn, org_a):
        """
        The 24-hour ceiling holds even for a session used continuously, AND the
        expiry at the end is the cap rather than the idle timeout.

        Every step advances 15 minutes -- strictly less than the 30-minute idle
        window -- so the idle timer is refreshed before it can ever fire. An
        earlier version of this test stepped by exactly 30 minutes, which is the
        inclusive idle boundary, so it died on the first step and never reached
        the assertion it was named for. Stepping under the window is what
        isolates the two expiries from each other.
        """
        dao.create_user(org_a, "long@a.example", "a sufficiently long password")
        session = dao.login("long@a.example", org_a, "a sufficiently long password")

        # 23h45m of steady activity, each step inside the idle window.
        for step in range(95):
            clock.advance(minutes=15)
            try:
                dao.session_valid(session.session_id)
            except SessionExpired:
                pytest.fail(f"session expired at step {step} ({(step + 1) * 15} min), before the cap")

        # One more 15-minute step lands exactly on the 24-hour cap. Activity was
        # 15 minutes ago, so the idle window is NOT exceeded -- the only thing
        # that can end the session here is the absolute ceiling.
        clock.advance(minutes=15)
        with pytest.raises(SessionExpired):
            dao.session_valid(session.session_id)

        # Prove that claim rather than assert it in prose: at the moment of
        # failure the session was well inside its idle window, so idle cannot
        # have been the cause.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_activity_at, idle_timeout_minutes, expires_at FROM sessions WHERE session_id = %s",
                (session.session_id,),
            )
            last_activity, idle_minutes, expires_at = cur.fetchone()

        now = clock.now()
        assert now < last_activity + dt.timedelta(minutes=idle_minutes), (
            "idle window had also elapsed -- this test no longer isolates the absolute cap"
        )
        assert now >= expires_at, "session should have reached its absolute cap"

    def test_terminate_session_requires_administrator(self, dao, org_a, admin_session):
        dao.create_user(org_a, "plain@a.example", "a sufficiently long password")
        plain = dao.login("plain@a.example", org_a, "a sufficiently long password")
        other = dao.login("plain@a.example", org_a, "a sufficiently long password")

        with pytest.raises(AuthorizationError):
            dao.terminate_session(plain, other.session_id)


# ─── roles ───────────────────────────────────────────────────────────────────


@requires_db
class TestRoles:
    def test_assign_role_requires_administrator_not_approver(self, dao, org_a, admin_session):
        """
        Approver signs reports; Administrator manages users. An Approver who
        could grant Approver would collapse the separation the role table
        exists to express.
        """
        approver_id = dao.create_user(org_a, "approver@a.example", "a sufficiently long password")
        dao.assign_role(admin_session, approver_id, "Approver", basis="GMC 1234567, consultant")
        approver = dao.login("approver@a.example", org_a, "a sufficiently long password")

        target = dao.create_user(org_a, "target@a.example", "a sufficiently long password")
        with pytest.raises(AuthorizationError):
            dao.assign_role(approver, target, "Interpreter")

    def test_approver_requires_basis(self, dao, org_a, admin_session):
        target = dao.create_user(org_a, "needsbasis@a.example", "a sufficiently long password")
        with pytest.raises(ValueError):
            dao.assign_role(admin_session, target, "Approver")

    def test_approver_basis_is_retrievable(self, dao, org_a, admin_session):
        """ISO 15189 7.4.1.5 c): who granted it, when, and on what basis."""
        target = dao.create_user(org_a, "signer@a.example", "a sufficiently long password")
        dao.assign_role(admin_session, target, "Approver", basis="GMC 7654321, histopathology")

        (assignment,) = dao.get_user_roles(admin_session, target)
        assert assignment.role == "Approver"
        assert assignment.basis == "GMC 7654321, histopathology"
        assert assignment.assigned_by == admin_session.user_id
        assert assignment.assigned_at is not None

    def test_schema_rejects_approver_without_basis(self, dao, conn, org_a):
        """The CHECK, not just the Python guard."""
        psycopg = pytest.importorskip("psycopg")
        target = dao.create_user(org_a, "rawinsert@a.example", "a sufficiently long password")
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                    "VALUES (%s, %s, 'Approver', %s, %s)",
                    (target, org_a, target, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)),
                )


# ─── TOTP ────────────────────────────────────────────────────────────────────


@requires_db
class TestTotp:
    def test_totp_required_once_enrolled(self, dao, org_a, admin_session):
        target = dao.create_user(org_a, "mfa@a.example", "a sufficiently long password")
        dao.enrol_totp(admin_session, target, "SECRET")

        with pytest.raises(AuthenticationError):
            dao.login("mfa@a.example", org_a, "a sufficiently long password")

        session = dao.login(
            "mfa@a.example",
            org_a,
            "a sufficiently long password",
            totp_code=FixedTotpVerifier.VALID,
        )
        assert session.user_id == target

    def test_recovery_code_used_once(self, dao, org_a, admin_session):
        target = dao.create_user(org_a, "codes@a.example", "a sufficiently long password")
        codes = dao.enrol_totp(admin_session, target, "SECRET")
        assert len(codes) == 8

        first = codes[0]
        session = dao.login("codes@a.example", org_a, "a sufficiently long password", totp_code=first)
        assert session.user_id == target

        with pytest.raises(AuthenticationError):
            dao.login("codes@a.example", org_a, "a sufficiently long password", totp_code=first)

        # A different, unused code still works.
        assert dao.login("codes@a.example", org_a, "a sufficiently long password", totp_code=codes[1]).user_id == target

    def test_backup_codes_are_not_stored_readably(self, dao, conn, org_a, admin_session):
        target = dao.create_user(org_a, "hashed@a.example", "a sufficiently long password")
        codes = dao.enrol_totp(admin_session, target, "SECRET")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT code_hash FROM totp_backup_codes WHERE user_id = %s AND org_id = %s",
                (target, org_a),
            )
            stored = [r[0] for r in cur.fetchall()]

        for code in codes:
            assert code not in stored
        assert all(h.startswith("$2") for h in stored), "backup codes must be bcrypt hashes"


# ─── structural isolation (runs without a database) ──────────────────────────


class TestStructuralIsolation:
    """
    The test the design promised: it fails if a raw-query path or an
    org-unscoped statement is introduced. Reads source, so it runs anywhere.
    """

    @staticmethod
    def _sql_literals() -> list[str]:
        source = (pathlib.Path(__file__).resolve().parent.parent / "data_access.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if any(kw in text for kw in ("SELECT ", "UPDATE ", "DELETE FROM", "INSERT INTO")):
                    out.append(" ".join(text.split()))
        return out

    def test_every_org_scoped_statement_binds_org_id(self):
        offenders = []
        for sql in self._sql_literals():
            touches_scoped = any(
                f"{verb} {table}" in sql for table in ORG_SCOPED_TABLES for verb in ("FROM", "INTO", "UPDATE", "JOIN")
            )
            if touches_scoped and "org_id" not in sql:
                offenders.append(sql)
        assert not offenders, "statement(s) against an org-scoped table without an org_id predicate:\n  " + "\n  ".join(
            offenders
        )

    def test_no_public_attribute_exposes_the_connection(self):
        sentinel = object()
        dao = DataAccess(sentinel, clock=FakeClock())
        exposed = [name for name in dir(dao) if not name.startswith("_") and getattr(dao, name, None) is sentinel]
        assert not exposed, f"connection reachable via public attribute(s): {exposed}"

    def test_no_method_accepts_caller_supplied_sql(self):
        source = (pathlib.Path(__file__).resolve().parent.parent / "data_access.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and not node.name.startswith("_")
            and any(a.arg in ("sql", "query", "where", "predicate") for a in node.args.args)
        ]
        assert not offenders, f"public method(s) accepting caller-supplied SQL: {offenders}"

    def test_layer_never_reads_the_wall_clock(self):
        """
        Clock injection, asserted rather than trusted. If any method starts
        calling datetime.now() directly, a test that advances a mock clock
        silently begins comparing two different clocks.
        """
        source = (pathlib.Path(__file__).resolve().parent.parent / "data_access.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = []
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            if cls.name == "SystemClock":
                continue  # the one permitted reader of the wall clock
            for node in ast.walk(cls):
                if isinstance(node, ast.Attribute) and node.attr in ("now", "utcnow", "today"):
                    value = node.value
                    name = getattr(value, "attr", None) or getattr(value, "id", None)
                    if name in ("datetime", "date", "_datetime"):
                        offenders.append(f"{cls.name}: {name}.{node.attr}()")
        assert not offenders, f"wall-clock read(s) outside SystemClock: {offenders}"

    def test_every_public_method_has_auditable_decorator(self):
        """
        Phase 2 audit: every public DataAccess method declares its audit status
        via @auditable. Absence of the decorator means "nobody looked", not
        "deliberately non-auditable". Non-auditable methods must declare
        auditable=False with a reason.
        """
        source = (pathlib.Path(__file__).resolve().parent.parent / "data_access.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        undecorated = []
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DataAccess"]:
            for node in cls.body:
                if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                    # Check if @auditable decorator is present
                    has_auditable = any(
                        (isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "auditable")
                        or (isinstance(d, ast.Name) and d.id == "auditable")
                        for d in node.decorator_list
                    )
                    if not has_auditable:
                        undecorated.append(node.name)

        assert not undecorated, (
            f"Public method(s) without @auditable decorator: {undecorated}. "
            f"Every public method must declare @auditable(auditable=True) or "
            f"@auditable(auditable=False, reason='...')."
        )

    def test_create_system_session_call_site_enforcement(self):
        """
        _create_system_session is an authentication bypass. It must be called from
        exactly one location (the automatic submission path), enforced via AST.
        """
        source = (pathlib.Path(__file__).resolve().parent.parent / "data_access.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        call_sites = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Match: self._create_system_session(...)
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_create_system_session"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"
                ):
                    # Find which method this call is in
                    for potential_parent in ast.walk(tree):
                        if isinstance(potential_parent, ast.FunctionDef):
                            for child in ast.walk(potential_parent):
                                if child is node:
                                    call_sites.append(potential_parent.name)
                                    break

        assert len(call_sites) <= 1, (
            f"_create_system_session must be called from at most one location "
            f"(authentication bypass enforcement), but found {len(call_sites)} call(s): {call_sites}"
        )

    def test_auditable_session_signature_enforcement(self):
        """
        When @auditable declares requires_session=True, the method must have
        Session as its first parameter (after self).
        """
        source = (pathlib.Path(__file__).resolve().parent.parent / "data_access.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        offenders = []
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "DataAccess"]:
            for node in cls.body:
                if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                    # Find @auditable decorator and check requires_session param
                    for dec in node.decorator_list:
                        if isinstance(dec, ast.Call) and getattr(dec.func, "id", None) == "auditable":
                            # Check if requires_session is explicitly set to False
                            requires_session = True  # default
                            for keyword in dec.keywords:
                                if keyword.arg == "requires_session":
                                    if isinstance(keyword.value, ast.Constant):
                                        requires_session = keyword.value.value
                                    elif isinstance(keyword.value, ast.NameConstant):
                                        requires_session = keyword.value.value

                            if requires_session:
                                # Check if first parameter (after self) is Session
                                if len(node.args.args) < 2:
                                    offenders.append((node.name, "requires_session=True but no first parameter"))
                                elif node.args.args[1].arg != "session":
                                    offenders.append(
                                        (
                                            node.name,
                                            f"requires_session=True but first param is '{node.args.args[1].arg}' not 'session'",
                                        )
                                    )

        assert not offenders, "Method(s) with requires_session=True mismatch:\n  " + "\n  ".join(
            f"{m}: {reason}" for m, reason in offenders
        )
