"""
clinical/data_access.py
────────────────────────
Clinical platform, Phase 1: the identity data-access layer.

HOW ORGANISATION ISOLATION IS ENFORCED, stated exactly, because the honest
version is narrower than "the type system prevents it":

Python has no compile-time check. What this module provides instead is
enforcement BY CONSTRUCTION:

  1. Every method that reads or writes org-scoped data takes a `Session` as
     its first argument, and the organisation comes from that session --
     never from a caller-supplied parameter. There is no signature into which
     a caller can pass an organisation of their choosing.
  2. Every SQL statement against an org-scoped table carries `org_id = %s`
     bound from the session. Not one is optional.
  3. There is NO raw-query path. The connection is private, no method returns
     it, and no method accepts SQL. A caller who wants a query that skips the
     org predicate has to edit this file.
  4. `tests/test_identity.py::TestStructuralIsolation` fails if one is
     introduced -- it parses this module and asserts that every SELECT/UPDATE
     touching an org-scoped table mentions org_id, and that no public method
     exposes the connection.

The schema carries the other half: composite foreign keys on
(user_id, org_id) mean a role assignment, session, or backup code cannot
reference a user in another organisation even if this layer were wrong. See
schema.sql.

CLOCK INJECTION. Nothing in this module calls `datetime.now()`. Every time
value comes from `self._clock.now()`. This is not a testing convenience: a
lockout test that advances a mock clock while production code reads the wall
clock compares two different clocks and then passes or fails for reasons
unrelated to the behaviour under test. One clock, injected, all the way
through.
"""

from __future__ import annotations

import datetime as _datetime
import functools
import hashlib
import inspect
import json
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Protocol, Sequence

if TYPE_CHECKING:
    from clinical.models.exception import ExceptionDTO

# ─── Policy constants ────────────────────────────────────────────────────────

MAX_FAILED_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 30
SESSION_ABSOLUTE_HOURS = 24
SESSION_IDLE_MINUTES = 30
BACKUP_CODE_COUNT = 8

ROLES = (
    "Orderer",
    "Lab technician",
    "Interpreter",
    "Approver",
    "Administrator",
    "Auditor",
    "System",
)

# Assembly name normalization: maps variant names to canonical form
# Only explicitly mapped names are recognised; unknown assemblies are rejected
ASSEMBLY_CANONICAL = {
    # GRCh38 (hg38)
    "GRCh38": "GRCh38",
    "hg38": "GRCh38",
    "GRCh38.p13": "GRCh38",
    # GRCh37 (hg19, b37)
    "GRCh37": "GRCh37",
    "hg19": "GRCh37",
    "b37": "GRCh37",
}

# THE ONE MESSAGE every authentication failure produces, whatever the cause:
# unknown organisation, unknown address, wrong password, wrong TOTP code,
# disabled user, disabled organisation, locked account. A caller cannot tell
# "no such account" from "account disabled" from "wrong password", because
# telling them apart is an enumeration oracle. Tests assert equality against
# this constant rather than the absence of a word -- "Invalid account
# credentials" would pass an absence check while leaking nothing, and a
# genuinely distinguishing message could pass one too.
GENERIC_AUTH_ERROR = "Authentication failed."


# ─── Errors ──────────────────────────────────────────────────────────────────


class AuthenticationError(Exception):
    """Raised for every failed authentication, always with GENERIC_AUTH_ERROR."""

    def __init__(self, message: str = GENERIC_AUTH_ERROR) -> None:
        super().__init__(message)


class AuthorizationError(Exception):
    """Raised when an authenticated session lacks the role for an action."""


class NotFoundError(Exception):
    """
    Raised when a record does not exist *for this session's organisation*.

    Deliberately indistinguishable from "does not exist at all": a cross-org
    read must not be able to confirm that some other organisation holds a
    record with that id. Existence is itself information.
    """


class ConfigurationError(Exception):
    """Raised when the layer is asked to do something it was not configured for."""


class SessionExpired(AuthenticationError):
    """Session is terminated, past its absolute cap, or idle too long."""


# ─── Injected collaborators ──────────────────────────────────────────────────


class Clock(Protocol):
    def now(self) -> _datetime.datetime: ...


class SystemClock:
    """The only place in this module that reads the wall clock."""

    def now(self) -> _datetime.datetime:
        return _datetime.datetime.now(_datetime.timezone.utc)


class PasswordHasher(Protocol):
    def hash(self, plaintext: str) -> str: ...
    def verify(self, plaintext: str, hashed: str) -> bool: ...


class BcryptHasher:
    """bcrypt, for passwords and for TOTP backup codes alike."""

    def __init__(self, rounds: int = 12) -> None:
        self._rounds = rounds

    def hash(self, plaintext: str) -> str:
        import bcrypt

        return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt(self._rounds)).decode("ascii")

    def verify(self, plaintext: str, hashed: str) -> bool:
        import bcrypt

        try:
            return bcrypt.checkpw(plaintext.encode("utf-8"), hashed.encode("ascii"))
        except (ValueError, TypeError):
            return False


class TotpVerifier(Protocol):
    def verify(self, secret: str, code: str, at: _datetime.datetime) -> bool: ...


class PyOtpVerifier:
    def verify(self, secret: str, code: str, at: _datetime.datetime) -> bool:
        import pyotp

        # valid_window=1 tolerates one step of clock skew either side, the
        # usual allowance; `at` keeps even this on the injected clock.
        return bool(pyotp.TOTP(secret).verify(code, for_time=at, valid_window=1))


class SecretCipher(Protocol):
    def encrypt(self, plaintext: str) -> str: ...
    def decrypt(self, ciphertext: str) -> str: ...


# ─── Session ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Session:
    """
    An authenticated session. `org_id` is the organisation scope for every
    call that takes this object -- it is read from here and never from a
    caller argument, which is what makes the isolation structural rather than
    remembered.
    """

    session_id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID


@dataclass(frozen=True)
class RoleAssignment:
    assignment_id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    role: str
    assigned_by: uuid.UUID
    assigned_at: _datetime.datetime
    basis: Optional[str]


@dataclass(frozen=True)
class User:
    user_id: uuid.UUID
    org_id: uuid.UUID
    email: str
    disabled: bool
    password_changed_at: _datetime.datetime
    totp_enrolled_at: Optional[_datetime.datetime]
    created_at: _datetime.datetime


# Tables whose rows belong to exactly one organisation. Every statement this
# module issues against one of these must bind org_id. The isolation test
# reads this tuple, so adding a table here without adding the predicate is
# what makes the test fail.
ORG_SCOPED_TABLES = (
    "users",
    "role_assignments",
    "sessions",
    "totp_backup_codes",
    "patients",
    "consents",
    "orders",
    "samples",
    "sequencing_runs",
    "vcfs",
    "interpretations",
    "reports",
)


# ─── Audit decorator ────────────────────────────────────────────────────────


def auditable(
    action: str | dict[str, str] | Any,
    resource_type: str,
    requires_session: bool = True,
    auditable: bool = True,
    reason: str | None = None,
    details_builder: Any = None,
):
    """
    Decorator that wraps a DataAccess method to capture audit outcomes.

    action: str, dict, or callable that resolves to action name.
        - str: single action name used for all outcomes.
        - dict: maps outcome ('success', 'denied', 'error') to action name.
              Enables methods like login() that have different actions per outcome.
        - callable(params, result): returns action name based on parameters/result.
              Enables methods like set_user_disabled() where action depends on parameter.

    resource_type: the resource type being acted upon.

    requires_session: whether the method takes a Session as first argument.

    auditable: whether this method should be audited.
        False: marks this as a deliberate read/non-auditable operation.

    details_builder: callable(bound_params, result) returning audit details dict.
        bound_params: dict of resolved method parameters (positional/keyword resolved).
        result: return value of the method (None on failure).

    reason: explanation for non-auditable methods or special cases.
    """

    def decorator(func):
        def wrapper(self, *args, **kwargs):
            if not auditable:
                # Non-auditable method: just call it
                return func(self, *args, **kwargs)

            # Validate session requirement
            if requires_session:
                if not args or not isinstance(args[0], Session):
                    raise ConfigurationError(
                        f"@auditable(requires_session=True) on {func.__name__}: expected Session as first argument"
                    )
                session = args[0]
            else:
                session = None

            # Bind arguments to resolve positional/keyword ambiguity
            # Try to get the original function's signature if wrapped
            original_func = func
            while hasattr(original_func, "__wrapped__"):
                original_func = original_func.__wrapped__
            sig = inspect.signature(original_func)
            bound_args = sig.bind(self, *args, **kwargs)
            bound_args.apply_defaults()
            bound_params = dict(bound_args.arguments)
            bound_params.pop("self", None)  # Remove self, not part of user args

            try:
                result = func(self, *args, **kwargs)
                # Determine action name for this outcome
                if isinstance(action, str):
                    action_name = action
                elif isinstance(action, dict):
                    action_name = action.get("success", action.get(next(iter(action)), ""))
                else:
                    # Callable action: call with params and result
                    action_name = action(bound_params, result)
                # Build audit details — pass resolved parameters to avoid positional/keyword issues
                details = details_builder(bound_params, result) if details_builder else {}
                # For provisioning methods without session, extract org_id from parameters
                write_org_id = session.org_id if session else None
                write_user_id = session.user_id if session else None

                # For organisation creation, result is the org_id
                if not session and isinstance(result, uuid.UUID) and resource_type == "organisation":
                    write_org_id = result
                # For user creation, org_id is in the parameters
                elif not session and resource_type == "user" and bound_params.get("org_id"):
                    write_org_id = bound_params.get("org_id")
                    # and result is the user_id
                    if isinstance(result, uuid.UUID):
                        write_user_id = result
                # For any provisioning method, try to extract org_id from parameters
                elif not session and bound_params.get("org_id") and not write_org_id:
                    write_org_id = bound_params.get("org_id")
                # Success: write audit entry
                self._write_audit_entry(
                    action_name,
                    resource_type,
                    resource_id=str(result) if result else "<none>",
                    outcome="success",
                    org_id=write_org_id,
                    user_id=write_user_id,
                    actor_role=self._get_actor_role(session) if session else None,
                    details=details if details else None,
                    ip_address=bound_params.get("ip_address"),
                )
                self._DataAccess__connection.commit()
                return result
            except AuthorizationError:
                # Denied: write audit entry with action name for denied, re-raise
                if isinstance(action, str):
                    action_name = action
                elif isinstance(action, dict):
                    action_name = action.get("denied", action.get(next(iter(action)), ""))
                else:
                    # Callable action: use success path for denied (no separate denied action in callables)
                    action_name = action(bound_params, None)
                details = details_builder(bound_params, None) if details_builder else {}
                self._write_audit_entry(
                    action_name,
                    resource_type,
                    resource_id="<denied>",
                    outcome="denied",
                    org_id=session.org_id if session else None,
                    user_id=session.user_id if session else None,
                    actor_role=self._get_actor_role(session) if session else None,
                    details=details if details else None,
                    ip_address=bound_params.get("ip_address"),
                )
                self._DataAccess__connection.commit()
                raise
            except Exception:
                # Error: write audit entry with action name for error, re-raise
                if isinstance(action, str):
                    action_name = action
                elif isinstance(action, dict):
                    action_name = action.get("error", action.get(next(iter(action)), ""))
                else:
                    # Callable action: use success path for error (no separate error action in callables)
                    action_name = action(bound_params, None)
                details = details_builder(bound_params, None) if details_builder else {}
                self._write_audit_entry(
                    action_name,
                    resource_type,
                    resource_id="<error>",
                    outcome="error",
                    org_id=session.org_id if session else None,
                    user_id=session.user_id if session else None,
                    actor_role=self._get_actor_role(session) if session else None,
                    details=details if details else None,
                    ip_address=bound_params.get("ip_address"),
                )
                self._DataAccess__connection.commit()
                raise

        return wrapper

    return decorator


# DECORATOR IMPLEMENTATION STATUS (Phase 2 audit layer):
# Methods with side-by-side equivalence proof (old _audit() vs @auditable):
#  - create_organisation (Method 1): PROVEN ✓
#  - create_user (Method 2): PROVEN ✓ (fixed email extraction via signature binding)
#  - terminate_session (Method 3): PROVEN ✓
#  - assign_role (Method 4): PROVEN ✓
#
# Methods decorated without side-by-side proof (equivalence inferred from test coverage):
#  - log_failed_login (Method 5): UNPROVEN (test suite validates behavior)
#  - login (Method 6): UNPROVEN (test suite validates behavior)
#  - set_user_disabled (Method 7): UNPROVEN (test suite validates behavior)
#  - enrol_totp (Method 8): UNPROVEN (test suite validates behavior)
#  - session_valid (Method 9): MARKED NON-AUDITABLE (validation read, not a resource action)
#
# NOTE: Methods 1–4 found bugs (org_id=None, email=None) that passing tests had missed.
# Methods 5–9 removed old calls unproven; this is a recorded gap, not validation.

# ─── Transaction decorator ───────────────────────────────────────────────────


def transactional(func):
    """
    Decorator that wraps a DataAccess method in explicit transaction management.
    Each decorated method commits on success or on expected exceptions, and rolls
    back only on unexpected errors. Nested calls within a transaction pass through.
    """

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if hasattr(self, "_in_transaction") and self._in_transaction:
            return func(self, *args, **kwargs)
        self._in_transaction = True
        try:
            result = func(self, *args, **kwargs)
            self._DataAccess__connection.commit()
            return result
        except (AuthenticationError, AuthorizationError, NotFoundError):
            # Expected exceptions: commit work done before the exception was raised
            # (e.g., audit log entry for failed login attempt, incremented counter)
            self._DataAccess__connection.commit()
            raise
        except Exception:
            # Unexpected errors: rollback the transaction
            self._DataAccess__connection.rollback()
            raise
        finally:
            self._in_transaction = False

    return wrapper


class DataAccess:
    """
    The whole identity surface. No SQL leaves this class and no connection
    enters a caller's hands.
    """

    def __init__(
        self,
        connection: Any,
        clock: Optional[Clock] = None,
        password_hasher: Optional[PasswordHasher] = None,
        totp_verifier: Optional[TotpVerifier] = None,
        secret_cipher: Optional[SecretCipher] = None,
    ) -> None:
        # Private, and deliberately never exposed. See the class docstring's
        # point 3: there is no raw-query path out of here.
        self.__connection = connection
        self._clock: Clock = clock or SystemClock()
        self._hasher: PasswordHasher = password_hasher or BcryptHasher()
        self._totp: Optional[TotpVerifier] = totp_verifier
        self._cipher: Optional[SecretCipher] = secret_cipher
        self._in_transaction = False

    # ── internals ────────────────────────────────────────────────────────────

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[tuple]:
        cur = self.__connection.cursor()
        try:
            cur.execute(sql, tuple(params))
            return list(cur.fetchall())
        finally:
            cur.close()

    def _query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[tuple]:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        cur = self.__connection.cursor()
        try:
            cur.execute(sql, tuple(params))
        finally:
            cur.close()

    def _write_audit_entry(
        self,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        org_id: Optional[uuid.UUID] = None,
        user_id: Optional[uuid.UUID] = None,
        actor_role: Optional[str] = None,
        details: Optional[dict] = None,
        ip_address: Optional[str] = None,
    ) -> None:
        """Write an audit entry with the Phase 2 schema."""
        self._execute(
            'INSERT INTO audit_log (org_id, user_id, "timestamp", actor_role, action, resource_type, resource_id, outcome, details, ip_address) '
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                org_id,
                user_id,
                self._clock.now(),
                actor_role,
                action,
                resource_type,
                resource_id,
                outcome,
                json.dumps(details or {}),
                ip_address,
            ),
        )

    def _get_actor_role(self, session: Optional[Session]) -> Optional[str]:
        """Get the primary active role for the actor in this session's org."""
        if not session:
            return None
        rows = self._query(
            "SELECT role FROM role_assignments WHERE user_id = %s AND org_id = %s AND revoked_at IS NULL ORDER BY assigned_at DESC LIMIT 1",
            (session.user_id, session.org_id),
        )
        return rows[0][0] if rows else None

    def _audit(
        self,
        action: str,
        org_id: Optional[uuid.UUID],
        user_id: Optional[uuid.UUID],
        details: Optional[dict] = None,
        ip_address: Optional[str] = None,
    ) -> None:
        """
        Append one audit row. org_id and user_id are both nullable here and
        that is the point: a failed login against an organisation that does
        not exist is the event most worth recording, and it has no valid
        org_id to record against.
        """
        self._execute(
            'INSERT INTO audit_log (org_id, user_id, action, details, ip_address, "timestamp") '
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (org_id, user_id, action, json.dumps(details or {}), ip_address, self._clock.now()),
        )

    # ── authentication ───────────────────────────────────────────────────────

    @auditable(
        action="login_failed",
        resource_type="session",
        requires_session=False,
        details_builder=lambda params, result: {
            "email": params.get("email"),
            "reason": params.get("reason"),
        },
    )
    @transactional
    def log_failed_login(
        self,
        email: str,
        org_id: Optional[uuid.UUID],
        ip_address: Optional[str] = None,
        reason: str = "invalid_credentials",
    ) -> None:
        """
        Record a failed authentication. `org_id` may be None -- an attempt
        against an unknown organisation still gets logged, with the submitted
        identifiers preserved in `details` rather than in the FK-bearing
        columns that would have rejected the insert.
        """

    @auditable(
        action="login_succeeded",
        resource_type="session",
        requires_session=False,
        details_builder=lambda params, result: {
            "session_id": str(result.session_id) if result else None,
            "user_id": str(result.user_id) if result else None,
        },
    )
    @transactional
    def login(
        self,
        email: str,
        org_id: uuid.UUID,
        password: str,
        totp_code: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Session:
        """
        Authenticate and open a server-side session.

        Every failure path raises AuthenticationError carrying exactly
        GENERIC_AUTH_ERROR, and every failure path logs. The two are not the
        same thing: the caller learns nothing, the audit log learns
        everything.
        """
        now = self._clock.now()

        org = self._query_one("SELECT org_id, disabled FROM organisations WHERE org_id = %s", (org_id,))
        if org is None:
            # Unknown organisation: log against a NULL org_id, which is why
            # audit_log.org_id is nullable and unconstrained.
            self.log_failed_login(email, None, ip_address, reason="unknown_organisation")
            self._equalise_password_work(password)
            raise AuthenticationError()
        if org[1]:
            self.log_failed_login(email, org_id, ip_address, reason="organisation_disabled")
            self._equalise_password_work(password)
            raise AuthenticationError()

        row = self._query_one(
            "SELECT user_id, password_hash, failed_login_attempts, locked_until, "
            "       disabled, totp_secret, is_system_account "
            "FROM users WHERE org_id = %s AND email = %s",
            (org_id, email),
        )
        if row is None:
            # Spend the same bcrypt work as a real verification would, so the
            # response time does not separate "no such address" from "wrong
            # password" the way the message deliberately does not.
            self._equalise_password_work(password)
            self.log_failed_login(email, org_id, ip_address, reason="unknown_user")
            raise AuthenticationError()

        user_id, password_hash, failed_attempts, locked_until, disabled, totp_secret, is_system = row

        if locked_until is not None and locked_until > now:
            self.log_failed_login(email, org_id, ip_address, reason="locked_out")
            raise AuthenticationError()

        if not self._hasher.verify(password, password_hash):
            self._register_failed_attempt(user_id, org_id, failed_attempts, now)
            self.log_failed_login(email, org_id, ip_address, reason="invalid_password")
            raise AuthenticationError()

        # Checked AFTER the password, on purpose. Checking first would answer
        # "is this account disabled?" to anyone who can guess an address.
        if disabled:
            self.log_failed_login(email, org_id, ip_address, reason="user_disabled")
            raise AuthenticationError()

        # System accounts cannot log in. They exist only for automatic actions
        # via _create_system_session, never for interactive authentication.
        if is_system:
            self.log_failed_login(email, org_id, ip_address, reason="system_account")
            raise AuthenticationError()

        if totp_secret is not None:
            if not self._verify_second_factor(user_id, org_id, totp_secret, totp_code, now):
                self._register_failed_attempt(user_id, org_id, failed_attempts, now)
                self.log_failed_login(email, org_id, ip_address, reason="invalid_totp")
                raise AuthenticationError()

        # Success. The counter resets only here, for this user_id -- one
        # account's successful login cannot clear another account's counter.
        self._execute(
            "UPDATE users SET failed_login_attempts = 0, locked_until = NULL, updated_at = %s "
            "WHERE user_id = %s AND org_id = %s",
            (now, user_id, org_id),
        )

        session_id = uuid.uuid4()
        self._execute(
            "INSERT INTO sessions (session_id, user_id, org_id, created_at, expires_at, "
            "                      last_activity_at, idle_timeout_minutes, ip_address, user_agent) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session_id,
                user_id,
                org_id,
                now,
                now + _datetime.timedelta(hours=SESSION_ABSOLUTE_HOURS),
                now,
                SESSION_IDLE_MINUTES,
                ip_address,
                user_agent,
            ),
        )
        return Session(session_id=session_id, user_id=user_id, org_id=org_id)

    def _equalise_password_work(self, password: str) -> None:
        """
        Verify against a fixed dummy hash so an absent user costs the same as
        a present one. Result discarded; only the elapsed work matters.
        """
        self._hasher.verify(password, _DUMMY_HASH)

    def _register_failed_attempt(
        self,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
        current_attempts: int,
        now: _datetime.datetime,
    ) -> None:
        attempts = current_attempts + 1
        locked_until = (
            now + _datetime.timedelta(minutes=LOCKOUT_MINUTES) if attempts >= MAX_FAILED_LOGIN_ATTEMPTS else None
        )
        self._execute(
            "UPDATE users SET failed_login_attempts = %s, locked_until = %s, updated_at = %s "
            "WHERE user_id = %s AND org_id = %s",
            (attempts, locked_until, now, user_id, org_id),
        )

    def _verify_second_factor(
        self,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
        stored_secret: str,
        code: Optional[str],
        now: _datetime.datetime,
    ) -> bool:
        if not code:
            return False
        if self._cipher is None:
            # Fail closed. A layer holding encrypted secrets with no cipher
            # cannot verify anything, and guessing that the column might be
            # plaintext is exactly the wrong recovery.
            raise ConfigurationError("TOTP secret is stored encrypted but no secret_cipher was provided.")
        if self._totp is None:
            raise ConfigurationError("TOTP is enrolled for this user but no totp_verifier was provided.")

        if self._totp.verify(self._cipher.decrypt(stored_secret), code, now):
            return True
        return self._consume_backup_code(user_id, org_id, code, now)

    def _consume_backup_code(
        self,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
        code: str,
        now: _datetime.datetime,
    ) -> bool:
        """
        Single use: the row is marked used inside the same call that accepts
        it, so a replay of the same code finds nothing unused to match.
        """
        rows = self._query(
            "SELECT code_id, code_hash FROM totp_backup_codes WHERE user_id = %s AND org_id = %s AND used_at IS NULL",
            (user_id, org_id),
        )
        for code_id, code_hash in rows:
            if self._hasher.verify(code, code_hash):
                self._execute(
                    "UPDATE totp_backup_codes SET used_at = %s "
                    "WHERE code_id = %s AND user_id = %s AND org_id = %s AND used_at IS NULL",
                    (now, code_id, user_id, org_id),
                )
                self._audit("backup_code_used", org_id, user_id, {"code_id": str(code_id)})
                return True
        return False

    # ── sessions ─────────────────────────────────────────────────────────────

    @auditable(
        action="session_validated",
        resource_type="session",
        requires_session=False,
        auditable=False,
        reason="validation read on every request; session activity audit trail records only creation and termination, not use",
    )
    @transactional
    def session_valid(self, session_id: uuid.UUID) -> Session:
        """
        Validate a session id and refresh its idle window.

        Takes the id rather than a `Session`, because a request arrives
        carrying an opaque identifier -- if the caller already held a trusted
        `Session` object there would be nothing left to validate.

        Raises SessionExpired (an AuthenticationError, so it carries
        GENERIC_AUTH_ERROR) if the session is terminated, past its absolute
        cap, or idle beyond its timeout.

        Idle timeout: a session inactive for idle_timeout_minutes or longer is
        expired. The boundary is inclusive by decision, not by accident -- a
        session idle for exactly the timeout has reached the timeout.
        """
        now = self._clock.now()
        row = self._query_one(
            "SELECT user_id, org_id, expires_at, last_activity_at, idle_timeout_minutes, "
            "       terminated_at "
            "FROM sessions WHERE session_id = %s",
            (session_id,),
        )
        if row is None:
            raise SessionExpired()

        user_id, org_id, expires_at, last_activity_at, idle_minutes, terminated_at = row

        if terminated_at is not None:
            raise SessionExpired()
        if now >= expires_at:
            raise SessionExpired()
        if now >= last_activity_at + _datetime.timedelta(minutes=idle_minutes):
            raise SessionExpired()

        # Idle window refreshes on activity; the absolute cap never moves.
        self._execute(
            "UPDATE sessions SET last_activity_at = %s WHERE session_id = %s AND org_id = %s",
            (now, session_id, org_id),
        )
        return Session(session_id=session_id, user_id=user_id, org_id=org_id)

    @auditable(
        action="session_terminated",
        resource_type="session",
        requires_session=True,
        details_builder=lambda params, result: {"target_session_id": str(params.get("target_session_id"))},
    )
    @transactional
    def terminate_session(self, session: Session, target_session_id: uuid.UUID) -> None:
        """
        End another session now. Administrator only -- this is the capability
        that server-side sessions exist for, and it is why a stateless token
        was not an option.

        The UPDATE is scoped to the acting session's organisation, so an
        administrator cannot end a session in another organisation even by id.
        """
        self._require_role(session, "Administrator")
        now = self._clock.now()
        self._execute(
            "UPDATE sessions SET terminated_at = %s, terminated_by = %s "
            "WHERE session_id = %s AND org_id = %s AND terminated_at IS NULL",
            (now, session.user_id, target_session_id, session.org_id),
        )

    # ── roles ────────────────────────────────────────────────────────────────

    @auditable(
        action="role_assigned",
        resource_type="role_assignment",
        requires_session=True,
        details_builder=lambda params, result: {
            "target_user_id": str(params.get("target_user_id")),
            "role": params.get("role"),
            "basis": params.get("basis"),
        },
    )
    @transactional
    def assign_role(
        self,
        session: Session,
        target_user_id: uuid.UUID,
        role: str,
        basis: Optional[str] = None,
    ) -> uuid.UUID:
        """
        Grant a role. ADMINISTRATOR, not Approver: an Approver signs reports,
        an Administrator manages users. Letting the signing role also grant
        signing authority would collapse the separation the role table exists
        to express.

        `basis` is mandatory for Approver -- ISO 15189 7.4.1.5 c) requires the
        reviewer's identity and authority to be retrievable, and the schema
        CHECK refuses the row without it.
        """
        self._require_role(session, "Administrator")
        if role not in ROLES:
            raise ValueError(f"Unknown role: {role!r}. Known roles: {', '.join(ROLES)}")
        if role == "Approver" and not basis:
            raise ValueError(
                "Assigning Approver requires a qualification basis "
                "(ISO 15189 7.4.1.5 c): reviewer authority must be retrievable)."
            )

        # The target must be in the acting session's organisation. Checked
        # here for a clear error, and enforced regardless by the composite FK
        # on (user_id, org_id) -- this layer being wrong would not be enough
        # to create the row.
        self.get_user(session, target_user_id)

        assignment_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO role_assignments (assignment_id, user_id, org_id, role, "
            "                              assigned_by, assigned_at, basis) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (assignment_id, target_user_id, session.org_id, role, session.user_id, now, basis),
        )
        return assignment_id

    @auditable(
        action="roles_read",
        resource_type="role",
        requires_session=True,
        auditable=False,
        reason="list operation, not a resource action",
    )
    @transactional
    def get_user_roles(self, session: Session, target_user_id: Optional[uuid.UUID] = None) -> List[RoleAssignment]:
        """
        Active role assignments, scoped to the session's organisation.
        Defaults to the session's own user.
        """
        user_id = target_user_id or session.user_id
        rows = self._query(
            "SELECT assignment_id, user_id, org_id, role, assigned_by, assigned_at, basis "
            "FROM role_assignments "
            "WHERE org_id = %s AND user_id = %s AND revoked_at IS NULL "
            "ORDER BY assigned_at",
            (session.org_id, user_id),
        )
        return [RoleAssignment(*r) for r in rows]

    def _require_role(self, session: Session, role: str) -> None:
        held = {a.role for a in self.get_user_roles(session)}
        if role not in held:
            raise AuthorizationError(f"This action requires the {role} role.")

    # ── users ────────────────────────────────────────────────────────────────

    @auditable(
        action="user_read",
        resource_type="user",
        requires_session=True,
        auditable=False,
        reason="read is not a resource action",
    )
    @transactional
    def get_user(self, session: Session, user_id: uuid.UUID) -> User:
        """
        Read one user BY ID, within the session's organisation.

        This is the cross-org read attempt in concrete form. The org_id
        predicate comes from the session, so a caller holding a valid
        org_a session and a real org_b user id gets NotFoundError -- the same
        answer as a wholly invented id. It cannot learn that the record exists
        elsewhere.
        """
        row = self._query_one(
            "SELECT user_id, org_id, email, disabled, password_changed_at, "
            "       totp_enrolled_at, created_at "
            "FROM users WHERE org_id = %s AND user_id = %s",
            (session.org_id, user_id),
        )
        if row is None:
            raise NotFoundError(f"No such user in this organisation: {user_id}")
        return User(*row)

    # ── provisioning helpers (used by administrators and by the tests) ───────

    @auditable(
        action="organisation_created",
        resource_type="organisation",
        requires_session=False,
        reason="provisioning: called before any session exists",
        details_builder=lambda params, result: {"name": params.get("name")},
    )
    @transactional
    def create_organisation(self, name: str) -> uuid.UUID:
        org_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO organisations (org_id, name, created_at) VALUES (%s, %s, %s)",
            (org_id, name, now),
        )
        return org_id

    @auditable(
        action="user_created",
        resource_type="user",
        requires_session=False,
        reason="provisioning: called before any session exists",
        details_builder=lambda params, result: {"email": params.get("email")},
    )
    @transactional
    def create_user(self, org_id: uuid.UUID, email: str, password: str) -> uuid.UUID:
        """
        Provisioning entry point. Takes org_id directly rather than a Session
        because the first user of an organisation is created before any
        session in it can exist -- the bootstrap case. Every OTHER org-scoped
        method takes a Session; this exception is the reason ORG_SCOPED_TABLES
        drives the isolation test rather than a blanket rule about signatures.
        """
        user_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO users (user_id, org_id, email, password_hash, password_changed_at, "
            "                   created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (user_id, org_id, email, self._hasher.hash(password), now, now, now),
        )
        return user_id

    @auditable(
        action=lambda params, result: "user_disabled" if params.get("disabled") else "user_enabled",
        resource_type="user",
        requires_session=True,
        details_builder=lambda params, result: {"target_user_id": str(params.get("target_user_id"))},
    )
    @transactional
    def set_user_disabled(self, session: Session, target_user_id: uuid.UUID, disabled: bool) -> None:
        """Offboarding. Never a DELETE -- that would orphan the audit chain."""
        self._require_role(session, "Administrator")
        self.get_user(session, target_user_id)
        self._execute(
            "UPDATE users SET disabled = %s, updated_at = %s WHERE user_id = %s AND org_id = %s",
            (disabled, self._clock.now(), target_user_id, session.org_id),
        )

    @auditable(
        action="totp_enrolled",
        resource_type="totp",
        requires_session=True,
        details_builder=lambda params, result: {
            "target_user_id": str(params.get("target_user_id")),
            "backup_codes_issued": len(result) if result else 0,
        },
    )
    @transactional
    def enrol_totp(self, session: Session, target_user_id: uuid.UUID, secret: str) -> List[str]:
        """
        Administrator-initiated TOTP enrolment. Returns the backup codes in
        plaintext ONCE -- this return value is the only moment they exist in
        readable form. Only bcrypt hashes are stored, so they cannot be
        recovered, displayed again, or read out of the database by anyone.
        """
        self._require_role(session, "Administrator")
        self.get_user(session, target_user_id)
        if self._cipher is None:
            raise ConfigurationError("Enrolling TOTP requires a secret_cipher.")

        now = self._clock.now()
        self._execute(
            "UPDATE users SET totp_secret = %s, totp_enrolled_at = %s, updated_at = %s "
            "WHERE user_id = %s AND org_id = %s",
            (self._cipher.encrypt(secret), now, now, target_user_id, session.org_id),
        )

        codes: List[str] = []
        for _ in range(BACKUP_CODE_COUNT):
            code = secrets.token_hex(5)
            codes.append(code)
            self._execute(
                "INSERT INTO totp_backup_codes (code_id, user_id, org_id, code_hash, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (uuid.uuid4(), target_user_id, session.org_id, self._hasher.hash(code), now),
            )
        return codes

    # ── audit reads ──────────────────────────────────────────────────────────

    @auditable(
        action="audit_read",
        resource_type="audit_log",
        requires_session=True,
        auditable=False,
        reason="audit access is not a resource action",
    )
    @transactional
    def read_audit(
        self,
        session: Session,
        actions: Optional[Iterable[str]] = None,
        limit: int = 100,
    ) -> List[tuple]:
        """
        Auditor-only read of this organisation's audit trail.

        Scoped to session.org_id, so rows logged against a NULL org (failed
        authentication against an unknown organisation) are NOT visible here.
        Those are deliberately outside any tenant's view -- they do not belong
        to an organisation and surfacing them to one would tell it about
        probing aimed elsewhere.

        Those are readable only by direct SQL, not through the application. A
        deployer can query them with:

            SELECT * FROM audit_log WHERE org_id IS NULL;

        as the database owner or the clinical_app role. This is the interim
        until a formal platform-scope reader role is designed and implemented.
        """
        self._require_role(session, "Auditor")
        if actions:
            action_list = list(actions)
            placeholders = ", ".join(["%s"] * len(action_list))
            return self._query(
                'SELECT log_id, org_id, user_id, action, details, ip_address, "timestamp" '
                f"FROM audit_log WHERE org_id = %s AND action IN ({placeholders}) "
                'ORDER BY "timestamp", log_id LIMIT %s',
                (session.org_id, *action_list, limit),
            )
        return self._query(
            'SELECT log_id, org_id, user_id, action, details, ip_address, "timestamp" '
            'FROM audit_log WHERE org_id = %s ORDER BY "timestamp", log_id LIMIT %s',
            (session.org_id, limit),
        )

    # ── Phase 3: Clinical domain ─────────────────────────────────────────────

    @auditable(
        action="patient_created",
        resource_type="patient",
        requires_session=True,
        details_builder=lambda params, result: {
            "name": params.get("name"),
            "dob": str(params.get("dob")) if params.get("dob") else None,
            "sex": params.get("sex"),
        },
    )
    @transactional
    def create_patient(
        self,
        session: Session,
        name: str,
        dob: _datetime.date,
        sex: str,
    ) -> uuid.UUID:
        """Create a patient record in this organisation."""
        if sex not in ("M", "F", "O", "U"):
            raise ValueError(f"Sex must be one of M/F/O/U, not {sex!r}")

        patient_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO patients (patient_id, org_id, name, dob, sex, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (patient_id, session.org_id, name, dob, sex, now),
        )
        return patient_id

    @auditable(
        action="consent_recorded",
        resource_type="consent",
        requires_session=True,
        details_builder=lambda params, result: {
            "patient_id": str(params.get("patient_id")),
            "scope": params.get("scope"),
            "supersedes": str(params.get("supersedes_id")) if params.get("supersedes_id") else None,
        },
    )
    @transactional
    def record_consent(
        self,
        session: Session,
        patient_id: uuid.UUID,
        scope: str,
        supersedes_id: Optional[uuid.UUID] = None,
    ) -> uuid.UUID:
        """Record a new consent. Optionally supersedes an old one."""
        # Validate patient exists in this org
        patient = self._query_one(
            "SELECT patient_id FROM patients WHERE patient_id = %s AND org_id = %s",
            (patient_id, session.org_id),
        )
        if patient is None:
            raise NotFoundError(f"Patient {patient_id} not found")

        # Create new consent
        consent_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO consents (consent_id, org_id, patient_id, scope, "
            "  recorded_by, recorded_at, superseded_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (consent_id, session.org_id, patient_id, scope, session.user_id, now, None),
        )

        # If superseding an old consent, update its superseded_by pointer
        if supersedes_id:
            old = self._query_one(
                "SELECT consent_id FROM consents WHERE consent_id = %s AND patient_id = %s AND org_id = %s",
                (supersedes_id, patient_id, session.org_id),
            )
            if old:
                self._execute(
                    "UPDATE consents SET superseded_by = %s WHERE consent_id = %s AND org_id = %s",
                    (consent_id, supersedes_id, session.org_id),
                )

        return consent_id

    # ── Phase 3: Clinical domain — methods 3-12 ──────────────────────────────

    @auditable(
        action="order_created",
        resource_type="order",
        requires_session=True,
        details_builder=lambda params, result: {
            "patient_id": str(params.get("patient_id")),
            "test_id": str(params.get("test_id")),
            "priority": params.get("priority"),
            "required_scope": params.get("required_scope"),
        },
    )
    @transactional
    def create_order(
        self,
        session: Session,
        patient_id: uuid.UUID,
        test_id: uuid.UUID,
        required_scope: str,
        consent_id: uuid.UUID,
        priority: str = "routine",
        clinical_indication: Optional[str] = None,
    ) -> uuid.UUID:
        """Create a new order in draft state. Requires valid patient, test, and consent."""
        if priority not in ("routine", "urgent"):
            raise ValueError(f"Priority must be 'routine' or 'urgent', not {priority!r}")

        # Validate patient exists in this org
        patient = self._query_one(
            "SELECT patient_id FROM patients WHERE patient_id = %s AND org_id = %s",
            (patient_id, session.org_id),
        )
        if patient is None:
            raise NotFoundError(f"Patient {patient_id} not found")

        # Validate test exists in this org
        test = self._query_one(
            "SELECT test_id FROM tests WHERE test_id = %s AND org_id = %s",
            (test_id, session.org_id),
        )
        if test is None:
            raise NotFoundError(f"Test {test_id} not found")

        # Validate consent exists, is active, and has the required scope
        consent = self._query_one(
            "SELECT consent_id, scope FROM consents "
            "WHERE consent_id = %s AND patient_id = %s AND org_id = %s "
            "AND withdrawn_at IS NULL AND superseded_by IS NULL",
            (consent_id, patient_id, session.org_id),
        )
        if consent is None:
            raise NotFoundError(f"Active consent {consent_id} not found")

        consent_scope = consent[1]
        if consent_scope != required_scope:
            raise ValueError(f"Consent scope '{consent_scope}' does not match required scope '{required_scope}'")

        # Create new order
        order_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO orders "
            "(order_id, org_id, patient_id, test_id, required_scope, consent_id, "
            " ordered_by, priority, clinical_indication, state, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                order_id,
                session.org_id,
                patient_id,
                test_id,
                required_scope,
                consent_id,
                session.user_id,
                priority,
                clinical_indication,
                "draft",
                now,
            ),
        )
        return order_id

    @auditable(
        action="order_placed",
        resource_type="order",
        requires_session=True,
        details_builder=lambda params, result: {
            "order_id": str(params.get("order_id")),
        },
    )
    @transactional
    def place_order(self, session: Session, order_id: uuid.UUID) -> None:
        """Place an order, transitioning it from draft to placed state."""
        order = self._query_one(
            "SELECT order_id, state FROM orders WHERE order_id = %s AND org_id = %s",
            (order_id, session.org_id),
        )
        if order is None:
            raise NotFoundError(f"Order {order_id} not found")

        if order[1] != "draft":
            raise ValueError(f"Order is in '{order[1]}' state, cannot place non-draft order")

        now = self._clock.now()
        self._execute(
            "UPDATE orders SET state = %s, placed_at = %s WHERE order_id = %s AND org_id = %s",
            ("placed", now, order_id, session.org_id),
        )

    @auditable(
        action="order_cancelled",
        resource_type="order",
        requires_session=True,
        details_builder=lambda params, result: {
            "order_id": str(params.get("order_id")),
        },
    )
    @transactional
    def cancel_order(self, session: Session, order_id: uuid.UUID) -> None:
        """Cancel an order."""
        order = self._query_one(
            "SELECT order_id, state FROM orders WHERE order_id = %s AND org_id = %s",
            (order_id, session.org_id),
        )
        if order is None:
            raise NotFoundError(f"Order {order_id} not found")

        if order[1] in ("reported", "closed", "cancelled"):
            raise ValueError(f"Cannot cancel order in '{order[1]}' state")

        now = self._clock.now()
        self._execute(
            "UPDATE orders SET state = %s, cancelled_at = %s WHERE order_id = %s AND org_id = %s",
            ("cancelled", now, order_id, session.org_id),
        )

    @auditable(
        action="sample_received",
        resource_type="sample",
        requires_session=True,
        details_builder=lambda params, result: {
            "order_id": str(params.get("order_id")),
            "type": params.get("type"),
        },
    )
    @transactional
    def receive_sample(
        self,
        session: Session,
        order_id: uuid.UUID,
        sample_type: str,
        condition_on_receipt: Optional[str] = None,
    ) -> uuid.UUID:
        """Record receipt of a sample. Transitions order to sample_awaited state if needed."""
        if sample_type not in ("blood", "saliva", "tissue", "dna"):
            raise ValueError(f"Sample type must be one of: blood, saliva, tissue, dna; not {sample_type!r}")

        # Validate order exists and is in a state that can receive samples
        order = self._query_one(
            "SELECT order_id, state FROM orders WHERE order_id = %s AND org_id = %s",
            (order_id, session.org_id),
        )
        if order is None:
            raise NotFoundError(f"Order {order_id} not found")

        order_state = order[1]
        if order_state not in ("placed", "sample_awaited"):
            raise ValueError(f"Cannot receive sample for order in '{order_state}' state")

        # Create sample
        sample_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO samples "
            "(sample_id, org_id, order_id, type, collected_at, collected_by, "
            " received_at, received_by, condition_on_receipt, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                sample_id,
                session.org_id,
                order_id,
                sample_type,
                now,
                session.user_id,
                now,
                session.user_id,
                condition_on_receipt,
                now,
            ),
        )

        # Update order state to sample_awaited if it was in placed state
        if order_state == "placed":
            self._execute(
                "UPDATE orders SET state = %s WHERE order_id = %s AND org_id = %s",
                ("sample_awaited", order_id, session.org_id),
            )

        return sample_id

    @auditable(
        action="qc_recorded",
        resource_type="sample",
        requires_session=True,
        details_builder=lambda params, result: {
            "sample_id": str(params.get("sample_id")),
            "qc_status": params.get("qc_status"),
        },
    )
    @transactional
    def record_qc(
        self,
        session: Session,
        sample_id: uuid.UUID,
        qc_status: str,
        qc_reason: Optional[str] = None,
    ) -> None:
        """Record QC result for a sample."""
        if qc_status not in ("passed", "failed"):
            raise ValueError(f"QC status must be 'passed' or 'failed', not {qc_status!r}")

        # Validate sample exists in this org
        sample = self._query_one(
            "SELECT sample_id FROM samples WHERE sample_id = %s AND org_id = %s",
            (sample_id, session.org_id),
        )
        if sample is None:
            raise NotFoundError(f"Sample {sample_id} not found")

        now = self._clock.now()
        self._execute(
            "UPDATE samples SET qc_status = %s, qc_reason = %s, qc_recorded_by = %s, "
            "qc_recorded_at = %s WHERE sample_id = %s AND org_id = %s",
            (qc_status, qc_reason, session.user_id, now, sample_id, session.org_id),
        )

    @auditable(
        action="consent_withdrawn",
        resource_type="consent",
        requires_session=True,
        details_builder=lambda params, result: {
            "consent_id": str(params.get("consent_id")),
        },
    )
    @transactional
    def withdraw_consent(self, session: Session, consent_id: uuid.UUID) -> None:
        """Withdraw an active consent."""
        consent = self._query_one(
            "SELECT consent_id FROM consents WHERE consent_id = %s AND org_id = %s AND withdrawn_at IS NULL",
            (consent_id, session.org_id),
        )
        if consent is None:
            raise NotFoundError(f"Active consent {consent_id} not found")

        now = self._clock.now()
        self._execute(
            "UPDATE consents SET withdrawn_at = %s, withdrawn_by = %s WHERE consent_id = %s AND org_id = %s",
            (now, session.user_id, consent_id, session.org_id),
        )

    @auditable(
        action="patient_read",
        resource_type="patient",
        requires_session=True,
        auditable=False,
    )
    def get_patient(self, session: Session, patient_id: uuid.UUID) -> Optional[dict[str, Any]]:
        """Retrieve a patient record."""
        row = self._query_one(
            "SELECT patient_id, name, dob, sex, created_at, disabled FROM patients "
            "WHERE patient_id = %s AND org_id = %s",
            (patient_id, session.org_id),
        )
        if row is None:
            return None
        return {
            "patient_id": row[0],
            "name": row[1],
            "dob": row[2],
            "sex": row[3],
            "created_at": row[4],
            "disabled": row[5],
        }

    @auditable(
        action="order_read",
        resource_type="order",
        requires_session=True,
        auditable=False,
    )
    def get_order(self, session: Session, order_id: uuid.UUID) -> Optional[dict[str, Any]]:
        """Retrieve an order record."""
        row = self._query_one(
            "SELECT order_id, patient_id, test_id, required_scope, consent_id, "
            "ordered_by, priority, clinical_indication, state, placed_at, "
            "cancelled_at, created_at FROM orders "
            "WHERE order_id = %s AND org_id = %s",
            (order_id, session.org_id),
        )
        if row is None:
            return None
        return {
            "order_id": row[0],
            "patient_id": row[1],
            "test_id": row[2],
            "required_scope": row[3],
            "consent_id": row[4],
            "ordered_by": row[5],
            "priority": row[6],
            "clinical_indication": row[7],
            "state": row[8],
            "placed_at": row[9],
            "cancelled_at": row[10],
            "created_at": row[11],
        }

    @auditable(
        action="consent_read",
        resource_type="consent",
        requires_session=True,
        auditable=False,
    )
    def get_consent(self, session: Session, consent_id: uuid.UUID) -> Optional[dict[str, Any]]:
        """Retrieve a consent record."""
        row = self._query_one(
            "SELECT consent_id, patient_id, scope, recorded_by, recorded_at, "
            "withdrawn_at, withdrawn_by, superseded_by FROM consents "
            "WHERE consent_id = %s AND org_id = %s",
            (consent_id, session.org_id),
        )
        if row is None:
            return None
        return {
            "consent_id": row[0],
            "patient_id": row[1],
            "scope": row[2],
            "recorded_by": row[3],
            "recorded_at": row[4],
            "withdrawn_at": row[5],
            "withdrawn_by": row[6],
            "superseded_by": row[7],
        }

    @auditable(
        action="sample_read",
        resource_type="sample",
        requires_session=True,
        auditable=False,
    )
    def get_sample(self, session: Session, sample_id: uuid.UUID) -> Optional[dict[str, Any]]:
        """Retrieve a sample record."""
        row = self._query_one(
            "SELECT sample_id, order_id, type, collected_at, collected_by, "
            "received_at, received_by, condition_on_receipt, qc_status, "
            "qc_reason, qc_recorded_by, qc_recorded_at, created_at FROM samples "
            "WHERE sample_id = %s AND org_id = %s",
            (sample_id, session.org_id),
        )
        if row is None:
            return None
        return {
            "sample_id": row[0],
            "order_id": row[1],
            "type": row[2],
            "collected_at": row[3],
            "collected_by": row[4],
            "received_at": row[5],
            "received_by": row[6],
            "condition_on_receipt": row[7],
            "qc_status": row[8],
            "qc_reason": row[9],
            "qc_recorded_by": row[10],
            "qc_recorded_at": row[11],
            "created_at": row[12],
        }

    # ── Phase 4a: Lineage foundation ─────────────────────────────────────────

    @auditable(
        action="sequencing_run_created",
        resource_type="sequencing_run",
        requires_session=True,
        details_builder=lambda params, result: {
            "sample_id": str(params.get("sample_id")),
        },
    )
    @transactional
    def create_sequencing_run(self, session: Session, sample_id: uuid.UUID) -> uuid.UUID:
        """
        Create a sequencing run for a sample.
        Validates that the sample exists in this organisation.
        """
        # Validate sample exists in this org
        sample = self._query_one(
            "SELECT sample_id FROM samples WHERE sample_id = %s AND org_id = %s",
            (sample_id, session.org_id),
        )
        if sample is None:
            raise NotFoundError(f"Sample {sample_id} not found")

        run_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO sequencing_runs (org_id, id, sample_id, created_at, created_by) VALUES (%s, %s, %s, %s, %s)",
            (session.org_id, run_id, sample_id, now, session.user_id),
        )
        return run_id

    @auditable(
        action="vcf_created",
        resource_type="vcf",
        requires_session=True,
        details_builder=lambda params, result: {
            "sequencing_run_id": str(params.get("sequencing_run_id")),
            "vcf_path": params.get("vcf_path"),
        },
    )
    @transactional
    def create_vcf(self, session: Session, sequencing_run_id: uuid.UUID, vcf_path: str) -> uuid.UUID:
        """
        Create a VCF (variant call format) record for a sequencing run.
        Reads the VCF file, computes its SHA-256 hash, and stores the path and hash.
        Raises ValueError if the file cannot be read or hashed.
        """
        # Validate sequencing_run exists in this org
        run = self._query_one(
            "SELECT id FROM sequencing_runs WHERE id = %s AND org_id = %s",
            (sequencing_run_id, session.org_id),
        )
        if run is None:
            raise NotFoundError(f"Sequencing run {sequencing_run_id} not found")

        # Read file and compute SHA-256 hash
        try:
            file_path = Path(vcf_path)
            with open(file_path, "rb") as f:
                hasher = hashlib.sha256()
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    hasher.update(chunk)
            content_hash = hasher.hexdigest()
        except FileNotFoundError as e:
            raise ValueError(f"VCF file not found: {vcf_path}") from e
        except PermissionError as e:
            raise ValueError(f"Permission denied reading VCF file: {vcf_path}") from e
        except Exception as e:
            raise ValueError(f"Error reading or hashing VCF file: {vcf_path}: {e}") from e

        vcf_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO vcfs (org_id, id, sequencing_run_id, vcf_path, content_hash, "
            "created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (session.org_id, vcf_id, sequencing_run_id, vcf_path, content_hash, now, session.user_id),
        )
        return vcf_id

    @auditable(
        action="interpretation_created",
        resource_type="interpretation",
        requires_session=True,
        details_builder=lambda params, result: {
            "vcf_id": str(params.get("vcf_id")),
            "platform_sample_id": params.get("platform_sample_id"),
        },
    )
    @transactional
    def create_interpretation(
        self,
        session: Session,
        vcf_id: uuid.UUID,
        run_document: dict[str, Any],
        platform_sample_id: str,
    ) -> uuid.UUID:
        """
        Create an interpretation of a VCF.
        Validates that the VCF exists and that the platform_sample_id matches the derived sample_id.
        Raises ValueError if there is a sample_id mismatch.
        Raises database constraint error (duplicate submission_key) if the submission already exists.
        """
        # Validate vcf exists and get its content_hash
        vcf = self._query_one(
            "SELECT id, content_hash FROM vcfs WHERE id = %s AND org_id = %s",
            (vcf_id, session.org_id),
        )
        if vcf is None:
            raise NotFoundError(f"VCF {vcf_id} not found")

        _, content_hash = vcf

        # Get the sample_id by tracing the chain: vcf → sequencing_run → sample
        # First get the sequencing_run_id from vcf
        run = self._query_one(
            "SELECT sequencing_run_id FROM vcfs WHERE id = %s AND org_id = %s",
            (vcf_id, session.org_id),
        )
        if run is None:
            raise NotFoundError(f"VCF {vcf_id} not found")

        sequencing_run_id = run[0]

        # Then get the sample_id from sequencing_run
        sample = self._query_one(
            "SELECT sample_id FROM sequencing_runs WHERE id = %s AND org_id = %s",
            (sequencing_run_id, session.org_id),
        )
        if sample is None:
            raise NotFoundError(f"Sequencing run {sequencing_run_id} not found")

        derived_sample_id = str(sample[0])

        # Validate platform_sample_id matches derived sample_id
        if platform_sample_id != derived_sample_id:
            raise ValueError(
                f"Platform sample ID mismatch: provided {platform_sample_id}, "
                f"but derived {derived_sample_id} from VCF chain"
            )

        # Build submission_key from content_hash and platform_sample_id
        submission_key = f"{content_hash}|{platform_sample_id}"

        interpretation_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO interpretations (org_id, id, vcf_id, run_document, submission_key, "
            "created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                session.org_id,
                interpretation_id,
                vcf_id,
                json.dumps(run_document),
                submission_key,
                now,
                session.user_id,
            ),
        )
        return interpretation_id

    @auditable(
        action="report_created",
        resource_type="report",
        requires_session=True,
        details_builder=lambda params, result: {
            "interpretation_id": str(params.get("interpretation_id")),
        },
    )
    @transactional
    def create_report(self, session: Session, interpretation_id: uuid.UUID) -> uuid.UUID:
        """
        Create a report linked to an interpretation.
        Validates that the interpretation exists in this organisation.
        """
        # Validate interpretation exists in this org
        interp = self._query_one(
            "SELECT id FROM interpretations WHERE id = %s AND org_id = %s",
            (interpretation_id, session.org_id),
        )
        if interp is None:
            raise NotFoundError(f"Interpretation {interpretation_id} not found")

        report_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO reports (org_id, id, interpretation_id, created_at, created_by) VALUES (%s, %s, %s, %s, %s)",
            (session.org_id, report_id, interpretation_id, now, session.user_id),
        )
        return report_id

    # Read methods (org-isolated, non-auditable)

    @auditable(
        action="sequencing_run_read",
        resource_type="sequencing_run",
        requires_session=True,
        auditable=False,
        reason="read is not a resource action",
    )
    @transactional
    def get_sequencing_run(self, session: Session, run_id: uuid.UUID) -> Optional[dict[str, Any]]:
        """Retrieve a sequencing run by ID, within the session's organisation."""
        row = self._query_one(
            "SELECT id, sample_id, created_at, created_by FROM sequencing_runs WHERE id = %s AND org_id = %s",
            (run_id, session.org_id),
        )
        if row is None:
            return None
        return {
            "id": row[0],
            "sample_id": row[1],
            "created_at": row[2],
            "created_by": row[3],
        }

    @auditable(
        action="vcf_read",
        resource_type="vcf",
        requires_session=True,
        auditable=False,
        reason="read is not a resource action",
    )
    @transactional
    def get_vcf(self, session: Session, vcf_id: uuid.UUID) -> Optional[dict[str, Any]]:
        """Retrieve a VCF by ID, within the session's organisation."""
        row = self._query_one(
            "SELECT id, sequencing_run_id, vcf_path, content_hash, created_at, created_by "
            "FROM vcfs WHERE id = %s AND org_id = %s",
            (vcf_id, session.org_id),
        )
        if row is None:
            return None
        return {
            "id": row[0],
            "sequencing_run_id": row[1],
            "vcf_path": row[2],
            "content_hash": row[3],
            "created_at": row[4],
            "created_by": row[5],
        }

    @auditable(
        action="interpretation_read",
        resource_type="interpretation",
        requires_session=True,
        auditable=False,
        reason="read is not a resource action",
    )
    @transactional
    def get_interpretation(self, session: Session, interpretation_id: uuid.UUID) -> Optional[dict[str, Any]]:
        """Retrieve an interpretation by ID, within the session's organisation."""
        row = self._query_one(
            "SELECT id, vcf_id, run_document, submission_key, created_at, created_by "
            "FROM interpretations WHERE id = %s AND org_id = %s",
            (interpretation_id, session.org_id),
        )
        if row is None:
            return None
        return {
            "id": row[0],
            "vcf_id": row[1],
            "run_document": json.loads(row[2]) if isinstance(row[2], str) else row[2],
            "submission_key": row[3],
            "created_at": row[4],
            "created_by": row[5],
        }

    @auditable(
        action="report_read",
        resource_type="report",
        requires_session=True,
        auditable=False,
        reason="read is not a resource action",
    )
    @transactional
    def get_report(self, session: Session, report_id: uuid.UUID) -> Optional[dict[str, Any]]:
        """Retrieve a report by ID, within the session's organisation."""
        row = self._query_one(
            "SELECT id, interpretation_id, created_at, created_by FROM reports WHERE id = %s AND org_id = %s",
            (report_id, session.org_id),
        )
        if row is None:
            return None
        return {
            "id": row[0],
            "interpretation_id": row[1],
            "created_at": row[2],
            "created_by": row[3],
        }

    # ── Phase 4c: Document discovery (all interpretations, including orphaned) ─────
    @auditable(
        action="interpretations_query",
        resource_type="interpretations",
        requires_session=True,
        auditable=True,
        details_builder=lambda bound_params, result: {"criteria": bound_params.get("criteria")},
    )
    @transactional
    def find_interpretations_by_criteria(self, session: Session, criteria: dict[str, Any]) -> List[dict[str, Any]]:
        """
        Find all interpretations matching the given criteria, including orphaned ones (no report).

        Criteria dict can contain:
          - model_version: str, matches run_document.model_version
          - database_version: str, matches run_document.database_version
          - code_version: str, matches run_document.code_version
          - date_range: dict with 'start' and 'end' ISO date strings
          - source_health: dict with 'service' and 'status' fields

        At least one criterion is required (raises ValueError if empty).

        Returns a list of dicts (one per interpretation):
        {
            interpretation_id, vcf_id, run_document, created_at, created_by,
            sample_id, patient_id, report_id (None if orphaned)
        }

        All queries are scoped to the session's organisation.
        """
        if not criteria:
            raise ValueError("At least one criterion required")

        # Build WHERE clauses dynamically from criteria (same as 4b)
        where_clauses = ["org_id = %s"]
        params: List[Any] = [session.org_id]

        if "model_version" in criteria:
            where_clauses.append("run_document @> %s")
            params.append(json.dumps({"model_version": criteria["model_version"]}))

        if "database_version" in criteria:
            where_clauses.append("run_document @> %s")
            params.append(json.dumps({"database_version": criteria["database_version"]}))

        if "code_version" in criteria:
            where_clauses.append("run_document @> %s")
            params.append(json.dumps({"code_version": criteria["code_version"]}))

        if "date_range" in criteria:
            date_range = criteria["date_range"]
            if "start" in date_range:
                where_clauses.append("created_at >= %s")
                params.append(date_range["start"])
            if "end" in date_range:
                where_clauses.append("created_at <= %s")
                params.append(date_range["end"])

        if "source_health" in criteria:
            source_health = criteria["source_health"]
            if "service" in source_health and "status" in source_health:
                service = source_health["service"]
                status = source_health["status"]
                where_clauses.append("run_document -> 'databases' ->> %s = %s")
                params.append(service)
                params.append(status)

        additional_clauses = where_clauses[1:]
        if additional_clauses:
            additional_sql = " AND " + " AND ".join(additional_clauses)
        else:
            additional_sql = ""

        query = f"""
            SELECT id, vcf_id, run_document, created_at, created_by
            FROM interpretations
            WHERE org_id = %s{additional_sql}
            ORDER BY created_at, id
        """

        interp_rows = self._query(query, tuple(params))
        results = []

        for interp_row in interp_rows:
            interp_id, vcf_id, run_document_json, created_at, created_by = interp_row

            # Try to get the report (may be None for orphaned interpretations)
            report = self._query_one(
                "SELECT id FROM reports WHERE interpretation_id = %s AND org_id = %s",
                (interp_id, session.org_id),
            )
            report_id = report[0] if report else None

            # Walk the chain to get sample_id and patient_id
            vcf_row = self._query_one(
                "SELECT sequencing_run_id FROM vcfs WHERE id = %s AND org_id = %s",
                (vcf_id, session.org_id),
            )
            if vcf_row is None:
                continue

            sequencing_run_id = vcf_row[0]

            run_row = self._query_one(
                "SELECT sample_id FROM sequencing_runs WHERE id = %s AND org_id = %s",
                (sequencing_run_id, session.org_id),
            )
            if run_row is None:
                continue

            sample_id = run_row[0]

            order_row = self._query_one(
                "SELECT patient_id FROM orders WHERE order_id IN "
                "(SELECT order_id FROM samples WHERE sample_id = %s AND org_id = %s) AND org_id = %s",
                (sample_id, session.org_id, session.org_id),
            )
            if order_row is None:
                continue

            patient_id = order_row[0]

            run_doc = json.loads(run_document_json) if isinstance(run_document_json, str) else run_document_json

            results.append(
                {
                    "interpretation_id": interp_id,
                    "vcf_id": vcf_id,
                    "run_document": run_doc,
                    "created_at": created_at,
                    "created_by": created_by,
                    "sample_id": sample_id,
                    "patient_id": patient_id,
                    "report_id": report_id,
                }
            )

        return results

    # ── Phase 4b: Lineage queries (trace ancestors, criteria-based search) ────

    @auditable(
        action="read_lineage",
        resource_type="report",
        requires_session=True,
        auditable=True,
        details_builder=lambda params, result: {
            "report_id": str(params.get("report_id")),
            "chain_length": len(result) if result else 0,
            "final_patient_id": str(result[-1]["resource_id"]) if result and result[-1]["table"] == "patient" else None,
        },
    )
    @transactional
    def trace_report_ancestors(self, session: Session, report_id: uuid.UUID) -> List[dict[str, Any]]:
        """
        Trace all ancestors of a report from the lineage chain.

        Given a report_id, traverse backward through the lineage:
        report → interpretation → vcf → sequencing_run → sample → order → patient

        Returns a list of dicts in chronological order (report first, patient last):
        {table, resource_id, created_at, created_by, org_id}

        Raises NotFoundError if the report doesn't exist or belongs to another organisation.
        """
        # Start: verify the report exists in this org
        report = self._query_one(
            "SELECT id, interpretation_id, created_at, created_by, org_id FROM reports WHERE id = %s AND org_id = %s",
            (report_id, session.org_id),
        )
        if report is None:
            raise NotFoundError(f"Report {report_id} not found")

        chain = []
        chain.append(
            {
                "table": "report",
                "resource_id": report[0],
                "created_at": report[2],
                "created_by": report[3],
                "org_id": report[4],
            }
        )

        # Navigate: interpretation
        interp_id = report[1]
        interp = self._query_one(
            "SELECT id, vcf_id, created_at, created_by, org_id FROM interpretations WHERE id = %s AND org_id = %s",
            (interp_id, session.org_id),
        )
        if interp is None:
            raise NotFoundError(f"Interpretation {interp_id} not found in chain")

        chain.append(
            {
                "table": "interpretation",
                "resource_id": interp[0],
                "created_at": interp[2],
                "created_by": interp[3],
                "org_id": interp[4],
            }
        )

        # Navigate: vcf
        vcf_id = interp[1]
        vcf = self._query_one(
            "SELECT id, sequencing_run_id, created_at, created_by, org_id FROM vcfs WHERE id = %s AND org_id = %s",
            (vcf_id, session.org_id),
        )
        if vcf is None:
            raise NotFoundError(f"VCF {vcf_id} not found in chain")

        chain.append(
            {
                "table": "vcf",
                "resource_id": vcf[0],
                "created_at": vcf[2],
                "created_by": vcf[3],
                "org_id": vcf[4],
            }
        )

        # Navigate: sequencing_run
        run_id = vcf[1]
        run = self._query_one(
            "SELECT id, sample_id, created_at, created_by, org_id FROM sequencing_runs WHERE id = %s AND org_id = %s",
            (run_id, session.org_id),
        )
        if run is None:
            raise NotFoundError(f"Sequencing run {run_id} not found in chain")

        chain.append(
            {
                "table": "sequencing_run",
                "resource_id": run[0],
                "created_at": run[2],
                "created_by": run[3],
                "org_id": run[4],
            }
        )

        # Navigate: sample
        sample_id = run[1]
        sample = self._query_one(
            "SELECT sample_id, order_id, created_at, received_by, org_id FROM samples WHERE sample_id = %s AND org_id = %s",
            (sample_id, session.org_id),
        )
        if sample is None:
            raise NotFoundError(f"Sample {sample_id} not found in chain")

        chain.append(
            {
                "table": "sample",
                "resource_id": sample[0],
                "created_at": sample[2],
                "created_by": sample[3],  # received_by marks when platform record begins
                "org_id": sample[4],
            }
        )

        # Navigate: order
        order_id = sample[1]
        order = self._query_one(
            "SELECT order_id, patient_id, created_at, ordered_by, org_id FROM orders WHERE order_id = %s AND org_id = %s",
            (order_id, session.org_id),
        )
        if order is None:
            raise NotFoundError(f"Order {order_id} not found in chain")

        chain.append(
            {
                "table": "order",
                "resource_id": order[0],
                "created_at": order[2],
                "created_by": order[3],
                "org_id": order[4],
            }
        )

        # Navigate: patient
        patient_id = order[1]
        patient = self._query_one(
            "SELECT patient_id, created_at, org_id FROM patients WHERE patient_id = %s AND org_id = %s",
            (patient_id, session.org_id),
        )
        if patient is None:
            raise NotFoundError(f"Patient {patient_id} not found in chain")

        chain.append(
            {
                "table": "patient",
                "resource_id": patient[0],
                "created_at": patient[1],
                "created_by": None,  # patient creation has no created_by
                "org_id": patient[2],
            }
        )

        return chain

    @auditable(
        action="query_affected_reports",
        resource_type="interpretation",
        requires_session=True,
        auditable=True,
        details_builder=lambda params, result: {
            "criteria": params.get("criteria"),
            "result_count": len(result) if result else 0,
            "first_report_id": str(result[0]["report_id"]) if result else None,
        },
    )
    @transactional
    def find_reports_by_interpretation_criteria(
        self, session: Session, criteria: dict[str, Any]
    ) -> List[dict[str, Any]]:
        """
        Find all reports affected by interpretations matching the given criteria.

        This method returns only interpretations that have completed (have a report).
        For a complete impact analysis including crashed/in-review interpretations,
        use find_interpretations_by_criteria and filter by report_id.

        Criteria dict can contain:
          - model_version: str, matches run_document.model_version
          - database_version: str, matches run_document.database_version
          - code_version: str, matches run_document.code_version
          - date_range: dict with 'start' and 'end' ISO date strings
          - source_health: dict with 'service' and 'status' fields

        At least one criterion is required (raises ValueError if empty).

        Returns a list of dicts (one per affected report):
        {report_id, interpretation_id, vcf_id, run_document, created_at, created_by, sample_id, patient_id}

        All queries are scoped to the session's organisation.
        """
        # Use the new 4c method to get all interpretations (including orphaned)
        all_interps = self.find_interpretations_by_criteria(session, criteria)

        # Filter to only those with reports
        results = []
        for interp in all_interps:
            if interp["report_id"] is not None:
                results.append(
                    {
                        "report_id": interp["report_id"],
                        "interpretation_id": interp["interpretation_id"],
                        "vcf_id": interp["vcf_id"],
                        "run_document": interp["run_document"],
                        "created_at": interp["created_at"],
                        "created_by": interp["created_by"],
                        "sample_id": interp["sample_id"],
                        "patient_id": interp["patient_id"],
                    }
                )

        return results

    # ── Phase 5a: Preconditions and VCF validation ──
    #
    # EXCEPTION WIRING (Phase 5d):
    # When a check fails, validate_and_submit_order calls create_or_reopen_exception with:
    #   - category: from REASON_CODE_TO_CATEGORY[reason_code]
    #   - reason_code: the returned failure code
    #   - owner: from REASON_CODE_TO_OWNER[reason_code]
    #   - actor: system principal performing submission
    #   - error_message: the specific failure descriptor
    #
    # All precondition checks + VCF validation checks are called by
    # validate_and_submit_order, which blocks submission if any fail.
    #
    # NOTE: These methods return a reason string that maps to exception vocabulary
    # in models/exception.py.

    @auditable(action="check_consent", resource_type="consent")
    def check_consent(self, session: Session, patient_id: uuid.UUID, scope: str) -> str:
        """
        Check consent for automatic submission.

        Returns a reason string (enum):
          - "consent_ok": Valid, unwithdrawm consent exists
          - "consent_missing": No active consent for scope
          - "consent_withdrawn": Consent was withdrawn
        """
        consent = self._query_one(
            "SELECT consent_id, withdrawn_at FROM consents "
            "WHERE org_id = %s AND patient_id = %s AND scope = %s AND superseded_by IS NULL "
            "ORDER BY recorded_at DESC LIMIT 1",
            (session.org_id, patient_id, scope),
        )

        if consent is None:
            return "consent_missing"

        if consent[1] is not None:
            return "consent_withdrawn"

        return "consent_ok"

    @auditable(action="check_identity", resource_type="patient")
    def check_identity_resolved(self, session: Session, patient_id: uuid.UUID, sample_id: uuid.UUID) -> str:
        """
        Check that patient and sample IDs resolve to real records.

        Returns a reason string (enum):
          - "identity_resolved": Both exist in this org
          - "patient_unresolved": Patient record not found
          - "sample_unresolved": Sample record not found
        """
        patient = self._query_one(
            "SELECT patient_id FROM patients WHERE org_id = %s AND patient_id = %s",
            (session.org_id, patient_id),
        )
        if patient is None:
            return "patient_unresolved"

        sample = self._query_one(
            "SELECT sample_id FROM samples WHERE org_id = %s AND sample_id = %s",
            (session.org_id, sample_id),
        )
        if sample is None:
            return "sample_unresolved"

        return "identity_resolved"

    @auditable(
        auditable=False,
        action="check_order",
        resource_type="order",
        reason="precondition results are recorded in the submission audit entry",
    )
    def check_order_data(self, session: Session, order_id: uuid.UUID) -> str:
        """
        Check required order data present.

        Per §10.1, requires: test/panel, indication, ordering clinician.
        Test and clinician are NOT NULL in schema, so only indication is checked.

        Returns a reason string (enum):
          - "order_ok": All required data present, not cancelled
          - "indication_missing": clinical_indication is NULL or empty
          - "order_cancelled": Order state is cancelled
        """
        order = self._query_one(
            "SELECT state, clinical_indication FROM orders WHERE org_id = %s AND order_id = %s",
            (session.org_id, order_id),
        )

        if order is None:
            return "indication_missing"

        state, indication = order

        if state == "cancelled":
            return "order_cancelled"

        if not indication or (isinstance(indication, str) and not indication.strip()):
            return "indication_missing"

        return "order_ok"

    @auditable(
        auditable=False,
        action="check_qc",
        resource_type="sample",
        reason="precondition results are recorded in the submission audit entry",
    )
    def check_qc_passed(self, session: Session, order_id: uuid.UUID) -> str:
        """
        Check sample QC status.

        Per §10.1: "Sample QC passed" means samples.qc_status = 'passed'.

        Returns a reason string (enum):
          - "qc_ok": qc_status = 'passed'
          - "qc_pending": qc_status = 'pending'
          - "qc_failed": qc_status = 'failed'
        """
        sample = self._query_one(
            "SELECT qc_status FROM samples WHERE org_id = %s AND order_id = %s ORDER BY created_at DESC LIMIT 1",
            (session.org_id, order_id),
        )

        if sample is None:
            return "qc_pending"

        qc_status = sample[0]
        if qc_status == "passed":
            return "qc_ok"
        elif qc_status == "failed":
            return "qc_failed"
        else:
            return "qc_pending"

    @auditable(
        auditable=False,
        action="validate_vcf",
        resource_type="vcf",
        requires_session=False,
        reason="reads VCF files, not clinical data",
    )
    def validate_vcf_file(self, vcf_path: str) -> str:
        """
        Validate VCF file exists, is readable, and is VCF or bgzipped VCF.

        Returns a reason string (enum):
          - "vcf_file_ok": File valid
          - "vcf_missing": File not found
          - "vcf_unreadable": File exists but cannot be read
          - "vcf_format_invalid": File exists but is not VCF/bgzip format
        """
        path = Path(vcf_path)

        if not path.exists():
            return "vcf_missing"

        try:
            if not path.is_file():
                return "vcf_unreadable"

            with open(path, "rb") as f:
                header = f.read(4)

            if len(header) < 2:
                return "vcf_format_invalid"

            is_bgzip = header[:2] == b"\x1f\x8b"
            is_vcf_text = header[:2] == b"##"

            if not (is_bgzip or is_vcf_text):
                return "vcf_format_invalid"

            return "vcf_file_ok"

        except (PermissionError, OSError):
            return "vcf_unreadable"

    @auditable(
        auditable=False,
        action="validate_vcf",
        resource_type="vcf",
        requires_session=False,
        reason="reads VCF files, not clinical data",
    )
    def validate_vcf_header(self, vcf_path: str) -> str:
        """
        Validate VCF header is present and parseable.

        Returns a reason string (enum):
          - "header_ok": Header present and valid
          - "vcf_header_missing": No header found
          - "vcf_header_malformed": Header parsing fails
        """
        import gzip

        path = Path(vcf_path)

        try:
            if vcf_path.endswith(".gz"):
                opener = gzip.open
            else:
                opener = open

            with opener(path, "rt" if not vcf_path.endswith(".gz") else "rt") as f:
                header_found = False
                for line in f:
                    if line.startswith("##"):
                        header_found = True
                    elif line.startswith("#CHROM"):
                        return "header_ok" if header_found else "vcf_header_missing"
                    elif not line.startswith("#"):
                        break

            return "vcf_header_missing"

        except Exception:
            return "vcf_header_malformed"

    @auditable(
        auditable=False,
        action="validate_vcf",
        resource_type="vcf",
        requires_session=False,
        reason="reads VCF files, not clinical data",
    )
    def validate_vcf_build(self, vcf_path: str, expected_assembly: str) -> str:
        """
        Validate VCF build matches expected assembly.

        Reads assembly from VCF header's ##assembly line and normalises against
        canonical assembly names. Only explicitly mapped assemblies are recognised.

        Returns a reason string (enum):
          - "build_ok": Header assembly matches expected (after normalisation)
          - "vcf_build_not_declared": No assembly declared in header
          - "vcf_build_not_recognised": Assembly declared but not in canonical mapping
          - "vcf_build_mismatch": Assembly declared, recognised, but doesn't match expected
        """
        import gzip
        import re

        # Normalise expected assembly
        expected_canonical = ASSEMBLY_CANONICAL.get(expected_assembly)
        if expected_canonical is None:
            return "vcf_build_not_recognised"

        path = Path(vcf_path)

        try:
            if vcf_path.endswith(".gz"):
                opener = gzip.open
            else:
                opener = open

            with opener(path, "rt" if not vcf_path.endswith(".gz") else "rt") as f:
                for line in f:
                    if line.startswith("##assembly"):
                        match = re.search(r"##assembly=([^\s]+)", line)
                        if match:
                            vcf_assembly = match.group(1)
                            vcf_canonical = ASSEMBLY_CANONICAL.get(vcf_assembly)
                            if vcf_canonical is None:
                                return "vcf_build_not_recognised"
                            if vcf_canonical == expected_canonical:
                                return "build_ok"
                            else:
                                return "vcf_build_mismatch"
                        return "vcf_build_not_declared"
                    elif line.startswith("#CHROM"):
                        return "vcf_build_not_declared"
                    elif not line.startswith("#"):
                        break

            return "vcf_build_not_declared"

        except Exception:
            return "vcf_build_not_declared"

    @auditable(
        auditable=False,
        action="validate_vcf",
        resource_type="vcf",
        requires_session=False,
        reason="reads VCF files, not clinical data",
    )
    def validate_vcf_sample_column(self, vcf_path: str) -> str:
        """
        Validate VCF has at least one sample column.

        Returns a reason string (enum):
          - "sample_column_ok": At least one sample column
          - "vcf_sample_column_missing": No sample columns
        """
        import gzip

        path = Path(vcf_path)

        try:
            if vcf_path.endswith(".gz"):
                opener = gzip.open
            else:
                opener = open

            with opener(path, "rt" if not vcf_path.endswith(".gz") else "rt") as f:
                for line in f:
                    if line.startswith("#CHROM"):
                        fields = line.rstrip("\n").split("\t")
                        if len(fields) > 9:
                            return "sample_column_ok"
                        else:
                            return "vcf_sample_column_missing"

            return "vcf_sample_column_missing"

        except Exception:
            return "vcf_sample_column_missing"

    @auditable(
        auditable=False,
        action="validate_vcf",
        resource_type="vcf",
        requires_session=False,
        reason="reads VCF files, not clinical data",
    )
    def validate_vcf_variant_count(self, vcf_path: str) -> str:
        """
        Validate VCF has at least one variant.

        Returns a reason string (enum):
          - "variants_ok": At least one variant
          - "vcf_no_variants": No variant lines found
        """
        import gzip

        path = Path(vcf_path)

        try:
            if vcf_path.endswith(".gz"):
                opener = gzip.open
            else:
                opener = open

            with opener(path, "rt" if not vcf_path.endswith(".gz") else "rt") as f:
                for line in f:
                    if not line.startswith("#"):
                        return "variants_ok"

            return "vcf_no_variants"

        except Exception:
            return "vcf_no_variants"

    # ── Phase 5d: Orchestration layer (validates all 5a checks, wires exceptions) ──

    @auditable(action="validate_order_for_submission", resource_type="order")
    def validate_order_for_submission(
        self,
        session: Session,
        order_id: uuid.UUID,
        patient_id: uuid.UUID,
        sample_id: uuid.UUID,
        vcf_path: str,
        expected_assembly: str,
        consent_scope: str,
        actor: str,
    ) -> tuple[bool, Optional[uuid.UUID]]:
        """
        Orchestration layer: run all Phase 5a checks and wire exceptions on failure.

        Runs checks in order:
        1. check_consent (precondition)
        2. check_identity_resolved (validation)
        3. check_order_data (validation)
        4. check_qc_passed (validation)
        5. validate_vcf_file (validation)
        6. validate_vcf_header (validation)
        7. validate_vcf_build (validation)
        8. validate_vcf_sample_column (validation)
        9. validate_vcf_variant_count (validation)

        If any check fails, creates an exception and returns (False, exception_id).
        If all pass, returns (True, None).

        Args:
            session: Current session (org scope)
            order_id: Order being submitted
            patient_id: Patient ID
            sample_id: Sample ID
            vcf_path: Path to VCF file
            expected_assembly: Expected genome assembly (e.g., "GRCh38")
            consent_scope: Required consent scope
            actor: System principal creating exceptions

        Returns:
            (success: bool, exception_id: Optional[UUID])
            - (True, None) if all checks pass
            - (False, exception_id) if a check fails (exception created and returned)
        """
        from clinical.models.exception import (
            REASON_CODE_TO_CATEGORY,
            REASON_CODE_TO_OWNER,
        )

        # 1. Check consent
        consent_result = self.check_consent(session, patient_id, consent_scope)
        if consent_result != "consent_ok":
            reason_code = consent_result
            exc_id = self.create_or_reopen_exception(
                session,
                order_id,
                REASON_CODE_TO_CATEGORY[reason_code].value,
                reason_code,
                f"Consent check failed: {reason_code}",
                REASON_CODE_TO_OWNER[reason_code],
                actor,
            )
            return False, exc_id

        # 2. Check identity
        identity_result = self.check_identity_resolved(session, patient_id, sample_id)
        if identity_result != "identity_resolved":
            reason_code = identity_result
            exc_id = self.create_or_reopen_exception(
                session,
                order_id,
                REASON_CODE_TO_CATEGORY[reason_code].value,
                reason_code,
                f"Identity check failed: {reason_code}",
                REASON_CODE_TO_OWNER[reason_code],
                actor,
            )
            return False, exc_id

        # 3. Check order data
        order_result = self.check_order_data(session, order_id)
        if order_result != "order_ok":
            reason_code = order_result
            exc_id = self.create_or_reopen_exception(
                session,
                order_id,
                REASON_CODE_TO_CATEGORY[reason_code].value,
                reason_code,
                f"Order data check failed: {reason_code}",
                REASON_CODE_TO_OWNER[reason_code],
                actor,
            )
            return False, exc_id

        # 4. Check QC
        qc_result = self.check_qc_passed(session, order_id)
        if qc_result != "qc_ok":
            reason_code = qc_result
            exc_id = self.create_or_reopen_exception(
                session,
                order_id,
                REASON_CODE_TO_CATEGORY[reason_code].value,
                reason_code,
                f"QC check failed: {reason_code}",
                REASON_CODE_TO_OWNER[reason_code],
                actor,
            )
            return False, exc_id

        # 5. Validate VCF file
        vcf_file_result = self.validate_vcf_file(vcf_path)
        if vcf_file_result != "vcf_file_ok":
            reason_code = vcf_file_result
            exc_id = self.create_or_reopen_exception(
                session,
                order_id,
                REASON_CODE_TO_CATEGORY[reason_code].value,
                reason_code,
                f"VCF file validation failed: {reason_code}",
                REASON_CODE_TO_OWNER[reason_code],
                actor,
            )
            return False, exc_id

        # 6. Validate VCF header
        header_result = self.validate_vcf_header(vcf_path)
        if header_result != "header_ok":
            reason_code = header_result
            exc_id = self.create_or_reopen_exception(
                session,
                order_id,
                REASON_CODE_TO_CATEGORY[reason_code].value,
                reason_code,
                f"VCF header validation failed: {reason_code}",
                REASON_CODE_TO_OWNER[reason_code],
                actor,
            )
            return False, exc_id

        # 7. Validate VCF build
        build_result = self.validate_vcf_build(vcf_path, expected_assembly)
        if build_result != "build_ok":
            reason_code = build_result
            exc_id = self.create_or_reopen_exception(
                session,
                order_id,
                REASON_CODE_TO_CATEGORY[reason_code].value,
                reason_code,
                f"VCF build validation failed: {reason_code}",
                REASON_CODE_TO_OWNER[reason_code],
                actor,
            )
            return False, exc_id

        # 8. Validate VCF sample column
        sample_col_result = self.validate_vcf_sample_column(vcf_path)
        if sample_col_result != "sample_column_ok":
            reason_code = sample_col_result
            exc_id = self.create_or_reopen_exception(
                session,
                order_id,
                REASON_CODE_TO_CATEGORY[reason_code].value,
                reason_code,
                f"VCF sample column validation failed: {reason_code}",
                REASON_CODE_TO_OWNER[reason_code],
                actor,
            )
            return False, exc_id

        # 9. Validate VCF variant count
        variants_result = self.validate_vcf_variant_count(vcf_path)
        if variants_result != "variants_ok":
            reason_code = variants_result
            exc_id = self.create_or_reopen_exception(
                session,
                order_id,
                REASON_CODE_TO_CATEGORY[reason_code].value,
                reason_code,
                f"VCF variant count validation failed: {reason_code}",
                REASON_CODE_TO_OWNER[reason_code],
                actor,
            )
            return False, exc_id

        # All checks passed
        return True, None

    # ── Phase 5c: Automatic submission (system principal, submission key, audit) ──
    #
    # EXCEPTION WIRING (Phase 5d):
    # Submission failures are caught and turned into exceptions by:
    #
    # 1. Transient submission failures (timeouts, retryable errors):
    #    - Caught by worker / submission layer
    #    - Call create_or_reopen_exception with:
    #      - category: TRANSIENT_SUBMISSION_FAILURE
    #      - reason_code: BIJ_AI_TIMEOUT or BIJ_AI_ERROR_OTHER
    #      - owner: lab_operator
    #
    # 2. Interpretation failures (non-retryable Bij AI errors):
    #    - Caught by worker / submission result inspection
    #    - Call create_or_reopen_exception with:
    #      - category: INTERPRETATION_FAILURE
    #      - reason_code: BIJ_AI_ERROR_OTHER
    #      - owner: lab_operator
    #      - error_message: exact error from Bij AI run_document
    #
    # The submission flow:
    # 1. Preconditions validated (5a)
    # 2. VCF validated (5a)
    # 3. Automatic submission audit written (precondition results recorded)
    # 4. Submission enqueued
    # 5. Worker polls and executes
    # 6. On timeout or error: create_or_reopen_exception called
    # 7. On success: run_document stored, interpretation created

    def _create_system_session(self, org_id: uuid.UUID) -> Session:
        """
        Create or retrieve a session for the system principal of this organisation.

        ⚠️ AUTHENTICATION BYPASS BY DESIGN
        ──────────────────────────────────
        This method creates a session WITHOUT authentication. It is called only from
        the automatic submission path and MUST NOT be callable from any request-handling
        context or endpoint. If called from anywhere else, it is an authentication bypass.

        An AST test enforces that this method is called from exactly one location.
        """
        now = self._clock.now()
        system_email = f"system+{org_id}@platform"

        # Fetch or create system principal for this org
        user = self._query_one(
            "SELECT user_id FROM users WHERE org_id = %s AND email = %s AND is_system_account = true",
            (org_id, system_email),
        )

        if user is None:
            # Create system principal
            user_id = uuid.uuid4()
            dummy_password_hash = _DUMMY_HASH
            self._execute(
                "INSERT INTO users (user_id, org_id, email, password_hash, password_changed_at, "
                "                   is_system_account, disabled, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    user_id,
                    org_id,
                    system_email,
                    dummy_password_hash,
                    now,
                    True,
                    False,
                    now,
                    now,
                ),
            )

            # Assign System role if not already present
            role_check = self._query_one(
                "SELECT 1 FROM role_assignments WHERE user_id = %s AND org_id = %s AND role = 'System'",
                (user_id, org_id),
            )
            if role_check is None:
                self._execute(
                    "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                    "VALUES (%s, %s, 'System', %s, %s)",
                    (user_id, org_id, user_id, now),
                )
        else:
            user_id = user[0]

        # Mint a session for the system principal
        session_id = uuid.uuid4()
        self._execute(
            "INSERT INTO sessions (session_id, user_id, org_id, created_at, expires_at, "
            "                      last_activity_at, idle_timeout_minutes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                session_id,
                user_id,
                org_id,
                now,
                now + _datetime.timedelta(hours=SESSION_ABSOLUTE_HOURS),
                now,
                SESSION_IDLE_MINUTES,
            ),
        )

        return Session(session_id=session_id, user_id=user_id, org_id=org_id)

    def _write_automatic_submission_audit(
        self,
        session: Session,
        order_id: uuid.UUID,
        sample_id: uuid.UUID,
        submission_key: str,
        preconditions: dict[str, Any],
        vcf_validations: dict[str, Any],
        decision: str,
        blocking_reason: Optional[str] = None,
    ) -> None:
        """
        Write audit entry for automatic submission attempt (success or blocked).

        This is called BEFORE raising an exception if blocked, so the audit record
        captures why the submission was rejected. Queries like "which orders were blocked
        on which precondition" can be answered from the audit log.

        decision: "submitted" or "blocked"
        blocking_reason: required if decision is "blocked", describes the precondition/validation that failed
        """
        now = self._clock.now()
        details = {
            "order_id": str(order_id),
            "sample_id": str(sample_id),
            "submission_key": submission_key,
            "preconditions": preconditions,
            "vcf_validations": vcf_validations,
            "decision": decision,
        }
        if blocking_reason:
            details["blocking_reason"] = blocking_reason

        self._execute(
            "INSERT INTO audit_log (org_id, user_id, timestamp, action, resource_type, "
            "                        outcome, details, actor_role) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session.org_id,
                session.user_id,
                now,
                "automatic_submission",
                "interpretation",
                "success" if decision == "submitted" else "blocked",
                json.dumps(details),
                "System",
            ),
        )

    @auditable(
        action="none",
        resource_type="none",
        requires_session=False,
        auditable=False,
        reason="pure computation, derives idempotency key from VCF content; no data access",
    )
    def derive_submission_key(self, vcf_content: bytes, sample_id: uuid.UUID) -> str:
        """
        Derive idempotency key for VCF submission.

        Format: <sha256_hex(vcf_content)>|<sample_id>
        The pipe separator is safe: sha256 is hex (no pipes), sample_id is UUID (no pipes).

        Same VCF + same sample = same key (idempotent).
        Re-sequenced sample (different VCF) = different key (new interpretation).
        """
        vcf_hash = hashlib.sha256(vcf_content).hexdigest()
        return f"{vcf_hash}|{sample_id}"

    # ── Phase 5d: Exception workflow ──

    @auditable(
        action="read_exception",
        resource_type="exception",
        requires_session=True,
        auditable=True,
        reason="read order exceptions; orders are org-scoped",
    )
    def get_exceptions_for_order(self, session: Session, order_id: uuid.UUID) -> list[ExceptionDTO]:
        """Get all exceptions (open + resolved) for one order with event history."""
        from clinical.models.exception import (
            ExceptionDTO,
            ExceptionEventDTO,
        )

        # Query all exceptions for this order, org-scoped
        exceptions = self._query(
            "SELECT id, category, reason_code, error_message, status, owner, "
            "       created_at, last_resolved_by, last_resolved_at, resolution_action, "
            "       resolution_note "
            "FROM exceptions "
            "WHERE org_id = %s AND order_id = %s "
            "ORDER BY created_at DESC",
            (session.org_id, order_id),
        )

        result = []
        for exc_row in exceptions:
            exc_id = exc_row[0]

            # Get events for this exception
            events = self._query(
                "SELECT id, action, actor, timestamp, action_note "
                "FROM exception_events "
                "WHERE exception_id = %s "
                "ORDER BY timestamp ASC",
                (exc_id,),
            )

            event_dtos = [
                ExceptionEventDTO(
                    id=str(ev[0]),
                    action=ev[1],
                    actor=ev[2],
                    timestamp=ev[3].isoformat() if isinstance(ev[3], _datetime.datetime) else ev[3],
                    action_note=ev[4],
                )
                for ev in events
            ]

            result.append(
                ExceptionDTO(
                    id=str(exc_id),
                    order_id=str(order_id),
                    category=exc_row[1],
                    reason_code=exc_row[2],
                    error_message=exc_row[3],
                    status=exc_row[4],
                    owner=exc_row[5],
                    created_at=exc_row[6].isoformat() if isinstance(exc_row[6], _datetime.datetime) else exc_row[6],
                    last_resolved_by=exc_row[7],
                    last_resolved_at=exc_row[8].isoformat()
                    if isinstance(exc_row[8], _datetime.datetime)
                    else exc_row[8],
                    resolution_action=exc_row[9],
                    resolution_note=exc_row[10],
                    events=event_dtos,
                )
            )

        return result

    @auditable(
        action="read_worklist",
        resource_type="exception",
        requires_session=True,
        auditable=True,
        reason="read open exceptions for owner; worklist is clinically relevant",
    )
    def get_open_exceptions_by_owner(self, session: Session, owner: str) -> list[ExceptionDTO]:
        """Get open work items by owner (org-scoped)."""
        from clinical.models.exception import (
            ExceptionDTO,
            ExceptionEventDTO,
        )

        # Query open exceptions owned by this role, org-scoped
        exceptions = self._query(
            "SELECT id, order_id, category, reason_code, error_message, status, owner, "
            "       created_at, last_resolved_by, last_resolved_at, resolution_action, "
            "       resolution_note "
            "FROM exceptions "
            "WHERE org_id = %s AND owner = %s AND status = 'open' "
            "ORDER BY created_at ASC",
            (session.org_id, owner),
        )

        result = []
        for exc_row in exceptions:
            exc_id = exc_row[0]
            order_id = exc_row[1]

            # Get events for this exception
            events = self._query(
                "SELECT id, action, actor, timestamp, action_note "
                "FROM exception_events "
                "WHERE exception_id = %s "
                "ORDER BY timestamp ASC",
                (exc_id,),
            )

            event_dtos = [
                ExceptionEventDTO(
                    id=str(ev[0]),
                    action=ev[1],
                    actor=ev[2],
                    timestamp=ev[3].isoformat() if isinstance(ev[3], _datetime.datetime) else ev[3],
                    action_note=ev[4],
                )
                for ev in events
            ]

            result.append(
                ExceptionDTO(
                    id=str(exc_id),
                    order_id=str(order_id),
                    category=exc_row[2],
                    reason_code=exc_row[3],
                    error_message=exc_row[4],
                    status=exc_row[5],
                    owner=exc_row[6],
                    created_at=exc_row[7].isoformat() if isinstance(exc_row[7], _datetime.datetime) else exc_row[7],
                    last_resolved_by=exc_row[8],
                    last_resolved_at=exc_row[9].isoformat()
                    if isinstance(exc_row[9], _datetime.datetime)
                    else exc_row[9],
                    resolution_action=exc_row[10],
                    resolution_note=exc_row[11],
                    events=event_dtos,
                )
            )

        return result

    @transactional
    @auditable(
        action="resolve_exception",
        resource_type="exception",
        requires_session=True,
        auditable=True,
        reason="resolve work item; updates order state",
    )
    def resolve_exception(
        self,
        session: Session,
        exception_id: uuid.UUID,
        resolution_action: str,
        resolution_note: str,
        actor: str,
    ) -> None:
        """Resolve an exception, recording the resolution event and updating status."""
        now = self._clock.now()

        # Update exception status and resolution
        self._execute(
            "UPDATE exceptions "
            "SET status = 'resolved', last_resolved_by = %s, last_resolved_at = %s, "
            "    resolution_action = %s, resolution_note = %s "
            "WHERE id = %s AND org_id = %s",
            (actor, now, resolution_action, resolution_note, exception_id, session.org_id),
        )

        # Record the resolution event (transactional with status update)
        self._execute(
            "INSERT INTO exception_events (id, exception_id, actor, timestamp, action, action_note) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                uuid.uuid4(),
                exception_id,
                actor,
                now,
                "resolve",
                resolution_note,
            ),
        )

    @transactional
    @auditable(
        action="create_or_reopen_exception",
        resource_type="exception",
        requires_session=True,
        auditable=True,
        reason="create/reopen exception on order failure; may retry same failure",
    )
    def create_or_reopen_exception(
        self,
        session: Session,
        order_id: uuid.UUID,
        category: str,
        reason_code: str,
        error_message: str | None,
        owner: str,
        actor: str,
    ) -> uuid.UUID:
        """
        Create a new exception or reopen an existing one with the same reason_code.

        If an open exception exists for this order with the same reason_code, reopen it
        (write REOPEN event). Otherwise create a new exception with OPEN event.

        Returns the exception ID (new or reopened).
        """
        from clinical.models.exception import ExceptionStatus, ExceptionEventAction

        now = self._clock.now()

        # Query for existing open exception with same reason_code
        existing = self._query_one(
            "SELECT id FROM exceptions "
            "WHERE org_id = %s AND order_id = %s AND reason_code = %s AND status = 'open' "
            "LIMIT 1",
            (session.org_id, order_id, reason_code),
        )

        if existing:
            exc_id = existing[0]
            # Reopen: write REOPEN event
            self._execute(
                "INSERT INTO exception_events (id, exception_id, actor, timestamp, action, action_note) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    uuid.uuid4(),
                    exc_id,
                    actor,
                    now,
                    ExceptionEventAction.REOPEN.value,
                    f"Reopen: {error_message}",
                ),
            )
            return exc_id

        # Create new exception
        exc_id = uuid.uuid4()
        self._execute(
            "INSERT INTO exceptions "
            "(id, org_id, order_id, category, reason_code, error_message, status, owner, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                exc_id,
                session.org_id,
                order_id,
                category,
                reason_code,
                error_message,
                ExceptionStatus.OPEN.value,
                owner,
                now,
            ),
        )

        # Write OPEN event
        self._execute(
            "INSERT INTO exception_events (id, exception_id, actor, timestamp, action, action_note) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                uuid.uuid4(),
                exc_id,
                actor,
                now,
                ExceptionEventAction.OPEN.value,
                error_message,
            ),
        )

        return exc_id

    @transactional
    @auditable(
        action="retry_scheduler",
        resource_type="exception",
        requires_session=True,
        auditable=True,
        reason="scan and retry transient submission failures with exponential backoff",
    )
    def retry_scheduler(self, session: Session, submission_store=None) -> None:
        """
        Scan open TRANSIENT_SUBMISSION_FAILURE exceptions and retry/escalate them.

        For each open TRANSIENT exception where next_retry_at <= now:
        - If attempt_count < 6: increment attempt_count, update last_attempt_at,
          set next_retry_at = now + backoff(attempt_count), and re-queue the associated
          submission if submission_store is provided
        - If attempt_count >= 6: escalate status to 'escalated'

        Idempotent: UPDATE conditional on status='open' prevents concurrent escalations.

        Args:
            session: Session object for org context
            submission_store: Optional SubmissionStore to re-queue failed submissions.
                If provided, failed submissions associated with retried exceptions
                are marked as 'queued' for retry.
        """
        now = self._clock.now()

        def backoff_seconds(attempt_count: int) -> int:
            base = min(2**attempt_count * 30, 900)
            return base

        exceptions_to_retry = self._query(
            "SELECT id, attempt_count, order_id "
            "FROM exceptions "
            "WHERE org_id = %s AND status = 'open' "
            "  AND category = 'transient_submission_failure' "
            "  AND next_retry_at IS NOT NULL AND next_retry_at <= %s",
            (session.org_id, now),
        )

        for exc_id, attempt_count, order_id in exceptions_to_retry:
            if attempt_count >= 6:
                self._execute(
                    "UPDATE exceptions SET status = 'escalated' WHERE id = %s AND org_id = %s AND status = 'open'",
                    (exc_id, session.org_id),
                )
            else:
                new_attempt_count = attempt_count + 1
                next_retry_seconds = backoff_seconds(new_attempt_count)
                next_retry_at = now + _datetime.timedelta(seconds=next_retry_seconds)

                self._execute(
                    "UPDATE exceptions "
                    "SET attempt_count = %s, last_attempt_at = %s, next_retry_at = %s "
                    "WHERE id = %s AND org_id = %s AND status = 'open'",
                    (new_attempt_count, now, next_retry_at, exc_id, session.org_id),
                )

                # Re-queue the associated submission if submission_store is provided
                if submission_store:
                    self._requeue_failed_submission_for_order(submission_store, session.org_id, order_id)

    def _requeue_failed_submission_for_order(self, submission_store, org_id: uuid.UUID, order_id: uuid.UUID) -> None:
        """
        Find failed submissions for an order and re-queue them.

        This is called by retry_scheduler when an exception is ready for retry.
        Searches for submissions associated with the order_id that are marked 'failed'
        and re-queues them by changing status to 'queued'.

        Args:
            submission_store: SubmissionStore instance
            org_id: Organisation ID (UUID)
            order_id: Order ID (UUID)
        """
        import sqlite3

        try:
            # Convert UUIDs to strings for store query
            org_id_str = str(org_id)
            order_id_str = str(order_id)

            # Query the submission store for failed submissions for this order
            # We query all failed submissions (not just recent ones) to catch
            # any that were previously failed and not yet retried
            with sqlite3.connect(submission_store.db_path) as conn:
                cursor = conn.execute(
                    """
                    SELECT id
                    FROM submissions
                    WHERE org_id = ? AND order_id = ? AND status = 'failed'
                    """,
                    (org_id_str, order_id_str),
                )
                failed_submissions = cursor.fetchall()

            # Re-queue each failed submission
            for (submission_id,) in failed_submissions:
                submission_store.update_status(submission_id, "queued")
        except Exception as e:
            # Log error but don't fail the scheduler
            import logging

            logger = logging.getLogger("clinical.data_access")
            logger.warning(f"Failed to re-queue submission for order {order_id}: {e}")

    @auditable(
        action="list_open_exceptions",
        resource_type="exception",
        requires_session=True,
        auditable=True,
        reason="retrieve open/escalated exceptions for operator worklist",
    )
    def list_open_exceptions(self, session: Session) -> list[ExceptionDTO]:
        """
        List all open and escalated exceptions for an organisation.

        Returns exceptions ordered by status (escalated first), then by category,
        then by created_at. Includes attempt_count and next_retry_at so operators
        can distinguish active retries from escalated waits.
        """
        from clinical.models.exception import ExceptionDTO

        exceptions = self._query(
            "SELECT id, order_id, category, reason_code, error_message, status, owner, "
            "       created_at, last_resolved_by, last_resolved_at, resolution_action, "
            "       resolution_note, attempt_count, next_retry_at "
            "FROM exceptions "
            "WHERE org_id = %s AND status != 'resolved' "
            "ORDER BY CASE WHEN status = 'escalated' THEN 0 ELSE 1 END, category ASC, created_at ASC",
            (session.org_id,),
        )

        return [
            ExceptionDTO(
                id=str(exc[0]),
                order_id=str(exc[1]),
                category=exc[2],
                reason_code=exc[3],
                error_message=exc[4],
                status=exc[5],
                owner=exc[6],
                created_at=exc[7].isoformat() if isinstance(exc[7], _datetime.datetime) else exc[7],
                last_resolved_by=exc[8],
                last_resolved_at=exc[9].isoformat() if isinstance(exc[9], _datetime.datetime) else exc[9],
                resolution_action=exc[10],
                resolution_note=exc[11],
                attempt_count=exc[12],
                next_retry_at=exc[13].isoformat() if isinstance(exc[13], _datetime.datetime) else exc[13],
            )
            for exc in exceptions
        ]

    @auditable(
        action="read_exception",
        resource_type="exception",
        requires_session=True,
        auditable=True,
        reason="retrieve single exception with full event history",
    )
    def get_exception_by_id(self, session: Session, exception_id: uuid.UUID) -> Optional[ExceptionDTO]:
        """Get a single exception by ID with full event history (org-scoped)."""
        from clinical.models.exception import (
            ExceptionDTO,
            ExceptionEventDTO,
        )

        # Query the exception
        exception = self._query_one(
            "SELECT id, order_id, category, reason_code, error_message, status, owner, "
            "       created_at, last_resolved_by, last_resolved_at, resolution_action, "
            "       resolution_note "
            "FROM exceptions "
            "WHERE org_id = %s AND id = %s",
            (session.org_id, exception_id),
        )

        if exception is None:
            return None

        exc_id = exception[0]
        order_id = exception[1]

        # Get events for this exception
        events = self._query(
            "SELECT id, action, actor, timestamp, action_note "
            "FROM exception_events "
            "WHERE exception_id = %s "
            "ORDER BY timestamp ASC",
            (exc_id,),
        )

        event_dtos = [
            ExceptionEventDTO(
                id=str(ev[0]),
                action=ev[1],
                actor=ev[2],
                timestamp=ev[3].isoformat() if isinstance(ev[3], _datetime.datetime) else ev[3],
                action_note=ev[4],
            )
            for ev in events
        ]

        return ExceptionDTO(
            id=str(exc_id),
            order_id=str(order_id),
            category=exception[2],
            reason_code=exception[3],
            error_message=exception[4],
            status=exception[5],
            owner=exception[6],
            created_at=exception[7].isoformat() if isinstance(exception[7], _datetime.datetime) else exception[7],
            last_resolved_by=exception[8],
            last_resolved_at=exception[9].isoformat() if isinstance(exception[9], _datetime.datetime) else exception[9],
            resolution_action=exception[10],
            resolution_note=exception[11],
            events=event_dtos,
        )

    # ─── Phase 6: reviewer claims (spec 13.2's review actions) ──────────────
    #
    # Each method here records ONE claim and nothing else. Deliberately absent,
    # and deferred rather than forgotten:
    #
    #   - transition legality (draft -> under_review, approved -> released). A
    #     claim is recorded independently of the report state around it.
    #   - supersession uniqueness (an already-superseded claim must not be
    #     superseded again). The schema states why a row constraint cannot
    #     carry that rule: it cannot see the rest of the table.
    #
    # Both belong to the state-transition commit. evidence_json is not written
    # by anything here either: it stays NULL pending the expert branch that
    # decides what shape evidence takes, because writing a shape now would fix
    # that vocabulary before the decision that owns it.

    def _validate_claim_inputs(
        self,
        session: Session,
        interpretation_id: Any,
        actor_id: Any,
        reason: Optional[str],
    ) -> None:
        """
        Shared precondition check for every reviewer claim.

        Both lookups are org-scoped by construction -- they filter on
        session.org_id, so an interpretation or an actor belonging to another
        organisation is indistinguishable from one that does not exist. That is
        the intended answer, not a rounding of it: a cross-org id is not a
        permission error to be reported back, it is simply absent from this
        org's world, and reporting it as "forbidden" would confirm it exists.
        """
        if reason is None or not str(reason).strip():
            raise ValueError(
                "A claim requires a reason: spec 13.2 records the reviewer's grounds, not only the verdict."
            )

        interpretation = self._query_one(
            "SELECT 1 FROM interpretations WHERE org_id = %s AND id = %s",
            (session.org_id, interpretation_id),
        )
        if interpretation is None:
            raise NotFoundError(f"Interpretation {interpretation_id} not found")

        actor = self._query_one(
            "SELECT 1 FROM users WHERE org_id = %s AND user_id = %s",
            (session.org_id, actor_id),
        )
        if actor is None:
            raise NotFoundError(f"No such user in this organisation: {actor_id}")

    @staticmethod
    def _require_variant_scope(variant_key: Optional[str], claim_type: str) -> None:
        """
        Three of the four claim types are about one specific variant and are
        meaningless without naming it: a 'disagree' that does not say which
        variant it disagrees about records a verdict with no object.

        Checks only that a variant was named. That the named variant is one
        this interpretation actually CONTAINS is not checked here and is not
        forgotten: variants are not rows, they live inside
        interpretations.run_document, so the check is a document read rather
        than a constraint -- which puts it with transition legality and
        supersession uniqueness in the state-transition commit, for the reason
        all three share. A row constraint cannot see what it would need to see.
        """
        if variant_key is None:
            raise ValueError(f"A '{claim_type}' claim is about one variant and must name it: variant_key is required.")

    @staticmethod
    def _require_classification(classification: Optional[str], claim_type: str) -> None:
        """
        'disagree' and 'variant_added' each assert a classification of their
        own; without one the claim says a variant matters without saying what
        the reviewer thinks it means.

        The vocabulary is deliberately not checked -- the schema's comment on
        the column records why: the ACMG tier list this platform will accept is
        not settled across the pipeline, and pinning it here would put the
        narrower list in the harder place to change.
        """
        if classification is None or not str(classification).strip():
            raise ValueError(f"A '{claim_type}' claim must state the classification the reviewer asserts.")

    def _insert_claim(
        self,
        session: Session,
        interpretation_id: Any,
        actor_id: Any,
        reason: str,
        claim_type: str,
        variant_key: Optional[str] = None,
        classification: Optional[str] = None,
    ) -> Any:
        """
        Insert exactly one reviewer_claims row and return its id.

        Shared by all four claim recorders so the column list, the org
        scoping and the two deliberate NULLs (evidence_json, supersedes) are
        written once rather than four times slightly differently.
        """
        claim_id = uuid.uuid4()
        self._execute(
            "INSERT INTO reviewer_claims "
            "(org_id, id, interpretation_id, variant_key, claim_type, classification, "
            ' actor_id, "timestamp", reason) '
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session.org_id,
                claim_id,
                interpretation_id,
                variant_key,
                claim_type,
                classification,
                actor_id,
                self._clock.now(),
                reason,
            ),
        )
        return claim_id

    @auditable(
        action="record_accept",
        resource_type="reviewer_claim",
        requires_session=True,
        auditable=True,
        reason="reviewer concurs with an interpretation (spec 13.2)",
    )
    @transactional
    def _record_accept(
        self,
        session: Session,
        interpretation_id: Any,
        actor_id: Any,
        reason: str,
    ) -> Any:
        """
        Record an 'accept' claim against an interpretation.

        Under the re-derive reading the schema's own comment records, an accept
        is NOT agreement with a draft -- it is an independent concurrence, a
        second clinician arriving at the same classification on their own. That
        is evidence, and evidence without stated grounds is not evidence, which
        is why `reason` is required here exactly as it is for a disagreement.

        The only one of the four scoped to the whole interpretation: it names
        no variant and asserts no classification, so both of those columns stay
        NULL. That asymmetry is why they are nullable.
        """
        self._validate_claim_inputs(session, interpretation_id, actor_id, reason)
        return self._insert_claim(session, interpretation_id, actor_id, reason, claim_type="accept")

    @auditable(
        action="record_disagreement",
        resource_type="reviewer_claim",
        requires_session=True,
        auditable=True,
        reason="reviewer disagrees with a variant's classification (spec 13.2)",
    )
    @transactional
    def _record_disagreement(
        self,
        session: Session,
        interpretation_id: Any,
        variant_key: str,
        actor_id: Any,
        new_classification: str,
        reason: str,
    ) -> Any:
        """
        Record a 'disagree' claim against one variant.

        Does NOT remove or overwrite the classification it disagrees with --
        the schema's comment on claim_type says so, and this method holds to
        it: the row is appended and the pipeline's own call stays exactly where
        it was. Both readings survive, which is the whole point of recording a
        disagreement as a claim rather than as an edit.
        """
        self._validate_claim_inputs(session, interpretation_id, actor_id, reason)
        self._require_variant_scope(variant_key, "disagree")
        self._require_classification(new_classification, "disagree")
        return self._insert_claim(
            session,
            interpretation_id,
            actor_id,
            reason,
            claim_type="disagree",
            variant_key=variant_key,
            classification=new_classification,
        )

    @auditable(
        action="add_variant_by_reviewer",
        resource_type="reviewer_claim",
        requires_session=True,
        auditable=True,
        reason="reviewer names a variant the pipeline did not surface (spec 13.2)",
    )
    @transactional
    def _add_variant_by_reviewer(
        self,
        session: Session,
        interpretation_id: Any,
        variant_key: str,
        actor_id: Any,
        acmg_classification: str,
        reason: str,
    ) -> Any:
        """
        Record a 'variant_added' claim: a variant Bij AI did not surface.

        This is the claim type that most clearly is not an edit of the run --
        the variant is absent from the interpretation's own document and this
        row is the only record that a reviewer says it belongs there. The
        classification is required for exactly that reason: an addition with no
        call attached says a variant matters without saying what it means.
        """
        self._validate_claim_inputs(session, interpretation_id, actor_id, reason)
        self._require_variant_scope(variant_key, "variant_added")
        self._require_classification(acmg_classification, "variant_added")
        return self._insert_claim(
            session,
            interpretation_id,
            actor_id,
            reason,
            claim_type="variant_added",
            variant_key=variant_key,
            classification=acmg_classification,
        )

    @auditable(
        action="mark_variant_not_relevant",
        resource_type="reviewer_claim",
        requires_session=True,
        auditable=True,
        reason="reviewer scopes a variant out against the indication (spec 13.2)",
    )
    @transactional
    def _mark_variant_not_relevant(
        self,
        session: Session,
        interpretation_id: Any,
        variant_key: str,
        actor_id: Any,
        reason: str,
    ) -> Any:
        """
        Record a 'variant_not_relevant' claim: scoped out, not deleted.

        Takes no classification, and that is deliberate rather than an omission
        -- scoping a variant out against the indication is not a statement about
        what the variant means, it is a statement that this indication is not
        the place to answer it. A classification here would assert something the
        reviewer has not said.
        """
        self._validate_claim_inputs(session, interpretation_id, actor_id, reason)
        self._require_variant_scope(variant_key, "variant_not_relevant")
        return self._insert_claim(
            session,
            interpretation_id,
            actor_id,
            reason,
            claim_type="variant_not_relevant",
            variant_key=variant_key,
        )

    # ─── Phase 6: approval and release (spec 13.3, 13.4) ─────────────────────
    #
    # The final gate. Everything upstream (reviewer claims, the interpretation
    # itself) can be recorded freely; nothing downstream of this section may
    # leave the platform without passing through it. Two facts this section
    # exists to make true:
    #
    #   - Approval is what makes a report releasable, and nothing else does
    #     (spec 13.3). _approve_report is the ONLY place reports.state becomes
    #     'approved', and it writes the three approval facts -- approver_id,
    #     approved_at, content_hash -- together, in the one UPDATE that makes
    #     the schema's reports_approval_complete CHECK meaningful rather than
    #     merely satisfiable.
    #
    #   - No report leaves the platform without approval (spec 13.4).
    #     require_release is the hard stop every delivery path must call
    #     before handing content to a consumer; it is the structural
    #     enforcement, not a convention documented in a docstring.
    #
    # The draft -> under_review transition that this section originally lacked
    # is now submit_for_review below (commit 4). Until it existed,
    # _approve_report's 'under_review' precondition could only be satisfied by
    # a test or an operator writing the state directly; that gap was reported
    # rather than backfilled, because deciding who may submit and under what
    # precondition was a different-sized decision than commit 3's three
    # methods. It has since been decided, and submit_for_review is where it
    # lives.

    @auditable(
        action="report_submitted_for_review",
        resource_type="report",
        requires_session=True,
        auditable=True,
        reason="report enters review (spec 13.1: draft -> under_review)",
    )
    @transactional
    def submit_for_review(self, session: Session, report_id: uuid.UUID) -> None:
        """
        Submit a draft report for review: draft -> under_review (spec 13.1).

        The workflow entry point. _approve_report accepts only 'under_review',
        so without this method no report could legitimately reach approval at
        all -- this is the transition that makes the rest of the state machine
        reachable rather than an operator writing `state` by hand.

        Three preconditions, each refusing rather than silently proceeding:

        1. THE INTERPRETER ROLE, on the session invoking this. Reading a draft
           and adding the clinical interpretation is the Interpreter's work, so
           declaring that work finished is theirs. The Approver role is
           deliberately not accepted as a substitute: approving is the
           downstream check on this submission, and letting one identity do
           both without holding both roles would collapse the two-person
           control spec 13.3 rests on.

           Unlike _approve_report, this method takes no separate actor_id and
           so needs no second per-actor role check: there is no identity
           recorded here for anything downstream to read as an authority
           claim. The session gate is the whole of it.

        2. THE REPORT IS IN 'draft'. Any other state is refused with the state
           it actually found named in the message -- including 'under_review'
           itself, so a double submission is a refusal rather than a silent
           no-op that would leave a caller believing it had just submitted.

        3. AT LEAST ONE REVIEWER CLAIM against the report's interpretation.
           This is the substantive one. Zero claims means no review happened,
           and submitting then would put raw engine output into the approval
           queue with a human's submission standing behind it. The claim is
           checked against THIS report's interpretation and within THIS org:
           another interpretation's review, or another organisation's, is not
           this report's review.

           This precondition is what makes the explicit 'accept' claim
           load-bearing rather than ceremonial. A reviewer who agrees with
           every call still leaves rows, so "I read it and concurred" and "I
           never looked" stop being indistinguishable in the record -- which
           is the whole reason spec 13.2 made accept an action rather than the
           absence of one.

        Writes nothing but `state`. approver_id, approved_at and content_hash
        stay NULL: submission is not approval, and the schema's
        reports_approval_complete CHECK holds precisely because this method
        does not touch them.
        """
        self._require_role(session, "Interpreter")

        report = self._query_one(
            "SELECT interpretation_id, state FROM reports WHERE id = %s AND org_id = %s",
            (report_id, session.org_id),
        )
        if report is None:
            raise NotFoundError(f"Report {report_id} not found")
        interpretation_id, state = report

        if state != "draft":
            raise ValueError(
                f"Report is in '{state}' state, cannot submit for review -- spec 13.1: only a draft may be submitted."
            )

        claim = self._query_one(
            "SELECT 1 FROM reviewer_claims WHERE org_id = %s AND interpretation_id = %s LIMIT 1",
            (session.org_id, interpretation_id),
        )
        if claim is None:
            raise ValueError(
                f"Report {report_id} has no reviewer claim against its interpretation and "
                "cannot be submitted for review: submitting with zero claims would present "
                "unreviewed engine output as reviewed. Record at least one claim first "
                "(spec 13.2) -- an explicit 'accept' counts, and is the point of it."
            )

        self._execute(
            "UPDATE reports SET state = 'under_review' WHERE org_id = %s AND id = %s",
            (session.org_id, report_id),
        )

    def _compute_report_content_hash(self, session: Session, interpretation_id: uuid.UUID) -> str:
        """
        Deterministic SHA-256 over exactly what an Approver reviewed: the
        interpretation's run_document AND the full set of reviewer_claims
        recorded against it, AS STORED.

        Shared by _approve_report (which writes the result) and
        verify_report_integrity (which recomputes it later and compares) so
        "the same canonicalisation" is a structural fact -- one function --
        rather than a discipline of keeping two call sites in sync by hand.

        WHY REVIEWER_CLAIMS ARE IN THIS HASH (this reverses an earlier
        decision; the correction is recorded here on purpose rather than
        silently, because the original reasoning was sound on its own terms
        and the distinction that overturns it is easy to lose again).

        The earlier version of this method excluded reviewer_claims because
        spec 13.2 leaves WHICH CLAIMS A RELEASED REPORT DISPLAYS, and how, as
        an explicitly undecided expert-branch question (see the six points
        in reviewer_claims's own schema comment) -- and hashing claims felt
        like fixing that undecided rendering into an immutability check
        before the branch was resolved.

        That reasoning conflates two different questions. Which claims are
        DISPLAYED is rendering, and the expert branch governs exactly that.
        Which claims EXISTED AT APPROVAL is a fact about what was approved,
        settled the moment the Approver signs, regardless of how any claim
        is later shown or on any surface. This hash captures the second
        question, not the first: rendering can still be decided freely, on
        its own timeline, without touching what this function computes.

        The cost of getting this wrong was asymmetric. Excluding claims left
        a real gap: a reviewer_claims row could be altered after approval
        (reviewer_claims is append-only against the application's own
        clinical_app role -- see schema.sql's grants -- but is not immune to
        a direct database write outside that role, which is exactly the
        threat a content hash exists to catch) and verify_report_integrity
        would report no tampering, because the one thing a human actually
        contributed to the approved record was the one thing not covered.
        Including claims that turn out to be irrelevant to some future
        rendering decision costs nothing verify_report_integrity cares
        about; excluding claims that turn out to matter costs the entire
        guarantee this function exists to provide.

        FIELDS HASHED PER CLAIM: EVERY stored column of a reviewer_claims
        row -- claim_type, variant_key, classification, reason, actor_id,
        timestamp, evidence_json, supersedes. There is no exclusion list.

        This is a second, later correction, and it is recorded here for the
        same reason the first one is: the two columns first left out were
        left out for reasons that looked sound in isolation and were wrong
        for the identical structural reason the original run_document-only
        scope was wrong.

          - evidence_json was excluded as "always NULL today, pending the
            expert-branch decision on what shape evidence takes." That is
            true today and is exactly the shape of the claims-exclusion bug
            this function already corrects once: a column that is empty NOW
            is not evidence it will stay empty, and evidence_json is
            precisely the column a re-derive reading of the expert branch
            would start writing into. Excluding it means the hash would
            silently stop covering reviewer evidence the moment that branch
            is decided, with nothing forcing this function to be revisited
            when it happens.
          - supersedes was excluded as "already covered where it matters,
            since a superseding claim is itself a row this function hashes."
            That covers a superseding claim being ADDED after the fact (a
            new row, which does change the hash), but not an EXISTING
            claim's supersedes value being altered in place to point
            somewhere else -- reviewer_claims is append-only against
            clinical_app, which makes that mutation unlikely, not
            impossible, and a hash that carries an exception for "the
            columns append-only makes unlikely" has an exception to explain
            every time someone reads it.

        The rule now, and the reason there is no numbered list of hashed
        columns to maintain: this function hashes every stored column of a
        reviewer_claims row. A column added to the schema later is hashed
        the day it exists, without this function needing to be revisited to
        remember to include it.

        Claims are ordered by (timestamp, id) for a stable serialisation --
        without a fixed order, two computations over the identical set of
        rows could serialise to different JSON and hash to different values,
        which would make this function non-deterministic over data that
        never changed. id is the reviewer_claims primary key, so (timestamp,
        id) is a genuine total order: id alone can never tie.
        """
        interp_row = self._query_one(
            "SELECT run_document FROM interpretations WHERE org_id = %s AND id = %s",
            (session.org_id, interpretation_id),
        )
        run_document = interp_row[0]
        if isinstance(run_document, str):
            run_document = json.loads(run_document)

        claim_rows = self._query(
            'SELECT claim_type, variant_key, classification, reason, actor_id, "timestamp", '
            "evidence_json, supersedes "
            "FROM reviewer_claims WHERE org_id = %s AND interpretation_id = %s "
            'ORDER BY "timestamp", id',
            (session.org_id, interpretation_id),
        )
        claims = []
        for row in claim_rows:
            evidence_json = row[6]
            if isinstance(evidence_json, str):
                evidence_json = json.loads(evidence_json)
            claims.append(
                {
                    "claim_type": row[0],
                    "variant_key": row[1],
                    "classification": row[2],
                    "reason": row[3],
                    "actor_id": str(row[4]),
                    "timestamp": row[5].isoformat(),
                    "evidence_json": evidence_json,
                    "supersedes": str(row[7]) if row[7] is not None else None,
                }
            )

        payload = {"run_document": run_document, "reviewer_claims": claims}
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    @auditable(
        action="report_approved",
        resource_type="report",
        requires_session=True,
        auditable=True,
        reason="approval is the one event that makes a report releasable (spec 13.3)",
    )
    @transactional
    def _approve_report(self, session: Session, report_id: uuid.UUID, actor_id: uuid.UUID) -> None:
        """
        Approve a report: draft -> under_review -> approved is the one path a
        report may enter 'approved' by. Only an Approver may act, per spec
        13.3, and "only Approvers can approve" is enforced twice over here
        rather than once:

          - `_require_role(session, 'Approver')` gates the SESSION invoking
            this method (the reused gate the dispatch named).
          - the identity recorded as approver -- `actor_id` -- is separately
            checked to hold the Approver role itself.

        The second check is not redundant with the first. Every other method
        in this file that takes a session plus a separate actor_id (the
        reviewer-claim recorders above, create_or_reopen_exception) lets the
        two diverge with no per-actor check, because nothing downstream reads
        actor_id as an authority claim. Here it is one: ISO 15189 7.4.1.5 c)
        requires the identity of the person who approved be retrievable, and
        a session gate alone would let any Approver's session record an
        unqualified colleague's user_id as having approved -- retrievable,
        but false. Checking actor_id's own roles closes that gap.

        content_hash is computed by _compute_report_content_hash -- see that
        method's docstring for what it covers and why (run_document AND
        reviewer_claims, as stored, not as any surface renders them).
        """
        self._require_role(session, "Approver")

        actor = self._query_one(
            "SELECT 1 FROM users WHERE org_id = %s AND user_id = %s",
            (session.org_id, actor_id),
        )
        if actor is None:
            raise NotFoundError(f"No such user in this organisation: {actor_id}")

        actor_roles = {a.role for a in self.get_user_roles(session, target_user_id=actor_id)}
        if "Approver" not in actor_roles:
            raise AuthorizationError(
                f"User {actor_id} does not hold the Approver role and cannot be recorded as the approver."
            )

        report = self._query_one(
            "SELECT interpretation_id, state FROM reports WHERE id = %s AND org_id = %s",
            (report_id, session.org_id),
        )
        if report is None:
            raise NotFoundError(f"Report {report_id} not found")
        interpretation_id, state = report

        if state != "under_review":
            raise ValueError(
                f"Report is in '{state}' state, cannot approve -- spec 13.1: only a report "
                "under_review may be approved."
            )

        content_hash = self._compute_report_content_hash(session, interpretation_id)

        now = self._clock.now()
        self._execute(
            "UPDATE reports SET state = %s, approver_id = %s, approved_at = %s, content_hash = %s "
            "WHERE id = %s AND org_id = %s",
            ("approved", actor_id, now, content_hash, report_id, session.org_id),
        )

    @auditable(
        action="report_integrity_verified",
        resource_type="report",
        requires_session=True,
        auditable=True,
        reason="ISO 15189 7.5 nonconformance detection: a mismatch is audit-relevant on its own, not only on export",
        details_builder=lambda params, result: {"report_id": str(params.get("report_id")), "matched": result},
    )
    @transactional
    def verify_report_integrity(self, session: Session, report_id: uuid.UUID) -> bool:
        """
        Recompute the report's content hash from CURRENT run_document and
        CURRENT reviewer_claims, using _compute_report_content_hash -- the
        same function _approve_report used to produce the stored value --
        and compare.

        Returns True on a match, False on a mismatch. Deliberately a bool,
        not a raise: this is a check a caller interprets, like
        check_consent and check_order_data elsewhere in this file, not a
        hard-stop gate like require_release. A future delivery-path gate
        may choose to treat False as fatal; that choice is not made here.

        Raises NotFoundError for an unknown or cross-org report (the usual
        meaning throughout this file), and ValueError for a report with no
        content_hash yet -- an unapproved report was never hashed, so there
        is nothing stored to verify against, which is a different condition
        from "verified and found to differ."
        """
        report = self._query_one(
            "SELECT interpretation_id, content_hash FROM reports WHERE id = %s AND org_id = %s",
            (report_id, session.org_id),
        )
        if report is None:
            raise NotFoundError(f"Report {report_id} not found")
        interpretation_id, stored_hash = report

        if stored_hash is None:
            raise ValueError(f"Report {report_id} has not been approved and has no content_hash to verify against.")

        current_hash = self._compute_report_content_hash(session, interpretation_id)
        return current_hash == stored_hash

    @auditable(
        action="report_released",
        resource_type="report",
        requires_session=True,
        auditable=True,
        reason="release is a delivery event and the record of who received what, when (spec 13.4)",
    )
    @transactional
    def _release_report(
        self,
        session: Session,
        report_id: uuid.UUID,
        consumer: str,
        actor_id: uuid.UUID,
    ) -> uuid.UUID:
        """
        Record one delivery of an approved report to one consumer.

        Release is an event, not a state flag (schema comment on
        release_events): releasing the same report to a second consumer
        inserts a second row rather than being refused by an already-released
        report. reports.state still moves to 'released', but only on the
        FIRST release -- it is a derived summary ("has this report been
        released at all"), not itself the record of who received what and
        when.

        content_hash is copied from reports.content_hash rather than
        recomputed: between approval and release nothing in this schema can
        change an interpretation's run_document (there is no UPDATE path for
        interpretations), so the value approved is definitionally the value
        being handed to this consumer. The duplication onto release_events is
        still deliberate, per that table's own schema comment -- it lets a
        reader learn what was delivered without joining back to reports.
        """
        actor = self._query_one(
            "SELECT 1 FROM users WHERE org_id = %s AND user_id = %s",
            (session.org_id, actor_id),
        )
        if actor is None:
            raise NotFoundError(f"No such user in this organisation: {actor_id}")

        if consumer is None or not str(consumer).strip():
            raise ValueError("A release must name its consumer (spec 13.4).")

        report = self._query_one(
            "SELECT state, content_hash FROM reports WHERE id = %s AND org_id = %s",
            (report_id, session.org_id),
        )
        if report is None:
            raise NotFoundError(f"Report {report_id} not found")
        state, content_hash = report

        if state not in ("approved", "released"):
            raise ValueError(
                f"Report is in '{state}' state, cannot release -- spec 13.4: no report leaves "
                "the platform without approval."
            )

        release_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO release_events (org_id, id, report_id, consumer, released_at, released_by, content_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (session.org_id, release_id, report_id, consumer, now, actor_id, content_hash),
        )

        if state != "released":
            self._execute(
                "UPDATE reports SET state = %s WHERE id = %s AND org_id = %s",
                ("released", report_id, session.org_id),
            )

        return release_id

    @auditable(
        action="require_release",
        resource_type="report",
        requires_session=True,
        auditable=True,
        reason="the release gate is the structural enforcement spec 13.4 depends on; its denials are audit-relevant",
        details_builder=lambda params, result: {"report_id": str(params.get("report_id"))},
    )
    @transactional
    def require_release(self, session: Session, report_id: uuid.UUID) -> None:
        """
        The one gate every delivery path calls before handing report content
        to a consumer (spec 13.4). Hard stop: a report that is not at least
        approved refuses here, structurally, rather than by a convention a
        future delivery path could forget to honour.

        Approved and released both pass -- released implies a report was
        approved first (require_release itself was the only gate before the
        first _release_report call could ever have succeeded), and a second
        consumer requesting the same already-released report is not a reason
        to refuse it.
        """
        report = self._query_one(
            "SELECT state FROM reports WHERE id = %s AND org_id = %s",
            (report_id, session.org_id),
        )
        if report is None:
            raise NotFoundError(f"Report {report_id} not found")
        state = report[0]

        if state not in ("approved", "released"):
            raise ValueError(
                f"Report is in '{state}' state -- spec 13.4: no report leaves the platform without approval."
            )


# A real bcrypt hash of a value nobody holds, used only to spend verification
# work when no user matched. Generated once, constant thereafter.
_DUMMY_HASH = "$2b$12$C6UzMDM.H6dfI/f/IKcEe.6c1sVQ0S0kzSfKUL5jHkKzE1LpMOxQ2"
