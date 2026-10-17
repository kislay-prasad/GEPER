"""
clinical/tests/test_w164_mfa.py
───────────────────────────────
MULTI-FACTOR AUTHENTICATION FOR STAFF, AND THE KEY THAT PROTECTS THE SECRET.

WHERE THE KEY COMES FROM, AND WHY THIS IS NOT A NEW DECISION. Answered from
this repository rather than from general practice, at 4ab1fbf:

  * `CLINICAL_DSN` (clinical/endpoints.py, clinical/app.py) already arrives
    through the process environment, and it CARRIES THE DATABASE PASSWORD --
    app.py's own comment says so. The environment is already the channel by
    which this service receives the credential granting full read and write on
    the very table `totp_secret` lives in. A key delivered the same way is not
    weaker than the credential already there; a key delivered any OTHER way
    would be a second secret channel for a system that has one.
  * `GEPER_API_KEYS` (geper/api/main.py::_load_api_keys) is this repository's
    production-credential pattern: read from the environment, REFUSE TO START
    on a malformed value, explicit dev opt-out. The spec records it as
    IMPLEMENTED at section 23.3 and it is the spec's only statement about how a
    service credential is supplied.
  * `.env.example` is committed, `.env` is gitignored, and docker-compose
    passes the variables through. The deployment channel exists.
  * `clinical/schema.sql`'s own comment on `users.totp_secret` already ruled:
    "encrypted at rest by the application before it ever reaches this column
    ... Encrypted rather than hashed". Application-side symmetric encryption
    was decided by the schema, not by this wave.
  * The `SecretCipher` Protocol takes `encrypt(str) -> str` and
    `decrypt(str) -> str` and is handed no user, no record and no
    organisation. That shape admits ONE architecture -- a single process-wide
    symmetric key from configuration -- and rules out per-user derivation and
    per-record envelopes.

SPEC SILENT on key management, encryption at rest and rotation (23.1-23.4 and
30.1-30.3 read in full; 23.4 is transport only). Fourteen held tips carry no
cipher, no key loader and no ruling (measured by `git grep` over every tip).

WHAT WAS NOT DECIDED AND IS NOT INVENTED: a rotation POLICY -- who rotates,
how often, and what re-encrypts the stored secrets. The variable is a
comma-separated LIST for the same reason `GEPER_API_KEYS` is, so a rotation is
EXPRESSIBLE without a further ruling (MultiFernet encrypts under the first key
and decrypts under any), but performing one is an operator act nobody has
scheduled. That is carded, not guessed.

RULE 10 AND RULE 11 ARE BOTH LOAD-BEARING HERE. Every structural row in this
file takes NO database fixture, so none of them can skip when the DSN is
absent. Every gated capability has a row that calls the DOMAIN METHOD DIRECTLY
with a wrong-role session -- the HTTP rows below stayed green against a build
whose gate had been moved into the router, and only the direct rows went red.
"""

from __future__ import annotations

import ast
import base64
import datetime as dt
import os
import pathlib

import pytest

from clinical.data_access import (
    TOTP_SECRET_KEYS_ENV,
    AuthenticationError,
    AuthorizationError,
    BcryptHasher,
    ConfigurationError,
    DataAccess,
    FernetSecretCipher,
    NotFoundError,
    PyOtpVerifier,
    default_secret_cipher,
    generate_totp_secret,
)

DSN = os.getenv("CLINICAL_TEST_DSN")
SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"
DATA_ACCESS_MODULE = pathlib.Path(__file__).resolve().parent.parent / "data_access.py"

requires_db = pytest.mark.skipif(
    not DSN, reason="CLINICAL_TEST_DSN not set -- database tests skipped (skip is not pass)"
)

PASSWORD = "correct horse battery staple"

# Two DIFFERENT keys, generated once and written down, so the rotation row
# below is a real two-key statement rather than the same key twice.
KEY_A = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
KEY_B = base64.urlsafe_b64encode(bytes(range(32, 64))).decode("ascii")


class FakeClock:
    def __init__(self, start: dt.datetime | None = None) -> None:
        self._now = start or dt.datetime(2026, 9, 19, 9, 0, 0, tzinfo=dt.timezone.utc)

    def now(self) -> dt.datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now = self._now + dt.timedelta(**kwargs)


class ExplodingCipher:
    """A cipher whose decrypt RAISES. Wrong-version C in fixture form."""

    def encrypt(self, plaintext: str) -> str:
        return "enc:" + plaintext

    def decrypt(self, ciphertext: str) -> str:
        raise RuntimeError("the crypto path blew up")


class ExplodingVerifier:
    def matching_step(self, secret, code, at):
        raise RuntimeError("the verifier blew up")


def _code_for(secret: str, moment: dt.datetime) -> str:
    import pyotp

    return pyotp.TOTP(secret).at(moment)


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
    """
    THE REAL CIPHER AND THE REAL VERIFIER. Not a reversible stand-in: the
    property under test here is that a real Fernet token in the column
    verifies through a real pyotp computation, and a stand-in for either would
    prove neither.
    """
    return DataAccess(
        conn,
        clock=clock,
        password_hasher=BcryptHasher(rounds=4),
        secret_cipher=FernetSecretCipher([KEY_A]),
    )


@pytest.fixture()
def org_a(dao):
    return dao.create_organisation("Org A")


@pytest.fixture()
def org_b(dao):
    return dao.create_organisation("Org B")


def _make_admin(dao, conn, org_id, email):
    user_id = dao.create_user(org_id, email, PASSWORD)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org_id, user_id, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)),
        )
    conn.commit()
    return user_id


@pytest.fixture()
def admin_session(dao, conn, org_a):
    _make_admin(dao, conn, org_a, "admin@a.example")
    return dao.login("admin@a.example", org_a, PASSWORD, ip_address="10.0.0.1")


@pytest.fixture()
def orderer_session(dao, conn, org_a):
    """A real, valid session that is NOT an Administrator. Rule 11's subject."""
    user_id = dao.create_user(org_a, "orderer@a.example", PASSWORD)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Orderer', %s, %s)",
            (user_id, org_a, user_id, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)),
        )
    conn.commit()
    return dao.login("orderer@a.example", org_a, PASSWORD, ip_address="10.0.0.1")


# ─── the cipher, and the key (no database: Rule 10) ──────────────────────────


class TestTheCipher:
    def test_a_secret_round_trips(self):
        cipher = FernetSecretCipher([KEY_A])
        assert cipher.decrypt(cipher.encrypt("JBSWY3DPEHPK3PXP")) == "JBSWY3DPEHPK3PXP"

    def test_the_ciphertext_is_not_the_plaintext(self):
        """WRONG-VERSION A in one line: a 'cipher' that returned its input
        would satisfy the round trip above and fail here."""
        cipher = FernetSecretCipher([KEY_A])
        secret = "JBSWY3DPEHPK3PXP"
        token = cipher.encrypt(secret)
        assert token != secret
        assert secret not in token

    def test_two_encryptions_of_one_secret_differ(self):
        """Fernet carries a random IV. A deterministic ciphertext would let an
        observer of the column tell two accounts share a secret."""
        cipher = FernetSecretCipher([KEY_A])
        assert cipher.encrypt("JBSWY3DPEHPK3PXP") != cipher.encrypt("JBSWY3DPEHPK3PXP")

    def test_a_tampered_token_is_refused(self):
        """Authenticated, not merely encrypted: a flipped character does not
        decrypt to something else, it raises."""
        cipher = FernetSecretCipher([KEY_A])
        token = cipher.encrypt("JBSWY3DPEHPK3PXP")
        tampered = token[:-2] + ("AA" if token[-2:] != "AA" else "BB")
        with pytest.raises(Exception):
            cipher.decrypt(tampered)

    def test_a_token_written_under_another_key_is_refused(self):
        with pytest.raises(Exception):
            FernetSecretCipher([KEY_B]).decrypt(FernetSecretCipher([KEY_A]).encrypt("s"))

    def test_a_second_key_still_decrypts_what_the_first_wrote(self):
        """
        THE ROTATION MECHANISM, and the reason the variable is a LIST. New key
        first, old key second: everything written from now on is under the new
        key, and everything already in the column still reads.
        """
        old = FernetSecretCipher([KEY_A])
        token = old.encrypt("JBSWY3DPEHPK3PXP")
        rotated = FernetSecretCipher([KEY_B, KEY_A])
        assert rotated.decrypt(token) == "JBSWY3DPEHPK3PXP"
        # And it WRITES under the new key -- proved by the old cipher alone
        # being unable to read what the rotated one writes.
        with pytest.raises(Exception):
            old.decrypt(rotated.encrypt("JBSWY3DPEHPK3PXP"))

    def test_a_key_of_the_wrong_length_is_refused_at_construction(self):
        short = base64.urlsafe_b64encode(b"too short").decode("ascii")
        with pytest.raises(ConfigurationError):
            FernetSecretCipher([short])

    def test_a_key_that_is_not_base64_is_refused_at_construction(self):
        with pytest.raises(ConfigurationError):
            FernetSecretCipher(["this is not base64 !!!"])

    def test_an_empty_key_list_is_refused(self):
        with pytest.raises(ConfigurationError):
            FernetSecretCipher([])
        with pytest.raises(ConfigurationError):
            FernetSecretCipher(["   "])


class TestTheKeyComesFromTheEnvironment:
    """
    The census that replaces w155's. w155 asserted that NO production
    construction supplies a cipher, which was true and is now false; the honest
    successor asserts WHERE the cipher comes from instead, and it is
    non-vacuous in both directions -- there is a row for the key present and a
    row for the key absent.
    """

    def test_no_key_configured_means_no_cipher(self, monkeypatch):
        monkeypatch.delenv(TOTP_SECRET_KEYS_ENV, raising=False)
        assert default_secret_cipher() is None

    def test_an_empty_value_means_no_cipher(self, monkeypatch):
        monkeypatch.setenv(TOTP_SECRET_KEYS_ENV, "   ")
        assert default_secret_cipher() is None

    def test_a_configured_key_builds_a_working_cipher(self, monkeypatch):
        monkeypatch.setenv(TOTP_SECRET_KEYS_ENV, KEY_A)
        cipher = default_secret_cipher()
        assert cipher is not None
        assert cipher.decrypt(cipher.encrypt("s")) == "s"

    def test_a_malformed_key_refuses_rather_than_falling_back(self, monkeypatch):
        """
        FAIL CLOSED. The alternative -- treating an unreadable key as 'no key'
        -- would turn a typo into a silently unprotected deployment.
        """
        monkeypatch.setenv(TOTP_SECRET_KEYS_ENV, "not-a-key")
        with pytest.raises(ConfigurationError):
            default_secret_cipher()

    def test_a_data_access_given_only_a_connection_gets_the_configured_cipher(self, monkeypatch):
        """
        THE POINT OF THE WHOLE WAVE, and it needs no database: all four
        production `DataAccess(...)` sites pass the connection alone, so the
        default is the only thing that can reach them.
        """
        monkeypatch.setenv(TOTP_SECRET_KEYS_ENV, KEY_A)
        assert DataAccess(None)._cipher is not None

    def test_and_none_when_the_key_is_absent(self, monkeypatch):
        """The twin. Without it the row above passes against a build that
        hardcodes a cipher, which is the defect it exists to prevent."""
        monkeypatch.delenv(TOTP_SECRET_KEYS_ENV, raising=False)
        assert DataAccess(None)._cipher is None

    def test_the_verifier_needs_no_configuration_and_is_always_present(self, monkeypatch):
        monkeypatch.delenv(TOTP_SECRET_KEYS_ENV, raising=False)
        assert isinstance(DataAccess(None)._totp, PyOtpVerifier)


class TestSecretGeneration:
    def test_a_generated_secret_is_base32_and_160_bits(self):
        secret = generate_totp_secret()
        assert len(secret) == 32
        assert len(base64.b32decode(secret)) == 20

    def test_two_generated_secrets_differ(self):
        assert len({generate_totp_secret() for _ in range(32)}) == 32

    def test_a_generated_secret_is_usable_by_the_verifier(self):
        secret = generate_totp_secret()
        at = dt.datetime(2026, 9, 19, 9, 0, 0, tzinfo=dt.timezone.utc)
        assert PyOtpVerifier().matching_step(secret, _code_for(secret, at), at) is not None


# ─── enrolment ───────────────────────────────────────────────────────────────


@requires_db
class TestEnrolment:
    def test_the_method_generates_the_secret(self, dao, conn, org_a, admin_session):
        target = dao.create_user(org_a, "mfa@a.example", PASSWORD)
        first = dao.enrol_totp(admin_session, target)
        second_target = dao.create_user(org_a, "mfa2@a.example", PASSWORD)
        second = dao.enrol_totp(admin_session, second_target)
        assert first.secret != second.secret
        assert len(first.backup_codes) == 8

    def test_the_stored_secret_is_not_the_plaintext(self, dao, conn, org_a, admin_session):
        """
        WRONG-VERSION A. The column is read DIRECTLY, because the claim is
        about what is AT REST -- a claim asserted through the method that
        decrypts it would be satisfied by a plaintext column.
        """
        target = dao.create_user(org_a, "atrest@a.example", PASSWORD)
        enrolment = dao.enrol_totp(admin_session, target)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT totp_secret FROM users WHERE user_id = %s", (target,))
            stored = cur.fetchone()[0]
        assert stored != enrolment.secret
        assert enrolment.secret not in stored
        assert stored.startswith("gAAAAA"), stored[:12]
        # The positive twin: it is not merely different, it is THE secret.
        assert FernetSecretCipher([KEY_A]).decrypt(stored) == enrolment.secret

    def test_enrolment_refuses_a_non_administrator_called_directly(self, dao, org_a, orderer_session):
        """RULE 11. Not through the app, not through the client. Direct."""
        target = dao.create_user(org_a, "target@a.example", PASSWORD)
        with pytest.raises(AuthorizationError):
            dao.enrol_totp(orderer_session, target)

    def test_a_refused_enrolment_writes_no_secret(self, dao, conn, org_a, orderer_session):
        target = dao.create_user(org_a, "untouched@a.example", PASSWORD)
        with pytest.raises(AuthorizationError):
            dao.enrol_totp(orderer_session, target)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT totp_secret, totp_enrolled_at FROM users WHERE user_id = %s", (target,))
            assert cur.fetchone() == (None, None)

    def test_enrolment_cannot_reach_another_organisation(self, dao, conn, org_a, org_b, admin_session):
        stranger = dao.create_user(org_b, "stranger@b.example", PASSWORD)
        with pytest.raises(NotFoundError):
            dao.enrol_totp(admin_session, stranger)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT totp_secret FROM users WHERE user_id = %s", (stranger,))
            assert cur.fetchone()[0] is None

    def test_enrolment_is_audited_to_the_acting_organisation(self, dao, conn, org_a, org_b, admin_session):
        """WRONG-VERSION F. The org is asserted, not just the row's existence."""
        target = dao.create_user(org_a, "audited@a.example", PASSWORD)
        dao.enrol_totp(admin_session, target)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id, user_id, resource_id, actor_role, outcome "
                "FROM audit_log WHERE action = 'totp_enrolled'",
            )
            rows = cur.fetchall()
        assert len(rows) == 1, rows
        org_id, user_id, resource_id, actor_role, outcome = rows[0]
        assert org_id == org_a
        assert org_id != org_b
        assert user_id == admin_session.user_id
        assert resource_id == str(target)
        assert actor_role == "Administrator"
        assert outcome == "success"

    def test_the_audit_row_never_contains_the_secret_or_a_backup_code(self, dao, conn, org_a, admin_session):
        """
        A REAL DEFECT AT 4ab1fbf, latent only because nobody could enrol.
        `@auditable` writes `resource_id=str(result)` unless the method
        declares `resource_id_param`, and `enrol_totp` returned the plaintext
        backup codes -- so every enrolment would have written all eight of them
        into `audit_log.resource_id`. Making enrolment reachable makes that
        reachable too, which is why the parameter is declared and why this row
        reads the WHOLE audit table rather than one column.
        """
        target = dao.create_user(org_a, "leak@a.example", PASSWORD)
        enrolment = dao.enrol_totp(admin_session, target)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT resource_id, details::text FROM audit_log")
            blob = " ".join(str(part) for row in cur.fetchall() for part in row)
        assert enrolment.secret not in blob
        for code in enrolment.backup_codes:
            assert code not in blob
        # The twin: something IS written, so the scan is not over an empty table.
        assert "totp_enrolled" in blob or str(target) in blob

    def test_backup_codes_are_stored_only_as_hashes(self, dao, conn, org_a, admin_session):
        target = dao.create_user(org_a, "hashed@a.example", PASSWORD)
        enrolment = dao.enrol_totp(admin_session, target)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT code_hash FROM totp_backup_codes WHERE user_id = %s", (target,))
            stored = [r[0] for r in cur.fetchall()]
        assert len(stored) == 8
        for code in enrolment.backup_codes:
            assert code not in stored
        assert all(h.startswith("$2") for h in stored)

    def test_re_enrolment_voids_the_previous_backup_codes(self, dao, conn, org_a, admin_session):
        """
        A code issued against the OLD secret must not survive the new one.
        Without this, revoking a compromised authenticator by re-enrolling
        leaves eight working credentials behind.
        """
        target = dao.create_user(org_a, "again@a.example", PASSWORD)
        first = dao.enrol_totp(admin_session, target)
        dao.enrol_totp(admin_session, target)
        conn.commit()
        with pytest.raises(AuthenticationError):
            dao.login("again@a.example", org_a, PASSWORD, totp_code=first.backup_codes[0])


# ─── verification ────────────────────────────────────────────────────────────


@requires_db
class TestVerification:
    def _enrol(self, dao, conn, org_a, admin_session, email="user@a.example"):
        target = dao.create_user(org_a, email, PASSWORD)
        enrolment = dao.enrol_totp(admin_session, target)
        conn.commit()
        return target, enrolment

    def test_a_valid_code_admits(self, dao, conn, clock, org_a, admin_session):
        target, enrolment = self._enrol(dao, conn, org_a, admin_session)
        session = dao.login("user@a.example", org_a, PASSWORD, totp_code=_code_for(enrolment.secret, clock.now()))
        assert session.user_id == target

    def test_the_password_alone_is_refused_once_enrolled(self, dao, conn, org_a, admin_session):
        self._enrol(dao, conn, org_a, admin_session)
        with pytest.raises(AuthenticationError):
            dao.login("user@a.example", org_a, PASSWORD)

    def test_the_same_code_is_refused_on_replay(self, dao, conn, clock, org_a, admin_session):
        """
        WRONG-VERSION B. One-time use, PROVED rather than asserted: the code is
        still inside its 30-second step and still cryptographically valid, and
        it is refused because the step it belongs to has been spent.
        """
        self._enrol(dao, conn, org_a, admin_session)
        code = _code_for(_secret_of(dao, conn, "user@a.example"), clock.now())
        assert dao.login("user@a.example", org_a, PASSWORD, totp_code=code) is not None
        conn.commit()
        with pytest.raises(AuthenticationError):
            dao.login("user@a.example", org_a, PASSWORD, totp_code=code)

    def test_an_earlier_step_is_refused_after_a_later_one(self, dao, conn, clock, org_a, admin_session):
        """
        The stronger half. `valid_window=1` means the PREVIOUS step's code is
        still arithmetically acceptable; a guard that only remembered the last
        code would admit it. The guard is the STEP NUMBER, so it cannot.
        """
        _, enrolment = self._enrol(dao, conn, org_a, admin_session)
        earlier = _code_for(enrolment.secret, clock.now())
        clock.advance(seconds=30)
        later = _code_for(enrolment.secret, clock.now())
        assert earlier != later
        assert dao.login("user@a.example", org_a, PASSWORD, totp_code=later) is not None
        conn.commit()
        with pytest.raises(AuthenticationError):
            dao.login("user@a.example", org_a, PASSWORD, totp_code=earlier)

    def test_a_later_step_still_works_after_an_earlier_one(self, dao, conn, clock, org_a, admin_session):
        """The positive twin: the replay guard refuses replays, not logins."""
        _, enrolment = self._enrol(dao, conn, org_a, admin_session)
        assert dao.login("user@a.example", org_a, PASSWORD, totp_code=_code_for(enrolment.secret, clock.now()))
        conn.commit()
        clock.advance(seconds=60)
        assert dao.login("user@a.example", org_a, PASSWORD, totp_code=_code_for(enrolment.secret, clock.now()))

    @pytest.mark.parametrize("code", [None, "", "000000", "12345", "not-a-code", "0" * 64, "٤٢٤٢٤٢"])
    def test_every_bad_code_gives_exactly_the_same_refusal(self, dao, conn, clock, org_a, admin_session, code):
        self._enrol(dao, conn, org_a, admin_session)
        with pytest.raises(AuthenticationError) as caught:
            dao.login("user@a.example", org_a, PASSWORD, totp_code=code)
        assert (type(caught.value), str(caught.value)) == (AuthenticationError, _generic())

    def test_an_undecryptable_secret_refuses_and_never_admits(self, conn, clock, org_a):
        """
        WRONG-VERSION C, first half. The stored secret cannot be read -- a lost
        or rotated-away key. The login must REFUSE. A path that fell back to
        treating the column as plaintext, or that let the exception be caught
        somewhere that returned True, would admit.
        """
        dao = DataAccess(conn, clock=clock, password_hasher=BcryptHasher(rounds=4), secret_cipher=ExplodingCipher())
        _make_admin(dao, conn, org_a, "admin2@a.example")
        admin = dao.login("admin2@a.example", org_a, PASSWORD)
        target = dao.create_user(org_a, "broken@a.example", PASSWORD)
        dao.enrol_totp(admin, target)
        conn.commit()
        for attempt in (None, "000000", "424242"):
            with pytest.raises(AuthenticationError):
                dao.login("broken@a.example", org_a, PASSWORD, totp_code=attempt)

    def test_a_verifier_that_raises_refuses_and_never_admits(self, conn, clock, org_a):
        """WRONG-VERSION C, second half: the failure is in the OTP computation
        rather than the cipher, and the answer is the same."""
        dao = DataAccess(
            conn,
            clock=clock,
            password_hasher=BcryptHasher(rounds=4),
            secret_cipher=FernetSecretCipher([KEY_A]),
            totp_verifier=ExplodingVerifier(),
        )
        _make_admin(dao, conn, org_a, "admin3@a.example")
        admin = dao.login("admin3@a.example", org_a, PASSWORD)
        target = dao.create_user(org_a, "boom@a.example", PASSWORD)
        enrolment = dao.enrol_totp(admin, target)
        conn.commit()
        with pytest.raises(AuthenticationError):
            dao.login("boom@a.example", org_a, PASSWORD, totp_code=_code_for(enrolment.secret, clock.now()))

    def test_a_backup_code_still_works_when_the_authenticator_does_not(self, dao, conn, org_a, admin_session):
        _, enrolment = self._enrol(dao, conn, org_a, admin_session)
        assert dao.login("user@a.example", org_a, PASSWORD, totp_code=enrolment.backup_codes[0])
        conn.commit()
        with pytest.raises(AuthenticationError):
            dao.login("user@a.example", org_a, PASSWORD, totp_code=enrolment.backup_codes[0])

    def test_an_unenrolled_account_is_unaffected(self, dao, org_a):
        dao.create_user(org_a, "plain@a.example", PASSWORD)
        assert dao.login("plain@a.example", org_a, PASSWORD) is not None

    def test_a_failed_second_factor_is_audited_with_its_reason(self, dao, conn, org_a, admin_session):
        self._enrol(dao, conn, org_a, admin_session)
        with pytest.raises(AuthenticationError):
            dao.login("user@a.example", org_a, PASSWORD, totp_code="000000")
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT details::text FROM audit_log WHERE action = 'login_failed'")
            reasons = " ".join(r[0] for r in cur.fetchall())
        assert "invalid_totp" in reasons

    def test_a_replay_is_audited_as_a_replay(self, dao, conn, clock, org_a, admin_session):
        _, enrolment = self._enrol(dao, conn, org_a, admin_session)
        code = _code_for(enrolment.secret, clock.now())
        dao.login("user@a.example", org_a, PASSWORD, totp_code=code)
        conn.commit()
        with pytest.raises(AuthenticationError):
            dao.login("user@a.example", org_a, PASSWORD, totp_code=code)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT org_id, user_id FROM audit_log WHERE action = 'totp_code_replayed'")
            rows = cur.fetchall()
        assert len(rows) == 1, rows
        assert rows[0][0] == org_a


# ─── interaction with the rest of the security model ─────────────────────────


@requires_db
class TestTheSecurityModel:
    def test_a_disabled_principal_cannot_pass_mfa(self, dao, conn, clock, org_a, admin_session):
        """
        WRONG-VERSION E. The disabled check runs BEFORE the second factor, so a
        holder of a perfectly valid code is still refused -- a build that
        reordered them would admit a disabled account into a live session.
        """
        target = dao.create_user(org_a, "gone@a.example", PASSWORD)
        enrolment = dao.enrol_totp(admin_session, target)
        dao.set_user_disabled(admin_session, target, True)
        conn.commit()
        with pytest.raises(AuthenticationError):
            dao.login("gone@a.example", org_a, PASSWORD, totp_code=_code_for(enrolment.secret, clock.now()))
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sessions WHERE user_id = %s AND terminated_at IS NULL", (target,))
            assert cur.fetchone()[0] == 0

    def test_enrolment_does_not_resurrect_a_terminated_session(self, dao, conn, clock, org_a, admin_session):
        target = dao.create_user(org_a, "zombie@a.example", PASSWORD)
        victim = dao.login("zombie@a.example", org_a, PASSWORD)
        dao.set_user_disabled(admin_session, target, True)
        conn.commit()
        dao.enrol_totp(admin_session, target)
        conn.commit()
        from clinical.data_access import SessionExpired

        with pytest.raises((SessionExpired, NotFoundError, AuthenticationError)):
            dao.session_valid(victim.session_id)

    def test_a_locked_out_account_is_refused_even_with_a_valid_code(self, dao, conn, clock, org_a, admin_session):
        target = dao.create_user(org_a, "locked@a.example", PASSWORD)
        enrolment = dao.enrol_totp(admin_session, target)
        conn.commit()
        for _ in range(5):
            with pytest.raises(AuthenticationError):
                dao.login("locked@a.example", org_a, "wrong password entirely")
        conn.commit()
        with pytest.raises(AuthenticationError):
            dao.login("locked@a.example", org_a, PASSWORD, totp_code=_code_for(enrolment.secret, clock.now()))

    def test_a_system_account_cannot_pass_mfa_into_a_session(self, dao, conn, org_a, admin_session):
        target = dao.create_user(org_a, "system@a.example", PASSWORD)
        enrolment = dao.enrol_totp(admin_session, target)
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_system_account = TRUE WHERE user_id = %s", (target,))
        conn.commit()
        with pytest.raises(AuthenticationError):
            dao.login("system@a.example", org_a, PASSWORD, totp_code=enrolment.backup_codes[0])


# ─── structural guards (NO database fixture: Rule 10) ────────────────────────


class TestStructuralGuards:
    def test_enrol_totp_does_not_take_the_secret_as_a_parameter(self):
        """
        The inversion of w155's finding. `enrol_totp` used to accept the
        secret, which meant the CALLER chose it; it now generates one.
        """
        tree = ast.parse(DATA_ACCESS_MODULE.read_text(encoding="utf-8"))
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "enrol_totp")
        names = [a.arg for a in node.args.args]
        assert "secret" not in names, names
        assert "generate_totp_secret" in ast.unparse(node)

    def test_the_users_table_carries_the_replay_guard_column(self):
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        start = schema.index("CREATE TABLE users")
        table = schema[start : schema.index(");", start)]
        assert "totp_last_used_step" in table, table
        # The twin: the column is not merely somewhere in a 126 kB file.
        assert "totp_last_used_step" in schema[: schema.index("CREATE TABLE totp_backup_codes")]

    def test_migration_0006_declares_a_post_condition(self):
        path = DATA_ACCESS_MODULE.parent / "migrations" / "0006_totp_replay_guard.sql"
        body = path.read_text(encoding="utf-8")
        assert "-- MIGRATION-ASSERT:" in body
        assert "totp_last_used_step" in body

    def test_the_second_factor_is_verified_inside_the_domain_method(self):
        """
        RULE 11, structurally. The gate lives in `DataAccess.login`; a build
        that moved it into `clinical/auth_endpoints.py` would leave every HTTP
        row in this file green.

        BY SYNTAX TREE, AND THE FIRST DRAFT OF THIS ROW GOT IT WRONG in
        exactly the way the standing rules warn about: it asserted
        `"totp_secret" not in auth_endpoints_source`, and FAILED against the
        correct build -- because `auth_endpoints.py` DISCUSSES the second
        factor at length in its `StaffLoginRequest` docstring and in
        `_refuse_uniformly`'s. A substring scan credits prose as code. What is
        asserted instead is the set of NAMES the module actually references,
        which is what an implementation would have to change.
        """
        tree = ast.parse(DATA_ACCESS_MODULE.read_text(encoding="utf-8"))
        login = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "login")
        assert "_verify_second_factor" in ast.unparse(login)

        auth_tree = ast.parse((DATA_ACCESS_MODULE.parent / "auth_endpoints.py").read_text(encoding="utf-8"))
        referenced = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(auth_tree)
            if isinstance(node, (ast.Name, ast.Attribute))
        }
        assert "_verify_second_factor" not in referenced
        assert "matching_step" not in referenced
        assert "totp_secret" not in referenced
        # The twin: the route module DOES reference the field it forwards, so
        # the name set is not empty and this scan is not blind.
        assert "totp_code" in referenced

    def test_the_key_variable_is_documented_for_a_deployer(self):
        env_example = DATA_ACCESS_MODULE.parent.parent / ".env.example"
        assert TOTP_SECRET_KEYS_ENV in env_example.read_text(encoding="utf-8")

    def test_cryptography_is_a_declared_dependency(self):
        """
        RULE 5. `FernetSecretCipher` imports `cryptography` lazily, exactly as
        `BcryptHasher` imports bcrypt, so an undeclared dependency would not
        show at startup -- it would show at the first enrolment.
        """
        requirements = (DATA_ACCESS_MODULE.parent / "requirements.txt").read_text(encoding="utf-8")
        assert any(line.strip().startswith("cryptography") for line in requirements.splitlines())


def _generic() -> str:
    from clinical.data_access import GENERIC_AUTH_ERROR

    return GENERIC_AUTH_ERROR


def _secret_of(dao, conn, email: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT totp_secret FROM users WHERE email = %s", (email,))
        return FernetSecretCipher([KEY_A]).decrypt(cur.fetchone()[0])


# ─── the HTTP surface ────────────────────────────────────────────────────────
#
# RULE 11, STATED BEFORE THE ROWS RATHER THAN AFTER. Every row in this class
# stayed GREEN against the wrong version in which `_require_role` was lifted
# out of `enrol_totp` and into the route as a dependency. They are here because
# the surface has to exist and has to refuse; they are NOT the evidence that
# the gate is in the method. That evidence is
# `TestEnrolment::test_enrolment_refuses_a_non_administrator_called_directly`.

APP_PASSWORD = "test-app-password"
RETENTION_PASSWORD = "test-retention-password"
STAFF_COOKIE = "clinical_session"
CLIENT_IP = "203.0.113.7"

# The two refusals this surface owns, as (status, RAW BODY BYTES) -- Rule 8:
# an oracle can live in either channel, so both are compared, as a set.
UNAUTHENTICATED = (401, b'{"detail":"Authentication failed."}')
FORBIDDEN = (403, b'{"detail":"Not permitted."}')
NOT_FOUND = (404, b'{"detail":"Not found."}')

NOWHERE = "00000000-0000-0000-0000-000000000000"
MALFORMED = "not-a-uuid"


def _pair(response):
    return (response.status_code, response.content)


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
    # THE KEY, SUPPLIED THE WAY A DEPLOYMENT SUPPLIES IT. Not injected into a
    # DataAccess: these rows go through `clinical.app`, which builds its own
    # per request from `_get_data_access`, so the ONLY way the cipher can
    # reach them is the environment variable. That makes this fixture the
    # end-to-end proof that the configured key is what production reads.
    monkeypatch.setenv(TOTP_SECRET_KEYS_ENV, KEY_A)
    return DSN


@pytest.fixture(autouse=True)
def reset_the_login_throttle():
    from clinical.auth_endpoints import _LOGIN_THROTTLE

    _LOGIN_THROTTLE.reset()
    yield
    _LOGIN_THROTTLE.reset()


@pytest.fixture()
def seeded(clinical_dsn):
    """
    One organisation, an Administrator, a non-administrator, and a target.

    THE SESSIONS ARE MINTED HERE AND PASSED AS COOKIES PER REQUEST, which is
    what every other HTTP file in this package does and it is not a
    convenience: `_set_session_cookie` sets `secure=True`, so a cookie set
    over the TestClient's plain http:// is accepted by the browser model and
    NEVER SENT BACK. A client relying on the jar therefore arrives at the next
    route anonymous, and the route answers 401 -- which looks exactly like an
    authorisation failure and is not one. (That cost me the first draft of
    this class; it is written down so it does not cost the next reader.)
    """
    import psycopg

    from clinical.data_access import BcryptHasher, DataAccess

    connection = psycopg.connect(clinical_dsn, autocommit=False, connect_timeout=10)
    dao = DataAccess(connection, password_hasher=BcryptHasher(rounds=4))
    org_id = dao.create_organisation("Org A")
    admin_id = _make_admin(dao, connection, org_id, "admin@a.example")
    orderer_id = dao.create_user(org_id, "orderer@a.example", PASSWORD)
    target_id = dao.create_user(org_id, "target@a.example", PASSWORD)
    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Orderer', %s, %s)",
            (orderer_id, org_id, admin_id, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)),
        )
    connection.commit()
    admin = dao.login("admin@a.example", org_id, PASSWORD, ip_address=CLIENT_IP)
    orderer = dao.login("orderer@a.example", org_id, PASSWORD, ip_address=CLIENT_IP)
    connection.commit()
    connection.close()
    return {
        "org_id": str(org_id),
        "target_id": str(target_id),
        "admin": {STAFF_COOKIE: str(admin.session_id)},
        "orderer": {STAFF_COOKIE: str(orderer.session_id)},
    }


@pytest.fixture()
def client(clinical_dsn):
    from fastapi.testclient import TestClient

    from clinical.app import app

    return TestClient(app, client=(CLIENT_IP, 45678), headers={"User-Agent": "w164-test/1.0"})


def _enrol(client, user_id, credential=None):
    return client.post(f"/admin/users/{user_id}/totp", cookies=credential or {})


def _login(client, email, org_id, totp_code=None):
    body = {"email": email, "org_id": org_id, "password": PASSWORD}
    if totp_code is not None:
        body["totp_code"] = totp_code
    return client.post("/auth/login", json=body)


@requires_db
class TestTheHttpSurface:
    def test_the_enrolment_route_is_served(self):
        """
        STANDING RULE 9, and the corrected form of it: `app.routes` on this
        stack holds opaque `_IncludedRouter` wrappers, so the naive census
        reports a surface of two. `served_routes` descends into them, and the
        non-vacuity assertion below refuses both the empty answer and the
        wrapper-shaped one.

        NO DATABASE IS TOUCHED by this row -- it reads the app object -- but
        it sits in this class because it is about the HTTP surface. It takes
        no database fixture, so it cannot skip (Rule 10).
        """
        from clinical.app import app
        from clinical.tests.test_w132_structural_guards import served_routes

        surface = served_routes(app)
        assert len(surface) > 5, surface
        assert ("/admin/users/{user_id}/totp", ("POST",)) in surface, surface

    def test_an_anonymous_caller_is_refused(self, client, seeded):
        assert _pair(_enrol(client, seeded["target_id"])) == UNAUTHENTICATED

    def test_a_non_administrator_is_refused(self, client, seeded):
        assert _pair(_enrol(client, seeded["target_id"], seeded["orderer"])) == FORBIDDEN

    def test_a_refused_enrolment_enrols_nobody(self, client, seeded):
        _enrol(client, seeded["target_id"], seeded["orderer"])
        assert _login(client, "target@a.example", seeded["org_id"]).status_code == 204

    def test_an_administrator_enrols_and_then_logs_in_with_the_code(self, client, seeded):
        """
        THE WHOLE WAVE, END TO END, THROUGH THE SERVED SURFACE: enrol over
        HTTP, then authenticate over HTTP with a code computed from what
        enrolment returned. Nothing is injected anywhere -- the cipher the app
        used came from the environment variable and nothing else.
        """
        response = _enrol(client, seeded["target_id"], seeded["admin"])
        assert response.status_code == 201, response.text
        body = response.json()
        assert sorted(body) == ["backup_codes", "secret"]
        assert len(body["backup_codes"]) == 8

        assert _pair(_login(client, "target@a.example", seeded["org_id"])) == UNAUTHENTICATED
        code = _code_for(body["secret"], dt.datetime.now(dt.timezone.utc))
        assert _login(client, "target@a.example", seeded["org_id"], code).status_code == 204

    def test_a_replayed_code_is_refused_over_http_too(self, client, seeded):
        body = _enrol(client, seeded["target_id"], seeded["admin"]).json()
        code = _code_for(body["secret"], dt.datetime.now(dt.timezone.utc))
        assert _login(client, "target@a.example", seeded["org_id"], code).status_code == 204
        assert _pair(_login(client, "target@a.example", seeded["org_id"], code)) == UNAUTHENTICATED

    def test_the_secret_never_appears_in_a_second_response(self, client, seeded):
        """
        Issued once and never readable again. The whole administrative read
        surface is swept, not one endpoint, because "it is not in the response
        I thought of" is not the claim.
        """
        secret = _enrol(client, seeded["target_id"], seeded["admin"]).json()["secret"]
        audit = client.get("/admin/audit", cookies=seeded["admin"])
        assert audit.status_code in (200, 403), audit.text
        assert secret.encode() not in audit.content

    def test_an_unknown_target_and_a_malformed_one_read_alike(self, client, seeded):
        refusals = {
            _pair(_enrol(client, NOWHERE, seeded["admin"])),
            _pair(_enrol(client, MALFORMED, seeded["admin"])),
        }
        assert refusals == {NOT_FOUND}, refusals


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# MIGRATION 0006 -- the post-condition, and the proof that it is CHECKABLE
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#
# WHY A MIGRATION WAS REQUIRED AT ALL, since the gate says to justify one:
# one-time use of a TOTP code means remembering which time-step was spent, and
# there was nowhere in this schema to remember it. `users` carried
# `totp_secret` and `totp_enrolled_at`; `totp_backup_codes` is a different
# credential with its own `used_at`. An in-process memo would not survive a
# restart and would not be shared between replicas.


REPLAY_COLUMN = "totp_last_used_step"
REPLAY_VERSION = 6
REPLAY_NAME = "totp_replay_guard"


def _the_migration():
    from clinical import migrate

    by_version = {m.version: m for m in migrate.discover()}
    assert REPLAY_VERSION in by_version, "migration 0006 is not on disk"
    return by_version[REPLAY_VERSION]


def _migrations_below_six():
    from clinical import migrate

    return tuple(m for m in migrate.discover() if m.version < REPLAY_VERSION)


def _evaluate(connection, expression: str) -> bool:
    with connection.cursor() as cur:
        cur.execute(f"SELECT ({expression})")  # noqa: S608 -- the expression is this repository's own file
        return bool(cur.fetchone()[0])


class TestMigration0006Declaration:
    """No database. These rows read files and cannot skip (Rule 10)."""

    def test_it_declares_a_post_condition(self):
        from clinical import migrate

        assert migrate.post_conditions(_the_migration()), "0006 declares nothing"

    def test_the_post_condition_reads_pg_catalog_and_names_the_type(self):
        """
        THE RULE'S SHAPE, NOT ITS LETTER. `to_regclass('users')` would satisfy
        w160 perfectly and measure nothing -- `users` exists in every
        generation of this schema there has ever been. So the declaration is
        required to read the ATTRIBUTE out of `pg_catalog`, to exclude dropped
        columns and system attributes, and to name the type: a version that
        declared INTEGER would accept every step this decade and overflow in
        2038, on a mechanism whose whole job is refusing a code twice.
        """
        from clinical import migrate

        joined = " ".join(inv.expression for inv in migrate.post_conditions(_the_migration()))
        assert "pg_attribute" in joined, joined
        assert "information_schema" not in joined, joined
        assert REPLAY_COLUMN in joined, joined
        assert "attisdropped" in joined, joined
        assert "int8" in joined, joined

    def test_the_migration_is_additive(self):
        """
        Unlike 0005, this one drops nothing and narrows nothing. Asserted over
        the DDL with comment lines removed, because the header DISCUSSES 0005's
        DROP at length and a substring scan would credit the prose.
        """
        body = "\n".join(
            line for line in _the_migration().sql.splitlines() if not line.strip().startswith("--")
        ).upper()
        assert body.count("ADD COLUMN") == 1, body
        for forbidden in ("DROP ", "RENAME ", "DELETE ", "TRUNCATE ", "NOT NULL", "DEFAULT ", "NOT VALID"):
            assert forbidden not in body, f"0006 contains {forbidden.strip()!r}"

    def test_schema_sql_stamps_it(self):
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        assert f"({REPLAY_VERSION}, '{REPLAY_NAME}', '{_the_migration().checksum}')" in schema


@requires_db
class TestMigration0006IsCheckable:
    def test_the_post_condition_is_false_one_generation_back_and_true_after(self):
        """
        THE ROW THE MIGRATION TURNS ON. A post-condition already true before
        the migration ran measures nothing: it can never fail, so `/readyz`
        re-evaluating it on every request costs something and signals nothing.
        Both halves are asserted against ONE database, in order.
        """
        from clinical import migrate
        from clinical.tests.migration_db import connect, scratch_database

        expression = " AND ".join(f"({inv.expression})" for inv in migrate.post_conditions(_the_migration()))
        with scratch_database("w164_0006") as dsn, connect(dsn) as connection:
            migrate.migrate(connection, _migrations_below_six())
            assert _evaluate(connection, expression) is False, "the post-condition is true BEFORE 0006"

            # THROUGH 0006, NOT EVERYTHING ON DISK. This row is about the step
            # from generation 5 to generation 6; a later migration is not part of
            # that step, and `migrate(discover())` would apply all of them (7, 8, 9
            # and whatever follows) and make the assertion below fail every time
            # a migration is added. The set is derived, so it needs no edit later.
            through_six = tuple(m for m in migrate.discover() if m.version <= REPLAY_VERSION)
            applied = migrate.migrate(connection, through_six)
            assert applied == (REPLAY_VERSION,), applied
            assert _evaluate(connection, expression) is True
            assert migrate.inspect_state(connection, through_six).ok is True

    def test_an_existing_row_survives_the_upgrade_with_the_column_null(self):
        """
        Additive means nothing already stored is disturbed. A row planted
        BEFORE the migration must still be there after it, and must read NULL
        in the new column -- "no code has been accepted yet", which is the
        correct state for every account that predates MFA.
        """
        from clinical import migrate
        from clinical.tests.migration_db import connect, scratch_database

        with scratch_database("w164_0006_rows") as dsn, connect(dsn) as connection:
            migrate.migrate(connection, _migrations_below_six())
            with connection.cursor() as cur:
                cur.execute("INSERT INTO organisations (name, created_at) VALUES ('w164 Org', now()) RETURNING org_id")
                org_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO users (org_id, email, password_hash, password_changed_at, created_at, updated_at) "
                    "VALUES (%s, 'before@a.example', 'x', now(), now(), now()) RETURNING user_id",
                    (org_id,),
                )
                user_id = cur.fetchone()[0]
            connection.commit()

            migrate.migrate(connection, migrate.discover())

            with connection.cursor() as cur:
                cur.execute(
                    f"SELECT email, {REPLAY_COLUMN} FROM users WHERE user_id = %s",  # noqa: S608
                    (user_id,),
                )
                assert cur.fetchone() == ("before@a.example", None)

    def test_migrate_refuses_a_0006_whose_declaration_is_stripped_and_applies_nothing(self, tmp_path):
        """
        THE w160 RULE, PROVED TO BITE ON THIS VERY FILE -- the cheapest
        available confirmation that the declaration above is load-bearing
        rather than decorative. NO FILE IS WRITTEN under clinical/migrations/.
        """
        from clinical import migrate
        from clinical.tests.migration_db import connect, scratch_database

        directory = tmp_path / "migrations"
        directory.mkdir(parents=True)
        for path in sorted(migrate.MIGRATIONS_DIR.iterdir()):
            if path.is_file():
                (directory / path.name).write_bytes(path.read_bytes())
        target = directory / _the_migration().path.name
        text = target.read_text(encoding="utf-8")
        stripped = "\n".join(line for line in text.splitlines() if "MIGRATION-ASSERT" not in line)
        assert stripped != text, "0006 carries no MIGRATION-ASSERT line to strip"
        target.write_text(stripped + "\n", encoding="utf-8")

        silent = migrate.discover(directory)
        assert {p.name: p.read_bytes() for p in migrate.MIGRATIONS_DIR.iterdir() if p.is_file()} == {
            p.name: p.read_bytes() for p in migrate.MIGRATIONS_DIR.iterdir() if p.is_file()
        }
        with scratch_database("w164_silent") as dsn, connect(dsn) as connection:
            with pytest.raises(migrate.MigrationNotVerifiable) as raised:
                migrate.migrate(connection, silent)
            assert f"{REPLAY_VERSION:04d}_{REPLAY_NAME}" in str(raised.value)
            connection.rollback()
            assert migrate.history_table_exists(connection) is False

    def test_the_positive_twin_the_same_set_with_the_line_applies(self, tmp_path):
        """
        Rule 4's pairing. Without it a `migrate()` that refused EVERYTHING
        would satisfy the row above.
        """
        from clinical import migrate
        from clinical.tests.migration_db import connect, scratch_database

        directory = tmp_path / "migrations"
        directory.mkdir(parents=True)
        for path in sorted(migrate.MIGRATIONS_DIR.iterdir()):
            if path.is_file():
                (directory / path.name).write_bytes(path.read_bytes())
        restored = migrate.discover(directory)
        with scratch_database("w164_declared") as dsn, connect(dsn) as connection:
            assert REPLAY_VERSION in migrate.migrate(connection, restored)
            assert migrate.inspect_state(connection, restored).ok is True
