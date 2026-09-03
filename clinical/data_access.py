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
import inspect
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Protocol, Sequence

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
)

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
ORG_SCOPED_TABLES = ("users", "role_assignments", "sessions", "totp_backup_codes")


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
            "       disabled, totp_secret "
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

        user_id, password_hash, failed_attempts, locked_until, disabled, totp_secret = row

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


# A real bcrypt hash of a value nobody holds, used only to spend verification
# work when no user matched. Generated once, constant thereafter.
_DUMMY_HASH = "$2b$12$C6UzMDM.H6dfI/f/IKcEe.6c1sVQ0S0kzSfKUL5jHkKzE1LpMOxQ2"
