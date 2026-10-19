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

import base64
import contextvars
import datetime as _datetime
import functools
import hashlib
import inspect
import json
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Protocol, Sequence

from clinical.retention import resolve_vcf_retention_days

if TYPE_CHECKING:
    from clinical.models.exception import ExceptionDTO

# ─── Policy constants ────────────────────────────────────────────────────────

MAX_FAILED_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 30
SESSION_ABSOLUTE_HOURS = 24
SESSION_IDLE_MINUTES = 30
BACKUP_CODE_COUNT = 8

# The roles that may read a report's state and revision through the staff read API (see
# DataAccess.read_report_status). A pilot policy choice, kept in one place.
REPORT_READ_ROLES = ("Interpreter", "Approver", "Auditor", "Administrator")

ROLES = (
    "Orderer",
    "Lab technician",
    "Interpreter",
    "Approver",
    "Administrator",
    "Auditor",
    "System",
)

# w141. THE ROLES A PERSON MAY BE GRANTED. `ROLES` is the schema's vocabulary
# -- the CHECK constraint on `role_assignments.role` enumerates all seven and
# it is unchanged -- but "System" is not a role anybody holds, it is the
# INTERNAL SYSTEM PRINCIPAL, and the difference was enforced by convention
# only.
#
# THE CONVENTION, WHICH ALREADY EXISTED EVERYWHERE ELSE:
#   * `login()` refuses any account with `is_system_account = true`
#     (reason="system_account"), so the system principal cannot authenticate
#     interactively;
#   * the ONLY writer of a `role_assignments` row naming "System" is
#     `_create_system_session`, which writes it with raw SQL alongside the
#     `is_system_account = true` user it just created, and whose four call
#     sites are pinned by name in test_identity.py.
#
# THE HOLE THOSE TWO LEFT. `_require_role(session, "System")` reads
# `role_assignments`; it does NOT read `is_system_account`. So an
# Administrator calling `assign_role(session, <an ordinary human>, "System")`
# granted that human the principal the fastq detection write path gates on
# -- and, because the human's own account is not a system account, that human
# could still log in interactively and use it. No tenant boundary is crossed
# (`session.org_id` binds the INSERT and the composite FK enforces it), so
# this is a privilege-separation defect inside one organisation, not a leak.
#
# Stated as a set rather than as `role != "System"` at the call site so the
# census is readable and so a future role that is also not human-assignable
# has one place to go.
HUMAN_ASSIGNABLE_ROLES = tuple(r for r in ROLES if r != "System")

# Stage 3 follow-up. The `release_events.consumer` recorded when a report is
# released over the review surface. The patient portal is the only delivery
# path that exists; the column itself stays free text for the paths that do not.
PORTAL_RELEASE_CONSUMER = "patient_portal"

# Phase 7 commit 4 (spec 22, human ruling D2). RE-RULED 2026-09-09:
# "everything downstream of a VCF inherits the VCF period" -- so
# "run_document" and "report" are NO LONGER independently configurable.
# Narrowed to just "vcf" here rather than left present-but-ignored: a
# config knob no purge path any longer consults is a control that looks
# live and does nothing, which is worse than an absent one. THIS NARROWING
# IS AN ENGINEERING DECISION, not part of the human's ruling -- see
# clinical/retention.py's module docstring for the reasoning in full.
RETENTION_ARTEFACT_CLASSES = ("vcf",)

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

# w130. The one refusal a patient-axis miss produces, wherever it is produced.
# A MODULE CONSTANT AND NOT AN f-STRING, for the reason the org axis already
# learned: a message that names the identifier, or that differs between "this
# patient is in another organisation", "this patient is not yours" and "this
# patient does not exist", tells the caller which of the three is true. All
# three are the same sentence, carrying no identifier.
PATIENT_NOT_FOUND = "Patient not found."

# w135. The one refusal the patient report surface produces, wherever it is
# produced, for the same reason PATIENT_NOT_FOUND is one constant. FIVE
# distinguishable conditions collapse onto this sentence: no such report, a
# report belonging to another patient, a report in another organisation, and
# the caller's OWN report in a state that has not been released -- draft,
# under_review, approved-not-yet-released, or returned. The last of those is
# the one worth naming: "your report exists but you may not have it yet" is a
# clinical fact, and the release gate exists to withhold exactly that.
REPORT_NOT_FOUND = "Report not found."


# ─── w134: the patient-visible progress vocabulary, and what it refuses ──────
#
# THE CENSUS THAT PRODUCED THIS IS THE WHOLE JUSTIFICATION, so it is recorded
# here and not in a document that ships separately from the code.
#
# COUNTED at 0b4bcac over non-test Python, whole tree, quoted-literal match:
# the workflow declares NINETEEN state values across its four state columns
# (orders.state 7, samples.qc_status 3, fastq_sets.state 4, reports.state 5).
# TWELVE have a writer. SEVEN have none: in_progress, reported and closed on
# orders; returned on reports; validated, rejected and consumed on fastq_sets.
# Of those seven, exactly TWO occur at all -- `reported` and `closed`, both on
# ONE line, data_access.py's `cancel_order` guard, and both as a READ
# predicate. A surface built on a value nothing writes reports the same thing
# for ever, so none of the seven appears below.
#
# AND A WRITER IS NOT ENOUGH. A value written and superseded inside ONE
# transaction is never seen by any reader, so it is not a state this platform
# can be observed in. `record_pipeline_result` inserts the sequencing run, the
# VCF, the interpretation AND the draft report in a single transaction --
# deliberately, so a failure cannot leave a lineage the platform never reported
# on. That makes "processing in progress" and "interpretation in progress"
# unobservable, and it is why the six stages below are six and not seven:
# Processing and Interpretation are ONE stage, `analysis`, because they are one
# commit. Splitting them would state twice what is observed once.
#
# WHAT EACH REFUSAL COSTS, so nobody "completes" the picture later:
#   order.not_started      -- a listed order has a row; there is no prior state
#   sample.in_progress     -- receive_sample writes collected_at AND
#                             received_at in the same INSERT; nothing records
#                             "collection requested"
#   analysis.in_progress   -- the four-row single transaction above
#   review.<returned>      -- zero writers, zero readers
#   report.in_progress     -- release is one method: the release_events INSERT
#                             and the state UPDATE. Nothing marks it pending
#   a `processing` stage   -- sequencing_runs has no state column and no
#                             completed_at, so a row cannot say "under way";
#                             and fastq_sets.state is a ONE-VALUE column,
#                             because schema.sql grants clinical_app SELECT and
#                             INSERT on that table and no UPDATE at all
#
# ADDING A VALUE HERE REQUIRES A WRITER FIRST, and giving a value a writer is a
# workflow decision -- who performs the transition and when -- not a wiring one.
PORTAL_STAGES = ("order", "sample", "qc", "analysis", "review", "report")

# The whole vocabulary. Held as a constant so a test can assert the emitted set
# is a SUBSET of it and catch a value invented in a later edit.
PORTAL_STAGE_STATES = ("not_started", "in_progress", "completed", "failed", "cancelled")


def derive_order_stages(
    *,
    placed_at: Any,
    cancelled_at: Any,
    sample_count: int,
    qc_pending: int,
    qc_passed: int,
    qc_failed: int,
    interpretation_count: int,
    report_count: int,
    reports_signed_off: int,
    reports_released: int,
) -> list[dict[str, str]]:
    """
    Six stages for one order, DERIVED from counts of rows that already exist.

    A PURE FUNCTION. No session, no connection, no clock, no writes. It is not
    a workflow engine and cannot become one by accident: it has no notion of a
    transition, of legality, or of what may follow what. It reads a tally and
    names it.

    KEYWORD-ONLY ON PURPOSE. Ten integers and two timestamps in positional
    order is a defect waiting for a refactor to transpose two of them, and the
    transposition that matters most -- qc_passed for qc_failed -- would tell a
    patient their failed sample passed.

    `failed` and `cancelled` are the two values outside the three-state
    vocabulary, and both are states the writers genuinely produce: record_qc
    validates its argument to exactly {passed, failed}, and cancel_order stamps
    cancelled_at. Neither is a synthesis.
    """
    # ORDER. `completed` here means THE ORDER WAS PLACED -- the stage's own
    # completion -- and nothing more. It does not and cannot mean "the test
    # finished": `reported` and `closed` have no writer, so an order has no
    # terminal success value to read.
    if cancelled_at is not None:
        order_state = "cancelled"
    elif placed_at is not None:
        order_state = "completed"
    else:
        order_state = "in_progress"

    # SAMPLE. Row existence, because that is the only signal there is: a
    # `samples` row is inserted by receive_sample and by nothing else, with
    # received_at stamped in the same statement.
    sample_state = "completed" if sample_count else "not_started"

    # QC. The ONLY stage whose column has a writer for every declared value,
    # and so the only one that supports the full distinction.
    #
    # A FAILURE ON ANY SAMPLE DOMINATES, and a pending one dominates a passed
    # one. An order with one failed and one passed sample is not "completed":
    # the failed specimen is the fact with a consequence for the patient, and a
    # precedence that let it be hidden behind a passed sibling would be the
    # status surface lying in the one place it must not.
    if not sample_count:
        qc_state = "not_started"
    elif qc_failed:
        qc_state = "failed"
    elif qc_pending:
        qc_state = "in_progress"
    else:
        qc_state = "completed"

    # ANALYSIS -- sequencing run, VCF and interpretation, which are one commit.
    # Keyed on the interpretation because it is the last of the four inserts:
    # if it is there, the whole transaction committed.
    analysis_state = "completed" if interpretation_count else "not_started"

    # REVIEW. draft and under_review are both `in_progress` DELIBERATELY. Both
    # have writers and the two are distinguishable, but "the report exists in
    # draft" versus "the report has been submitted" is an internal distinction;
    # collapsing them is refusing to make a product ruling nobody has made.
    if not report_count:
        review_state = "not_started"
    elif reports_signed_off:
        review_state = "completed"
    else:
        review_state = "in_progress"

    # REPORT -- release. MEASURED AND STATED PLAINLY: `_release_report` is the
    # writer, it exists, and COUNTED at 0b4bcac it has ZERO callers outside
    # this repository's tests. In this deployment this stage therefore reads
    # not_started for every order. That is the measurement being reported, not
    # a defect introduced by reporting it, and the alternative -- treating
    # `approved` as released -- would tell a patient a report had been issued
    # when no release event exists.
    report_state = "completed" if reports_released else "not_started"

    return [
        {"stage": "order", "state": order_state},
        {"stage": "sample", "state": sample_state},
        {"stage": "qc", "state": qc_state},
        {"stage": "analysis", "state": analysis_state},
        {"stage": "review", "state": review_state},
        {"stage": "report", "state": report_state},
    ]


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


class PatientAlreadyEnrolledError(Exception):
    """
    Raised when a patient already holds a portal credential (w130).

    NOT an AuthenticationError and deliberately not folded into one: this is
    raised to a STAFF principal performing enrolment, who is entitled to know
    why the enrolment did not happen. The generic-error discipline applies to
    the unauthenticated login surface, where a distinguishing message is an
    enumeration oracle; it does not apply here.
    """


class ConfigurationError(Exception):
    """Raised when the layer is asked to do something it was not configured for."""


class ImmutabilityViolationError(Exception):
    """
    Raised when a database-enforced immutability trigger refuses an UPDATE
    (D0b half two: reports/interpretations/vcfs BEFORE UPDATE triggers,
    schema.sql's "content may not change" enforcement).

    Translated in _execute() from the raw driver error (Postgres SQLSTATE
    P0001, the code a plain PL/pgSQL RAISE EXCEPTION uses) rather than
    imported and caught as a specific psycopg exception class, so this
    module stays driver-agnostic the same way __init__'s `connection: Any`
    already is -- nothing here assumes psycopg specifically.

    Every current DataAccess write to these three tables is written to
    never trigger this in normal operation (submit_for_review only ever
    touches state; _approve_report only writes approver_id/approved_at/
    content_hash while they are still NULL; _release_report only touches
    state). This exists for the paths that "currently can't fail" per the
    human ruling requiring it -- a future bug, a race, or a raw-SQL bypass
    attempt -- so that when the database refuses, the caller gets a named,
    catchable application exception instead of a raw driver error surfacing
    through a method whose signature never previously mentioned failure
    here.
    """


class IncompleteClinicianIdentityError(Exception):
    """
    R10 (human ruling 2026-09-13): a clinical report shows the signing
    clinician's NAME, REGISTRATION NUMBER and HOSPITAL. Raised when the user
    record that signed a report does not carry all three, naming exactly which
    are missing.

    REFUSAL RATHER THAN A MARKING, and the choice is recorded here because
    both were available. Rendering "Reg. No.: not provided" would produce a
    document that asserts a qualified signature while withholding the one
    credential that evidences the qualification -- and a released report is
    the artefact a recipient acts on, so the defect leaves the platform.
    Refusing keeps the failure inside the lab, where the remedy is one
    administrator call (set_clinician_identity) against an account that should
    have carried these fields before it was ever given the Approver role.

    Deliberately NOT raised by _approve_report. Approval behaviour is
    unchanged for accounts with no identity -- the gate is at the point the
    identity is read for printing, so no existing report, workflow or legacy
    account changes behaviour because these columns now exist.
    """


class SessionExpired(AuthenticationError):
    """Session is terminated, past its absolute cap, or idle too long."""


class BrokenRevisionRecordError(Exception):
    """
    get_report_revision: the amendment / re-analysis records for this report
    cannot be read into a coherent statement about it.

    HUMAN RULING R7 (2026-09-13): "a broken or unreadable revision record is
    an ERROR. Fail closed. No marker text, no silent omission."

    Its own class rather than a bare ValueError because the two ways the
    banners can fail a reader are OPPOSITE mistakes and a caller may want to
    tell them apart: refusing to produce the document (this) versus producing
    a document that reads as an original when it is not (which is what a
    marker string or an omitted block would do). Any caller that catches this
    must NOT fall back to rendering the report.
    """


class VcfChangedError(ValueError):
    """
    record_pipeline_result: the VCF on disk no longer hashes to the value the
    caller took before running the pipeline. The run analysed different bytes
    from the ones that would be recorded against it, so nothing is recorded.
    """


class DuplicateInterpretationError(Exception):
    """
    record_pipeline_result: an interpretation with this submission key
    (same VCF content, same sample) already exists in this organisation --
    uk_interp_submission refused the insert. Not an error to the worker: it
    means an earlier attempt (or a concurrent one) already recorded this run,
    and the caller should look it up with find_interpretation_by_submission_key.
    Nothing from this attempt is written.
    """

    def __init__(self, submission_key: str) -> None:
        super().__init__(f"An interpretation with submission key {submission_key!r} already exists")
        self.submission_key = submission_key


_TRANSIENT_CATEGORY = "transient_submission_failure"  # ExceptionCategory.TRANSIENT_SUBMISSION_FAILURE's value
_MAX_RETRY_ATTEMPTS = 6


def _retry_backoff_seconds(attempt_count: int) -> int:
    """Seconds to wait before the next retry after `attempt_count` attempts: 30 s doubling, capped at 15 minutes."""
    return min(2**attempt_count * 30, 900)


def _claim_timestamp_utc(ts: "_datetime.datetime") -> str:
    """
    A claim timestamp as it enters the approval hash: converted to UTC first, then `isoformat()`.

    WHY. psycopg returns a `timestamptz` as an aware datetime in the zone of the CONNECTION's session, and the
    service pins no zone, so a bare `ts.isoformat()` printed the same instant with a different offset on a different
    connection and the hash changed with it. Converting to UTC makes the hash a function of the instant alone.

    COMPATIBILITY. For a session that was already UTC, `ts.astimezone(utc).isoformat()` is byte-for-byte what
    `ts.isoformat()` printed (offset `+00:00`, the fraction omitted when the microseconds are zero), so every hash
    computed under UTC is reproduced unchanged. A hash computed under another zone is NOT reproduced by this form;
    `DataAccess._classify_report_integrity` recognises it separately and never calls it tampering.

    A naive datetime cannot come out of a `timestamptz` column; it is refused rather than guessed, because
    `astimezone` would silently read it as local time.
    """
    if ts.tzinfo is None:
        raise ValueError("a reviewer claim timestamp must be timezone-aware to be hashed")
    return ts.astimezone(_datetime.timezone.utc).isoformat()


# The classification `verify_report_integrity` reached, handed to its audit-details builder. The decorator gives a
# builder only (parameters, return value), and the return value must stay a plain bool, so the detail travels in a
# ContextVar: set by the method just before it returns, consumed (and cleared) by the builder in the same call.
_INTEGRITY_AUDIT: contextvars.ContextVar = contextvars.ContextVar("report_integrity_audit", default=None)


def _integrity_audit_details(params: dict, result: Any) -> dict:
    """
    Audit `details` for `report_integrity_verified`.

    Always: `report_id` and `matched` (the bool the method returned; None on a failed call). On a completed
    verification also `outcome`: "match" (canonical UTC form reproduced the stored hash), "match_legacy_zone" (the
    stored hash was made by the previous algorithm in a non-UTC session and the current content reproduces it; the
    IANA zone that did so is in `legacy_zone`), or "no_match" (nothing reproduced it: integrity could NOT be verified,
    which is not by itself a finding of tampering). Nothing from the report's content is recorded.

    The pending detail is consumed here so one call's outcome can never be attached to another call's row, and a
    failed call (result None) records no outcome.
    """
    pending = _INTEGRITY_AUDIT.get()
    _INTEGRITY_AUDIT.set(None)
    details: dict = {"report_id": str(params.get("report_id")), "matched": result}
    if result is not None and pending is not None:
        outcome, zone = pending
        details["outcome"] = outcome
        if outcome == "match_legacy_zone":
            details["legacy_zone"] = zone
    return details


def interpretation_submission_key(content_hash: str, platform_sample_id: Any) -> str:
    """
    interpretations.submission_key for an original (non-reanalysis)
    interpretation: `<sha256 hex of the VCF bytes>|<sample id>`. ONE formula,
    read by create_interpretation, derive_submission_key and
    record_pipeline_result, so the key the worker looks up and the key the
    row is written with cannot drift apart.
    """
    return f"{content_hash}|{platform_sample_id}"


def _sha256_file(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


@dataclass(frozen=True)
class RecordedPipelineResult:
    """What record_pipeline_result wrote. str() is the interpretation id, which
    is what the audit row's resource_id records."""

    interpretation_id: uuid.UUID
    report_id: uuid.UUID
    vcf_id: uuid.UUID
    sequencing_run_id: uuid.UUID
    submission_key: str

    def __str__(self) -> str:
        return str(self.interpretation_id)


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


# ─── the second factor (w164) ────────────────────────────────────────────────

# RFC 6238's default, and pyotp's. Named here rather than written as a literal
# in two places, because `matching_step` below and the replay guard in
# `_verify_second_factor` MUST agree about what a step is -- a disagreement
# would either refuse valid codes or forget spent ones.
TOTP_STEP_SECONDS = 30

# One step of tolerance either side, the usual allowance for clock skew
# between an authenticator and this server. It is deliberately small and it is
# NOT free: a wider window is a larger set of simultaneously valid codes for an
# attacker to guess at, and every extra step is one more that the replay guard
# has to burn on a single login. One step each way means a code lives for at
# most 90 seconds and exactly once.
TOTP_WINDOW_STEPS = 1

# 20 bytes = 160 bits, which is what RFC 4226 section 4 R6 requires as a
# minimum for the shared secret, encoded base32 because that is what every
# authenticator and every otpauth:// URI speaks.
TOTP_SECRET_BYTES = 20


class TotpVerifier(Protocol):
    """
    IT RETURNS THE STEP, NOT A BOOLEAN, AND THAT IS THE WHOLE POINT (w164).

    A `-> bool` verifier can say "this code is currently acceptable" and
    nothing more, which is not enough to refuse a REPLAY: the same code stays
    acceptable for the rest of its step and, with the skew allowance, into the
    next. One-time use needs to know WHICH step was accepted so that step can
    be recorded as spent. RFC 6238 section 5.2 says the same thing -- the
    verifier "MUST NOT accept the second attempt of the OTP generated for the
    same time-step".

    So the primitive is "which step does this code belong to, if any", and the
    boolean is derivable from it. Returning None means no step in the window
    matched.
    """

    def matching_step(self, secret: str, code: str, at: _datetime.datetime) -> Optional[int]: ...


class PyOtpVerifier:
    """
    The production verifier. `pyotp` is imported lazily, exactly as `bcrypt` is
    in `BcryptHasher`, so this class can be constructed anywhere.

    CONSTANT-TIME COMPARISON IS pyotp's, NOT REIMPLEMENTED HERE:
    `pyotp.TOTP.verify` compares through `pyotp.utils.strings_equal`, which is
    `hmac.compare_digest` over the encoded strings. A `==` on a six-digit code
    short-circuits on the first wrong digit and leaks the prefix by timing.
    """

    def matching_step(self, secret: str, code: str, at: _datetime.datetime) -> Optional[int]:
        if not code:
            return None

        import pyotp

        totp = pyotp.TOTP(secret)
        centre = int(at.timestamp()) // TOTP_STEP_SECONDS
        # THE WINDOW IS WALKED HERE RATHER THAN HANDED TO `valid_window`,
        # because `valid_window=1` answers yes without saying WHICH of the
        # three steps matched -- and the step is the thing the replay guard
        # records. Each candidate is checked with valid_window=0 against the
        # instant that step begins, so the arithmetic is identical and the
        # answer is more specific. Ascending, so the EARLIEST matching step is
        # the one returned: that is the conservative choice, since a later one
        # would mark more steps spent than the code actually used.
        for step in range(centre - TOTP_WINDOW_STEPS, centre + TOTP_WINDOW_STEPS + 1):
            moment = _datetime.datetime.fromtimestamp(step * TOTP_STEP_SECONDS, tz=_datetime.timezone.utc)
            if totp.verify(code, for_time=moment, valid_window=0):
                return step
        return None


def generate_totp_secret() -> str:
    """
    A NEW SHARED SECRET, FROM `secrets`, AND THERE IS NO PARAMETER (w164).

    Until this wave `enrol_totp` took the secret as an argument, which meant
    the CALLER chose it -- and a secret the caller chose can be weak, reused
    across accounts, or a constant. This is the house's defence-by-absent-
    parameter applied to the one value in this system that must never be
    attacker-influenced: there is no argument through which a worse secret
    could arrive.

    `secrets.token_bytes` is the CSPRNG, not `random`. base32 without padding,
    which is what `pyotp.TOTP` and every authenticator expect.
    """
    return base64.b32encode(secrets.token_bytes(TOTP_SECRET_BYTES)).decode("ascii").rstrip("=")


class SecretCipher(Protocol):
    def encrypt(self, plaintext: str) -> str: ...
    def decrypt(self, ciphertext: str) -> str: ...


# THE KEY THAT PROTECTS A STORED TOTP SECRET, AND WHERE IT COMES FROM (w164).
#
# THIS IS NOT A NEW DECISION. It is the decision `clinical/bootstrap.py`
# already made for this service, in its own words:
#
#     "PASSWORDS COME FROM THE OPERATOR, NOT FROM THIS REPOSITORY. There is no
#      secret store here and this file does not invent one: the two passwords
#      are read from the environment and have NO DEFAULTS. A missing one is a
#      hard refusal, never a fallback ... How the operator supplies them (a
#      secrets manager, a CI secret, an interactive shell) is a deployment
#      decision this file deliberately does not make."
#
# Four further things in the tree say the same:
#
#   * `CLINICAL_DSN` (clinical/endpoints.py, clinical/app.py) already arrives
#     this way and CARRIES THE DATABASE PASSWORD -- app.py's own comment says
#     so. The environment is already the channel by which this service is given
#     the credential that grants full read and write on the very table
#     `totp_secret` lives in. A key delivered the same way is not weaker than
#     what is already there; a key delivered any other way would be a second
#     secret channel for a system that has one.
#   * `GEPER_API_KEYS` (geper/api/main.py::_load_api_keys) is this repository's
#     production-credential pattern -- read from the environment, refuse on a
#     malformed value -- and spec section 23.3 records it as IMPLEMENTED. It is
#     the spec's ONLY statement about how a service credential is supplied.
#   * `.env.example` is committed, `.env` is gitignored, and docker-compose
#     passes these through. The deployment channel exists and is documented.
#   * `clinical/schema.sql`'s comment on `users.totp_secret` already ruled the
#     shape: "encrypted at rest by the application before it ever reaches this
#     column ... Encrypted rather than hashed". Application-side symmetric
#     encryption was decided by the schema, not by this wave. The `SecretCipher`
#     Protocol above agrees: it is handed no user, no record and no
#     organisation, which admits exactly one architecture -- a single
#     process-wide symmetric key from configuration.
#
# GEPER_CLINICAL_PLATFORM_SPEC.md IS SILENT on key management, encryption at
# rest and rotation (23.1-23.4 and 30.1-30.3; 23.4 covers transport only), and
# none of the fourteen held branches carries a cipher, a key loader or a
# ruling. Recorded here so that nobody later reads either as agreement.
#
# A LIST, NOT A VALUE, and that is the rotation mechanism rather than a
# convenience. `MultiFernet` encrypts under the FIRST key and decrypts under
# ANY, so putting a new key first and keeping the old one second re-protects
# every new enrolment while every existing row still reads. What is NOT decided
# here, and is deliberately not invented: who rotates, how often, and what
# re-encrypts the rows already stored. That is an operator policy and it is
# carded.
TOTP_SECRET_KEYS_ENV = "CLINICAL_TOTP_SECRET_KEYS"


class FernetSecretCipher:
    """
    Authenticated symmetric encryption for the TOTP shared secret.

    FERNET, NOT A CONSTRUCTION OF OUR OWN. It is AES-128-CBC with an
    HMAC-SHA256 over the ciphertext (encrypt-then-MAC), a random IV per token
    and a versioned format, from `cryptography` -- the standard library offers
    no AEAD at all, and the alternative to a maintained implementation here
    would be writing a cipher for a clinical product, which is not defensible
    however carefully it is done.

    WHY AUTHENTICATED AND NOT MERELY ENCRYPTED. Without the MAC, anybody who
    can write to `users.totp_secret` -- which `clinical_app` can, it holds
    UPDATE -- could replace the token with one whose plaintext they control and
    then log in with codes they generate. The MAC makes that a decryption
    FAILURE, and `_verify_second_factor` turns a decryption failure into a
    refusal.

    THE IMPORT IS LAZY, exactly as `BcryptHasher`'s bcrypt and
    `PyOtpVerifier`'s pyotp are, so constructing this class costs nothing and
    a deployment that has not configured a key never touches `cryptography`.
    The KEY IS VALIDATED EAGERLY, in `__init__`, using only base64 from the
    standard library -- so a typo in the variable is a refusal at construction
    rather than a traceback at the first enrolment, without that check itself
    depending on the optional package.
    """

    def __init__(self, keys: Sequence[str]) -> None:
        cleaned: List[str] = [key.strip() for key in keys if key and key.strip()]
        if not cleaned:
            raise ConfigurationError(
                f"{TOTP_SECRET_KEYS_ENV} holds no usable key. It has no default on purpose: a "
                "layer that invented its own key would encrypt every secret under a value nobody "
                "knows and could not decrypt them after a restart."
            )
        for position, key in enumerate(cleaned, start=1):
            try:
                raw = base64.urlsafe_b64decode(key.encode("ascii"))
            except Exception as exc:  # noqa: BLE001 -- every decode failure is the same refusal
                raise ConfigurationError(
                    f"{TOTP_SECRET_KEYS_ENV} entry {position} is not url-safe base64. Generate one "
                    'with: python -c "from cryptography.fernet import Fernet; '
                    'print(Fernet.generate_key().decode())"'
                ) from exc
            if len(raw) != 32:
                # The VALUE is never quoted back, here or anywhere else: this
                # message reaches a log.
                raise ConfigurationError(
                    f"{TOTP_SECRET_KEYS_ENV} entry {position} decodes to {len(raw)} bytes; a Fernet key is exactly 32."
                )
        self._keys: tuple = tuple(cleaned)
        self._cipher: Any = None

    def _fernet(self) -> Any:
        if self._cipher is None:
            from cryptography.fernet import Fernet, MultiFernet

            self._cipher = MultiFernet([Fernet(key) for key in self._keys])
        return self._cipher

    def encrypt(self, plaintext: str) -> str:
        return self._fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")


def default_secret_cipher(raw: Optional[str] = None) -> Optional[SecretCipher]:
    """
    The cipher a `DataAccess` gets when nobody injects one.

    READ AT CALL TIME, NOT AT IMPORT. `clinical/app.py` reads its docs flag
    once at import so the served surface cannot change under a running process;
    the opposite argument applies here. `_get_data_access` builds a
    `DataAccess` per request, and a key read once into a module constant would
    be untestable without reloading the module and would make a restart the
    only way to observe a corrected variable. Neither costs anything: this is
    a dictionary lookup and a base64 decode beside a bcrypt verification.

    ABSENT MEANS None, AND MALFORMED MEANS REFUSE. Those are different states
    and collapsing them is the defect worth naming: treating an unreadable key
    as "no key" would turn one typo into a deployment that silently cannot
    enrol and silently cannot verify, reported to the caller as an ordinary
    authentication failure. Absent is a deployment that has not turned MFA on;
    malformed is a deployment that thinks it has.
    """
    value = os.environ.get(TOTP_SECRET_KEYS_ENV, "") if raw is None else raw
    value = value.strip()
    if not value:
        return None
    return FernetSecretCipher(value.split(","))


@dataclass(frozen=True)
class TotpEnrolment:
    """
    What `enrol_totp` returns: the one moment either value exists in readable
    form.

    The secret is stored encrypted and is selected by no read method; the
    backup codes are stored only as bcrypt hashes. Neither can be recovered,
    which is why re-enrolling is the recovery path and why re-enrolment voids
    the previous codes.
    """

    secret: str
    backup_codes: List[str]


# ─── Session ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PatientSession:
    """
    The patient principal's session. DELIBERATELY NOT A `Session`, and not a
    subclass of one (w130).

    WHAT IT CARRIES AND WHY. `org_id` answers "which organisation?" exactly as
    it does on Session. `patient_id` answers "WHICH PATIENT?" -- a second axis
    that does not exist anywhere else in this codebase, and it is carried here
    for precisely the reason Session's own docstring gives for org_id: it is
    read from here and NEVER from a caller argument, which is what makes the
    isolation structural rather than remembered.

    WHAT IT DELIBERATELY DOES NOT CARRY: `user_id`. Roughly thirty statements
    in this layer write `session.user_id` into a created_by / recorded_by /
    actor column. A patient session that reached one of them would write a
    patient id into a staff actor column. With no such attribute, it raises
    AttributeError at the first use instead -- a runtime backstop UNDER the
    real control, which is the @auditable refusal (decision 7).

    HONEST LIMIT. Annotations are not enforced at run time and this object
    carries org_id, so it would duck-type into any method that touches only
    session.org_id. That is why the refusal is enforced in the decorator every
    public DataAccess method already carries, and not left to the type
    checker.
    """

    session_id: uuid.UUID
    patient_login_id: uuid.UUID
    patient_id: uuid.UUID
    org_id: uuid.UUID


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
    # R10: the three values a signed report shows. Optional because most
    # accounts never sign one (system accounts, Auditors, Administrators) --
    # see schema.sql's users comments and IncompleteClinicianIdentityError.
    # Defaulted so that a caller constructing a User positionally from an
    # older SELECT list still works.
    full_name: Optional[str] = None
    registration_number: Optional[str] = None
    hospital: Optional[str] = None


def _clean_identity_value(value: Optional[str], field: str, allow_none: bool = False) -> Optional[str]:
    """
    Trim one signing-identity value, or refuse it (R10).

    Whitespace-only is refused rather than stored, because " " is not a
    missing value to any SQL predicate or to `if value:` in a renderer -- it
    is a present one that prints as nothing. That is precisely the "report
    shows a signature with a blank credential" outcome
    IncompleteClinicianIdentityError exists to prevent, arriving by the one
    route a NOT NULL constraint would never catch.

    `allow_none` is for provisioning (create_user), where omitting the fields
    entirely is the normal case for every account that will never sign a
    report. It permits None and nothing else: an explicitly supplied blank is
    still refused there, so the provisioning path cannot admit what the
    administrative path rejects.
    """
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{field} is required and must not be empty.")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} is required and must not be blank (received whitespace only).")
    return cleaned


# Tables whose rows belong to exactly one organisation. Every statement this
# module issues against one of these must bind org_id.
#
# w129 — HOW THIS LIST IS NOW MAINTAINED, AND WHY THE OLD ANSWER WAS THE DEFECT.
# This used to be a hand-written subset: twelve names out of the schema's
# twenty-six tables. The other twelve org-owned tables were simply absent, and
# absence is invisible — so the guard rail in test_identity.py had no opinion
# about them at all. That is how GAP-1 shipped green: exception_events, the
# table at the centre of a cross-tenant write, was never on the list, so the
# test that exists to catch exactly that never looked at it.
#
# The fix is NOT to add the missing twelve names; that reproduces the defect
# for table twenty-seven. The coverage is now DERIVED from schema.sql by
# clinical/tests/test_identity.py: every CREATE TABLE in the schema is
# org-owned unless it appears in TABLES_EXEMPT_FROM_ORG_SCOPING below with a
# written reason. This tuple is checked against that derivation, so a new
# table is covered the day it is created and the only way to leave one out is
# to argue for it in writing.
ORG_SCOPED_TABLES = (
    "organisations",
    "users",
    "role_assignments",
    "sessions",
    "totp_backup_codes",
    "audit_log",
    "patients",
    "external_identifiers",
    "consents",
    "tests",
    "orders",
    "samples",
    "fastq_sets",
    "sequencing_runs",
    "vcfs",
    "interpretations",
    "reports",
    "reviewer_claims",
    "release_events",
    "amendments",
    "amendment_notifications",
    "notification_read_receipts",
    "retention_policies",
    "exceptions",
    "exception_events",
    # w130. The patient portal principal and its sessions. Both carry org_id,
    # so NEITHER MAY BE EXEMPTED -- test_only_a_table_without_an_org_id_column_
    # may_be_exempt permits an exemption only where the column genuinely does
    # not exist. Every statement against either binds org_id, INCLUDING the
    # patient login lookup, which is WHERE org_id = %s AND email = %s exactly
    # as login()'s users lookup already is.
    "patient_logins",
    "patient_sessions",
    # w148. The w122 retraction tables, brought into the schema by migration
    # 0003 at 2de2fef. BOTH CARRY org_id, so NEITHER MAY BE EXEMPTED --
    # test_only_a_table_without_an_org_id_column_may_be_exempt permits an
    # exemption only where the column genuinely does not exist, and that
    # interlock is here because GAP-1 shipped green while exception_events was
    # simply missing from this list.
    #
    # COUNTED, NOT ASSUMED. This file issues exactly ONE statement against
    # either of them: the `is_retracted` projection inside the shared
    # patient-report statement below, whose subquery binds
    # `rr.org_id = rep.org_id` (and `rr.report_id = rep.id`). The constant's
    # name is deliberately not spelled here -- the guard that pins its caller
    # count counts occurrences of the name in this file's TEXT, so prose
    # naming it would read as a fourth caller. Against
    # `retraction_notifications` it issues none, which is why listing it is a
    # true claim rather than an empty one -- the claim is about every
    # statement there is, and there are none.
    "report_retractions",
    "retraction_notifications",
)


# Tables in schema.sql that are deliberately NOT held to the org_id rule, each
# with the reason. An exemption you can read is a decision; an omission you
# cannot see is a hole — and the hole is what GAP-1 rode in on.
#
# A name may only appear here if the table genuinely has no org_id column;
# test_identity.py enforces that, so a table cannot be exempted out of trouble
# once it has the column.
TABLES_EXEMPT_FROM_ORG_SCOPING = {
    "test_genes": (
        "No org_id column exists on this table, so no statement against it could "
        "bind one. Its single reference, test_genes.test_id -> tests (test_id), is "
        "the non-composite foreign key recorded as GAP-4 by the w129 Phase A audit; "
        "tests carries no UNIQUE (org_id, test_id) that would let it be composite. "
        "data_access.py issues zero statements against this table — counted, not "
        "assumed. This exemption records a known structural asymmetry; it does not "
        "claim the table is not org-owned."
    ),
    # w126m. THE ONE TABLE IN THIS SCHEMA THAT IS NOT ABOUT PATIENTS AT ALL.
    #
    # This exemption was WRITTEN BECAUSE THE GUARD DEMANDED IT: adding
    # schema_migrations to schema.sql turned
    # test_org_scoped_table_coverage_is_derived_from_the_schema red, which is
    # the w129 GAP-3 derivation doing exactly the job it was built for -- a new
    # table was covered the day it was created, and leaving it out required an
    # argument in writing. That is recorded here rather than smoothed over,
    # because the alternative (adding the name to ORG_SCOPED_TABLES) would have
    # been the false answer: it would assert that every statement against this
    # table binds org_id, and none could, because there is nothing to bind.
    #
    # WHY IT IS GENUINELY NOT ORG-OWNED, and not merely inconvenient to scope.
    # Its rows are DEPLOYMENT FACTS about the database as a whole -- which
    # migration ran, over which bytes, when, as whom. A schema is at one
    # version; it is not at one version per organisation. There is no tenant
    # dimension to carry, which is why the table has no org_id column and why
    # test_only_a_table_without_an_org_id_column_may_be_exempt permits this
    # entry at all: that interlock still binds, so this cannot become a hiding
    # place the day someone adds the column.
    #
    # COUNTED, NOT ASSUMED: data_access.py issues ZERO statements against this
    # table -- grep for the name in this file returns only this comment. It is
    # written only by clinical/migrate.py, which runs as the administrative
    # principal and never through DataAccess, and read there and by
    # clinical/app.py's readiness probe. The application role is granted SELECT
    # on it and REVOKEd INSERT, UPDATE and DELETE by
    # clinical/migrations/0001_schema_migrations.sql, so clinical_app cannot
    # write deployment state even by a route this list does not police.
    "schema_migrations": (
        "No org_id column exists on this table, and none could: its rows are deployment "
        "facts about the database as a whole -- which migration ran, over which bytes, "
        "when and as whom -- and a schema is at one version, not at one version per "
        "organisation. data_access.py issues zero statements against it, counted, not "
        "assumed; it is written only by clinical/migrate.py as the administrative "
        "principal, and clinical_app holds SELECT on it with INSERT, UPDATE and DELETE "
        "revoked, so the application cannot forge its own version."
    ),
}


# ─── Session references for the audit log ───────────────────────────────────
#
# THE SESSION ID IS THE BEARER CREDENTIAL. `sessions.session_id` is the value of
# the session cookie (auth_endpoints._set_session_cookie), and the cookie is the
# whole of authentication. audit_log is append-only by grant, so an id written to
# it can never be removed, and anyone who can read the table -- an Auditor
# through the audit route, a database reader, a backup -- could present it for as
# long as that session lives. So an audit row must never carry one.
#
# What an audit row carries instead is a REFERENCE: a one-way digest that lets a
# login row and its logout row be matched, and that cannot be presented as a
# cookie. The input is a server-generated uuid4 (122 random bits), so the digest
# cannot be walked back by guessing; no key is needed for that, and adding one
# would add a secret to manage for no gain. The version prefix lets the scheme be
# changed later without ambiguity.
_SESSION_REF_PREFIX = "geper-audit-session-ref:v1:"

# details keys that name a session, and the reference key each is rewritten to.
_AUDIT_SESSION_KEYS = {
    "session_id": "session_ref",
    "target_session_id": "target_session_ref",
}


def session_audit_ref(session_id: Any) -> Optional[str]:
    """The audit reference for a session id (UUID or its string form), or None."""
    if session_id is None:
        return None
    text = str(session_id)
    try:
        text = str(uuid.UUID(text))  # one canonical spelling, so every writer agrees
    except ValueError:
        pass
    return "sref_" + hashlib.sha256((_SESSION_REF_PREFIX + text).encode("utf-8")).hexdigest()[:32]


def scrub_audit_details(details: Any) -> Any:
    """
    Rewrite any session-id key in an audit `details` mapping to its reference.

    Used twice: as a FAIL-SAFE on every audit writer, so a future details_builder
    that names `session_id` cannot put a credential in the log; and at the audit
    route, so rows written BEFORE this existed show a reference rather than a
    live credential. It returns a new mapping and never touches the stored row.
    """
    if not isinstance(details, dict):
        return details
    cleaned = {}
    for key, value in details.items():
        if key in _AUDIT_SESSION_KEYS:
            cleaned[_AUDIT_SESSION_KEYS[key]] = session_audit_ref(value) if value not in (None, "None") else None
        else:
            cleaned[key] = value
    return cleaned


# ─── Audit decorator ────────────────────────────────────────────────────────


def auditable(
    action: str | dict[str, str] | Any,
    resource_type: str,
    requires_session: bool = True,
    auditable: bool = True,
    reason: str | None = None,
    details_builder: Any = None,
    resource_id_param: str | None = None,
    audit_denials_only: bool = False,
    resource_id_from_result: Any = None,
    resource_id_transform: Any = None,
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

    resource_id_param: name of the method PARAMETER that identifies the resource
        this entry is about.

    resource_id_from_result: opt-in for a method whose RETURN VALUE is (or
        contains) the identifier. `True` means the return value IS the id;
        a callable is passed the return value and must return the id, so a
        method returning a structure names one field of it and leaves the rest
        out of the log. Declaring both this and resource_id_param is a
        ConfigurationError -- two answers to one question is a mistake, not a
        preference order.

    ── WHY A RETURN VALUE IS NEVER RECORDED UNLESS ASKED FOR (w121) ──────────

        THE EXPOSURE. This decorator used to take resource_id from whatever the
        method returned:

            resource_id=str(result) if result else "<none>"

        `enrol_totp(session, target_user_id, secret) -> List[str]` returns the
        eight TOTP backup codes in PLAINTEXT. Its docstring says that return
        value "is the only moment they exist in readable form". It was not: the
        decorator stringified the list into

            action='totp_enrolled'  resource_type='totp'
            resource_id="['d620e8cae2', '89ebd91e0f', ... ]"

        THE CONSEQUENCE IS PERMANENT. audit_log is APPEND-ONLY BY GRANT (see
        schema.sql: GRANT SELECT, INSERT ON audit_log TO clinical_app; REVOKE
        UPDATE, DELETE ...). The application cannot delete or redact a row it
        has written. So every TOTP enrolment ever performed left an
        unremovable copy of that user's second-factor recovery codes in the
        audit table, beside the user id they belong to. Rotating the codes does
        not remove the old ones from the log; nothing the application can do
        does.

        WHY THE DEFAULT CHANGED RATHER THAN enrol_totp. The same mechanism --
        "record whatever came back" -- produced all three groups of wrong
        rows: the credential exposure above, w120's '<none>' approvals, and the
        stringified result sets of the read/query methods. Silencing one method
        would leave the mechanism armed for the next method someone writes. So
        a return value is now written ONLY when the decorator was explicitly
        told to write it. Undeclared means resource_id='<none>'.

        The methods whose returned value legitimately IS the id -- create_report,
        create_patient, create_organisation and the rest of the creators --
        record exactly what they recorded before, but now because they say so.

        ENFORCED STRUCTURALLY by
        clinical/tests/test_w121_audit_optin_identifier.py, whose
        test_every_auditable_method_declares_where_its_identifier_comes_from
        fails any auditable method that declares neither source and is not on
        that test's reasoned NO_RESOURCE_ID list.

        THE DEFECT resource_id_param ORIGINALLY ARRIVED FOR (w120). A method
        declared `-> None`
        returns no identifier ever, so `str(result) if result else "<none>"`
        recorded '<none>' every time. _approve_report is one of those, and its
        entry -- action='report_approved', resource_type='report' -- therefore
        said a report had been approved without saying WHICH, for the single
        event that makes a report releasable (spec 13.3). `-> bool`
        verify_report_integrity was wrong in the direction that matters most: a
        MATCH recorded the string 'True', and a MISMATCH -- the nonconformance
        ISO 15189 7.5 wants findable -- recorded '<none>', because False is
        falsy.

        A method that cannot return an identifier must therefore say where its
        identifier comes from. Enforced structurally by
        clinical/tests/test_w120_audit_resource_id.py, whose
        test_every_auditable_method_that_cannot_return_an_id_declares_where_its_id_comes_from
        fails any auditable `-> None`/`-> bool` method that neither declares
        this nor appears on that test's reasoned exemption list.

        SUCCESS PATH ONLY. Denied and errored outcomes keep their existing
        '<denied>'/'<error>' markers: what those rows should record is a
        separate question, and widening it here would be a redesign rather
        than a correction.

    resource_id_transform: applied to the identifier AFTER it is chosen (by either
        source) and before it is written. For an identifier that is itself a
        credential: the session methods pass `session_audit_ref`, so the row
        carries a reference and never the session id.

    audit_denials_only: for a PUBLIC WRAPPER that does nothing but gate and
        delegate to an @auditable method (approve_report, release_report,
        record_disagreement_claim). Such a wrapper must not write a success
        row -- the delegate already writes the one row for the act, and a
        second would count one clinical act twice -- but a refusal raised by
        the wrapper's OWN gate never reaches the delegate, so without this it
        left no trace at all. With the flag, the wrapper writes NOTHING on
        success and NOTHING on an ordinary error (the delegate has recorded
        those), and writes exactly ONE `denied` row when it raises
        AuthorizationError itself.

        A refusal that a delegate already recorded is not recorded again: the
        denied branch marks the exception it has audited, and a denials-only
        wrapper skips an exception so marked. So the flag cannot produce a
        duplicate even if the delegate later gains a gate of its own.

        Requires `auditable=False` (it is a non-success-auditing wrapper) and
        is meaningless otherwise.
    """
    if resource_id_param is not None and resource_id_from_result is not None:
        raise ConfigurationError(
            "@auditable: resource_id_param and resource_id_from_result both declared. "
            "An audit row has one subject; declare one source for it."
        )

    def decorator(func):
        def wrapper(self, *args, **kwargs):
            # ── w130, DECISION 7: a patient session is refused by every staff
            # operation, and the refusal lives HERE rather than in the type
            # annotations.
            #
            # WHY HERE AND NOWHERE ELSE. Python annotations are not enforced at
            # run time, and a PatientSession carries org_id -- so it duck-types
            # perfectly into any staff method that touches only session.org_id,
            # which is most of the twenty-nine ungated readers. MEASURED, not
            # argued: before this check existed, dao.get_patient(alice_session,
            # bobs_patient_id) RETURNED BOB'S RECORD. Two tests failed with
            # "DID NOT RAISE" and thirty-two passed.
            #
            # This decorator is the one thing every public DataAccess method
            # carries -- test_every_public_method_has_auditable_decorator makes
            # that structural -- so it is the only place a single check covers
            # all of them. The check runs BEFORE the `auditable` early return
            # on purpose: get_patient is auditable=False, and it is exactly the
            # method that leaked.
            #
            # It NARROWS: it refuses a PatientSession where a Session is
            # required, and changes nothing else about any existing call.
            if requires_session and args and isinstance(args[0], PatientSession):
                raise AuthorizationError(
                    f"{func.__name__} is a staff operation and does not accept a patient session. "
                    "Patient access is served by the portal surface, which binds the patient from "
                    "the session rather than from an argument."
                )
            if not auditable and not audit_denials_only:
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

            # ── w137: WHICH ORGANISATION THE ROW IS FILED UNDER, WHEN THE
            # ACTING PRINCIPAL IS NOT A STAFF `Session`.
            #
            # THE DEFECT. Every audit row's org_id/user_id was bound from
            # `session` above and from nothing else, and `session` is None for
            # every requires_session=False method. `read_audit` is
            # Auditor-gated and filters `WHERE org_id = %s`, so a row written
            # with org_id NULL is recorded where the organisation that owns it
            # cannot look. `terminate_my_patient_session` is the method that
            # shape reaches -- a patient logging out of the portal -- and the
            # organisation was never unknown: it is on the PatientSession, and
            # w132 had to carry it in `details` instead.
            #
            # THIS IS NOT A WIDENING OF DECISION 7. The refusal above is keyed
            # on requires_session=True and is untouched; a PatientSession is
            # still refused by every staff operation, and the tripwire for
            # that is test_w137_patient_backend.py::TestDecisionSevenWasNotWidened
            # as well as w130's own suite. What changes here is only which
            # column an already-permitted call's row lands in.
            #
            # COUNTED at 4213cd3, so the blast radius is a fact: sixteen
            # methods declare requires_session=False; ten are auditable=False
            # and write no row at all; of the remaining six,
            # log_failed_login, login, create_organisation, create_user and
            # patient_login all carry a literal org_id (or a name) parameter
            # and are already bound by the fallbacks below.
            # terminate_my_patient_session is the only one whose acting
            # principal is an object, and it is the only row this changes.
            #
            # user_id COMES FROM `getattr`, AND THAT IS THE POINT.
            # audit_log.user_id means a `users` row; PatientSession
            # deliberately has no user_id (see its docstring), so a patient
            # logout keeps a NULL there and the acting credential stays in
            # `details`. A staff Session presented to a patient-principal
            # method -- the denied path _require_patient_session exists for --
            # does name its user, which is exactly what an auditor reading a
            # refused attempt needs.
            acting_principal = (
                args[0] if (not requires_session and args and isinstance(args[0], (Session, PatientSession))) else None
            )
            principal_org_id = session.org_id if session else getattr(acting_principal, "org_id", None)
            principal_user_id = session.user_id if session else getattr(acting_principal, "user_id", None)

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
                if audit_denials_only:
                    # A gate-and-delegate wrapper: the delegate wrote the one
                    # success row for the act. Writing another would count the
                    # act twice.
                    return result
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
                write_org_id = principal_org_id
                write_user_id = principal_user_id

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
                # Which value identifies the resource this entry is about. Only
                # a DECLARED source is ever written: a named parameter, or the
                # return value when the method explicitly opted in. Undeclared
                # records no id, because a return value that was never chosen
                # as an identifier may be a payload -- see the w121 block in
                # this decorator's docstring for the credential that reached
                # the append-only audit_log that way.
                if resource_id_param is not None:
                    identifier = bound_params.get(resource_id_param)
                elif resource_id_from_result is True:
                    identifier = result
                elif callable(resource_id_from_result):
                    identifier = resource_id_from_result(result)
                else:
                    identifier = None
                if resource_id_transform is not None and identifier is not None:
                    identifier = resource_id_transform(identifier)
                # Success: write audit entry
                self._write_audit_entry(
                    action_name,
                    resource_type,
                    resource_id=str(identifier) if identifier else "<none>",
                    outcome="success",
                    org_id=write_org_id,
                    user_id=write_user_id,
                    actor_role=self._get_actor_role(session) if session else None,
                    details=details if details else None,
                    ip_address=bound_params.get("ip_address"),
                )
                self._DataAccess__connection.commit()
                return result
            except AuthorizationError as denial:
                # A denials-only wrapper does not repeat a refusal that a
                # delegate has already put on the record.
                if audit_denials_only and getattr(denial, "_audit_recorded", False):
                    raise
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
                    org_id=principal_org_id,
                    user_id=principal_user_id,
                    actor_role=self._get_actor_role(session) if session else None,
                    details=details if details else None,
                    ip_address=bound_params.get("ip_address"),
                )
                self._DataAccess__connection.commit()
                denial._audit_recorded = True
                raise
            except Exception:
                if audit_denials_only:
                    # The delegate has already recorded an ordinary error.
                    raise
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
                    org_id=principal_org_id,
                    user_id=principal_user_id,
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
        # w164. THE DEFAULTS ARE WHERE MFA BECAME REACHABLE, and they are here
        # rather than at the four call sites for a structural reason.
        #
        # Measured at 4ab1fbf by an alias-resolving ast.Call census: EXACTLY
        # FOUR non-test constructions of DataAccess exist --
        # clinical/endpoints.py:262, geper/api/exception_retry_worker.py:132,
        # geper/api/submission_worker.py:850 and geper/review/signoff.py:936 --
        # and all four pass the connection ALONE. Both collaborators were
        # therefore None in every production process, which is why
        # `_verify_second_factor` and `enrol_totp` could only ever raise there.
        #
        # Editing the four sites would have worked and would have been WORSE:
        # a fifth site -- a worker, a CLI path, a future router -- would then
        # be one forgotten keyword away from a DataAccess that cannot verify a
        # second factor, and nothing would say so until an enrolled user tried
        # to log in. Defaulting here means there is nothing to remember. The
        # keyword arguments stay, for tests that inject a stand-in.
        #
        # The verifier needs no configuration, so it is unconditional. The
        # cipher needs a key and has none unless the deployment supplies one,
        # so `default_secret_cipher` returns None when the variable is unset --
        # which leaves the fail-closed behaviour below exactly as it was.
        self._totp: Optional[TotpVerifier] = totp_verifier or PyOtpVerifier()
        self._cipher: Optional[SecretCipher] = secret_cipher if secret_cipher is not None else default_secret_cipher()
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
            try:
                cur.execute(sql, tuple(params))
            except Exception as e:
                # SQLSTATE P0001: a plain PL/pgSQL RAISE EXCEPTION with no
                # SQLSTATE of its own -- currently only the reports/
                # interpretations/vcfs immutability triggers use this.
                # Checked via the driver-agnostic .sqlstate attribute rather
                # than importing and catching a specific psycopg exception
                # class; see ImmutabilityViolationError's docstring.
                if getattr(e, "sqlstate", None) == "P0001":
                    raise ImmutabilityViolationError(str(e)) from e
                raise
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
                json.dumps(scrub_audit_details(details or {})),
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
            (org_id, user_id, action, json.dumps(scrub_audit_details(details or {})), ip_address, self._clock.now()),
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
        # login returns a Session dataclass; str() of it is a repr, not an
        # identifier. Name the one field that is.
        resource_id_from_result=lambda result: result.session_id if result else None,
        # The session id is the cookie value. Record its reference, never the id.
        resource_id_transform=session_audit_ref,
        requires_session=False,
        details_builder=lambda params, result: {
            "session_ref": session_audit_ref(result.session_id) if result else None,
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
            "       disabled, totp_secret, is_system_account, totp_last_used_step "
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

        (
            user_id,
            password_hash,
            failed_attempts,
            locked_until,
            disabled,
            totp_secret,
            is_system,
            totp_last_used_step,
        ) = row

        if locked_until is not None and locked_until > now:
            # EQUALISE HERE TOO -- w139. This branch used to be the ONE failure
            # path in either login method that spent no bcrypt work at all, so
            # a locked-out address answered measurably faster than a wrong
            # password against a live one. That is a timing oracle in the
            # direction that helps: only an address that EXISTS can be locked,
            # so "cheap" confirmed the address, and an attacker can create the
            # state themselves with five wrong guesses.
            #
            # IT MUST NOT TOUCH THE COUNTER. `_equalise_password_work` verifies
            # against the dummy hash and discards the result; it does not call
            # `_register_failed_attempt`, so a locked account is not driven
            # deeper and its `locked_until` is not extended. The lockout policy
            # is unchanged -- this branch still refuses, before the real hash is
            # ever consulted.
            self._equalise_password_work(password)
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

        # THE SECOND FACTOR IS CHECKED LAST, AND THE ORDER IS THE POLICY.
        # `disabled`, `is_system_account` and the lockout all refuse ABOVE
        # this line, so a principal who has been taken out of service cannot
        # be let back in by holding a valid code -- the code is never reached.
        if totp_secret is not None:
            if not self._verify_second_factor(user_id, org_id, totp_secret, totp_code, now, totp_last_used_step):
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
        last_used_step: Optional[int] = None,
    ) -> bool:
        """
        THE ONLY PLACE A SECOND FACTOR IS ACCEPTED, AND IT NEVER RETURNS True
        BY ACCIDENT.

        Read the exits rather than the flow: there are exactly two `return
        True` statements reachable from here -- one after a step that has not
        been spent, one inside `_consume_backup_code` after a row was marked
        used in the same statement that matched it. Every other path returns
        False or raises, INCLUDING every exception the crypto can produce.

        ONE-TIME USE (RFC 6238 section 5.2). `matching_step` names the step the
        code belongs to; `users.totp_last_used_step` remembers the last one
        accepted; a step at or before it is refused. Storing the STEP and not
        the CODE is what makes the guard monotonic -- remembering only the last
        code would still admit the PREVIOUS step's code, which the one-step
        skew window keeps arithmetically valid for another half minute.

        FAIL CLOSED, IN TWO DIFFERENT SHAPES, DELIBERATELY:

          * A MISSING COLLABORATOR RAISES. `ConfigurationError` is an operator
            fault -- the account has a secret and this process was given no key
            -- and it must be loud. It is not a login refusal dressed up as one,
            and it never admits: the caller (`clinical/auth_endpoints.py::
            _refuse_uniformly`) flattens it into the same 401 every other
            failure produces, so the loudness reaches the audit trail and the
            operator without reaching the caller.
          * A FAILING COLLABORATOR REFUSES. A secret that will not decrypt (a
            lost key, a rotated-away key, a tampered column) or a verifier that
            raises is a fault about ONE ROW, not about the process. The broad
            `except` below is the point rather than a lapse: whatever it
            catches, `step` stays None and the only paths left are the backup
            code -- which is exactly the recovery route a lost key needs -- and
            refusal. There is no arrangement of an exception here that can
            produce a session.
        """
        if not code:
            return False
        if self._cipher is None:
            # Fail closed. A layer holding encrypted secrets with no cipher
            # cannot verify anything, and guessing that the column might be
            # plaintext is exactly the wrong recovery.
            raise ConfigurationError("TOTP secret is stored encrypted but no secret_cipher was provided.")
        if self._totp is None:
            raise ConfigurationError("TOTP is enrolled for this user but no totp_verifier was provided.")

        step: Optional[int]
        try:
            step = self._totp.matching_step(self._cipher.decrypt(stored_secret), code, now)
        except Exception:  # noqa: BLE001 -- see the docstring; a failure here is a refusal, never an admission
            # AUDITED, because it is otherwise indistinguishable from a wrong
            # code and it is not the same event: it means this organisation's
            # stored secrets cannot be read by this process.
            self._audit("totp_secret_unreadable", org_id, user_id, {})
            step = None

        if step is not None:
            if last_used_step is not None and step <= last_used_step:
                self._audit("totp_code_replayed", org_id, user_id, {"step": str(step)})
                return False
            self._execute(
                "UPDATE users SET totp_last_used_step = %s WHERE user_id = %s AND org_id = %s",
                (step, user_id, org_id),
            )
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
        resource_id_param="target_session_id",
        resource_id_transform=session_audit_ref,
        requires_session=True,
        details_builder=lambda params, result: {
            "target_session_ref": session_audit_ref(params.get("target_session_id"))
        },
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

    @auditable(
        action="session_logged_out",
        resource_type="session",
        resource_id_from_result=True,
        resource_id_transform=session_audit_ref,
        requires_session=True,
        details_builder=lambda params, result: {"session_ref": session_audit_ref(result)},
    )
    @transactional
    def terminate_my_session(self, session: Session) -> uuid.UUID:
        """
        End THE CALLING session. Log out.

        IT TAKES NO TARGET IDENTIFIER, AND THAT IS THE WHOLE DESIGN. The
        obvious version of this method accepts a `target_session_id` and ends
        it, scoped to the caller's organisation — and that hands every
        authenticated user the capability `terminate_session` reserves for an
        Administrator, through a route called "logout". The defence is not a
        check that the target belongs to the caller: a check can be forgotten,
        refactored away, or made optional by a later `if target_session_id is
        None` convenience. THERE IS NOTHING TO CHECK, because the id ended is
        read from the session object the dependency produced and there is no
        other way to name one.

        NO ROLE IS REQUIRED, and that distinguishes it from
        `terminate_session` above rather than duplicating it. Ending somebody
        else's session is an administrative act; ending your own is not, and
        a self-logout gated on Administrator would mean ordinary staff could
        not log out at all — a defect that would read like security.

        `terminated_by` holds the acting user. `terminated_at IS NULL` in the
        predicate makes a second call a no-op rather than a rewrite: the
        recorded moment a session ended must be the first one, not the last
        redundant request.

        RETURNS THE SESSION ID so that @auditable's `resource_id` names the
        session that ended. A `-> None` here would record `<none>` — the w120
        defect — and an audit row that cannot name its subject is not an
        audit.

        NOT A CREDENTIAL REVOCATION. This ends one session. The credential
        that minted it is untouched and can mint another immediately, which is
        correct for logout and is NOT what `users.disabled` /
        `patient_logins.disabled` are for.
        """
        now = self._clock.now()
        self._execute(
            "UPDATE sessions SET terminated_at = %s, terminated_by = %s "
            "WHERE session_id = %s AND org_id = %s AND terminated_at IS NULL",
            (now, session.user_id, session.session_id, session.org_id),
        )
        return session.session_id

    # ── roles ────────────────────────────────────────────────────────────────

    @auditable(
        action="role_assigned",
        resource_type="role_assignment",
        resource_id_from_result=True,
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

        "System" IS NOT ASSIGNABLE, BY ANYBODY, INCLUDING AN ADMINISTRATOR
        (w141). See HUMAN_ASSIGNABLE_ROLES above for why: it is the internal
        system principal that `_require_role(session, "System")` gates the
        fastq detection write path on, and `_create_system_session` is the
        only thing entitled to hold it. The refusal is a ValueError and not an
        AuthorizationError deliberately -- an AuthorizationError would say
        "you may not do this", which invites somebody to look for a principal
        who may. There is none.

        THE ORDER MATTERS: the Administrator check stays FIRST, so a caller
        without it learns nothing about which roles are assignable.
        """
        self._require_role(session, "Administrator")
        if role not in ROLES:
            raise ValueError(f"Unknown role: {role!r}. Known roles: {', '.join(ROLES)}")
        if role not in HUMAN_ASSIGNABLE_ROLES:
            raise ValueError(
                f"Role {role!r} is the internal system principal and cannot be granted to a "
                f"person. Assignable roles: {', '.join(HUMAN_ASSIGNABLE_ROLES)}."
            )
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
        action="role_revoked",
        resource_type="role_assignment",
        resource_id_param="target_user_id",
        requires_session=True,
        details_builder=lambda params, result: {
            "target_user_id": str(params.get("target_user_id")),
            "role": params.get("role"),
            "assignments_revoked": result,
        },
    )
    @transactional
    def revoke_role(self, session: Session, target_user_id: uuid.UUID, role: str) -> int:
        """
        Withdraw a role. THE FIRST PRODUCTION WRITER OF `revoked_at` /
        `revoked_by`, and w154 (WAVE 4) exists because there was none.

        THE STATE HAD NO WRITER AND THE ENFORCEMENT WAS ALREADY COMPLETE.
        Measured at 4700f40 over non-test Python, whole tree: `revoked_at`
        and `revoked_by` occurred at exactly two code sites, `_get_actor_role`
        and `get_user_roles`, and BOTH were `revoked_at IS NULL` read
        predicates. Three sites wrote a `role_assignments` row -- this
        method's neighbour `assign_role`, `provision_organisation`, and
        `_create_system_session` -- and not one of them ever set the two
        columns back. So the platform could grant a role and could read a
        revocation it had no way to record.

        ── WHY THIS METHOD DOES NOT TOUCH SESSIONS, WHICH IS THE DESIGN
           QUESTION AND IS SETTLED BY MEASUREMENT RATHER THAN BY TASTE ──

        ROLES ARE RESOLVED PER REQUEST, FROM THIS TABLE. `Session` is a
        frozen dataclass of exactly three fields -- session_id, user_id,
        org_id -- and carries no role. `session_valid` reads one `sessions`
        row and returns those three fields; it does not join
        `role_assignments` and never has. `_require_role` calls
        `get_user_roles`, which reads `role_assignments` with
        `revoked_at IS NULL`, on EVERY gated call. Nothing captures a role at
        login, so there is no stale copy anywhere to invalidate.

        Therefore setting `revoked_at` IS the enforcement: the caller's very
        next gated request recomputes the set and fails. Terminating the
        target's sessions here would be a SECOND mechanism for an effect the
        first one already has, and the two could then disagree -- which is
        precisely the failure `set_user_disabled` has to live with and this
        method does not.

        AND THAT IS NOT WHAT `set_user_disabled` DOES, FOR A REASON.
        `users.disabled` is the mirror image: NOTHING on the authenticated
        path reads it. `session_valid` reads only `sessions`, so a disabled
        user's live session stayed valid until its own window closed -- which
        is why w133 had to end the sessions inside that method's body. The
        house pattern is "write the state, then close whatever enforcement
        does not reach"; here enforcement reaches everything, so there is
        nothing to close. Copying the session UPDATE across would not be
        following the pattern, it would be inverting it.

        ── THE UNIT OF REVOCATION IS (USER, ROLE), NOT AN ASSIGNMENT ROW ──

        `_require_role` tests membership in a SET of role NAMES, so the thing
        authorisation is a function of is "does this user still hold this
        role", not "is this particular assignment row live". Revoking one row
        by `assignment_id` would leave a duplicate grant standing and mark a
        row while changing nothing an authorisation check can see -- a writer
        whose effect on authorisation is not guaranteed. This method revokes
        EVERY live assignment of that role for that user in that
        organisation, in one statement, so the post-condition is the one the
        gate actually reads.

        ── REFUSALS, IN THE ORDER `assign_role` ALREADY ESTABLISHED ──

        The Administrator check is FIRST, before the vocabulary check and
        before any row is read, so a caller who is not an Administrator
        learns nothing about which roles exist or which users do.

        "System" IS NOT REVOCABLE HERE, on the same `HUMAN_ASSIGNABLE_ROLES`
        tuple `assign_role` refuses it on (w141) and deliberately not on a
        second, differently-derived rule. That tuple names the roles that are
        part of the HUMAN role lifecycle; "System" is the internal principal
        that `_create_system_session` is the only thing entitled to hold, and
        an Administrator able to withdraw it would hold new authority over
        the internal principal rather than the preserved authority w141 left
        them. The lifecycle is closed at both ends by one constant. This is
        fail-closed: if the platform later needs a System revoker it is an
        addition, not a repair.

        NO SELF-TARGETING RULE IS ADDED, and the absence is preserved on
        purpose. There is no such rule anywhere in this class today --
        `set_user_disabled` lets an Administrator disable their own account
        and says so, and nothing guards a "last Administrator". An
        Administrator who revokes their own Administrator role therefore
        succeeds and is refused on their next administrative request, because
        roles are resolved per request. Whether an organisation should be
        prevented from locking itself out is a product decision nobody has
        made; inventing one here would be widening an existing rule under
        cover of preserving it.

        A USER WHO DOES NOT HOLD THE ROLE IS NOT A REFUSAL. The target
        existing is checked (`get_user`, which is where the 404 and the
        organisation predicate live); holding the role is not. Revoking a
        role nobody holds returns 0 and writes nothing, because the
        post-condition asked for -- "this user does not hold this role" -- is
        already true. Returning the count rather than None is what makes that
        distinction visible to a caller and to the audit row.

        ONE STATEMENT WITH `RETURNING`, not a read followed by a write. The
        count and the change then come from the same statement, so a
        concurrent revoker cannot make this method report a number that does
        not match what it did.
        """
        self._require_role(session, "Administrator")
        if role not in ROLES:
            raise ValueError(f"Unknown role: {role!r}. Known roles: {', '.join(ROLES)}")
        if role not in HUMAN_ASSIGNABLE_ROLES:
            raise ValueError(
                f"Role {role!r} is the internal system principal and cannot be revoked from a "
                f"person. Revocable roles: {', '.join(HUMAN_ASSIGNABLE_ROLES)}."
            )

        # The target must be in the acting session's organisation. The same
        # call assign_role makes, in the same position, for the same reason:
        # a clear NotFoundError, and one that cannot distinguish "no such
        # user" from "a real user in another organisation".
        self.get_user(session, target_user_id)

        now = self._clock.now()
        revoked = self._query(
            "UPDATE role_assignments SET revoked_at = %s, revoked_by = %s "
            "WHERE org_id = %s AND user_id = %s AND role = %s AND revoked_at IS NULL "
            "RETURNING assignment_id",
            (now, session.user_id, session.org_id, target_user_id, role),
        )
        return len(revoked)

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

    def _require_any_role(self, session: Session, roles: Sequence[str]) -> None:
        """Like `_require_role`, satisfied by holding ANY ONE of `roles`. The message names them all."""
        held = {a.role for a in self.get_user_roles(session)}
        if not held.intersection(roles):
            raise AuthorizationError(f"This action requires one of the roles: {', '.join(roles)}.")

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
            "       totp_enrolled_at, created_at, full_name, registration_number, hospital "
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
        resource_id_from_result=True,
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
        resource_id_from_result=True,
        requires_session=False,
        reason="provisioning: called before any session exists",
        details_builder=lambda params, result: {"email": params.get("email")},
    )
    @transactional
    def create_user(
        self,
        org_id: uuid.UUID,
        email: str,
        password: str,
        full_name: Optional[str] = None,
        registration_number: Optional[str] = None,
        hospital: Optional[str] = None,
    ) -> uuid.UUID:
        """
        Provisioning entry point. Takes org_id directly rather than a Session
        because the first user of an organisation is created before any
        session in it can exist -- the bootstrap case. Every OTHER org-scoped
        method takes a Session; this exception is the reason ORG_SCOPED_TABLES
        drives the isolation test rather than a blanket rule about signatures.

        R10: the three signing-identity fields may be supplied here, so an
        account that will sign reports carries them from the moment it exists
        rather than acquiring them in a second step someone can forget. All
        three are OPTIONAL and default to NULL, not to "": most accounts never
        sign a report, and an empty string would print as a signature with a
        blank credential rather than refusing (see get_signing_identity).
        Whatever is supplied is validated by the same
        _clean_identity_value the administrator path uses, so "  " cannot
        enter through provisioning what set_clinician_identity would refuse.
        """
        user_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO users (user_id, org_id, email, password_hash, password_changed_at, "
            "                   full_name, registration_number, hospital, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                user_id,
                org_id,
                email,
                self._hasher.hash(password),
                now,
                _clean_identity_value(full_name, "full_name", allow_none=True),
                _clean_identity_value(registration_number, "registration_number", allow_none=True),
                _clean_identity_value(hospital, "hospital", allow_none=True),
                now,
                now,
            ),
        )
        return user_id

    # ── w153 (WAVE 3): the third bootstrap step, which never had a ──────────
    #    session-free form. Built to the human's w128 ruling
    #    (`hive/agents/god/briefs/w128-provisioning.md`), which specifies this
    #    operation exactly: "organisation row -> first user -> first
    #    Administrator role assignment, in one transaction", session-free
    #    "because it is called before any session can exist", operator-run,
    #    and with NO HTTP ROUTE.
    #
    # WHY IT IS NEEDED, COUNTED AT fd9d63b RATHER THAN ASSERTED. `INSERT INTO
    # role_assignments` occurs at exactly TWO production sites in this
    # repository -- one inside `assign_role`, which requires an existing
    # Administrator, and one inside `_create_system_session`, which only ever
    # writes 'System' and which w141 made unassignable to a person. So a fresh
    # deployment could not produce its first Administrator by any code path,
    # and the four administrative routes `clinical/admin_endpoints.py` mounted
    # were correct, tested and unreachable because of it.
    #
    # `clinical/bootstrap.py` IS NOT THIS AND NEVER WAS. Read rather than
    # inferred from its name, it creates the two POSTGRES LOGIN ROLES that
    # `schema.sql` grants to and optionally applies the schema. It writes no
    # application row of any kind. It is a DATABASE bootstrap; this is an
    # IDENTITY one, and they are not substitutes.
    #
    # THE THREE WRITES ARE DELIBERATELY OPEN-CODED, and this is the single
    # most important thing about the method. Composing it out of the existing
    # `create_organisation` and `create_user` would be the obvious
    # implementation and it would be silently WRONG: `@auditable` calls
    # `commit()` ITSELF after writing each audit row, OUTSIDE `@transactional`'s
    # `_in_transaction` re-entrancy guard, so each inner call would commit
    # mid-way. A failure at the role step would then leave a committed
    # organisation and a committed user with NO ADMINISTRATOR -- the exact
    # unusable state this method exists to prevent.
    #
    # THE TRANSACTION BOUNDARY is `@transactional` on this method. `@auditable`
    # is the OUTER decorator, so on an unexpected exception `@transactional`
    # has already ROLLED BACK before `@auditable`'s error branch writes and
    # commits its row: the data goes, the evidence stays.
    #
    # WHAT MAKES IT INERT ONCE AN ADMINISTRATOR EXISTS -- and it is not a
    # flag, because a flag is something a caller supplies:
    #   * `org_id` is generated HERE, by `uuid.uuid4()`. There is no parameter
    #     of any kind by which a caller could name an existing organisation,
    #     so this method can never add an Administrator to a tenant that
    #     already has one. The wrong version is UNWRITABLE, not merely
    #     refused -- the same defence-by-absent-parameter shape as
    #     `terminate_my_session` and `list_my_report_trend`.
    #   * the role is a SQL LITERAL, not a parameter, so 'System' cannot be
    #     reached through it either.
    #   * it is on no served surface, and the only caller is an operator who
    #     already holds the database credential and could therefore already
    #     write these three rows by hand. It grants nobody a capability they
    #     did not already have; it makes an out-of-band raw INSERT into one
    #     audited atomic act.
    # A SECOND CALL IS NOT REFUSED. It creates a SECOND organisation with that
    # organisation's own first Administrator, which is spec 4.2's
    # single-instance multi-organisation tenancy, not a bypass.
    #
    # THE ACTOR IS NULL AND THAT IS NOT AN OVERSIGHT. There is no session, so
    # the audit row's `user_id` and `actor_role` are NULL: the creator is an
    # operator outside the tenancy model and no principal exists to name. An
    # `organisations.created_by` column was considered in the w128 ruling and
    # recommended AGAINST for exactly that reason -- it could not be populated
    # truthfully. What CAN be recorded truthfully is the operating context,
    # which is why `operating_context` is a REQUIRED parameter rather than a
    # convenience, and it goes in the existing `details` JSONB.
    #
    # NO SCHEMA CHANGE. Every column written already exists and `clinical_app`
    # already holds INSERT on organisations, users and role_assignments.
    @auditable(
        action={
            "success": "organisation_provisioned",
            "error": "organisation_provisioning_failed",
            "denied": "organisation_provisioning_denied",
        },
        resource_type="organisation",
        requires_session=False,
        resource_id_from_result=True,
        reason="provisioning: called before any session exists -- this method is what makes the first session possible",
        details_builder=lambda params, result: {
            "name": params.get("name"),
            "admin_email": params.get("admin_email"),
            "operating_context": params.get("operating_context"),
        },
    )
    @transactional
    def provision_organisation(
        self,
        name: str,
        admin_email: str,
        admin_password: str,
        operating_context: str,
        full_name: Optional[str] = None,
        registration_number: Optional[str] = None,
        hospital: Optional[str] = None,
    ) -> uuid.UUID:
        """
        Organisation row -> first user -> first Administrator role assignment,
        in ONE transaction. Returns the new org_id, which is what `login`
        needs (there is no lookup by name, by design).

        Session-free, like `create_organisation` and `create_user`, and for
        the same reason. NOT an administrator action: an operator with a
        database credential and a Python interpreter runs it out of band.

        `operating_context` is REQUIRED, not optional. The audit row this
        method writes can carry no actor, so a free-text statement of who ran
        it and why is the only accountability available; making it defaultable
        would mean it was NULL on every row in practice.

        WHAT THIS METHOD DELIBERATELY DOES NOT DO:
          * It does not grant Approver. An Approver signs reports and the
            grant needs a qualification basis (ISO 15189 7.4.1.5 c);
            inventing one here would collapse the separation `assign_role`
            exists to state.
          * It does not seed retention policies. `_resolve_vcf_retention_days`
            LEFT JOINs and falls back to the code default, so their absence is
            a valid and already-tested state.
          * It does not enable or disable the organisation. `disabled` takes
            its schema default of FALSE.

        `assigned_by` is the new user's own id. The column is NOT NULL and
        there is no earlier principal in this organisation to name -- the same
        self-granted shape `_create_system_session` already uses for 'System'.
        """
        now = self._clock.now()
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        assignment_id = uuid.uuid4()

        self._execute(
            "INSERT INTO organisations (org_id, name, created_at) VALUES (%s, %s, %s)",
            (org_id, name, now),
        )
        self._execute(
            "INSERT INTO users (user_id, org_id, email, password_hash, password_changed_at, "
            "                   full_name, registration_number, hospital, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                user_id,
                org_id,
                admin_email,
                self._hasher.hash(admin_password),
                now,
                _clean_identity_value(full_name, "full_name", allow_none=True),
                _clean_identity_value(registration_number, "registration_number", allow_none=True),
                _clean_identity_value(hospital, "hospital", allow_none=True),
                now,
                now,
            ),
        )
        self._execute(
            "INSERT INTO role_assignments (assignment_id, user_id, org_id, role, "
            "                              assigned_by, assigned_at) "
            "VALUES (%s, %s, %s, 'Administrator', %s, %s)",
            (assignment_id, user_id, org_id, user_id, now),
        )
        return org_id

    @auditable(
        action="clinician_identity_set",
        resource_type="user",
        resource_id_param="target_user_id",
        requires_session=True,
        auditable=True,
        reason="the identity a signed report prints is a clinical fact about who stands behind it (R10)",
        details_builder=lambda params, result: {
            "target_user_id": str(params.get("target_user_id")),
            "registration_number": params.get("registration_number"),
        },
    )
    @transactional
    def set_clinician_identity(
        self,
        session: Session,
        target_user_id: uuid.UUID,
        full_name: str,
        registration_number: str,
        hospital: str,
    ) -> None:
        """
        Record (or correct) the three values a signed report shows for this
        user: name, registration number, hospital.

        ADMINISTRATOR ONLY, and deliberately not self-service. These are the
        credentials a report prints as evidence of who stands behind it; a
        clinician who could edit their own would make the printed credential
        self-asserted, which is the property the ruling exists to remove. The
        same separation assign_role already draws between holding a role and
        granting one.

        All three are required together. A partial update would let an
        administrator leave an account in exactly the state this method exists
        to end -- a name with no registration number -- while believing the
        identity had been recorded.

        Corrections are supported (this is an UPDATE, not an insert-once), and
        they propagate: signing_identity_for_report reads the record live, so
        a corrected registration number corrects what an already-approved
        report may print. A value frozen into the report at sign-off could not
        be corrected without re-signing, which would mean a typo forced a
        re-approval.

        Raises NotFoundError for an unknown or cross-org user (get_user's
        org-scoped read is what enforces that), ValueError for a blank value,
        and lets the database's users_org_registration_number_unique refuse a
        registration number already held by another account in this
        organisation.
        """
        self._require_role(session, "Administrator")
        self.get_user(session, target_user_id)

        self._execute(
            "UPDATE users SET full_name = %s, registration_number = %s, hospital = %s, updated_at = %s "
            "WHERE user_id = %s AND org_id = %s",
            (
                _clean_identity_value(full_name, "full_name"),
                _clean_identity_value(registration_number, "registration_number"),
                _clean_identity_value(hospital, "hospital"),
                self._clock.now(),
                target_user_id,
                session.org_id,
            ),
        )

    @auditable(
        action="signing_identity_read",
        resource_type="user",
        requires_session=True,
        auditable=False,
        reason="a read of who a user is, not an action on them; the sign-off that uses it is itself audited",
    )
    @transactional
    def get_signing_identity(self, session: Session, user_id: uuid.UUID) -> Dict[str, Any]:
        """
        The three values a report may print for this user, read from the user
        record -- never from anything a caller typed.

        Returns {"user_id", "full_name", "registration_number", "hospital"}.

        Raises NotFoundError for an unknown or cross-org user, and
        IncompleteClinicianIdentityError -- naming every missing field -- when
        the record does not carry all three. See that exception's docstring
        for why an incomplete identity is refused rather than printed with a
        placeholder.
        """
        user = self.get_user(session, user_id)
        missing = [
            field
            for field in ("full_name", "registration_number", "hospital")
            if not (getattr(user, field) or "").strip()
        ]
        if missing:
            raise IncompleteClinicianIdentityError(
                f"User {user_id} cannot be shown as the signing clinician: the user record is missing "
                f"{', '.join(missing)}. A clinical report shows the signing clinician's name, registration "
                "number and hospital (R10), so a report will not be produced with any of them absent. An "
                "Administrator can record them with set_clinician_identity()."
            )
        return {
            "user_id": user.user_id,
            "full_name": user.full_name,
            "registration_number": user.registration_number,
            "hospital": user.hospital,
        }

    @auditable(
        action="signing_identity_read",
        resource_type="report",
        requires_session=True,
        auditable=False,
        reason="reading who signed is not itself a resource action; the approval it reads is already audited",
    )
    @transactional
    def signing_identity_for_report(self, session: Session, report_id: uuid.UUID) -> Dict[str, Any]:
        """
        THE ONE PLACE A RENDERER ASKS "whose name, registration number and
        hospital does this report show?" -- answered from the approver's user
        record, so a report cannot show an identity that contradicts it.

        Returns get_signing_identity's dict for the report's approver_id.

        Raises:
          - NotFoundError for an unknown or cross-org report id (the usual
            meaning throughout this file: a caller in another organisation
            cannot learn that the report exists);
          - ValueError when the report has not been approved. NOT-YET-SIGNED
            IS A DIFFERENT ANSWER FROM SIGNED-BY-SOMEONE, and an empty
            identity here would be a report claiming an unnamed signature;
          - IncompleteClinicianIdentityError when the approver's record lacks
            any of the three.
        """
        row = self._query_one(
            "SELECT approver_id FROM reports WHERE org_id = %s AND id = %s",
            (session.org_id, report_id),
        )
        if row is None:
            raise NotFoundError(f"Report {report_id} not found")
        approver_id = row[0]
        if approver_id is None:
            raise ValueError(
                f"Report {report_id} has not been approved and has no signing clinician. Nothing may be "
                "printed as its signature until it is approved (spec 13.3)."
            )
        return self.get_signing_identity(session, approver_id)

    @auditable(
        action=lambda params, result: "user_disabled" if params.get("disabled") else "user_enabled",
        resource_type="user",
        resource_id_param="target_user_id",
        requires_session=True,
        details_builder=lambda params, result: {"target_user_id": str(params.get("target_user_id"))},
    )
    @transactional
    def set_user_disabled(self, session: Session, target_user_id: uuid.UUID, disabled: bool) -> None:
        """
        Offboarding. Never a DELETE -- that would orphan the audit chain.

        AND IT ENDS THE USER'S LIVE SESSIONS (w133). Until w133 this method
        wrote one row in `users` and nothing else, and that was a real gap
        rather than a tidy omission: `session_valid` reads only `sessions` and
        `_require_role` reads only `role_assignments`, so NOTHING anywhere
        consulted `users.disabled` once a session existed. A user disabled at
        10:00 passed validation at 10:01 and kept every Administrator-gated
        capability -- including disabling somebody else -- for up to the 24h
        absolute cap, with the 30-minute idle window refreshed by their own
        activity. Measured, not reasoned: the two capability rows in
        test_w133_staff_revocation.py failed with "DID NOT RAISE" against the
        previous version of this method.

        The statement is in THIS body, not a second decorated call, because
        @auditable commits after every audit row it writes: a composition
        would commit "user_disabled" while the sessions ran on, and an audit
        that records something that did not happen is worse than one that
        records nothing.

        `terminated_by` holds the ACTING Administrator, consistent with
        terminate_session. `terminated_at IS NULL` makes a second disable a
        no-op rather than a rewrite. ONLY WHEN `disabled` IS TRUTHY: re-enabling
        must not terminate anything, and it does not resurrect anything either.

        POINT-IN-TIME, NOT CONTINUOUS, AND THE RESIDUE IS REAL. This ends the
        sessions that exist WHEN IT RUNS. `session_valid` is unchanged -- one
        row from `sessions` by primary key, no join on `users` -- so a
        `disabled` flag set by any other route (direct SQL, a restore, a
        future bootstrap) leaves that user's live sessions valid until their
        own window closes. Closing that residue means reading the principal
        row on every authenticated request, which is a change to the hottest
        path in this layer and was deliberately not made here. Proved, not
        described, by test_w133_staff_revocation.py::TestTheResidueIsReal.

        WHAT IT DOES NOT DO: it does not touch role resolution. `_require_role`
        still reads `role_assignments` and still never consults this flag. The
        capability is removed by the session ending, which is what
        `_get_session` revalidates on every request -- not by the roles
        changing. That distinction is why the staff tests make a real
        role-gated call through the request path rather than asserting on
        validation alone.
        """
        self._require_role(session, "Administrator")
        self.get_user(session, target_user_id)
        now = self._clock.now()
        self._execute(
            "UPDATE users SET disabled = %s, updated_at = %s WHERE user_id = %s AND org_id = %s",
            (disabled, now, target_user_id, session.org_id),
        )

        if disabled:
            self._execute(
                "UPDATE sessions SET terminated_at = %s, terminated_by = %s "
                "WHERE user_id = %s AND org_id = %s AND terminated_at IS NULL",
                (now, session.user_id, target_user_id, session.org_id),
            )

    @auditable(
        action="totp_enrolled",
        resource_type="totp",
        # THE CREDENTIAL EXPOSURE (w121). This method returns the plaintext
        # backup codes. With no declaration the decorator used to write that
        # list into resource_id, and audit_log is append-only by grant -- the
        # codes could never be removed. The enrolment record names WHOSE second
        # factor was set; the codes themselves are recorded nowhere, and only
        # their COUNT appears in details.
        resource_id_param="target_user_id",
        requires_session=True,
        # w164. WITHOUT THIS THE AUDIT ROW WOULD HAVE HELD THE CREDENTIALS.
        # `@auditable` writes `resource_id=str(result)` for any method that
        # does not declare where its identifier comes from, and this method's
        # result is the secret and the eight backup codes. At 4ab1fbf that was
        # latent only because nobody could reach `enrol_totp` at all; making it
        # reachable makes it reachable too. The identifier of a TOTP enrolment
        # is the USER it was issued to, which is a parameter, so it is declared
        # as one -- exactly the defect w120 added this parameter for.
        details_builder=lambda params, result: {
            "target_user_id": str(params.get("target_user_id")),
            "backup_codes_issued": len(result.backup_codes) if result else 0,
        },
    )
    @transactional
    def enrol_totp(self, session: Session, target_user_id: uuid.UUID) -> TotpEnrolment:
        """
        Administrator-initiated TOTP enrolment.

        THE SECRET IS GENERATED HERE AND THERE IS NO PARAMETER FOR IT (w164).
        This method used to accept the secret, which meant the caller chose it,
        and a secret the caller chose can be weak, shared between accounts or a
        constant. `generate_totp_secret()` takes 160 bits from `secrets`; there
        is no argument through which a worse one could arrive.

        THE RETURN VALUE IS THE ONLY MOMENT either the secret or the backup
        codes exist in readable form. The secret is stored encrypted under the
        deployment's key and is selected by no read method on this class; the
        codes are stored only as bcrypt hashes. Neither can be recovered,
        displayed again, or read out of the database by anyone -- an
        administrator who loses this response re-enrols.

        RE-ENROLMENT VOIDS WHAT CAME BEFORE, and that is a security property
        rather than tidiness. Before this wave a second enrolment added eight
        MORE codes beside the old ones, so replacing a compromised
        authenticator left the compromised account's backup codes working.
        `totp_last_used_step` is cleared for the mirror-image reason: the new
        secret's steps have not been spent, and inheriting the old
        authenticator's counter would refuse valid codes until the clock caught
        up.

        ATOMIC. `@transactional` wraps the whole body, so the secret, the
        voiding of the old codes and all eight new rows land together or not at
        all -- there is no state in which the column has been replaced and the
        codes have not.
        """
        self._require_role(session, "Administrator")
        self.get_user(session, target_user_id)
        if self._cipher is None:
            raise ConfigurationError(
                f"Enrolling TOTP requires a secret cipher; {TOTP_SECRET_KEYS_ENV} is not configured."
            )

        now = self._clock.now()
        secret = generate_totp_secret()
        self._execute(
            "UPDATE users SET totp_secret = %s, totp_enrolled_at = %s, totp_last_used_step = NULL, "
            "                 updated_at = %s "
            "WHERE user_id = %s AND org_id = %s",
            (self._cipher.encrypt(secret), now, now, target_user_id, session.org_id),
        )
        # MARKED USED RATHER THAN DELETED, because nothing in this schema
        # deletes (the D0b ruling; `clinical_app` holds no DELETE on this table
        # and the REVOKE is explicit). A used row is also the honest record:
        # the code existed and stopped being valid at a known moment.
        self._execute(
            "UPDATE totp_backup_codes SET used_at = %s WHERE user_id = %s AND org_id = %s AND used_at IS NULL",
            (now, target_user_id, session.org_id),
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
        return TotpEnrolment(secret=secret, backup_codes=codes)

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
        resource_id_from_result=True,
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
        dob: "_datetime.date | str",
        sex: str,
    ) -> uuid.UUID:
        """
        Create a patient record in this organisation.

        REQUIRES THE ORDERER ROLE (w162, human ruling 2026-09-19: "PATIENT
        REGISTRATION + CONSENT -- the ORDERER/CLINICIAN owns both. Do NOT make
        Administrator registration mandatory. Do NOT make Lab Technician a
        patient identity creator.").

        THE TOKEN IS READ OFF THE VOCABULARY, NOT CHOSEN. `ROLES` is what the
        schema's CHECK on `role_assignments.role` enumerates, and exactly one
        of its seven names a clinician -- the same one `create_order` already
        names for the same reason ("Ordering a test is the clinical decision
        the Orderer role names"). Registration and consent are the acts that
        precede an order and they belong to the principal who places it.

        NO HIERARCHY IS INTRODUCED, which is the half a later reader will be
        tempted to undo. An Administrator is NOT implicitly an Orderer here,
        exactly as `create_order` already says: the ruling gives Administrator
        organisation/staff administration, and a role that silently subsumes
        the others is the RBAC redesign the same ruling forbids. The ruling
        forbids MANDATORY Administrator registration and is silent on whether
        Administrator may ALSO register; the narrower reading is implemented
        and the widening is carded, because widening a gate later is a line
        and a test while narrowing one after deployment withdraws a
        capability somebody is using.

        UNTIL w162 THIS METHOD ENFORCED NO ROLE AT ALL. Every authenticated
        session in the organisation could mint a patient identity, and w155
        correctly refused to guess a role without a ruling. The ruling is now
        in force and this is it.

        `dob` ACCEPTS A `date` OR AN ISO-8601 STRING, AND THE PARSE IS BELOW
        THE GATE. That is not a convenience: `clinical/clinic_endpoints.py`
        takes the date as a `str` because Pydantic runs BEFORE the handler and
        therefore before this gate, so a `datetime.date` annotation on the
        request model would answer "that is not a date" to a caller whose
        authorisation had not been evaluated. The format check belongs where
        the `sex` vocabulary already is -- after the gate -- so it lives here.
        Existing callers passing a `date` are unaffected.

        THE ORDER MATTERS AND IS PINNED BY A TEST: the role gate is the FIRST
        EXECUTABLE STATEMENT, above the format parse and above the sex
        vocabulary, so an unauthorised caller's refusal cannot vary with the
        content of the body.
        """
        self._require_role(session, "Orderer")

        if isinstance(dob, str):
            try:
                dob = _datetime.date.fromisoformat(dob)
            except ValueError:
                raise ValueError(f"Date of birth must be an ISO-8601 date (YYYY-MM-DD), not {dob!r}")

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
        resource_id_from_result=True,
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
        """
        Record a new consent. Optionally supersedes an old one.

        REQUIRES THE ORDERER ROLE (w162, the same ruling `create_patient`
        carries in full). Consent capture is the second half of the act the
        ruling names, and the tree already treats it as the ordering
        clinician's: `create_order` re-reads this row, checks its scope
        against the order's `required_scope`, and refuses without an active
        one. Gating creation while leaving capture open would move the hole
        rather than close it -- the argument `place_order` already makes.

        THE GATE IS THE FIRST EXECUTABLE STATEMENT, above the patient lookup,
        so an unauthorised caller's 403 is a function of its own roles alone
        and carries no information about whether the named patient exists --
        in this organisation or in another.

        `recorded_by` IS `session.user_id` AND IS NOT A PARAMETER. The actor
        is derived from the authenticated principal, never claimed by the
        caller: under ISO 15189 the record of who performed an action is
        evidence, and a claimed actor is indistinguishable in the log from a
        verified one.
        """
        self._require_role(session, "Orderer")

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
        resource_id_from_result=True,
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
        """
        Create a new order in draft state. Requires valid patient, test, and consent.

        Requires the Orderer role (w129 phase B). Ordering a test is the
        clinical decision the Orderer role names, and until w129 nothing
        consulted it: `Orderer` occurred exactly once in all non-test Python
        of clinical/ and geper/, inside the ROLES tuple, so every
        authenticated session in the organisation could order equally.

        Gated with `_require_role` and the session model -- the same
        mechanism the nine pre-existing role gates use. No hierarchy is
        introduced: an Administrator is not implicitly an Orderer, because
        the ruling gives Administrator organisation/staff administration and
        a role that silently subsumes the others is the RBAC redesign the
        same ruling forbids.

        SUPERSEDED AT w162, AND THE OLD TEXT IS REPLACED RATHER THAN LEFT TO
        ROT. This docstring used to say that `create_patient`,
        `record_consent` and `withdraw_consent` were NOT gated "because no
        role in the vocabulary names them, and inventing one from
        create_order's proximity would be reading the model off a workflow
        diagram instead of off the code". That reasoning was correct while it
        stood and it is no longer the state of the tree: the human ruled on
        2026-09-19 that the ordering clinician owns registration and consent,
        and all three are now `_require_role(session, "Orderer")` as their
        first executable statement. The role was not inferred from proximity;
        it was RULED, and the token was then resolved against `ROLES`.
        """
        self._require_role(session, "Orderer")

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
            "SELECT test_id FROM tests WHERE test_id = %s AND org_id = %s AND status = 'active'",
            (test_id, session.org_id),
        )
        if test is None:
            # Unknown, another organisation's, and RETIRED all read the same:
            # a retired test is withdrawn from the catalogue and cannot be
            # ordered, and the caller must not be able to tell which of the
            # three it was.
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
        resource_id_param="order_id",
        requires_session=True,
        details_builder=lambda params, result: {
            "order_id": str(params.get("order_id")),
        },
    )
    @transactional
    def place_order(self, session: Session, order_id: uuid.UUID) -> None:
        """
        Place an order, transitioning it from draft to placed state.

        Requires the Orderer role (w129 phase B). This is the act that
        commits the laboratory to the work; it belongs to whoever may create
        the order, and gating creation while leaving placement open would
        move the hole rather than close it.
        """
        self._require_role(session, "Orderer")

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
        resource_id_param="order_id",
        requires_session=True,
        details_builder=lambda params, result: {
            "order_id": str(params.get("order_id")),
        },
    )
    @transactional
    def cancel_order(self, session: Session, order_id: uuid.UUID) -> None:
        """
        Cancel an order.

        Requires the Orderer role (w129 phase B). Withdrawal is the third and
        last of the ordering operations this file defines -- counted by
        scanning every method definition, not by grepping for a word -- and
        it moves `orders.state` exactly as the other two do.
        """
        self._require_role(session, "Orderer")

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

    # ── the test catalogue (stage 2) ─────────────────────────────────────────
    #
    # `tests` and `test_genes` have always been in the schema, and
    # `create_order` has always required a `test_id` into them, but no method
    # of any kind wrote either table, so no production path could create the
    # subject of an order. These three are that path.
    #
    # THE CATALOGUE IS PER-ORGANISATION (`tests.org_id`) and is managed by the
    # Administrator only: who may define a panel is a controlled act, not a
    # clinician's. Every statement binds `org_id` from the session. A test is
    # never edited, only retired: `test_id` is the stable identifier orders
    # point at, and rewriting what a placed order referred to would change the
    # clinical meaning of a record that already exists. To change a panel,
    # retire it and create its successor.
    #
    # `approved_at` / `approved_by` are stamped at creation: the Administrator
    # who creates the entry is the approving act. A separate approval step is
    # a product decision nobody has made, and the columns are there for it.

    _GENE_SYMBOL = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,24}$")
    _MAX_TEST_NAME = 200
    _MAX_PANEL_GENES = 1000

    @auditable(
        action="test_created",
        resource_type="test",
        # The returned UUID IS the new test's id, so this method opts in (master's w121
        # default records no return value unless asked, and without this the row
        # would say "<none>").
        resource_id_from_result=True,
        requires_session=True,
        details_builder=lambda params, result: {
            "name": params.get("name"),
            "assembly": params.get("assembly"),
            "gene_count": len(params.get("genes") or []),
        },
    )
    @transactional
    def create_test(
        self,
        session: Session,
        name: str,
        assembly: str,
        genes: Sequence[str],
        cdsco_class: Optional[str] = None,
    ) -> uuid.UUID:
        """
        Add an active test to the caller's organisation's catalogue.

        Administrator only. `assembly` must be one `ASSEMBLY_CANONICAL`
        recognises and is stored CANONICAL, so a test saved as 'hg38' and one
        saved as 'GRCh38' are the same test. `genes` is a non-empty list of
        symbols, de-duplicated case-insensitively, order preserved. An active
        test of the same name in this organisation is refused (ValueError).
        """
        self._require_role(session, "Administrator")

        clean_name = (name or "").strip()
        if not clean_name or len(clean_name) > self._MAX_TEST_NAME:
            raise ValueError(f"Test name must be 1-{self._MAX_TEST_NAME} characters")
        canonical = ASSEMBLY_CANONICAL.get((assembly or "").strip())
        if canonical is None:
            raise ValueError(f"Unrecognised assembly {assembly!r}")
        if isinstance(genes, str):
            raise ValueError("genes must be a list of symbols, not a string")
        symbols: list[str] = []
        seen: set[str] = set()
        for raw in genes:
            symbol = (raw or "").strip() if isinstance(raw, str) else ""
            if not self._GENE_SYMBOL.match(symbol):
                raise ValueError(f"Invalid gene symbol {raw!r}")
            if symbol.upper() not in seen:
                seen.add(symbol.upper())
                symbols.append(symbol)
        if not symbols:
            raise ValueError("A test needs at least one gene")
        if len(symbols) > self._MAX_PANEL_GENES:
            raise ValueError(f"A test may list at most {self._MAX_PANEL_GENES} genes")
        clean_class = cdsco_class.strip() if cdsco_class and cdsco_class.strip() else None

        if self._query_one(
            "SELECT 1 FROM tests WHERE org_id = %s AND status = 'active' AND lower(name) = lower(%s)",
            (session.org_id, clean_name),
        ):
            raise ValueError(f"An active test named {clean_name!r} already exists")

        test_id = uuid.uuid4()
        now = self._clock.now()
        try:
            self._execute(
                "INSERT INTO tests (test_id, org_id, name, cdsco_class, assembly, status, "
                "                   approved_at, approved_by, created_at) "
                "VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s)",
                (test_id, session.org_id, clean_name, clean_class, canonical, now, session.user_id, now),
            )
        except Exception as exc:  # noqa: BLE001 -- narrowed immediately below
            # The check above is a courtesy and is racy; migration 0008's
            # partial unique index is the control. Two concurrent creations of
            # one name both pass the check and the second INSERT lands here.
            if getattr(exc, "sqlstate", None) == "23505" and (
                getattr(getattr(exc, "diag", None), "constraint_name", None) == "tests_org_active_name_unique"
            ):
                raise ValueError(f"An active test named {clean_name!r} already exists") from exc
            raise
        for symbol in symbols:
            self._execute(
                "INSERT INTO test_genes (gene_id, test_id, gene, added_at, added_by) VALUES (%s, %s, %s, %s, %s)",
                (uuid.uuid4(), test_id, symbol, now, session.user_id),
            )
        return test_id

    @auditable(
        action="test_retired",
        resource_type="test",
        resource_id_param="test_id",
        requires_session=True,
        details_builder=lambda params, result: {"test_id": str(params.get("test_id"))},
    )
    @transactional
    def retire_test(self, session: Session, test_id: uuid.UUID) -> None:
        """
        Withdraw a test from the catalogue so it can no longer be ordered.

        Administrator only. Existing orders keep their reference (the row is
        not deleted and is still returned by `list_tests(include_retired=True)`).
        An unknown, cross-organisation or already-retired test is NotFoundError.
        """
        self._require_role(session, "Administrator")
        row = self._query_one(
            "SELECT test_id FROM tests WHERE test_id = %s AND org_id = %s AND status = 'active'",
            (test_id, session.org_id),
        )
        if row is None:
            raise NotFoundError(f"Test {test_id} not found")
        self._execute(
            "UPDATE tests SET status = 'retired' WHERE test_id = %s AND org_id = %s",
            (test_id, session.org_id),
        )

    @auditable(
        action="test_read",
        resource_type="test",
        requires_session=True,
        auditable=False,
        reason="read is not a resource action",
    )
    @transactional
    def list_tests(self, session: Session, include_retired: bool = False) -> list[dict[str, Any]]:
        """
        The caller's organisation's catalogue. Administrator or Orderer: the
        former maintains it, the latter must be able to see what can be
        ordered. Neither role implies the other.
        """
        held = {a.role for a in self.get_user_roles(session)}
        if not held & {"Administrator", "Orderer"}:
            raise AuthorizationError("This action requires the Administrator or Orderer role.")
        rows = self._query(
            "SELECT t.test_id, t.name, t.cdsco_class, t.assembly, t.status, t.created_at, "
            "       COALESCE(array_agg(g.gene ORDER BY g.added_at, g.gene) "
            "                FILTER (WHERE g.gene IS NOT NULL), '{}') "
            "FROM tests t LEFT JOIN test_genes g ON g.test_id = t.test_id "
            "WHERE t.org_id = %s AND (%s OR t.status = 'active') "
            "GROUP BY t.test_id ORDER BY t.created_at, t.name",
            (session.org_id, include_retired),
        )
        return [
            {
                "test_id": r[0],
                "name": r[1],
                "cdsco_class": r[2],
                "assembly": r[3],
                "status": r[4],
                "created_at": r[5],
                "genes": list(r[6]),
            }
            for r in rows
        ]

    @auditable(
        action="sample_received",
        resource_type="sample",
        resource_id_from_result=True,
        requires_session=True,
        details_builder=lambda params, result: {
            "order_id": str(params.get("order_id")),
            # w157. WAS `params.get("type")`, WHICH IS NOT A PARAMETER OF THIS
            # METHOD. `auditable` builds `params` by binding the ORIGINAL
            # function's signature (`sig.bind(self, *args, **kwargs)`), so its
            # keys are the real parameter names -- and this one is
            # `sample_type`. Every `sample_received` audit row this repository
            # has ever written therefore recorded `"type": null`, for every
            # specimen, silently: the row landed, the count was right, and the
            # one clinical fact it carried was absent.
            #
            # Nothing observed it. No test asserted on this action's details at
            # all, and no production caller existed to write a row in the first
            # place (zero call sites at 764d646) -- which is why wiring the
            # route is what surfaced it. The OUTPUT key stays `"type"`, because
            # that is the column it describes (`samples.type`); only the lookup
            # is corrected.
            "type": params.get("sample_type"),
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
        """
        Record receipt of a sample. Transitions order to sample_awaited state if needed.

        Requires the Lab technician role (w129 phase B). Receiving a specimen
        is bench work, and this method stamps `received_by` and
        `collected_by` from the session -- laboratory provenance, which is
        exactly the claim a role qualifies. Until w129 `Lab technician`
        occurred once in all non-test Python of clinical/ and geper/, inside
        the ROLES tuple, and gated nothing.

        The two operational roles do not overlap and neither subsumes the
        other: an Orderer may order and may not receive, a Lab technician may
        receive and may not order. Administrator is not a superuser over
        either -- the ruling gives it organisation/staff administration.
        """
        self._require_role(session, "Lab technician")

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
        resource_id_param="sample_id",
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
        """
        Record QC result for a sample.

        Requires the Lab technician role (w129 phase B). The QC verdict
        decides whether a specimen is fit to sequence and is stamped with
        `qc_recorded_by` from the session; it is the operation in this group
        with the clearest patient-safety consequence, and before w129 any
        authenticated session in the organisation could sign it.
        """
        self._require_role(session, "Lab technician")

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
        resource_id_param="consent_id",
        requires_session=True,
        details_builder=lambda params, result: {
            "consent_id": str(params.get("consent_id")),
        },
    )
    @transactional
    def withdraw_consent(self, session: Session, consent_id: uuid.UUID) -> None:
        """
        Withdraw an active consent.

        REQUIRES THE ORDERER ROLE (w162, the same ruling `create_patient`
        carries in full). Withdrawal is the third and last of the consent
        operations this file defines -- counted by scanning every method
        definition, not by grepping for a word -- and it is the inverse of
        `record_consent`, so it answers to the same principal. A withdrawal
        open to anyone authenticated would let a role that may not record
        consent destroy the basis for an order.

        THE GATE IS THE FIRST EXECUTABLE STATEMENT, above the consent lookup,
        for the reason `record_consent` states.

        WHAT THIS IS NOT: a deletion. The row stays and `withdrawn_at` /
        `withdrawn_by` are stamped on it, so the consent remains readable as
        the historical fact it is. The `withdrawn_at IS NULL` predicate below
        is also why withdrawing an already-withdrawn consent is NOT FOUND
        rather than a distinct refusal -- it is simply not among the active
        ones, and a distinct status would tell a caller that a consent it may
        not act on nevertheless exists.

        WHETHER WITHDRAWAL IS RETROACTIVE -- what it means for orders already
        placed against this consent -- IS NOT DECIDED HERE AND IS NOT DECIDED
        ANYWHERE IN THIS TREE. This method touches `consents` and nothing
        else; no order state moves, no report is recalled. That is the
        behaviour as shipped, it is unchanged by w162, and changing it would
        be a product ruling nobody has made. Carded, not guessed.
        """
        self._require_role(session, "Orderer")

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

    # ── the portal read surface: THE SECOND AXIS OF ISOLATION (w130) ─────────
    #
    # THE ONE RULE, AND EVERY METHOD BELOW OBEYS IT:
    #
    #     The patient is bound FROM THE SESSION. It is never an argument.
    #
    # NOT a comparison. Not `if requested_patient_id != session.patient_id:
    # raise`. A value read from the session and never from a caller argument is
    # STRUCTURAL; a comparison is REMEMBERED, and every tenancy property in
    # this codebase was built the first way -- see Session's own docstring,
    # which says org_id "is read from here and never from a caller argument,
    # which is what makes the isolation structural rather than remembered".
    #
    # THE PARAMETER DOES NOT EXIST, which is the strong form of the rule: if a
    # patient id cannot be passed in, the class of bug where it is passed in
    # unvalidated cannot exist.
    #
    # MEASURED, NOT ARGUED. The version of these two methods that took
    # `patient_id` as an argument and bound only org_id PASSED 41 OF 46 TESTS
    # in this file. It passed every cross-organisation test, every
    # authentication test, every enrolment test, every decision-7 test, and it
    # returned the right record whenever the supplied id happened to match the
    # session. The five it failed are the five that hand it a DIFFERENT, REAL,
    # SAME-ORGANISATION patient's id.
    #
    # EVERY READ ANCHORS ON `patients`. Only four tables carry a patient_id
    # column -- patients, external_identifiers, consents, orders -- so anything
    # further downstream is reachable only by joining back up to `patients` and
    # binding both org_id and patient_id at the anchor. patient_id is
    # deliberately NOT denormalised onto samples, runs, VCFs, interpretations
    # or reports.

    def _require_patient_session(self, session: Any) -> None:
        """
        DECISION 8: a staff session does not automatically become a patient
        session. Patient access is explicit.

        A Session and a PatientSession both carry org_id, so a staff session
        handed to a portal method would duck-type into the org predicate and
        then fail on the missing patient_id attribute with an AttributeError --
        an accident that looks like a bug rather than a refusal. This makes it
        a refusal.
        """
        if not isinstance(session, PatientSession):
            raise AuthorizationError(
                "This is a patient portal operation and requires a patient session. A staff "
                "session does not automatically become a patient session."
            )

    @auditable(
        action="portal_patient_record_read",
        resource_type="patient",
        requires_session=False,
        auditable=False,
        reason="every patient-record reader in this layer is auditable=False, and whether a patient "
        "portal changes that is an OPEN RULING -- it is one decision covering get_patient, get_order, "
        "get_consent, get_sample and the held find_variant_trends_for_patient, and it changes the "
        "trends endpoint's stated two-round-trip cost. Deciding it here, for one method, would settle "
        "by accident a question that needs settling on purpose",
    )
    @transactional
    def get_my_patient_record(self, patient_session: "PatientSession") -> dict[str, Any]:
        """
        The authenticated patient's own record. TAKES NO PATIENT IDENTIFIER.

        Returns name, date of birth and sex, because that is the whole of what
        a `patients` row holds. There is no contact detail on it and no report
        content anywhere in this schema -- `reports` has no content column, so
        "view my report" is not a route waiting to be written.
        """
        self._require_patient_session(patient_session)
        row = self._query_one(
            "SELECT patient_id, name, dob, sex, created_at FROM patients WHERE patient_id = %s AND org_id = %s",
            # BOTH bound from the session. The org predicate answers "which
            # organisation?" and the patient predicate answers "which patient?"
            # -- in the same position, with the same status.
            (patient_session.patient_id, patient_session.org_id),
        )
        if row is None:
            # Reachable only if the patient row was removed under a live
            # session. The same constant every other patient-axis miss uses.
            raise NotFoundError(PATIENT_NOT_FOUND)
        return {
            "patient_id": row[0],
            "name": row[1],
            "dob": row[2],
            "sex": row[3],
            "created_at": row[4],
        }

    @auditable(
        action="portal_orders_read",
        resource_type="order",
        requires_session=False,
        auditable=False,
        reason="see get_my_patient_record -- the same open ruling covers this method",
    )
    @transactional
    def list_my_test_orders(self, patient_session: "PatientSession") -> list[dict[str, Any]]:
        """
        The authenticated patient's own test orders. TAKES NO PATIENT
        IDENTIFIER.

        ANCHORED ON `patients`, not on `orders`. orders does carry patient_id,
        so the join is not strictly required for this one table -- it is
        written this way because it is the shape every deeper portal read will
        need, and because the anchor is where both predicates are bound.

        WHAT IS STILL DELIBERATELY NOT RETURNED: `orders.state`. w134 re-counted
        it and the refusal stands, for a sharper reason than "three values have
        no writer". `sample_awaited` is written by `receive_sample` AFTER the
        specimen has arrived (see that method), so the column's own vocabulary
        is inverted: a patient whose sample is already sequenced would be shown
        "awaiting sample". The column is not renderable to a patient under any
        name, and no caller of this method is given the chance to try.

        WHAT IS RETURNED INSTEAD (w134): `stages`, DERIVED. Six named stages,
        each carrying one value out of a vocabulary that this repository's
        WRITERS support -- see `derive_order_stages`, which is where every
        refusal is justified value by value. Nothing here is stored; there is
        no status column, no status table and no transition. Delete
        `derive_order_stages` and the workflow is exactly what it was.

        THE COUNTS THE DERIVATION NEEDS ARE AGGREGATED IN THE DATABASE, in two
        lateral subqueries, each of which is an ungrouped aggregate and so
        returns exactly one row. That is not a micro-optimisation: a plain
        LEFT JOIN to `samples` and to `reports` would FAN OUT, and an order
        with two samples would appear twice in a patient's list of orders.

        EVERY SUBQUERY IS ORG-SCOPED ON ITS OWN, correlated to `o.org_id`
        rather than trusting the outer join to have narrowed it. The chain
        samples -> sequencing_runs -> vcfs -> interpretations -> reports is
        followed with the composite (org_id, id) keys the schema declares, so
        no hop can cross an organisation even if a bare id were duplicated.
        """
        self._require_patient_session(patient_session)
        rows = self._query(
            "SELECT o.order_id, o.created_at, o.placed_at, o.cancelled_at, "
            "       sm.total, sm.qc_pending, sm.qc_passed, sm.qc_failed, "
            "       an.interpretations, an.reports_total, an.reports_signed_off, an.reports_released "
            "FROM orders o "
            "JOIN patients p ON p.patient_id = o.patient_id AND p.org_id = o.org_id "
            "LEFT JOIN LATERAL ("
            "    SELECT count(*) AS total, "
            "           count(*) FILTER (WHERE s.qc_status = 'pending') AS qc_pending, "
            "           count(*) FILTER (WHERE s.qc_status = 'passed')  AS qc_passed, "
            "           count(*) FILTER (WHERE s.qc_status = 'failed')  AS qc_failed "
            "      FROM samples s "
            "     WHERE s.org_id = o.org_id AND s.order_id = o.order_id"
            ") sm ON TRUE "
            "LEFT JOIN LATERAL ("
            "    SELECT count(DISTINCT i.id) AS interpretations, "
            "           count(DISTINCT rep.id) AS reports_total, "
            "           count(DISTINCT rep.id) FILTER (WHERE rep.state IN ('approved', 'released')) "
            "               AS reports_signed_off, "
            "           count(DISTINCT rep.id) FILTER (WHERE rep.state = 'released' "
            "               AND EXISTS (SELECT 1 FROM release_events re "
            "                            WHERE re.org_id = rep.org_id AND re.report_id = rep.id "
            "                             AND re.consumer = 'patient_portal')) AS reports_released "
            "      FROM samples s2 "
            "      JOIN sequencing_runs r ON r.org_id = s2.org_id AND r.sample_id = s2.sample_id "
            "      JOIN vcfs v            ON v.org_id = r.org_id  AND v.sequencing_run_id = r.id "
            "      JOIN interpretations i ON i.org_id = v.org_id  AND i.vcf_id = v.id "
            "      LEFT JOIN reports rep  ON rep.org_id = i.org_id AND rep.interpretation_id = i.id "
            "     WHERE s2.org_id = o.org_id AND s2.order_id = o.order_id"
            ") an ON TRUE "
            "WHERE p.patient_id = %s AND p.org_id = %s "
            "ORDER BY o.created_at",
            (patient_session.patient_id, patient_session.org_id),
        )
        return [
            {
                "order_id": r[0],
                "created_at": r[1],
                "placed_at": r[2],
                "cancelled_at": r[3],
                "stages": derive_order_stages(
                    placed_at=r[2],
                    cancelled_at=r[3],
                    sample_count=r[4] or 0,
                    qc_pending=r[5] or 0,
                    qc_passed=r[6] or 0,
                    qc_failed=r[7] or 0,
                    interpretation_count=r[8] or 0,
                    report_count=r[9] or 0,
                    reports_signed_off=r[10] or 0,
                    reports_released=r[11] or 0,
                ),
            }
            for r in rows
        ]

    # ── w135: the patient's own RELEASED reports ─────────────────────────────
    #
    # WHAT A RELEASED REPORT CONSISTS OF IN THIS MODEL, STATED AS A
    # MEASUREMENT BEFORE ANYTHING IS RETURNED. `reports` carries org_id, id,
    # interpretation_id, created_at, created_by, state, approver_id,
    # approved_at, content_hash, tombstoned_at, tombstoned_by and
    # retention_days -- and NO CONTENT COLUMN. A `reports` row is a governance
    # record ABOUT a document, not the document. The document itself is
    # rendered outside this schema, from `interpretations.run_document`, by
    # geper/report/clinical_report_builder.py, and is not stored here in any
    # form.
    #
    # SO THE TWO METHODS BELOW RETURN METADATA AND RELEASE STATE, AND THAT IS
    # THE WHOLE OF WHAT THERE IS TO RETURN. This is not a reduced version of a
    # richer surface: there is no richer surface in this schema, and inventing
    # report-content storage to make one is the thing the brief forbids.
    #
    # WHY `run_document` IS NOT THE SUBSTITUTE, and this is the one edge a
    # future implementer will be tempted by. `get_interpretation` returns the
    # entire raw run_document with NO report-state check at all -- so routing a
    # patient through it would hand out the unapproved pipeline output of a
    # DRAFT report, which is precisely the "raw result data must not bypass
    # report-release rules" failure. Neither method below reads
    # `interpretations`, `vcfs`, `variants` or `run_document`, and neither
    # returns `interpretation_id`: an interpretation id is the handle that
    # addresses raw pipeline output, and handing it to a patient makes the
    # bypass one route away instead of zero.
    #
    # THE RELEASE REQUIREMENT, AND WHY `require_release` IS NOT CALLED HERE.
    # `require_release` is the gate every STAFF delivery path calls, and since
    # w129 phase B it is an AUTHORITY gate as well as a state gate: its first
    # line is `self._require_role(session, "Approver")`. A patient never holds
    # Approver and never will, so calling it from a patient read would either
    # refuse every patient or require weakening the gate -- and weakening it is
    # forbidden. These methods do not re-implement it either. They read the
    # RECORDED OUTCOME of a release that already happened: a `release_events`
    # row, written by `_release_report` behind that same Approver gate, for a
    # report whose own state says `released`. That is strictly NARROWER than
    # `require_release` admits (it passes `approved` as well), and narrower is
    # the safe direction.
    #
    # NOTHING IN THIS REPOSITORY CALLS `_release_report`. COUNTED at d6246cd,
    # non-test callers: ZERO (the one grep hit is prose in
    # geper/review/signoff.py). So in THIS deployment no report ever reaches
    # `released` and both methods below return nothing for everybody, for ever.
    # They are still correct, and they are tested by driving `_release_report`
    # itself; what cannot be demonstrated is a production path that reaches
    # them. Recorded here rather than implied.
    #
    # RETRACTION IS FLAGGED HERE AND NEVER FILTERED -- w148, human decision 1.
    # COUNTED, AND THE COUNT HAS MOVED since this note was first written:
    # `report_retractions` DOES now exist in schema.sql (migration 0003,
    # 2de2fef), while `clinical/data_access.py` STILL has no
    # unapprove/retract/rescind method -- re-counted at 2de2fef, and
    # enforce_content_immutability() still FORBIDS walking an approved report
    # back at the database. So the table is readable and nothing in this
    # repository can write to it; see the flag's own note below the readers.
    #
    # The human rulings settle the reader question in the opposite direction
    # from hiding: E7 says a retracted report must NOT become unfindable, and
    # E3 says retraction does not refuse at the release gate -- the retraction
    # TRAVELS with the document instead. Decision 1 is that form exactly: these
    # methods filter on RELEASE, never on retraction, and return
    # `is_retracted` as a plain boolean beside the row. No reason, no
    # retractor, no timestamp, no `replacement_report_id`.

    @auditable(
        action="portal_reports_read",
        resource_type="report",
        requires_session=False,
        auditable=False,
        reason="see get_my_patient_record -- the same open ruling covers this method",
    )
    @transactional
    def list_my_released_reports(self, patient_session: "PatientSession") -> list[dict[str, Any]]:
        """
        Every RELEASED report belonging to the authenticated patient. TAKES NO
        PATIENT IDENTIFIER, and takes no identifier of any other kind either.

        ANCHORED ON `patients`, like every other portal read: both predicates
        are bound at the anchor, from the session, and the chain
        orders -> samples -> sequencing_runs -> vcfs -> interpretations ->
        reports is walked with the composite (org_id, id) keys the schema
        declares, so no hop can cross an organisation even if a bare id
        repeated.

        THE RELEASE PREDICATE IS TWO CONDITIONS, NOT ONE, and the second is
        the load-bearing one. `reports.state = 'released'` is, by
        `_release_report`'s own docstring, a DERIVED SUMMARY -- "has this
        report been released at all" -- while `release_events` is the record of
        who received what and when. A surface that trusted the summary alone
        would show a report as delivered on the strength of a column the
        application holds a column-scoped write grant on (schema.sql's
        `GRANT` block, near the bottom, on `reports`). So the delivery record
        is required to exist as well.

        THE LATERAL AGGREGATE IS A CORRECTNESS REQUIREMENT, not a performance
        choice, and it is the same hazard w134 hit one table over: a report
        released to three consumers has THREE `release_events` rows, and a
        plain JOIN would return that patient's one report three times.

        `released_at` IS THE FIRST DELIVERY (`min`), which is also the anchor
        the retention clock uses (ruling D3, clinical/retention.py): re-delivery
        to a second consumer does not restart it and does not change what the
        patient is told about when their report was issued.

        WHAT IS DELIBERATELY NOT RETURNED, each with its reason:
          * `interpretation_id` -- the handle to raw pipeline output (above).
          * `approver_id` / any approver name -- ISO 15189 7.4.1.5 c) requires
            the authoriser to be RETRIEVABLE, and it is, from `reports`, by a
            staff reader. Naming a member of laboratory staff to a patient over
            an unauthenticated-by-default internet surface is a separate
            ruling nobody has made.
          * `content_hash` -- a SHA-256 of content this surface does not carry.
          * `state` -- every row returned is `released` by construction, so a
            state field could only ever say one thing.
          * `tombstoned_at` -- see the module note above and w135's card.
          * EVERY COLUMN OF `report_retractions` EXCEPT THE FACT ITSELF --
            w148, human decision 1, stated as a refusal because that is what
            it is: `reason`, `retracted_by`, `retracted_at`,
            `retracted_from_state` and `replacement_report_id` are all
            available in the row this statement already touches, and none of
            them is returned. The reason is internal review detail; the
            retractor is a member of laboratory staff (the same ruling that
            withholds `approver_id`); the replacement is E4's optional edge,
            which the human declined for this surface. What IS returned is one
            boolean, `is_retracted`.

        WHAT IS NOW RETURNED, AND IT IS FOUR FIELDS RATHER THAN THREE:
        `report_id`, `order_id`, `released_at`, `is_retracted`.
        """
        self._require_patient_session(patient_session)
        rows = self._query(
            self._MY_RELEASED_REPORTS_SQL + " ORDER BY rel.first_released_at, rep.id",
            (patient_session.patient_id, patient_session.org_id),
        )
        return [{"report_id": r[0], "order_id": r[1], "released_at": r[2], "is_retracted": r[4]} for r in rows]

    @auditable(
        action="portal_report_read",
        resource_type="report",
        requires_session=False,
        auditable=False,
        reason="see get_my_patient_record -- the same open ruling covers this method",
    )
    @transactional
    def get_my_released_report(self, patient_session: "PatientSession", report_id: uuid.UUID) -> dict[str, Any]:
        """
        ONE released report of the authenticated patient's, by its id.

        `report_id` IS A RESOURCE IDENTIFIER AND NOT AN IDENTITY. The patient
        is still bound from the session and is still never an argument: the id
        below is an extra NARROWING predicate applied AFTER the anchor has
        already restricted the rows to this patient, in this organisation. It
        can only ever select from among the caller's own released reports, so
        supplying somebody else's selects nothing. That is the structural form
        -- the parameter that would matter, `patient_id`, does not exist here
        either.

        EVERY MISS RAISES THE SAME NotFoundError WITH THE SAME CONSTANT. A
        report that does not exist, one belonging to another patient, one in
        another organisation, and one of the caller's OWN that is still draft,
        under_review, approved-but-never-released or returned are all one
        answer. Distinguishing any of them would tell a caller which report ids
        are real, and "my own unreleased report exists" is the one that leaks a
        clinical fact the release gate exists to withhold.
        """
        self._require_patient_session(patient_session)
        row = self._query_one(
            self._MY_RELEASED_REPORTS_SQL + " AND rep.id = %s",
            (patient_session.patient_id, patient_session.org_id, report_id),
        )
        if row is None:
            raise NotFoundError(REPORT_NOT_FOUND)
        return {"report_id": row[0], "order_id": row[1], "released_at": row[2], "is_retracted": row[4]}

    # ── w136: the patient's own released reports, AS A SERIES ────────────────
    #
    # WHAT A PATIENT TREND IS IN THIS MODEL, AND WHAT IT IS NOT. The held
    # trends work (`w127-trends`, tip 861e59f, read via `git show`, NEVER
    # MERGED) builds a series of ACMG CLASSIFICATIONS, one point per variant
    # per INTERPRETATION, read out of `interpretations.run_document`. That
    # series is NOT served to a patient here, and there are two separate
    # reasons, either of which alone would be sufficient:
    #
    #   1. IT IS THE REFUSAL w135 ALREADY MADE, ONE WAVE AGO, IN THIS FILE.
    #      `run_document` is raw pipeline output with no report-state check
    #      anywhere near it; `get_interpretation` returns the whole of it. The
    #      two report readers above return no `interpretation_id` precisely so
    #      that the bypass is zero routes away rather than one, and two tests
    #      in test_w135_patient_report_access.py pin that. Reversing that
    #      refusal one wave later, with no ruling, is not a reconciliation.
    #   2. A CLASSIFICATION SERIES CANNOT CARRY A RETRACTION. Retraction, on
    #      the held `w122-retraction` (a4907d3), attaches to a REPORT --
    #      `report_retractions.report_id`. A w127 trend point is keyed on an
    #      INTERPRETATION, and one interpretation carries many reports (an
    #      amendment is a new `reports` row on the SAME interpretation_id; see
    #      create_amendment). So there is no function from a w127 point to a
    #      retraction status: the point names the wrong object. Human ruling
    #      E3's whole bargain is that the retraction TRAVELS WITH THE
    #      DOCUMENT, and a series that structurally cannot carry it has taken
    #      the benefit of that bargain without paying it.
    #
    # SO THE SERIES THIS FILE SERVES A PATIENT IS KEYED ON REPORTS, which is
    # the object retraction, amendment and release all already attach to. The
    # trend a patient can be told about, in THIS model, is: which reports were
    # issued to me, in what order, and which of them replaced which.
    #
    # THE EDGE IS `amendments`, WHICH EXISTS AT THIS SHA -- COUNTED. The
    # corrective act this schema has is AMENDMENT (schema.sql:1213,
    # `clinical_app` holds SELECT at schema.sql:1594, `create_amendment` is
    # Approver-gated).
    #
    # RE-COUNTED IN w148, BECAUSE HALF OF WHAT THIS PARAGRAPH SAID HAS STOPPED
    # BEING TRUE and a census claim gets fixed rather than left standing. It
    # used to read "the one it does not have is RETRACTION (`report_retractions`
    # appears 0 times in schema.sql ...)". At 2de2fef `report_retractions`
    # appears in schema.sql and has its own migration. What has NOT changed is
    # the other half: there is still no retract/unapprove/rescind verb in this
    # class, and enforce_content_immutability still REFUSES to walk an approved
    # report back. So the schema can RECORD a retraction and this layer can
    # READ one; nothing in this repository can CREATE one.
    #
    # AND NOTHING IS FILTERED. No retraction predicate, no tombstone
    # predicate -- only release, exactly as the two readers above. Rulings E3
    # and E7 settle the reader question in the opposite direction from hiding:
    # a retracted report is STILL RETURNED, carrying the fact, because
    # filtering it "would make a retracted report unfindable".
    #
    # w148: THAT IS NO LONGER A FORECAST. `report_retractions` arrived in the
    # schema with migration 0003 (2de2fef), the two rows that said "fail the
    # moment this table appears" did exactly that, and the human ruled both
    # patient surfaces: a SIMPLE BOOLEAN, and nothing else.
    #
    #   1. PATIENT RETRACTED REPORT -- Decision A. The report responses expose
    #      a retraction flag. NOT the reason, NOT the reviewer, NOT
    #      `replacement_report_id`.
    #   2. PATIENT RETRACTED TREND POINT -- Decision A. The point REMAINS in
    #      the series and carries the same flag. Again no replacement id and
    #      no internal detail.
    #
    # SO E4's OPTIONAL `replacement_report_id` IS REFUSED, and its refusal is
    # the whole of the difference between this wave and the forecast above:
    # the edge shape was available and the human declined it.
    #
    # WHERE THE FLAG LIVES, AND WHY IT COULD NOT LIVE ANYWHERE ELSE. It is a
    # SCALAR `EXISTS` IN THE SELECT LIST of this same statement. Two standing
    # requirements forced that and neither had any give in it:
    #   * `test_every_portal_statement_binds_the_patient_from_the_session`
    #     requires every statement a portal method issues to bind the
    #     session's patient, and `test_the_two_methods_share_one_sql_text`
    #     pins the caller count of this constant at exactly four -- one
    #     definition and three callers. A SECOND statement breaks both.
    #   * Rulings E3/E7 forbid the flag from removing a row.
    # A projection satisfies both by construction: it is evaluated per row
    # that the FROM/WHERE has ALREADY chosen, so it cannot add a row and
    # cannot drop one, and it inherits this statement's patient anchor rather
    # than needing its own.
    #
    # AND IT IS BOUND TO THE ROW IT DESCRIBES, on BOTH axes, structurally:
    # `rr.org_id = rep.org_id AND rr.report_id = rep.id`. Without the first, a
    # retraction in another tenant would flag this patient's report; without
    # the second, one patient's retraction would flag their neighbour's. Both
    # wrong versions have their own test.
    #
    # THE TWO GUARD ROWS THAT SAID `"retract" not in shared` WERE RE-AUTHORED,
    # NOT DELETED, and the property they protected is unchanged: retraction
    # must not decide WHICH ROWS COME BACK. They now assert that the word
    # appears ONLY in the select list and NOWHERE in the FROM/JOIN/WHERE --
    # which is the same property stated where it can still be enforced, and is
    # strictly stronger than the old text on the half that matters, because
    # the old text would have been satisfied by deleting the constant.

    # ONE SQL TEXT, THREE CALLERS. The list, the single-report read and the
    # trend must not be able to disagree about what "released" and "mine"
    # mean: three copies of this join is three places for an org predicate to
    # be dropped from, and only one of them is exercised by the isolation
    # tests somebody remembers to write.
    #
    # THE FOURTH COLUMN IS THE AMENDMENT EDGE, and it is inside THIS text
    # rather than in a second statement for exactly that reason. The two w135
    # readers select it and ignore it; their returned dicts are unchanged.
    #
    # WHY THE EDGE CANNOT POINT OUTSIDE THE CALLER'S OWN RELEASED REPORTS,
    # STRUCTURALLY AND NOT BY ARGUMENT:
    #   * `orig.interpretation_id = rep.interpretation_id` -- create_amendment
    #     copies the original's interpretation_id onto the amendment, so an
    #     amendment and its original always share one interpretation, which
    #     already sits under this patient's own order via the five hops above.
    #     An edge to another patient's report cannot satisfy it.
    #   * `orig.state = 'released'` AND an EXISTS over `release_events` -- the
    #     same two conditions, in the same order, that gate the rows
    #     themselves. Naming an original the patient was never sent would be
    #     an existence oracle for an unreleased report id.
    #   * `amd.org_id = rep.org_id` on every hop, as everywhere else here.
    # LATERAL ... LIMIT 1 rather than a plain JOIN: `amendments` has no unique
    # constraint on `amendment_report_id`, and a plain join would return the
    # patient's one report twice if one ever appeared twice. Same hazard the
    # `rel` aggregate exists for, one table over.
    #
    # THE FIFTH COLUMN IS THE RETRACTION FLAG (w148), and it is a SCALAR
    # SUBQUERY IN THE PROJECTION rather than a join for the reason set out at
    # length above: a projection cannot change the row set, which is exactly
    # what rulings E3 and E7 forbid it from doing. It is bound on both axes to
    # the row it describes. It returns a plain boolean and nothing else --
    # no reason, no retractor, no timestamp, no replacement id -- so there is
    # no internal detail in this result set for a handler to leak by accident.
    #
    # THE LITERAL 'patient_portal' BELOW IS `PORTAL_RELEASE_CONSUMER`, written out
    # and NOT interpolated: test_patient_identity's structural scan reads each
    # portal statement as one plain string constant, and an f-string is a
    # different AST node that it cannot read. test_w169_portal_visibility ties
    # the literal to the constant, so they cannot drift.
    #
    # STAGE 3 FOLLOW-UP: "RELEASED" MEANS RELEASED TO THE PATIENT PORTAL. Both
    # the `rel` aggregate and the amendment edge's EXISTS now count only
    # release_events whose consumer is `PORTAL_RELEASE_CONSUMER`. Before this, ANY
    # release row published a report to its patient -- so a report delivered
    # only to an external consumer (a LIMS, a referring clinician; `_release_report`
    # accepts any consumer and the schema reserves the column for them) would have
    # appeared in the patient's portal as a side effect. `released_at` is
    # therefore the FIRST PORTAL release, which is what the patient is told. The
    # retention clock (first delivery of ANY kind) and the retraction debts
    # (one per distinct consumer) deliberately still read every release.
    _MY_RELEASED_REPORTS_SQL = (
        "SELECT rep.id, o.order_id, rel.first_released_at, sup.amends_report_id, "
        "       EXISTS (SELECT 1 FROM report_retractions rr "
        "                WHERE rr.org_id = rep.org_id AND rr.report_id = rep.id) AS is_retracted "
        "FROM patients p "
        "JOIN orders o          ON o.org_id = p.org_id  AND o.patient_id = p.patient_id "
        "JOIN samples s         ON s.org_id = o.org_id  AND s.order_id = o.order_id "
        "JOIN sequencing_runs r ON r.org_id = s.org_id  AND r.sample_id = s.sample_id "
        "JOIN vcfs v            ON v.org_id = r.org_id  AND v.sequencing_run_id = r.id "
        "JOIN interpretations i ON i.org_id = v.org_id  AND i.vcf_id = v.id "
        "JOIN reports rep       ON rep.org_id = i.org_id AND rep.interpretation_id = i.id "
        "JOIN LATERAL ("
        "    SELECT min(re.released_at) AS first_released_at, count(*) AS deliveries "
        "      FROM release_events re "
        "     WHERE re.org_id = rep.org_id AND re.report_id = rep.id "
        "      AND re.consumer = 'patient_portal'"
        ") rel ON rel.deliveries > 0 "
        "LEFT JOIN LATERAL ("
        "    SELECT orig.id AS amends_report_id "
        "      FROM amendments amd "
        "      JOIN reports orig ON orig.org_id = amd.org_id AND orig.id = amd.original_report_id "
        "     WHERE amd.org_id = rep.org_id "
        "       AND amd.amendment_report_id = rep.id "
        "       AND orig.interpretation_id = rep.interpretation_id "
        "       AND orig.state = 'released' "
        "       AND EXISTS (SELECT 1 FROM release_events re2 "
        "                    WHERE re2.org_id = orig.org_id AND re2.report_id = orig.id "
        "                     AND re2.consumer = 'patient_portal') "
        "     ORDER BY amd.created_at, amd.id "
        "     LIMIT 1"
        ") sup ON TRUE "
        "WHERE p.patient_id = %s AND p.org_id = %s "
        "  AND rep.state = 'released'"
    )

    @auditable(
        action="portal_report_trend_read",
        resource_type="report",
        requires_session=False,
        auditable=False,
        reason="see get_my_patient_record -- the same open ruling covers this method",
    )
    @transactional
    def list_my_report_trend(self, patient_session: "PatientSession") -> list[dict[str, Any]]:
        """
        The authenticated patient's own released reports AS A SERIES, in
        first-delivery order, with the supersession edge between them.

        TAKES NO IDENTIFIER OF ANY KIND -- not a patient id, not a report id,
        not a date range. The patient is bound from the session at the anchor,
        in the ONE shared SQL text above, exactly as the two w135 readers are.

        WHAT A POINT IS: one RELEASED report of the caller's own. Not an
        interpretation, not a variant, not a classification. The node key is a
        `report_id`, which is the object `amendments`, `release_events` and
        (on the held w122 branch) `report_retractions` all attach to.

        `amends_report_id` -- the caller's own released report that THIS one
        was created to correct, or None. `superseded_by_report_id` is its
        inverse, DERIVED IN PYTHON FROM THIS SAME LIST and never from a second
        query: it can therefore only ever name a report already in the answer,
        so it is structurally incapable of being an existence oracle for a
        report the caller cannot see. A second query would not have that
        property for free.

        NOTHING IS HIDDEN. There is no retraction predicate and no tombstone
        predicate; the only thing that keeps a report out of this series is
        that it was never released to anyone. See the long note above the SQL
        for rulings E3/E7 and for why a classification series is refused.

        `is_retracted` -- w148, human decision 2: "a trend point originating
        from a retracted report REMAINS in the patient's longitudinal data but
        carries an `is_retracted` flag." A boolean, per POINT, derived in the
        shared statement's projection and bound there to (org_id, report_id),
        so a retraction belonging to another patient or another organisation
        cannot reach it. NO `replacement_report_id` and no other internal
        retraction detail, which the same decision refuses explicitly -- and
        that refusal is what makes this different from `amends_report_id`
        beside it, whose edge shape E4 would have allowed.

        IN THIS DEPLOYMENT THIS METHOD RETURNS `[]` FOR EVERY PATIENT, FOR
        EVER, because `_release_report` has ZERO non-test callers (re-counted
        at 3a482f1). It is still correct, and the tests construct the released
        state the only legitimate way there is -- by calling `_release_report`
        behind its own Approver gate. What is not claimed is that any
        production path reaches it. Same finding as w134-C3 and w135-C3.
        """
        self._require_patient_session(patient_session)
        rows = self._query(
            self._MY_RELEASED_REPORTS_SQL + " ORDER BY rel.first_released_at, rep.id",
            (patient_session.patient_id, patient_session.org_id),
        )
        series = [
            {
                "report_id": r[0],
                "order_id": r[1],
                "released_at": r[2],
                "amends_report_id": r[3],
                "superseded_by_report_id": None,
                # w148, human decision 2. A BOOLEAN, PER POINT, AND THE POINT
                # STAYS. Nothing is dropped from `series` on account of it --
                # there is no comprehension condition and no `if` anywhere in
                # this method that reads it, which is what the re-authored
                # guard in test_w136 now asserts structurally.
                "is_retracted": r[4],
            }
            for r in rows
        ]
        # The inverse edge, over the answer itself. `amends_report_id` is
        # already restricted by the SQL to a report in this same list, so this
        # loop cannot invent a node and cannot name one the caller may not see.
        by_id = {point["report_id"]: point for point in series}
        for point in series:
            earlier = by_id.get(point["amends_report_id"])
            if earlier is not None:
                earlier["superseded_by_report_id"] = point["report_id"]
        return series

    # ── patient authentication and patient sessions (w130) ───────────────────
    #
    # THE SAME MECHANISM OVER A SECOND PRINCIPAL TABLE. Not a second
    # authentication system: no new secret material, no new credential format,
    # no new failure vocabulary, no new session policy, no new endpoint
    # semantics. The same bcrypt hasher, the same dummy-hash timing
    # equalisation, the same MAX_FAILED_LOGIN_ATTEMPTS / LOCKOUT_MINUTES, the
    # same GENERIC_AUTH_ERROR, the same SESSION_ABSOLUTE_HOURS and
    # SESSION_IDLE_MINUTES with the same inclusive idle boundary.
    #
    # WHY NOT REFACTOR login() INTO A SHARED HELPER PARAMETERISED BY PRINCIPAL
    # TABLE: it is available and it was not done. It would edit the single
    # authenticated path of a clinical system whose behaviour is pinned by
    # existing tests, to save about sixty lines. The cost of the choice made
    # instead is one duplicated validation method, stated rather than hidden.
    #
    # NO SECOND FACTOR, AND THIS IS A DECLARED GAP RATHER THAN AN OMISSION.
    # users.totp_secret is enrolled by enrol_totp, which is Administrator-gated
    # and presumes an administrator sitting with the account holder and a
    # device the organisation controls. That assumption does not hold for a
    # patient at home. Whether patient authentication is single-factor is a
    # ruling; shipping a totp_secret column nothing ever enrols would be how
    # external_identifiers came to exist.

    @auditable(
        action={
            "success": "patient_login_succeeded",
            "error": "patient_login_failed",
            "denied": "patient_login_failed",
        },
        resource_type="patient_session",
        # Record the session's reference (login and logout rows then correlate); the id
        # is the portal cookie and never goes in the log.
        resource_id_from_result=lambda result: result.session_id if result else None,
        resource_id_transform=session_audit_ref,
        requires_session=False,
        details_builder=lambda params, result: {
            "session_ref": session_audit_ref(result.session_id) if result else None,
            "patient_login_id": str(result.patient_login_id) if result else None,
        },
    )
    @transactional
    def patient_login(
        self,
        email: str,
        org_id: uuid.UUID,
        password: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> "PatientSession":
        """
        Authenticate a patient and open a server-side patient session.

        Every failure path raises AuthenticationError carrying exactly
        GENERIC_AUTH_ERROR -- the SAME constant staff authentication uses, so a
        caller cannot tell "no such address" from "wrong password" from
        "disabled" from "locked out", and cannot tell a patient address from a
        staff one either.

        The returned session carries patient_id. Nothing downstream asks the
        caller which patient this is.
        """
        now = self._clock.now()

        org = self._query_one("SELECT org_id, disabled FROM organisations WHERE org_id = %s", (org_id,))
        if org is None:
            self.log_failed_login(email, None, ip_address, reason="unknown_organisation")
            self._equalise_password_work(password)
            raise AuthenticationError()
        if org[1]:
            self.log_failed_login(email, org_id, ip_address, reason="organisation_disabled")
            self._equalise_password_work(password)
            raise AuthenticationError()

        # BINDS org_id, exactly as login()'s users lookup does. An email is
        # unique only WITHIN an organisation -- patient_logins_org_email_unique
        # -- so a lookup keyed on the address alone would authenticate one
        # tenant's patient against another tenant's credential.
        row = self._query_one(
            "SELECT patient_login_id, patient_id, password_hash, failed_login_attempts, locked_until, disabled "
            "FROM patient_logins WHERE org_id = %s AND email = %s",
            (org_id, email),
        )
        if row is None:
            self._equalise_password_work(password)
            self.log_failed_login(email, org_id, ip_address, reason="unknown_patient_login")
            raise AuthenticationError()

        patient_login_id, patient_id, password_hash, failed_attempts, locked_until, disabled = row

        if locked_until is not None and locked_until > now:
            # EQUALISE HERE TOO -- w139, and for the same reason as login()'s
            # locked-out branch, which this mirrors line for line. The patient
            # half had the identical shape and w138 recorded it rather than
            # carding it precisely because login() did too; both are fixed
            # together so the two methods cannot drift.
            #
            # The counter is NOT touched: `_equalise_password_work` spends the
            # work and discards the result, so a locked patient credential is
            # not driven deeper and `locked_until` is not extended. The lockout
            # policy is unchanged.
            self._equalise_password_work(password)
            self.log_failed_login(email, org_id, ip_address, reason="locked_out")
            raise AuthenticationError()

        if not self._hasher.verify(password, password_hash):
            self._register_failed_patient_attempt(patient_login_id, org_id, failed_attempts, now)
            self.log_failed_login(email, org_id, ip_address, reason="invalid_password")
            raise AuthenticationError()

        # AFTER the password, exactly as login() does it and for the same
        # reason: checking first answers "is this account disabled?" to anyone
        # who can guess an address.
        if disabled:
            self.log_failed_login(email, org_id, ip_address, reason="patient_login_disabled")
            raise AuthenticationError()

        self._execute(
            "UPDATE patient_logins SET failed_login_attempts = 0, locked_until = NULL, updated_at = %s "
            "WHERE patient_login_id = %s AND org_id = %s",
            (now, patient_login_id, org_id),
        )

        session_id = uuid.uuid4()
        self._execute(
            "INSERT INTO patient_sessions (session_id, patient_login_id, patient_id, org_id, created_at, "
            "                              expires_at, last_activity_at, idle_timeout_minutes, ip_address, "
            "                              user_agent) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session_id,
                patient_login_id,
                patient_id,
                org_id,
                now,
                now + _datetime.timedelta(hours=SESSION_ABSOLUTE_HOURS),
                now,
                SESSION_IDLE_MINUTES,
                ip_address,
                user_agent,
            ),
        )
        return PatientSession(
            session_id=session_id,
            patient_login_id=patient_login_id,
            patient_id=patient_id,
            org_id=org_id,
        )

    def _register_failed_patient_attempt(
        self,
        patient_login_id: uuid.UUID,
        org_id: uuid.UUID,
        current_attempts: int,
        now: _datetime.datetime,
    ) -> None:
        """The staff lockout policy, unchanged, applied to the patient row."""
        attempts = current_attempts + 1
        locked_until = (
            now + _datetime.timedelta(minutes=LOCKOUT_MINUTES) if attempts >= MAX_FAILED_LOGIN_ATTEMPTS else None
        )
        self._execute(
            "UPDATE patient_logins SET failed_login_attempts = %s, locked_until = %s, updated_at = %s "
            "WHERE patient_login_id = %s AND org_id = %s",
            (attempts, locked_until, now, patient_login_id, org_id),
        )

    @auditable(
        action="patient_session_validated",
        resource_type="patient_session",
        requires_session=False,
        auditable=False,
        reason="validation read on every request; the patient session audit trail records creation and "
        "termination, not use -- the same decision session_valid already carries",
    )
    @transactional
    def patient_session_valid(self, session_id: uuid.UUID) -> "PatientSession":
        """
        Validate a patient session id and refresh its idle window.

        A DELIBERATE MIRROR of session_valid, against a different table. The
        two session id spaces are therefore different tables: a STAFF session
        id presented here is simply not found, and a patient session id
        presented to session_valid is simply not found. That separation is
        structural, not checked -- which is why _get_session was not widened
        and must not be.
        """
        now = self._clock.now()
        row = self._query_one(
            "SELECT patient_login_id, patient_id, org_id, expires_at, last_activity_at, "
            "       idle_timeout_minutes, terminated_at "
            "FROM patient_sessions WHERE session_id = %s",
            (session_id,),
        )
        if row is None:
            raise SessionExpired()

        patient_login_id, patient_id, org_id, expires_at, last_activity_at, idle_minutes, terminated_at = row

        if terminated_at is not None:
            raise SessionExpired()
        if now >= expires_at:
            raise SessionExpired()
        if now >= last_activity_at + _datetime.timedelta(minutes=idle_minutes):
            raise SessionExpired()

        self._execute(
            "UPDATE patient_sessions SET last_activity_at = %s WHERE session_id = %s AND org_id = %s",
            (now, session_id, org_id),
        )
        return PatientSession(
            session_id=session_id,
            patient_login_id=patient_login_id,
            patient_id=patient_id,
            org_id=org_id,
        )

    @auditable(
        action="patient_session_logged_out",
        resource_type="patient_session",
        resource_id_from_result=True,
        resource_id_transform=session_audit_ref,
        # requires_session=False BECAUSE THE DECORATOR REFUSES A PatientSession
        # WHERE requires_session=True (w130 decision 7), which is correct and
        # is not being worked around: this method is not a staff operation.
        #
        # w132 SHIPPED THIS WITH A NULL org_id AND SAID SO; w137 CLOSED IT.
        # The decorator bound the audit row's org_id/user_id COLUMNS from a
        # staff Session and from nothing else, so every row written here had
        # org_id NULL — and read_audit is Auditor-gated and filters
        # `WHERE org_id = %s`, so a patient session termination was recorded
        # where the organisation that owns it could not look. The organisation
        # was never unknown: it is on the PatientSession. The decorator now
        # reads it from the acting principal for requires_session=False
        # methods, WITHOUT touching decision 7's refusal, which is keyed on
        # requires_session=True. `user_id` stays NULL here because a patient
        # is not a `users` row; the acting credential stays in `details`
        # below, where it also was before.
        requires_session=False,
        # `getattr`, NOT attribute access, AND IT IS NOT DEFENSIVE HABIT. The
        # decorator runs the details_builder on the DENIED and ERRORED paths
        # too, where the argument is whatever the caller passed -- including
        # the staff `Session` that `_require_patient_session` exists to
        # refuse, which has no `patient_login_id`. Plain access raised
        # AttributeError from inside the audit writer and MASKED the
        # AuthorizationError, turning a clean refusal into a crash. Found by
        # test_a_staff_session_is_refused_by_the_patient_logout_method.
        details_builder=lambda params, result: {
            "session_ref": session_audit_ref(getattr(params.get("patient_session"), "session_id", None)),
            "org_id": str(getattr(params.get("patient_session"), "org_id", None)),
            "patient_login_id": str(getattr(params.get("patient_session"), "patient_login_id", None)),
        },
    )
    @transactional
    def terminate_my_patient_session(self, patient_session: "PatientSession") -> uuid.UUID:
        """
        End THE CALLING patient session. The patient's logout.

        A DELIBERATE MIRROR of `terminate_my_session`, against a different
        table, and a mirror rather than a shared helper for the same reason
        `patient_login` mirrors `login`: one function reaching two session
        tables is the union this design refuses.

        IT TAKES NO TARGET IDENTIFIER, for the reason given there. And it ends
        ONE ROW, NOT EVERY ROW THE CREDENTIAL HOLDS — the plausible wrong
        version keys the UPDATE on `patient_login_id`, which reads like a
        tidier logout and silently signs a patient out of every device they
        have. "Log out everywhere" is a different, rulable feature with a
        different name; it is not the default meaning of a logout button, and
        nothing in an absence-shaped test can tell the two apart. The positive
        twin in test_w132_logout.py is what does.

        `terminated_by` HOLDS THE `patient_login_id` — the credential that
        performed the act, which `patient_sessions` already carries — and this
        is expressible WITHOUT A SCHEMA CHANGE only because `terminated_by`
        has NO foreign key on either session table. Had it referenced
        `users (user_id, org_id)`, which `terminate_session` writing
        `session.user_id` there makes a reasonable thing to assume, a patient
        could not name themselves as the actor and this would have been a
        migration in a repository that has no migration mechanism and three
        schema changes already held unlanded.

        NOT A CREDENTIAL REVOCATION: `patient_logins.disabled` still has no
        writer anywhere in this repository, and this method is not one.
        """
        self._require_patient_session(patient_session)
        now = self._clock.now()
        self._execute(
            "UPDATE patient_sessions SET terminated_at = %s, terminated_by = %s "
            "WHERE session_id = %s AND org_id = %s AND terminated_at IS NULL",
            (now, patient_session.patient_login_id, patient_session.session_id, patient_session.org_id),
        )
        return patient_session.session_id

    # ── patient portal identity (w130) ───────────────────────────────────────
    #
    # A PATIENT IS A PRINCIPAL AND IT IS NOT A STAFF USER. The tables are
    # separate (see the note above patient_logins in schema.sql), the session
    # type is separate (PatientSession), the login method is separate, and the
    # HTTP dependency is separate. What is NOT separate is the mechanism: the
    # same bcrypt hasher, the same dummy-hash timing equalisation, the same
    # lockout constants, the same GENERIC_AUTH_ERROR, the same absolute and
    # idle session windows. No second security policy was invented here.

    @auditable(
        action="patient_login_enrolled",
        resource_type="patient_login",
        resource_id_from_result=True,
        requires_session=True,
        details_builder=lambda params, result: {
            "patient_id": str(params.get("patient_id")),
            "email": params.get("email"),
        },
    )
    @transactional
    def enrol_patient_login(
        self,
        session: Session,
        patient_id: uuid.UUID,
        email: str,
        password: str,
    ) -> uuid.UUID:
        """
        Issue a portal credential for one patient. STAFF ENROLMENT: a staff
        principal chooses the patient record, and there is no self-registration
        path of any kind.

        That is what answers "how is a person matched to their patient record?"
        -- a staff member who already knows which record this is picks it. It
        is not answered by an external identifier vocabulary, and this method
        deliberately does not consult external_identifiers.

        WHY THIS IS ONE METHOD AND NOT A COMPOSITION. @auditable commits after
        each audit row it writes. So a version assembled out of two decorated
        calls -- "check the patient, then create the credential" -- would leave
        the first call's audit row committed when the second failed, and the
        audit would record an enrolment that did not happen. The precondition
        reads, the uniqueness read and the INSERT are therefore all inside one
        @transactional body with exactly one @auditable around the whole of it.
        A failure anywhere leaves NOTHING: no credential row, and no
        success-outcome audit row.

        THE ROLE. Administrator, on the conservative default the design
        recorded: issuing a credential is account administration, and
        assign_role, enrol_totp and set_user_disabled are all Administrator
        today. Which role SHOULD own patient credential issuance is a ruling
        that has not been made; this picks the tightest existing gate rather
        than inventing an eighth role or leaving the method ungated. Note that
        it is strictly tighter than the surrounding patient lifecycle, where
        create_patient and record_consent are gated by nothing at all -- this
        does not weaken that; it declines to copy it.

        Raises NotFoundError for a patient that does not exist IN THIS
        ORGANISATION -- the same refusal, byte for byte, as for a patient that
        does not exist anywhere, because which patients another tenant holds is
        not a question this method answers.
        """
        self._require_role(session, "Administrator")

        # The org predicate is bound from the session and never from an
        # argument. A real patient in another organisation reads exactly like
        # an invented one.
        patient = self._query_one(
            "SELECT patient_id FROM patients WHERE patient_id = %s AND org_id = %s",
            (patient_id, session.org_id),
        )
        if patient is None:
            raise NotFoundError(PATIENT_NOT_FOUND)

        # One login per patient is a DECISION (proxy and guardian access would
        # relax it and that is a ruling nobody has made), so it is refused here
        # with a message that says so, as well as by the UNIQUE constraint.
        existing = self._query_one(
            "SELECT patient_login_id FROM patient_logins WHERE org_id = %s AND patient_id = %s",
            (session.org_id, patient_id),
        )
        if existing is not None:
            raise PatientAlreadyEnrolledError(
                "This patient already has a portal credential. A second credential for the same "
                "patient is a proxy-access decision, not a re-enrolment."
            )

        patient_login_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO patient_logins (patient_login_id, patient_id, org_id, email, password_hash, "
            "                            password_changed_at, enrolled_by, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                patient_login_id,
                patient_id,
                session.org_id,
                email,
                # The same hasher that hashes staff passwords. Not a second
                # credential format, and not a second cost parameter.
                self._hasher.hash(password),
                now,
                session.user_id,
                now,
                now,
            ),
        )
        return patient_login_id

    @auditable(
        # The pair mirrors user_disabled / user_enabled, keyed on the same
        # parameter and for the same reason: the two are different events and
        # an audit that spells them the same cannot answer "who turned it back
        # on".
        action=lambda params, result: "patient_login_disabled" if params.get("disabled") else "patient_login_enabled",
        resource_type="patient_login",
        # -> None, so the identifier cannot come from the return value. Without
        # this the row records the literal '<none>' -- the w120 defect -- and
        # test_w120_audit_resource_id.py refuses the method outright.
        resource_id_param="target_patient_login_id",
        requires_session=True,
        # `params.get`, not attribute access: the builder runs on the DENIED
        # and ERRORED paths too, with whatever the caller actually passed.
        details_builder=lambda params, result: {
            "target_patient_login_id": str(params.get("target_patient_login_id")),
            "disabled": bool(params.get("disabled")),
        },
    )
    @transactional
    def set_patient_login_disabled(
        self,
        session: Session,
        target_patient_login_id: uuid.UUID,
        disabled: bool,
    ) -> None:
        """
        Revoke (or restore) a patient's portal credential, AND END THE LIVE
        SESSIONS IT HOLDS. Administrator only.

        Never a DELETE, for the reason set_user_disabled gives: it would orphan
        the audit chain, and nothing in this schema deletes.

        WHY THE SESSION TERMINATION IS IN THIS BODY AND NOT A SECOND CALL.
        @auditable commits after every audit row it writes. A revocation
        assembled out of two decorated calls -- "disable the credential, then
        end its sessions" -- would leave a committed audit row saying the
        credential was revoked while the sessions ran on, which is the worst
        of the three possible outcomes: the record says the thing happened.
        So the role gate, the precondition read, the credential UPDATE and the
        session UPDATE are one @transactional body inside exactly one
        @auditable. A failure anywhere leaves NOTHING: the credential still
        authenticates and no success-outcome row exists. Pinned by
        test_w133_revocation.py::TestAtomicity.

        THE UPDATE IS KEYED ON patient_login_id, AND THAT IS THE STATEMENT
        terminate_my_patient_session DELIBERATELY REFUSED. The two are not in
        conflict; they are opposites and both are deliberate:

            logout      is SELF-SCOPED, takes no target, and ends ONE session.
                        Keying it on the credential would sign a patient out
                        of every device they have, under a button that says
                        "log out".
            revocation  is ADMINISTRATIVE, names a target, and withdraws THE
                        CREDENTIAL. Every session that credential minted was
                        minted by the thing being withdrawn, so ending only
                        one of them would leave the revoked holder logged in
                        on the other device -- which is the actor the
                        revocation was invoked against.

        `terminated_by` HOLDS THE ACTING ADMINISTRATOR'S user_id -- the column
        names the actor and the actor is the Administrator, consistent with
        terminate_session. It is expressible without a schema change only
        because `terminated_by` has NO foreign key on either session table.
        (Which of the two ids an administrative termination SHOULD record is
        an open question, carded; this picks the actor and says so.)

        `terminated_at IS NULL` makes a second revocation a no-op rather than
        a rewrite: the recorded moment a session ended is the first one.

        ONLY WHEN `disabled` IS TRUTHY. Re-enabling must not terminate
        anything -- and it does not resurrect anything either: a session ended
        by a revocation stays ended, and the restored credential must be used
        to obtain a new one.

        POINT-IN-TIME, NOT CONTINUOUS -- AND THE RESIDUE IS REAL. This method
        ends the sessions that exist WHEN IT RUNS. Session validation is
        unchanged: patient_session_valid still reads one row from
        patient_sessions by primary key and never joins patient_logins. So a
        `disabled` flag set by ANY OTHER ROUTE -- direct SQL, a restore, a
        future bootstrap -- leaves that credential's live sessions valid until
        their own idle or absolute window closes. Closing that residue means
        teaching session validation to read the principal row on every
        request, which is a different change to the hottest path in the layer.
        It was not made here. The residue is proved, not merely described, by
        test_w133_revocation.py::TestTheResidueIsReal.

        THE WAY BACK IS NARROW, and a caller should know it before revoking: a
        disabled credential cannot be deleted (nothing deletes), cannot be
        replaced (patient_logins_one_per_patient, and enrol_patient_login
        raises PatientAlreadyEnrolledError), and its password cannot be
        changed (no principal in this repository has a password-change
        method). Re-enabling the same credential, with the same hash, is the
        only route back today.

        Raises NotFoundError carrying PATIENT_NOT_FOUND -- the module
        constant, never an f-string naming the id -- for a credential that
        does not exist IN THIS ORGANISATION, which is byte for byte the
        refusal for one that does not exist anywhere.
        """
        self._require_role(session, "Administrator")

        # The org predicate is bound from the session and never from an
        # argument, and it is here TWICE on purpose. Without this read a
        # cross-organisation attempt would update zero rows and return
        # successfully, and the caller could not tell a refusal from a
        # no-op. With it the caller gets NotFoundError; the AND org_id on the
        # statements below is what makes the layer being wrong insufficient.
        existing = self._query_one(
            "SELECT patient_login_id FROM patient_logins WHERE patient_login_id = %s AND org_id = %s",
            (target_patient_login_id, session.org_id),
        )
        if existing is None:
            raise NotFoundError(PATIENT_NOT_FOUND)

        now = self._clock.now()
        self._execute(
            "UPDATE patient_logins SET disabled = %s, updated_at = %s WHERE patient_login_id = %s AND org_id = %s",
            (disabled, now, target_patient_login_id, session.org_id),
        )

        if disabled:
            self._execute(
                "UPDATE patient_sessions SET terminated_at = %s, terminated_by = %s "
                "WHERE patient_login_id = %s AND org_id = %s AND terminated_at IS NULL",
                (now, session.user_id, target_patient_login_id, session.org_id),
            )

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
        resource_id_from_result=True,
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

        Requires the Lab technician role (w129 phase B): the run is
        laboratory processing performed on a specimen, and `created_by` is
        stamped from the session.

        This does NOT gate the automatic pipeline. record_pipeline_result is
        the System principal's path and does not call this method -- counted,
        `self.create_sequencing_run(` occurs zero times in this file -- and
        the ruling names System an internal principal that must not be
        treated as a normal human staff role.
        """
        self._require_role(session, "Lab technician")

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

    def _resolve_vcf_retention_days(self, session: Session, sequencing_run_id: uuid.UUID) -> int:
        """
        Human ruling D2 (2026-09-09): resolve the age-conditional retention
        floor ONCE, here, at VCF creation -- the value returned is persisted
        on vcfs.retention_days and every downstream artefact inherits it
        rather than ever calling this again. See
        clinical/retention.py::resolve_vcf_retention_days for the rule
        itself (pure, unit-testable without a connection) -- this method is
        only the DB-side plumbing: trace sequencing_run -> sample -> order
        -> patient for dob, use the sample's own collected_at as "age at
        collection", and read this org's 'vcf' policy overrides, if any.
        """
        row = self._query_one(
            "SELECT p.dob, s.collected_at, rp.retention_days, rp.retention_days_minor "
            "FROM sequencing_runs run "
            "JOIN samples s ON s.org_id = run.org_id AND s.sample_id = run.sample_id "
            "JOIN orders o ON o.org_id = s.org_id AND o.order_id = s.order_id "
            "JOIN patients p ON p.org_id = o.org_id AND p.patient_id = o.patient_id "
            "LEFT JOIN retention_policies rp "
            "  ON rp.org_id = run.org_id AND rp.artefact_class = 'vcf' "
            "WHERE run.id = %s AND run.org_id = %s",
            (sequencing_run_id, session.org_id),
        )
        if row is None:
            raise NotFoundError(f"Sequencing run {sequencing_run_id} not found")
        dob, collected_at, policy_adult_days, policy_minor_days = row
        return resolve_vcf_retention_days(
            dob,
            collected_at.date() if hasattr(collected_at, "date") else collected_at,
            policy_adult_days=policy_adult_days,
            policy_minor_days=policy_minor_days,
        )

    @auditable(
        action="vcf_created",
        resource_type="vcf",
        resource_id_from_result=True,
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

        Human ruling D2 (2026-09-09): also resolves and persists this VCF's
        own retention_days here, once -- see _resolve_vcf_retention_days
        and clinical/retention.py's module docstring. Every downstream
        artefact (interpretation, report, amendment) inherits this same
        value at ITS OWN creation rather than recomputing it.

        Requires the Lab technician role (w129 phase B): the VCF is the
        artefact the sequencing run produced, registered against it with
        `created_by` stamped from the session. Same exclusion as
        create_sequencing_run -- the automatic path does not come through
        here (`self.create_vcf(` occurs zero times in this file).

        w129 reported here that record_fastq_set and find_fastq_set_by_checksums
        were NOT gated because "they take a raw org_id and no session, so there
        is no session whose roles could be consulted". THAT IS NO LONGER TRUE
        and the gap it reported is closed: w129c gave both a caller-supplied
        `Session`, and `record_fastq_set` now requires the `System` role. It is
        gated on the INTERNAL principal rather than on Lab technician, because
        detection is the platform observing a directory and not a member of
        laboratory staff. The read keeps its existing authorisation posture --
        no role gate, still non-auditable -- and only stopped naming its own
        organisation.
        """
        self._require_role(session, "Lab technician")

        # Validate sequencing_run exists in this org
        run = self._query_one(
            "SELECT id FROM sequencing_runs WHERE id = %s AND org_id = %s",
            (sequencing_run_id, session.org_id),
        )
        if run is None:
            raise NotFoundError(f"Sequencing run {sequencing_run_id} not found")

        retention_days = self._resolve_vcf_retention_days(session, sequencing_run_id)

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
            "created_at, created_by, retention_days) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (session.org_id, vcf_id, sequencing_run_id, vcf_path, content_hash, now, session.user_id, retention_days),
        )
        return vcf_id

    @auditable(
        action="interpretation_created",
        resource_type="interpretation",
        resource_id_from_result=True,
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
        # Validate vcf exists and get its content_hash and its already-resolved
        # retention_days -- human ruling D2 (2026-09-09): INHERITED here, never
        # recomputed (see clinical/retention.py's module docstring).
        vcf = self._query_one(
            "SELECT id, content_hash, retention_days FROM vcfs WHERE id = %s AND org_id = %s",
            (vcf_id, session.org_id),
        )
        if vcf is None:
            raise NotFoundError(f"VCF {vcf_id} not found")

        _, content_hash, retention_days = vcf

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
        submission_key = interpretation_submission_key(content_hash, platform_sample_id)

        interpretation_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO interpretations (org_id, id, vcf_id, run_document, submission_key, "
            "created_at, created_by, retention_days) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session.org_id,
                interpretation_id,
                vcf_id,
                json.dumps(run_document),
                submission_key,
                now,
                session.user_id,
                retention_days,
            ),
        )
        return interpretation_id

    @auditable(
        action="report_created",
        resource_type="report",
        resource_id_from_result=True,
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
        # Validate interpretation exists in this org, and read its already-
        # resolved retention_days -- human ruling D2 (2026-09-09): INHERITED
        # here, never recomputed.
        interp = self._query_one(
            "SELECT id, retention_days FROM interpretations WHERE id = %s AND org_id = %s",
            (interpretation_id, session.org_id),
        )
        if interp is None:
            raise NotFoundError(f"Interpretation {interpretation_id} not found")
        _, retention_days = interp

        report_id = uuid.uuid4()
        now = self._clock.now()
        self._execute(
            "INSERT INTO reports (org_id, id, interpretation_id, created_at, created_by, retention_days) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (session.org_id, report_id, interpretation_id, now, session.user_id, retention_days),
        )
        return report_id

    @auditable(
        action="reanalysis_created",
        resource_type="interpretation",
        resource_id_from_result=True,
        requires_session=True,
        details_builder=lambda params, result: {
            "vcf_id": str(params.get("vcf_id")),
            "parent_interpretation_id": str(params.get("parent_interpretation_id")),
        },
    )
    @transactional
    def create_reanalysis(
        self,
        session: Session,
        vcf_id: uuid.UUID,
        parent_interpretation_id: uuid.UUID,
        run_document: dict[str, Any],
    ) -> uuid.UUID:
        """
        Spec 15.3: "Re-analysis produces a new interpretation, a new report,
        and a new lineage branch from the same VCF. It does not update the
        old one." This creates the new interpretation only; call
        create_report() with the returned id exactly as for a fresh
        interpretation -- the two-call pattern is unchanged, only the source
        of the interpretation differs.

        Human ruling 1 (who may trigger): same act as create_interpretation,
        with a different trigger -- create_interpretation has no explicit
        role gate today, so this does not add one either. Widening either
        method's gating is a separate decision, not this commit's.

        Human ruling 2 (predecessor state): not gated on the parent's report
        state. 15.3 re-interprets the VCF, not the report -- a draft or
        under-review report on the parent must not block new evidence being
        applied to the underlying data. No report-state query appears here.

        Human ruling 3 (branching): parent_interpretation_id is the caller's
        explicit claim, never derived as "the current tip" of anything --
        A->B and A->C are both valid, and so is re-branching from B after C
        exists. The only validation is that the named parent exists, is in
        this org, and belongs to the named vcf_id -- so a caller who passes
        a parent from a different VCF is refused rather than silently
        re-analysing the wrong data.

        submission_key: interpretations.submission_key is UNIQUE per org
        (Phase 4a idempotency for original submissions). Reusing that
        formula (content_hash|platform_sample_id) here would make every
        re-analysis of the same VCF collide with the original AND with each
        other, which directly contradicts ruling 3's "A->B and A->C both
        valid". A re-analysis is a deliberate new act, not a resubmission,
        so it gets a key that is unconditionally unique per call instead of
        idempotent on content.
        """
        # retention_days: INHERITED from the vcf, same as create_interpretation
        # -- human ruling D2 (2026-09-09), never recomputed for a re-analysis.
        vcf = self._query_one(
            "SELECT id, retention_days FROM vcfs WHERE id = %s AND org_id = %s",
            (vcf_id, session.org_id),
        )
        if vcf is None:
            raise NotFoundError(f"VCF {vcf_id} not found")
        _, retention_days = vcf

        parent = self._query_one(
            "SELECT vcf_id FROM interpretations WHERE org_id = %s AND id = %s",
            (session.org_id, parent_interpretation_id),
        )
        if parent is None:
            raise NotFoundError(f"Parent interpretation {parent_interpretation_id} not found")
        parent_vcf_id = parent[0]
        if parent_vcf_id != vcf_id:
            raise ValueError(
                f"Parent interpretation {parent_interpretation_id} belongs to VCF "
                f"{parent_vcf_id}, not the named VCF {vcf_id}"
            )

        interpretation_id = uuid.uuid4()
        now = self._clock.now()
        submission_key = f"reanalysis:{parent_interpretation_id}:{interpretation_id}"
        self._execute(
            "INSERT INTO interpretations (org_id, id, vcf_id, run_document, submission_key, "
            "parent_interpretation_id, created_at, created_by, retention_days) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session.org_id,
                interpretation_id,
                vcf_id,
                json.dumps(run_document),
                submission_key,
                parent_interpretation_id,
                now,
                session.user_id,
                retention_days,
            ),
        )
        return interpretation_id

    @auditable(
        action="reanalyses_query",
        resource_type="interpretations",
        requires_session=True,
        auditable=True,
        details_builder=lambda params, result: {
            "parent_interpretation_id": str(params.get("parent_interpretation_id")),
        },
    )
    @transactional
    def find_reanalyses(self, session: Session, parent_interpretation_id: uuid.UUID) -> List[dict[str, Any]]:
        """
        The read path spec 15.3's lineage branch needs: every interpretation
        that names `parent_interpretation_id` as its parent, i.e. the direct
        children of one node in the branch (ruling 3 allows more than one --
        A->B and A->C both exist as siblings). Ordered by (created_at, id)
        for a stable, total order, same discipline as
        find_interpretations_by_criteria.
        """
        rows = self._query(
            "SELECT id, vcf_id, run_document, created_at, created_by "
            "FROM interpretations WHERE org_id = %s AND parent_interpretation_id = %s "
            "ORDER BY created_at, id",
            (session.org_id, parent_interpretation_id),
        )
        results = []
        for row in rows:
            interp_id, vcf_id, run_document_json, created_at, created_by = row
            run_doc = json.loads(run_document_json) if isinstance(run_document_json, str) else run_document_json
            results.append(
                {
                    "interpretation_id": interp_id,
                    "vcf_id": vcf_id,
                    "parent_interpretation_id": parent_interpretation_id,
                    "run_document": run_doc,
                    "created_at": created_at,
                    "created_by": created_by,
                }
            )
        return results

    # ── D0 (2026-09-12): a pipeline run becomes clinical records ──────────────

    @auditable(
        action="pipeline_result_recorded",
        resource_type="interpretation",
        # Returns a RecordedPipelineResult that also carries submission_key and the
        # sibling ids. Name the ONE field that is the subject, and no other.
        resource_id_from_result=lambda result: result.interpretation_id if result else None,
        requires_session=True,
        auditable=True,
        reason="one completed pipeline run recorded as clinical records (spec 10.1 step 7)",
        details_builder=lambda params, result: {
            "sample_id": str(params.get("sample_id")),
            "vcf_path": params.get("vcf_path"),
            "expected_vcf_hash": params.get("expected_vcf_hash"),
            "vcf_id": str(result.vcf_id) if result else None,
            "report_id": str(result.report_id) if result else None,
        },
    )
    @transactional
    def record_pipeline_result(
        self,
        session: Session,
        sample_id: uuid.UUID,
        vcf_path: str,
        expected_vcf_hash: str,
        run_document: dict[str, Any],
    ) -> RecordedPipelineResult:
        """
        Record one completed pipeline run: a sequencing run for the sample,
        the VCF it analysed, the interpretation (its run_document), and a
        draft report on that interpretation -- ALL OR NOTHING.

        WHY THIS DOES NOT CALL create_sequencing_run / create_vcf /
        create_interpretation / create_report. Each of those is @auditable,
        and @auditable commits when it returns. Called in a row they are four
        commits, and a failure at the third or fourth left a VCF with no
        interpretation, or an interpretation with no report, in the record --
        a lineage the platform never produced a report for. So this method
        performs the same inserts (same columns, same org scoping, same
        retention inheritance, same submission-key formula) inside ONE
        @transactional body, writes the same per-resource audit rows WITHOUT
        committing, and commits once. The existing methods are unchanged.

        VCF IDENTITY. `expected_vcf_hash` is the SHA-256 the caller took of
        the VCF BEFORE running the pipeline. It is recomputed here from the
        file on disk; if the two differ the run analysed different bytes from
        the ones about to be recorded against it, and VcfChangedError is
        raised before anything is written.

        DUPLICATES. uk_interp_submission makes a second interpretation of the
        same VCF content for the same sample impossible; that refusal
        surfaces as DuplicateInterpretationError (nothing from this attempt
        written) so a retrying worker can find the first one with
        find_interpretation_by_submission_key instead of failing.

        Refusals before any write: the sample is not in this organisation, or
        its order/patient chain does not resolve (NotFoundError), or the VCF
        changed (VcfChangedError). Any failure after the first write rolls
        every row back; the outer @auditable still records the attempt as an
        error.
        """
        # ── Refusals, before any write ────────────────────────────────────
        chain = self._query_one(
            "SELECT s.sample_id FROM samples s "
            "JOIN orders o ON o.org_id = s.org_id AND o.order_id = s.order_id "
            "JOIN patients p ON p.org_id = o.org_id AND p.patient_id = o.patient_id "
            "WHERE s.org_id = %s AND s.sample_id = %s",
            (session.org_id, sample_id),
        )
        if chain is None:
            raise NotFoundError(f"Sample {sample_id} not found")

        try:
            content_hash = _sha256_file(vcf_path)
        except OSError as e:
            raise VcfChangedError(f"VCF {vcf_path} can no longer be read: {e}") from e
        if content_hash != expected_vcf_hash:
            raise VcfChangedError(
                f"VCF {vcf_path} hashes to {content_hash}, not the {expected_vcf_hash} "
                "it had when the pipeline ran; refusing to record a run against bytes it did not analyse."
            )

        submission_key = interpretation_submission_key(content_hash, sample_id)
        actor_role = self._get_actor_role(session)
        now = self._clock.now()

        def audit(action: str, resource_type: str, resource_id: uuid.UUID, details: dict[str, Any]) -> None:
            self._write_audit_entry(
                action,
                resource_type,
                resource_id=str(resource_id),
                outcome="success",
                org_id=session.org_id,
                user_id=session.user_id,
                actor_role=actor_role,
                details=details,
            )

        # ── Writes: one transaction ───────────────────────────────────────
        try:
            run_id = uuid.uuid4()
            self._execute(
                "INSERT INTO sequencing_runs (org_id, id, sample_id, created_at, created_by) "
                "VALUES (%s, %s, %s, %s, %s)",
                (session.org_id, run_id, sample_id, now, session.user_id),
            )
            audit("sequencing_run_created", "sequencing_run", run_id, {"sample_id": str(sample_id)})

            # Ruling D2: resolved once, here at the VCF; inherited below.
            retention_days = self._resolve_vcf_retention_days(session, run_id)

            vcf_id = uuid.uuid4()
            self._execute(
                "INSERT INTO vcfs (org_id, id, sequencing_run_id, vcf_path, content_hash, "
                "created_at, created_by, retention_days) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (session.org_id, vcf_id, run_id, vcf_path, content_hash, now, session.user_id, retention_days),
            )
            audit("vcf_created", "vcf", vcf_id, {"sequencing_run_id": str(run_id), "vcf_path": vcf_path})

            interpretation_id = uuid.uuid4()
            try:
                self._execute(
                    "INSERT INTO interpretations (org_id, id, vcf_id, run_document, submission_key, "
                    "created_at, created_by, retention_days) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        session.org_id,
                        interpretation_id,
                        vcf_id,
                        json.dumps(run_document),
                        submission_key,
                        now,
                        session.user_id,
                        retention_days,
                    ),
                )
            except Exception as e:
                if getattr(e, "sqlstate", None) == "23505":  # unique_violation: uk_interp_submission
                    raise DuplicateInterpretationError(submission_key) from e
                raise
            audit(
                "interpretation_created",
                "interpretation",
                interpretation_id,
                {"vcf_id": str(vcf_id), "platform_sample_id": str(sample_id)},
            )

            report_id = uuid.uuid4()
            self._execute(
                "INSERT INTO reports (org_id, id, interpretation_id, created_at, created_by, retention_days) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (session.org_id, report_id, interpretation_id, now, session.user_id, retention_days),
            )
            audit("report_created", "report", report_id, {"interpretation_id": str(interpretation_id)})
        except NotFoundError as e:
            # @transactional COMMITS on NotFoundError (it is an "expected"
            # refusal elsewhere). Here it can only arrive after the first
            # insert, so re-raise as something that rolls back.
            raise RuntimeError(f"Lineage vanished mid-record: {e}") from e

        return RecordedPipelineResult(
            interpretation_id=interpretation_id,
            report_id=report_id,
            vcf_id=vcf_id,
            sequencing_run_id=run_id,
            submission_key=submission_key,
        )

    @auditable(
        action="interpretation_read",
        resource_type="interpretation",
        requires_session=True,
        auditable=False,
        reason="read is not a resource action",
    )
    @transactional
    def find_interpretation_by_submission_key(self, session: Session, submission_key: str) -> Optional[dict[str, Any]]:
        """
        The interpretation this organisation already holds under
        `submission_key` (see interpretation_submission_key), or None.

        What a retrying or reconciling worker asks before running the
        pipeline again: "was this run already recorded?". Returns the
        interpretation, its VCF, and its ORIGINAL report -- the earliest
        report on the interpretation that is not itself an amendment (an
        amendment is a later report row on the same interpretation, and is
        not what the run produced). report_id is None only for an
        interpretation created without one (the pre-D0 two-call path).
        """
        row = self._query_one(
            "SELECT id, vcf_id FROM interpretations WHERE org_id = %s AND submission_key = %s",
            (session.org_id, submission_key),
        )
        if row is None:
            return None
        interpretation_id, vcf_id = row
        report = self._query_one(
            "SELECT r.id FROM reports r "
            "WHERE r.org_id = %s AND r.interpretation_id = %s "
            "AND NOT EXISTS (SELECT 1 FROM amendments a WHERE a.org_id = r.org_id AND a.amendment_report_id = r.id) "
            "ORDER BY r.created_at, r.id LIMIT 1",
            (session.org_id, interpretation_id),
        )
        return {
            "interpretation_id": interpretation_id,
            "report_id": report[0] if report else None,
            "vcf_id": vcf_id,
        }

    @auditable(
        action="retention_policy_set",
        resource_type="retention_policy",
        resource_id_param="artefact_class",
        requires_session=True,
        details_builder=lambda params, result: {
            "artefact_class": params.get("artefact_class"),
            "retention_days": params.get("retention_days"),
            "retention_days_minor": params.get("retention_days_minor"),
        },
    )
    @transactional
    def set_retention_policy(
        self,
        session: Session,
        artefact_class: str,
        retention_days: int,
        retention_days_minor: Optional[int] = None,
    ) -> None:
        """
        Spec 22, human ruling D2 (re-ruled 2026-09-09, age-conditional):
        periods are configuration, never code -- this is the write side of
        that configuration surface, Administrator-gated, the same
        governance level as assign_role, because a retention period is a
        policy decision about clinical data, not routine data entry.

        `retention_days` is the ADULT override; `retention_days_minor` is
        the MINOR override, independently optional -- an org may set
        either, both, or neither. Either one left unset falls back to this
        codebase's own product default (clinical/retention.py
        ::DEFAULT_ADULT_VCF_RETENTION_DAYS/DEFAULT_MINOR_VCF_RETENTION_DAYS)
        rather than to the other override.

        Restricted to RETENTION_ARTEFACT_CLASSES (now just "vcf" -- see
        that tuple's own comment): a policy for a class with no purge path
        is not configuration, it is a policy nobody can ever act on, and
        accepting it here would let one exist unnoticed.
        """
        self._require_role(session, "Administrator")
        if artefact_class not in RETENTION_ARTEFACT_CLASSES:
            raise ValueError(
                f"Unknown retention artefact_class: {artefact_class!r}. "
                f"Known classes: {', '.join(RETENTION_ARTEFACT_CLASSES)}"
            )
        if retention_days <= 0:
            raise ValueError(f"retention_days must be positive, got {retention_days}")
        if retention_days_minor is not None and retention_days_minor <= 0:
            raise ValueError(f"retention_days_minor must be positive, got {retention_days_minor}")

        now = self._clock.now()
        self._execute(
            "INSERT INTO retention_policies "
            "(org_id, artefact_class, retention_days, retention_days_minor, created_at, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (org_id, artefact_class) DO UPDATE SET "
            "retention_days = EXCLUDED.retention_days, "
            "retention_days_minor = EXCLUDED.retention_days_minor, "
            "created_at = EXCLUDED.created_at, "
            "created_by = EXCLUDED.created_by",
            (session.org_id, artefact_class, retention_days, retention_days_minor, now, session.user_id),
        )

    @auditable(
        action="retention_policy_read",
        resource_type="retention_policy",
        requires_session=True,
        auditable=False,
        reason="read is not a resource action",
    )
    @transactional
    def get_retention_policy(self, session: Session, artefact_class: str) -> Optional[int]:
        """Returns the configured ADULT retention_days for this org and class, or None if unset."""
        row = self._query_one(
            "SELECT retention_days FROM retention_policies WHERE org_id = %s AND artefact_class = %s",
            (session.org_id, artefact_class),
        )
        return row[0] if row else None

    @auditable(
        action="retention_policy_read",
        resource_type="retention_policy",
        requires_session=True,
        auditable=False,
        reason="read is not a resource action",
    )
    @transactional
    def get_retention_policy_minor(self, session: Session, artefact_class: str) -> Optional[int]:
        """Returns the configured MINOR retention_days override for this org and
        class, or None if this org has not overridden the product default for
        minors -- see clinical/retention.py::DEFAULT_MINOR_VCF_RETENTION_DAYS."""
        row = self._query_one(
            "SELECT retention_days_minor FROM retention_policies WHERE org_id = %s AND artefact_class = %s",
            (session.org_id, artefact_class),
        )
        return row[0] if row else None

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
        """
        Retrieve a report by ID, within the session's organisation.

        `state` is returned (added w117, 2026-09-13) because a caller outside
        this module has to be able to ASK what state a report is in without
        being given SQL. review/signoff.py needs it to refuse an override of
        an already-approved report and point at the amendment path instead;
        without it, that refusal could only be written by reaching past this
        class. Reading state here is not a state FILTER -- every report is
        still returned in every state, which is the property
        test_report_state_is_a_control.py guards.

        `is_retracted` is returned (added w122, human ruling E7) for the same
        reason and under the same restriction. E7 ruled that a retracted
        report is STILL RETURNED, carrying the fact: filtering it out would be
        a state filter on a retrieval path, which is the exact property the
        guard above forbids, and it would make a retracted report unfindable
        by the route `geper/api/submission_worker.py` uses on the duplicate-
        submission path. So the report comes back and the caller is told -- a
        fact recorded alongside, never a record removed.
        """
        row = self._query_one(
            "SELECT id, interpretation_id, created_at, created_by, state FROM reports WHERE id = %s AND org_id = %s",
            (report_id, session.org_id),
        )
        if row is None:
            return None
        return {
            "id": row[0],
            "interpretation_id": row[1],
            "created_at": row[2],
            "created_by": row[3],
            "state": row[4],
            "is_retracted": self._read_retraction_row(session, report_id) is not None,
        }

    # ── Staff reads for the HUMAN TOOLS (sign-off, revision refresh, LIMS export), over HTTP ──────
    #
    # These three exist so that a person's tool can learn a report's authoritative state through
    # the authenticated HTTP API and never hold a database credential. Each is a READ that wraps
    # an existing, organisation-scoped read behind a role gate; none writes, none widens what the
    # underlying read returns, and none has a counterpart that names another organisation or user.
    #
    # WHO MAY READ. A report's state and revision are clinical-governance information, not
    # something every member of staff needs: the roles that review, approve, audit or administer
    # reports may (REPORT_READ_ROLES); an Orderer or a Lab technician may not. This list is a POLICY
    # choice made for the pilot and is stated here so it can be reviewed and changed in one place.

    @auditable(
        action="report_status_read",
        resource_type="report",
        requires_session=True,
        auditable=False,
        reason="read is not a resource action; the sign-off or export that uses it is itself audited",
    )
    @transactional
    def read_report_status(self, session: Session, report_id: uuid.UUID) -> Optional[dict[str, Any]]:
        """`get_report` for a staff reader: the report's state, interpretation and retraction flag, or
        None for an unknown or cross-organisation id. Raises AuthorizationError without a reading role."""
        self._require_any_role(session, REPORT_READ_ROLES)
        return self.get_report(session, report_id)

    @auditable(
        action="report_revision_read",
        resource_type="report",
        requires_session=True,
        auditable=False,
        reason="read is not a resource action; the sign-off or export that uses it is itself audited",
    )
    @transactional
    def read_report_revision(self, session: Session, report_id: uuid.UUID) -> dict[str, Any]:
        """`get_report_revision` for a staff reader. `{}` means a successful lookup found nothing to
        say; EVERY failure is an exception -- NotFoundError for an unknown or cross-organisation id,
        AuthorizationError without a reading role, BrokenRevisionRecordError for a record that cannot
        be read into a true sentence -- and none is ever turned into an empty answer."""
        self._require_any_role(session, REPORT_READ_ROLES)
        return self.get_report_revision(session, report_id)

    @auditable(
        action="signoff_state_read",
        resource_type="report",
        requires_session=True,
        auditable=False,
        reason="read is not a resource action; it writes nothing, and recovery from an interrupted sign-off "
        "is itself audited when it acts",
    )
    @transactional
    def read_signoff_state(self, session: Session, report_id: uuid.UUID) -> Optional[dict[str, Any]]:
        """
        Where a report's sign-off stands, for an operator working out what an interrupted sign-off left behind.
        READ-ONLY: it writes nothing (no audit row either: `auditable=False`), and it does not call
        `verify_report_integrity`, which does write one.

        Returns None for an unknown or cross-organisation report (the usual meaning throughout this file).
        Raises AuthorizationError without a reading role (REPORT_READ_ROLES, the same set as the other staff
        reads); the role gate runs first, so a caller without one learns nothing about existence.

        ONE STATEMENT, ONE SNAPSHOT. The report row, the retraction flag and the claim summary are read in a
        single SELECT, so under READ COMMITTED they are one consistent view: a claim recorded or an approval
        committed by someone else cannot appear in one part and not in another. (Two statements would each get
        their own snapshot.)

        WHAT IT RETURNS, AND WHAT IT DOES NOT. The report's state, interpretation, approver and approval time,
        whether an approval hash exists (the hash itself is not returned), the retraction flag, and for each
        claim on THIS report: its id, type, actor and time. NOT the claim's reason, variant, classification or
        evidence, and nothing from the report's content or any patient record.
        """
        self._require_any_role(session, REPORT_READ_ROLES)
        row = self._query_one(
            "SELECT r.id, r.interpretation_id, r.state, r.approver_id, r.approved_at, "
            "(r.content_hash IS NOT NULL), "
            "EXISTS (SELECT 1 FROM report_retractions x WHERE x.org_id = r.org_id AND x.report_id = r.id), "
            "COALESCE((SELECT json_agg(json_build_object("
            "'id', c.id, 'claim_type', c.claim_type, 'actor_id', c.actor_id, "
            "'recorded_at', to_char(c.\"timestamp\" AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"')"
            ') ORDER BY c."timestamp", c.id) '
            "FROM reviewer_claims c WHERE c.org_id = r.org_id AND c.report_id = r.id), '[]'::json) "
            "FROM reports r WHERE r.id = %s AND r.org_id = %s",
            (report_id, session.org_id),
        )
        if row is None:
            return None
        return {
            "id": row[0],
            "interpretation_id": row[1],
            "state": row[2],
            "approver_id": row[3],
            "approved_at": row[4],
            "has_content_hash": bool(row[5]),
            "is_retracted": bool(row[6]),
            "claims": [
                {
                    "id": uuid.UUID(str(claim["id"])),
                    "claim_type": claim["claim_type"],
                    "actor_id": uuid.UUID(str(claim["actor_id"])),
                    "recorded_at": claim["recorded_at"],
                }
                for claim in (row[7] or [])
            ],
        }

    @auditable(
        action="signing_identity_read",
        resource_type="user",
        requires_session=True,
        auditable=False,
        reason="a read of the caller's own printed identity; the sign-off that uses it is itself audited",
    )
    @transactional
    def read_own_signing_identity(self, session: Session) -> Dict[str, Any]:
        """The CALLER'S OWN name, registration number and hospital (get_signing_identity for
        `session.user_id`). There is no parameter naming anybody else: a person's tool asks who it is
        signing as, and a colleague's identity is not reachable through it. Raises
        IncompleteClinicianIdentityError when the record lacks any of the three."""
        return self.get_signing_identity(session, session.user_id)

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
    def check_qc_passed(self, session: Session, order_id: uuid.UUID, sample_id: Optional[uuid.UUID] = None) -> str:
        """
        Check sample QC status.

        Per §10.1: "Sample QC passed" means samples.qc_status = 'passed'.

        WHICH SAMPLE. With `sample_id` (every submission names one) the verdict is THAT sample's, and only if it is a
        sample of THIS order in THIS organisation: a sample that is not is "sample_unresolved", never another sample's
        result. An order can hold several samples (a re-collection after a failed QC), so reading "the order's latest
        sample" lets an older failed sample ride on a newer one's pass, or a passed sample be blocked by a newer one
        that has not. Without `sample_id` the earlier order-level behaviour (the latest sample) is kept for callers that
        cannot name one.

        Returns a reason string (enum):
          - "qc_ok": qc_status = 'passed'
          - "qc_pending": qc_status = 'pending'
          - "qc_failed": qc_status = 'failed'
          - "sample_unresolved": `sample_id` given and not a sample of this order
        """
        if sample_id is not None:
            sample = self._query_one(
                "SELECT qc_status FROM samples WHERE org_id = %s AND order_id = %s AND sample_id = %s",
                (session.org_id, order_id, sample_id),
            )
            if sample is None:
                return "sample_unresolved"
        else:
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
        qc_result = self.check_qc_passed(session, order_id, sample_id)
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
        This method creates a session WITHOUT authentication, and the session_id it
        mints is a real row in `sessions` and therefore a valid bearer credential for
        clinical/endpoints.py::_get_session. It MUST NOT be called from any
        request-handling context or endpoint. If it is, that is an authentication
        bypass.

        THE CALL SITES, AND WHY THIS PARAGRAPH USED TO BE FALSE (w129 GAP-5).
        It used to say "an AST test enforces that this method is called from exactly
        one location". Both halves were wrong. The test read only data_access.py and
        matched only `self._create_system_session(`, of which there are none, so it
        asserted 0 <= 1 and had never examined a call site. And the real number is not
        one: there are FOUR, all spelled `self.data_access._create_system_session`,
        all outside this file --

            geper/api/exception_retry_worker.py  ExceptionRetryWorker._process_all_orgs
            geper/api/submission_worker.py       InterpretationWorker._create_submission_failure_exception
            geper/api/submission_worker.py       InterpretationWorker._clinical_preflight
            geper/api/submission_worker.py       InterpretationWorker.reconcile_interrupted_submissions

        THE RULE IS ABOUT WHERE, NOT HOW MANY. Every call site must sit inside a
        background worker class in geper/api/ that runs as its own process, has no
        human actor, and is not reachable from any served route -- counted: neither
        worker class is referenced by geper/api/main.py, clinical/app.py or
        clinical/endpoints.py, and each is instantiated only by its own module's
        main(). The four sites are pinned BY NAME in
        clinical/tests/test_identity.py::TestStructuralIsolation, so a fifth fails
        the suite and so does narrowing the scan back to this file.
        """
        now = self._clock.now()
        system_email = f"system+{org_id}@platform"

        # Fetch or create the system principal for this org. Two workers making their first request for an organisation at the
        # same moment both see no row; a plain SELECT-then-INSERT lets the loser hit users_org_email_unique and fail its job.
        # So the INSERT yields to an existing row (ON CONFLICT DO NOTHING on the same unique constraint, which exists in every
        # schema) and the principal is read back afterwards: whichever INSERT won, both callers get the one row.
        select_user = "SELECT user_id FROM users WHERE org_id = %s AND email = %s AND is_system_account = true"
        user = self._query_one(select_user, (org_id, system_email))
        if user is None:
            self._execute(
                "INSERT INTO users (user_id, org_id, email, password_hash, password_changed_at, "
                "                   is_system_account, disabled, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT ON CONSTRAINT users_org_email_unique DO NOTHING",
                (uuid.uuid4(), org_id, system_email, _DUMMY_HASH, now, True, False, now, now),
            )
            user = self._query_one(select_user, (org_id, system_email))
            if user is None:
                # The address is taken by something that is not this organisation's system principal. Never adopt it.
                raise RuntimeError(f"system principal for organisation {org_id} could not be created or found")
        user_id = user[0]

        # The System role, ensured on every call rather than only by the caller that created the user: the caller that lost
        # the race above must not mint a session for a principal whose creator has not yet granted the role.
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
        return interpretation_submission_key(vcf_hash, sample_id)

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
                "WHERE exception_id = %s AND org_id = %s "
                "ORDER BY timestamp ASC",
                (exc_id, session.org_id),
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
                "WHERE exception_id = %s AND org_id = %s "
                "ORDER BY timestamp ASC",
                (exc_id, session.org_id),
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
        resource_id_param="exception_id",
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
        """
        Resolve an exception, recording the resolution event and updating status.

        w129 GAP-1. This method used to have an asymmetry inside its own body:
        the UPDATE bound org_id, the INSERT did not. `_execute` returns None and
        never inspects rowcount, so when the UPDATE matched zero rows -- which is
        exactly what happens when the caller names another organisation's
        exception -- nothing stopped execution falling through into an INSERT
        that appended a 'resolve' row to the victim's append-only history. The
        method RETURNED NORMALLY. Measured, not reasoned.

        Two changes, and they are deliberately both here rather than either one
        alone:

          1. The exception is resolved org-scoped FIRST and a miss raises
             NotFoundError. A zero-row UPDATE must not be a silent success, and
             refusing here is also what makes the endpoint's two refusals
             identical -- a foreign id and an invented id both arrive at this
             same NotFoundError with the same message shape (w129 GAP-2).
          2. The event INSERT binds org_id from the session, and the schema's
             composite foreign key (exception_id, org_id) -> exceptions (id,
             org_id) makes the cross-tenant row inexpressible rather than merely
             unwritten. The guard above is the clean refusal; the key is the
             guarantee that survives the next person editing this method.

        ONE METHOD, NOT A COMPOSITION. The read, the UPDATE and the INSERT are
        written inline rather than delegated to get_exception_by_id or any other
        decorated method: @auditable commits after each audit row, so composing
        transactional work out of decorated methods destroys atomicity silently
        (the w128 lesson).
        """
        now = self._clock.now()

        # Org-scoped existence check. NOT get_exception_by_id -- see the
        # docstring's note on composition.
        target = self._query_one(
            "SELECT id FROM exceptions WHERE id = %s AND org_id = %s",
            (exception_id, session.org_id),
        )
        if target is None:
            # Identical for an id belonging to another organisation and an id
            # that exists nowhere. Telling them apart is a cross-tenant
            # existence oracle; the endpoint's 404 carries this message for both.
            raise NotFoundError(f"Exception {exception_id} not found")

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
            "INSERT INTO exception_events (id, org_id, exception_id, actor, timestamp, action, action_note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                uuid.uuid4(),
                session.org_id,
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
        resource_id_from_result=True,
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
                "INSERT INTO exception_events (id, org_id, exception_id, actor, timestamp, action, action_note) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    uuid.uuid4(),
                    session.org_id,
                    exc_id,
                    actor,
                    now,
                    ExceptionEventAction.REOPEN.value,
                    f"Reopen: {error_message}",
                ),
            )
            if category == _TRANSIENT_CATEGORY:  # an exception recorded before scheduling existed has no schedule yet
                self._execute(
                    "UPDATE exceptions SET next_retry_at = %s "
                    "WHERE id = %s AND org_id = %s AND next_retry_at IS NULL AND status = 'open'",
                    (now + _datetime.timedelta(seconds=_retry_backoff_seconds(0)), exc_id, session.org_id),
                )
            return exc_id

        # Create new exception. A TRANSIENT submission failure is scheduled for its first retry here, with the same
        # backoff `retry_scheduler` applies afterwards (attempt 0); any other category has no schedule and is never
        # retried automatically. Without this the scheduler (which selects only a non-NULL, due `next_retry_at`) could
        # never pick up a new failure.
        next_retry_at = (
            now + _datetime.timedelta(seconds=_retry_backoff_seconds(0)) if category == _TRANSIENT_CATEGORY else None
        )
        exc_id = uuid.uuid4()
        self._execute(
            "INSERT INTO exceptions "
            "(id, org_id, order_id, category, reason_code, error_message, status, owner, created_at, next_retry_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
                next_retry_at,
            ),
        )

        # Write OPEN event
        self._execute(
            "INSERT INTO exception_events (id, org_id, exception_id, actor, timestamp, action, action_note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                uuid.uuid4(),
                session.org_id,
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

        backoff_seconds = _retry_backoff_seconds

        exceptions_to_retry = self._query(
            "SELECT id, attempt_count, order_id, reason_code "
            "FROM exceptions "
            "WHERE org_id = %s AND status = 'open' "
            "  AND category = 'transient_submission_failure' "
            "  AND next_retry_at IS NOT NULL AND next_retry_at <= %s",
            (session.org_id, now),
        )

        for exc_id, attempt_count, order_id, reason_code in exceptions_to_retry:
            if attempt_count >= _MAX_RETRY_ATTEMPTS:
                # Escalation and its audit record are ONE statement: the row that changes state is the row that is recorded,
                # so a crash cannot escalate silently and a second pass (the exception is no longer 'open') records nothing
                # more. Only identifiers and codes go in the record, never the error text, which can carry a path.
                escalated = self._query(
                    "WITH esc AS ("
                    "  UPDATE exceptions SET status = 'escalated' "
                    "  WHERE id = %s AND org_id = %s AND status = 'open' "
                    "  RETURNING id, org_id, order_id, category, reason_code, attempt_count"
                    ") "
                    'INSERT INTO audit_log (org_id, user_id, "timestamp", actor_role, action, resource_type, '
                    "                       resource_id, outcome, details) "
                    "SELECT org_id, %s, %s, 'System', 'exception_retry_escalated', 'exception', id::text, "
                    "       'escalated', jsonb_build_object('order_id', order_id::text, 'category', category, "
                    "                                       'reason_code', reason_code, 'attempt_count', attempt_count) "
                    "FROM esc RETURNING resource_id",
                    (exc_id, session.org_id, session.user_id, now),
                )
                if escalated:
                    import logging

                    logging.getLogger("clinical.data_access").error(
                        "retry_escalated exception_id=%s org_id=%s order_id=%s reason_code=%s attempts=%s: "
                        "automatic retries are exhausted; an operator must act (see the worklist)",
                        exc_id,
                        session.org_id,
                        order_id,
                        reason_code,
                        attempt_count,
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
                    self._requeue_failed_submission_for_order(submission_store, session.org_id, order_id, reason_code)

    def _requeue_failed_submission_for_order(
        self, submission_store, org_id: uuid.UUID, order_id: uuid.UUID, reason_code: Optional[str] = None
    ) -> None:
        """
        Re-queue the FAILED submissions of one order that failed for the reason this exception records.

        An exception is keyed by (order, reason code), so the submissions it speaks for are exactly the order's failed
        submissions whose own failure code is that reason. A submission that failed for any other reason (a missing
        VCF, a failed QC, a clinical-record refusal) is not this exception's to retry and stays failed. The row keeps
        its error message when it moves back to the queue (`SubmissionStore.requeue`), so the reason survives the retry.

        `reason_code` None keeps the earlier behaviour of re-queueing every failed submission of the order, for a caller
        that cannot name one.
        """
        import logging

        logger = logging.getLogger("clinical.data_access")
        try:
            if reason_code is not None and hasattr(submission_store, "failed_submission_ids"):
                for submission_id in submission_store.failed_submission_ids(str(org_id), str(order_id), reason_code):
                    submission_store.requeue(submission_id)
                return
            import sqlite3

            with sqlite3.connect(submission_store.db_path) as conn:
                failed_submissions = conn.execute(
                    "SELECT id FROM submissions WHERE org_id = ? AND order_id = ? AND status = 'failed'",
                    (str(org_id), str(order_id)),
                ).fetchall()
            for (submission_id,) in failed_submissions:
                submission_store.update_status(submission_id, "queued")
        except Exception as e:
            # Log error but don't fail the scheduler
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
            "WHERE exception_id = %s AND org_id = %s "
            "ORDER BY timestamp ASC",
            (exc_id, session.org_id),
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
        report_id: Any,
        actor_id: Any,
        reason: Optional[str],
    ) -> None:
        """
        Shared precondition check for every reviewer claim.

        All three lookups are org-scoped by construction -- they filter on
        session.org_id, so an interpretation, a report or an actor belonging to
        another organisation is indistinguishable from one that does not exist.
        That is the intended answer, not a rounding of it: a cross-org id is not
        a permission error to be reported back, it is simply absent from this
        org's world, and reporting it as "forbidden" would confirm it exists.

        THE REPORT MUST BE A REPORT OF THIS INTERPRETATION, and that check is
        here rather than in the schema because a row constraint cannot read the
        reports row to compare against -- the same reason variant membership
        and supersession uniqueness are methods' work (see reviewer_claims's
        own schema comment). Without it, report_id would be a column the
        application could fill with any report at all, which would make
        "this report's claims" a query over an association nothing established.
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

        # FOR SHARE: any number of claims may be recorded together, but not while the report is being approved
        # (which holds FOR UPDATE until it commits). A claim that arrived after the seal would not be in the
        # content hash and would read as tampering to verify_report_integrity.
        report = self._query_one(
            "SELECT interpretation_id, state FROM reports WHERE org_id = %s AND id = %s FOR SHARE",
            (session.org_id, report_id),
        )
        if report is None:
            raise NotFoundError(f"Report {report_id} not found")
        if report[1] in ("approved", "released"):
            raise ValueError(
                f"Report {report_id} is '{report[1]}': a claim can only be recorded while the report is a draft "
                "or under review. The approval sealed the claims that existed (content_hash covers them), so a "
                "claim added now would make the sealed report look altered. A correction to an approved report "
                "is an amendment, a second report on the same interpretation."
            )
        if report[0] != interpretation_id:
            raise ValueError(
                f"Report {report_id} is not a report of interpretation {interpretation_id}: a claim is "
                "recorded against one report of the interpretation it is about, and attaching it to "
                "another interpretation's report would attribute the review to a document it never read."
            )

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
        report_id: Any,
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
            "(org_id, id, interpretation_id, report_id, variant_key, claim_type, classification, "
            ' actor_id, "timestamp", reason) '
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session.org_id,
                claim_id,
                interpretation_id,
                report_id,
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
        # Returns the new claim's UUID (from _insert_claim) and nothing else; the claim's
        # free-text reason is deliberately in no audit column.
        resource_id_from_result=True,
        requires_session=True,
        auditable=True,
        reason="reviewer concurs with an interpretation (spec 13.2)",
    )
    @transactional
    def _record_accept(
        self,
        session: Session,
        interpretation_id: Any,
        report_id: Any,
        actor_id: Any,
        reason: str,
    ) -> Any:
        """
        Record an 'accept' claim against one report of an interpretation.

        Under the re-derive reading the schema's own comment records, an accept
        is NOT agreement with a draft -- it is an independent concurrence, a
        second clinician arriving at the same classification on their own. That
        is evidence, and evidence without stated grounds is not evidence, which
        is why `reason` is required here exactly as it is for a disagreement.

        The only one of the four that names no VARIANT: it concurs with the
        interpretation as a whole, so variant_key and classification both stay
        NULL. That asymmetry is why they are nullable. It is still scoped to a
        report like every other claim -- see report_id's schema comment for
        why every claim names one.
        """
        self._validate_claim_inputs(session, interpretation_id, report_id, actor_id, reason)
        return self._insert_claim(session, interpretation_id, report_id, actor_id, reason, claim_type="accept")

    @auditable(
        action="record_sole_signatory",
        resource_type="reviewer_claim",
        # Returns the new claim's UUID (from _insert_claim) and nothing else; the claim's
        # free-text reason is deliberately in no audit column.
        resource_id_from_result=True,
        requires_session=True,
        auditable=True,
        reason="the one signatory records their own concurrence, as a single signature and not as a second one",
    )
    @transactional
    def record_sole_signatory_claim(
        self,
        session: Session,
        interpretation_id: Any,
        report_id: Any,
        reason: str,
    ) -> Any:
        """
        Record a 'sole_signatory' claim: THE ONE PERSON SIGNING THIS REPORT
        concurs with the interpretation.

        WHY THIS IS NOT AN 'accept' (human ruling, 2026-09-13, w117). The
        product signs off with one person. _record_accept above documents an
        'accept' as an INDEPENDENT CONCURRENCE BY A SECOND CLINICIAN, so the
        sole signatory writing one would write a row the schema itself defines
        as false: the record would assert two-clinician review where one
        person acted. A distinct kind makes the record true at the level
        things query it -- an audit asking which reports had independent
        concurrence answers correctly from claim_type alone, and a site that
        later adopts two-person sign-off can see which historical reports were
        single-signed.

        PUBLIC, unlike the four spec-13.2 recorders, and the difference is not
        cosmetic. This is the method a delivery path (review/signoff.py) calls,
        and a delivery path reaching into private methods is how enforcement
        gets bypassed by accident. There is no separate actor_id: THE SIGNATORY
        IS THE SESSION. Letting the two diverge here would let one identity
        record another person's concurrence, which is exactly the falsehood
        this claim kind exists to stop.

        Interpretation-scoped, so variant_key and classification both stay
        NULL, exactly like 'accept'. `reason` is required, exactly like every
        other claim: concurrence without stated grounds is not evidence.

        CHANGES NO ENFORCEMENT. It satisfies submit_for_review's
        at-least-one-claim precondition the way any claim does, and
        _approve_report's Approver checks are untouched. Whether this platform
        should require two signatures is a separate, carded decision.

        STAGE 3: THE INTERPRETER ROLE, as the first statement. Until the claim
        recorders were reachable over HTTP nothing consulted a role here, so
        any authenticated session in the organisation could write the review
        evidence `submit_for_review` then accepts. Recording a claim is the
        Interpreter's work -- the same role `submit_for_review` already
        requires, and the one the only production caller (review/signoff.py,
        which records the claim and then submits with the SAME session)
        necessarily holds already.
        """
        self._require_role(session, "Interpreter")
        self._validate_claim_inputs(session, interpretation_id, report_id, session.user_id, reason)
        return self._insert_claim(
            session,
            interpretation_id,
            report_id,
            session.user_id,
            reason,
            claim_type="sole_signatory",
        )

    @auditable(
        action="record_disagreement",
        resource_type="reviewer_claim",
        requires_session=True,
        auditable=False,
        audit_denials_only=True,
        details_builder=lambda params, result: {
            "interpretation_id": str(params.get("interpretation_id")),
            "report_id": str(params.get("report_id")),
            "variant_key": params.get("variant_key"),
        },
        reason=(
            "declared non-SUCCESS-auditing HERE because the act IS audited, once, by _record_disagreement, "
            "which this method does nothing but delegate to. A second success row on the wrapper would "
            "write two audit entries for one clinical act, which is worse than none: it would inflate "
            "the count of recorded disagreements. A REFUSAL by this wrapper's own role gate never reaches "
            "the delegate, so audit_denials_only records exactly that one row (no reason text: it is "
            "free clinical prose, and the ids say what was attempted)."
        ),
    )
    def record_disagreement_claim(
        self,
        session: Session,
        interpretation_id: Any,
        report_id: Any,
        variant_key: str,
        new_classification: str,
        reason: str,
    ) -> Any:
        """
        PUBLIC wrapper over _record_disagreement, with the actor fixed to the
        session's own user.

        Exists so review/signoff.py -- a delivery path -- never has to reach
        into a private method to record the disagreement an override is. A
        caller outside this module cannot see the private recorders' contracts
        and would be one refactor away from silently bypassing them.

        Deliberately NOT separately @auditable: the method it delegates to
        already writes the 'record_disagreement' audit entry, and a second
        decorator here would record one clinical act twice. Nesting is safe --
        @transactional passes through an open transaction (see its docstring).

        No actor_id parameter, for the same reason record_sole_signatory_claim
        has none: the person acting is the authenticated person.

        STAGE 3: THE INTERPRETER ROLE, checked here and first, for the reason
        given on record_sole_signatory_claim. It is on the PUBLIC wrapper and
        not on `_record_disagreement` because the wrapper is the only entry a
        session can reach; the private recorder takes an `actor_id` and is
        exercised directly by the domain tests. A denial raised here IS
        audit-logged, once, by `audit_denials_only` on this wrapper's decorator
        (action 'record_disagreement', outcome 'denied', the acting user and
        role, the ids attempted); a success is audited once, by the recorder.
        """
        self._require_role(session, "Interpreter")
        return self._record_disagreement(
            session,
            interpretation_id=interpretation_id,
            report_id=report_id,
            variant_key=variant_key,
            actor_id=session.user_id,
            new_classification=new_classification,
            reason=reason,
        )

    @auditable(
        action="report_approved",
        resource_type="report",
        requires_session=True,
        auditable=False,
        reason=(
            "declared non-auditable HERE because the approval IS audited, once, by _approve_report, "
            "which this method does nothing but delegate to with actor_id fixed to the session's own "
            "user. A second decorator would record one approval twice -- and an approval counted "
            "twice in the audit log is a worse record than one counted once."
        ),
    )
    def approve_report(self, session: Session, report_id: uuid.UUID) -> None:
        """
        PUBLIC wrapper over _approve_report, recording the SESSION'S OWN USER
        as the approver.

        The one-person sign-off path needs an approval it can call without
        touching a private method, and without being handed the ability to
        name somebody else as approver. Fixing actor_id to session.user_id is
        that: the person authenticated is the person recorded.

        ENFORCEMENT IS UNCHANGED AND IS NOT RE-IMPLEMENTED HERE. _approve_report
        still requires the Approver role on the session AND on the recorded
        approver, still refuses anything but 'under_review', and still writes
        approver_id/approved_at/content_hash together. Because the approver is
        the session's user, its two role checks collapse onto one identity --
        that is a consequence of one person signing, not a relaxation: an
        actor without the Approver role is refused exactly as before.

        Not separately @auditable, for the same reason as
        record_disagreement_claim: _approve_report already writes the
        'report_approved' entry.
        """
        return self._approve_report(session, report_id, actor_id=session.user_id)

    @auditable(
        action="report_released",
        resource_type="report",
        resource_id_param="report_id",
        requires_session=True,
        auditable=False,
        audit_denials_only=True,
        details_builder=lambda params, result: {"report_id": str(params.get("report_id"))},
        reason=(
            "declared non-SUCCESS-auditing HERE because the release IS audited, once, by _release_report "
            "('report_released'), which this method does nothing but delegate to with actor_id fixed "
            "to the session's own user. A second success row would record one delivery twice. A refusal "
            "by this wrapper itself (already released) never reaches the delegate, so audit_denials_only "
            "is what puts it on the record."
        ),
    )
    def release_report(self, session: Session, report_id: uuid.UUID) -> uuid.UUID:
        """
        PUBLIC wrapper over _release_report, recording the SESSION'S OWN USER
        as the releaser. Stage 3 -- the first production path to a release.

        THE ONLY SUPPORTED DELIVERY PATH IS THE PATIENT PORTAL, so this method
        takes NO consumer: it releases the report to `PORTAL_RELEASE_CONSUMER`,
        and a caller cannot name anyone else. `release_events.consumer` is
        unchanged -- still free text, still NOT NULL, no schema or migration
        change -- and `_release_report` still takes any consumer for the future
        delivery paths (a LIMS, an external clinician) that the schema comment
        reserves it for. What was removed is the ability of an HTTP caller to
        type an arbitrary recipient into a permanent delivery record.

        A report already released to the portal is REFUSED (ValueError) rather
        than given a second identical row: a duplicate inflates the delivery
        count and states a delivery that did not happen. The read below is a
        courtesy that gives a sequential second call a clean refusal; it is
        racy, and MEASURED to be (eight simultaneous releases produced eight
        rows). The control is migration 0009's partial unique index
        `release_events_portal_once`, and the loser of a race is turned into
        the same ValueError. Nothing else is constrained: other consumers, and
        re-delivery to the same external consumer, remain ordinary.

        Exactly the shape of approve_report, for exactly its reason: a delivery
        path needs a release it can call without reaching into a private
        method and without being handed the ability to name somebody else as
        the releaser. `released_by` is read as an authority claim, and fixing
        it to the authenticated person is what keeps it true.

        ENFORCEMENT IS UNCHANGED AND NOT RE-IMPLEMENTED: _release_report still
        requires Approver on the session, re-checks it on the recorded
        releaser, refuses a blank consumer, and refuses any state but
        'approved' or 'released' (spec 13.4). Release is an event: a second
        consumer inserts a second release_events row.
        """
        self._require_role(session, "Approver")
        already = self._query_one(
            "SELECT 1 FROM release_events WHERE org_id = %s AND report_id = %s AND consumer = %s "
            "AND tombstoned_at IS NULL LIMIT 1",
            (session.org_id, report_id, PORTAL_RELEASE_CONSUMER),
        )
        if already is not None:
            raise ValueError("Report has already been released to the patient portal.")
        try:
            return self._release_report(session, report_id, PORTAL_RELEASE_CONSUMER, actor_id=session.user_id)
        except Exception as exc:  # noqa: BLE001 -- narrowed immediately below
            # The read above is a courtesy and is racy. Migration 0009's partial
            # unique index is the control: of N simultaneous releases exactly one
            # INSERT lands and the rest arrive here, as the same ordinary refusal
            # a sequential second release gets. The delegate has already rolled
            # back and recorded the failed attempt as an 'error' outcome.
            if getattr(exc, "sqlstate", None) == "23505" and (
                getattr(getattr(exc, "diag", None), "constraint_name", None) == "release_events_portal_once"
            ):
                raise ValueError("Report has already been released to the patient portal.") from exc
            raise

    @auditable(
        action="record_disagreement",
        resource_type="reviewer_claim",
        # Returns the new claim's UUID (from _insert_claim) and nothing else; the claim's
        # free-text reason is deliberately in no audit column.
        resource_id_from_result=True,
        requires_session=True,
        auditable=True,
        reason="reviewer disagrees with a variant's classification (spec 13.2)",
    )
    @transactional
    def _record_disagreement(
        self,
        session: Session,
        interpretation_id: Any,
        report_id: Any,
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
        self._validate_claim_inputs(session, interpretation_id, report_id, actor_id, reason)
        self._require_variant_scope(variant_key, "disagree")
        self._require_classification(new_classification, "disagree")
        return self._insert_claim(
            session,
            interpretation_id,
            report_id,
            actor_id,
            reason,
            claim_type="disagree",
            variant_key=variant_key,
            classification=new_classification,
        )

    @auditable(
        action="add_variant_by_reviewer",
        resource_type="reviewer_claim",
        # Returns the new claim's UUID (from _insert_claim) and nothing else; the claim's
        # free-text reason is deliberately in no audit column.
        resource_id_from_result=True,
        requires_session=True,
        auditable=True,
        reason="reviewer names a variant the pipeline did not surface (spec 13.2)",
    )
    @transactional
    def _add_variant_by_reviewer(
        self,
        session: Session,
        interpretation_id: Any,
        report_id: Any,
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
        self._validate_claim_inputs(session, interpretation_id, report_id, actor_id, reason)
        self._require_variant_scope(variant_key, "variant_added")
        self._require_classification(acmg_classification, "variant_added")
        return self._insert_claim(
            session,
            interpretation_id,
            report_id,
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
        report_id: Any,
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
        self._validate_claim_inputs(session, interpretation_id, report_id, actor_id, reason)
        self._require_variant_scope(variant_key, "variant_not_relevant")
        return self._insert_claim(
            session,
            interpretation_id,
            report_id,
            actor_id,
            reason,
            claim_type="variant_not_relevant",
            variant_key=variant_key,
        )

    @auditable(
        action="reviewer_claims_read",
        resource_type="reviewer_claim",
        requires_session=True,
        auditable=False,
        reason="read is not a resource action",
    )
    @transactional
    def get_reviewer_claims(self, session: Session, report_id: uuid.UUID) -> List[dict[str, Any]]:
        """
        This report's reviewer claims, oldest first -- ITS OWN AND ONLY ITS
        OWN (R9 defect (a), human ruling 2026-09-13, resolving expert-branch
        decision point 4 in reviewer_claims's schema comment).

        An amended report does NOT carry the cumulative set including the
        original's, and the reason is the reader rather than tidiness:
        showing the original's claims presents reasoning about a document
        the reader is not holding, and a cumulative set makes it impossible
        to tell which claim applied to which version. The original keeps its
        own claims, on its own report, and stays queryable -- nothing is
        withdrawn (ISO 15189 7.4.1.8 b)/d)); the two sets simply do not mix.

        Ordered by ("timestamp", id), the same total order
        _compute_report_content_hash uses, so what a surface displays and
        what was hashed enumerate the rows identically. WHICH of these
        claims a given surface chooses to show, and how, is expert-branch
        point 3 and still open: this method answers what the report's claims
        ARE, not how any renderer presents them.

        Raises NotFoundError for an unknown or cross-org report, the usual
        meaning throughout this file. A report with no claims returns [] --
        an empty review is a fact a caller may read, not an error.
        """
        report = self._query_one(
            "SELECT 1 FROM reports WHERE id = %s AND org_id = %s",
            (report_id, session.org_id),
        )
        if report is None:
            raise NotFoundError(f"Report {report_id} not found")

        rows = self._query(
            'SELECT id, claim_type, variant_key, classification, reason, actor_id, "timestamp", '
            "evidence_json, supersedes "
            "FROM reviewer_claims WHERE org_id = %s AND report_id = %s "
            'ORDER BY "timestamp", id',
            (session.org_id, report_id),
        )

        claims: List[dict[str, Any]] = []
        for row in rows:
            evidence_json = row[7]
            if isinstance(evidence_json, str):
                evidence_json = json.loads(evidence_json)
            claims.append(
                {
                    "id": row[0],
                    "claim_type": row[1],
                    "variant_key": row[2],
                    "classification": row[3],
                    "reason": row[4],
                    "actor_id": row[5],
                    "timestamp": row[6],
                    "evidence_json": evidence_json,
                    "supersedes": row[8],
                }
            )
        return claims

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
        resource_id_param="report_id",
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

        3. AT LEAST ONE REVIEWER CLAIM AGAINST THIS REPORT.
           This is the substantive one. Zero claims means no review happened,
           and submitting then would put raw engine output into the approval
           queue with a human's submission standing behind it.

           Checked against THIS REPORT, not against its interpretation (R9,
           human ruling 2026-09-13: a report carries its own claims and only
           its own). The difference is the whole amendment case. An amendment
           is a second report on the same interpretation, so an
           interpretation-scoped check would let an amendment be submitted on
           the strength of the ORIGINAL's review -- the original's reviewer
           would stand behind a document they never read, which is the
           failure this precondition exists to prevent, arriving by the one
           route the old check could not see.

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

        # FOR UPDATE, for the same reason as in _approve_report: N simultaneous submissions moved the report N times
        # (and wrote N audit rows); now the others wait, re-read 'under_review' and are refused.
        report = self._query_one(
            "SELECT state FROM reports WHERE id = %s AND org_id = %s FOR UPDATE",
            (report_id, session.org_id),
        )
        if report is None:
            raise NotFoundError(f"Report {report_id} not found")
        state = report[0]

        if state != "draft":
            raise ValueError(
                f"Report is in '{state}' state, cannot submit for review -- spec 13.1: only a draft may be submitted."
            )

        claim = self._query_one(
            "SELECT 1 FROM reviewer_claims WHERE org_id = %s AND report_id = %s LIMIT 1",
            (session.org_id, report_id),
        )
        if claim is None:
            raise ValueError(
                f"Report {report_id} has no reviewer claim of its own and "
                "cannot be submitted for review: submitting with zero claims would present "
                "unreviewed engine output as reviewed. Record at least one claim first "
                "(spec 13.2) -- an explicit 'accept' counts, and is the point of it."
            )

        self._execute(
            "UPDATE reports SET state = 'under_review' WHERE org_id = %s AND id = %s",
            (session.org_id, report_id),
        )

    def _compute_report_content_hash(self, session: Session, report_id: uuid.UUID) -> str:
        """
        Deterministic SHA-256 over exactly what an Approver reviewed: the
        interpretation's run_document AND the full set of reviewer_claims
        recorded against THIS REPORT, AS STORED.

        KEYED ON THE REPORT, NOT THE INTERPRETATION (R9 defect (b), human
        ruling 2026-09-13). It took interpretation_id and hashed every claim
        on the interpretation. An amendment is a second report row on the
        same interpretation, so the first claim recorded for an amendment
        landed inside the ORIGINAL report's recomputed hash and
        verify_report_integrity() reported the original as tampered with.
        Nothing had been tampered with, and a false alarm on an integrity
        check teaches people to ignore integrity checks.

        The rejected alternative was a cutoff at reports.approved_at, which
        needed no schema change. It was refused because it trades a false
        alarm for a real blind spot: a claim injected after approval is
        exactly what this hash exists to catch, and a timestamp cutoff
        cannot tell that injection apart from an amendment's first
        legitimate claim. See reviewer_claims.report_id's schema comment.

        run_document is still read through the report, so an amendment and
        its original -- which share an interpretation -- hash the same
        document and differ only where they genuinely differ: their claims.

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

        TIMESTAMPS ARE HASHED IN UTC (`_claim_timestamp_utc`), so the hash does not depend on the PostgreSQL session
        time zone of the connection that computes it. For a UTC session the text is identical to what the earlier
        `isoformat()` produced, which is what keeps every UTC-computed hash verifiable. See
        `_classify_report_integrity` for hashes made under another zone.

        Claims are ordered by (timestamp, id) for a stable serialisation --
        without a fixed order, two computations over the identical set of
        rows could serialise to different JSON and hash to different values,
        which would make this function non-deterministic over data that
        never changed. id is the reviewer_claims primary key, so (timestamp,
        id) is a genuine total order: id alone can never tie.
        """
        run_document, claim_rows = self._load_report_hash_inputs(session, report_id)
        return self._report_hash_digest(run_document, claim_rows, _claim_timestamp_utc)

    def _load_report_hash_inputs(self, session: Session, report_id: uuid.UUID) -> tuple[Any, list]:
        """The CURRENT run_document and CURRENT reviewer_claims rows that the approval hash covers (read-only)."""
        interp_row = self._query_one(
            "SELECT i.run_document FROM interpretations i "
            "JOIN reports r ON r.org_id = i.org_id AND r.interpretation_id = i.id "
            "WHERE r.org_id = %s AND r.id = %s",
            (session.org_id, report_id),
        )
        run_document = interp_row[0]
        if isinstance(run_document, str):
            run_document = json.loads(run_document)

        claim_rows = self._query(
            'SELECT claim_type, variant_key, classification, reason, actor_id, "timestamp", '
            "evidence_json, supersedes "
            "FROM reviewer_claims WHERE org_id = %s AND report_id = %s "
            'ORDER BY "timestamp", id',
            (session.org_id, report_id),
        )
        return run_document, claim_rows

    @staticmethod
    def _report_hash_digest(run_document: Any, claim_rows: Iterable[Any], render_timestamp: Any) -> str:
        """
        SHA-256 over the canonical JSON of the run document and the claims. `render_timestamp` turns a claim's
        `timestamptz` into the text that enters the hash. The CANONICAL renderer is `_claim_timestamp_utc`; the
        alternatives exist only so `_classify_report_integrity` can recognise hashes made by the previous algorithm.
        """
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
                    "timestamp": render_timestamp(row[5]),
                    "evidence_json": evidence_json,
                    "supersedes": str(row[7]) if row[7] is not None else None,
                }
            )

        payload = {"run_document": run_document, "reviewer_claims": claims}
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def _classify_report_integrity(self, session: Session, report_id: uuid.UUID) -> tuple[str, Optional[str]]:
        """
        Recompute the stored approval hash and say HOW it relates to the current data. Read-only; never writes.

        Returns (outcome, zone):
          ("match", None)                   the canonical (UTC) recomputation equals the stored hash;
          ("match_legacy_zone", "<zone>")   it does not, but the PREVIOUS algorithm (timestamps printed in the zone of
                                            the computing session) run in this IANA zone does. The stored hash was made
                                            by an approval whose session was not UTC, and the content is unchanged;
          ("no_match", None)                no candidate reproduces it. This is NOT proof of tampering: it is exactly
                                            what both tampering and an unrecognised legacy form look like, so callers
                                            must report it as "does not verify" and investigate, never as "tampered".

        The zone search runs only after the canonical form has failed. Matching a SHA-256 under some zone while the
        content had changed is not feasible, so a legacy match is real evidence that the approved content is intact.
        Raises NotFoundError / ValueError exactly as `verify_report_integrity` always has.
        """
        report = self._query_one(
            "SELECT content_hash FROM reports WHERE id = %s AND org_id = %s",
            (report_id, session.org_id),
        )
        if report is None:
            raise NotFoundError(f"Report {report_id} not found")
        stored_hash = report[0]

        if stored_hash is None:
            raise ValueError(f"Report {report_id} has not been approved and has no content_hash to verify against.")

        run_document, claim_rows = self._load_report_hash_inputs(session, report_id)
        if self._report_hash_digest(run_document, claim_rows, _claim_timestamp_utc) == stored_hash:
            return "match", None
        zone = self._legacy_zone_reproducing(run_document, claim_rows, stored_hash)
        if zone is not None:
            return "match_legacy_zone", zone
        return "no_match", None

    def _legacy_zone_reproducing(self, run_document: Any, claim_rows: list, stored_hash: str) -> Optional[str]:
        """The first IANA zone (alphabetical, so deterministic) in which the PREVIOUS algorithm reproduces `stored_hash`,
        or None. Zones that render every claim timestamp identically are tried once."""
        try:
            import zoneinfo

            names = sorted(zoneinfo.available_timezones())
        except Exception:  # no tz database available: no legacy recognition, which fails closed ("no_match")
            return None
        tried: set = set()
        for name in names:
            try:
                zone = zoneinfo.ZoneInfo(name)
            except Exception:
                continue
            rendering = tuple(row[5].astimezone(zone).isoformat() for row in claim_rows)
            if rendering in tried:
                continue
            tried.add(rendering)
            if (
                self._report_hash_digest(
                    run_document, claim_rows, lambda ts, zone=zone: ts.astimezone(zone).isoformat()
                )
                == stored_hash
            ):
                return name
        return None

    @auditable(
        action="report_approved",
        resource_type="report",
        requires_session=True,
        auditable=True,
        resource_id_param="report_id",
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

        # FOR UPDATE: the row is locked from this read until the transaction commits, so a second approval of the
        # same report WAITS here, then re-reads the committed row, finds 'approved', and is refused with the
        # domain's own ValueError below. Without the lock both requests read 'under_review' and both went on to
        # write; the immutability trigger refused the loser, but as a raw database error. The lock also holds
        # claim recorders off (they take FOR SHARE on this row, see _validate_claim_inputs), so the claims the
        # hash below covers are exactly the claims that exist when the seal commits.
        report = self._query_one(
            "SELECT interpretation_id, state FROM reports WHERE id = %s AND org_id = %s FOR UPDATE",
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

        content_hash = self._compute_report_content_hash(session, report_id)

        now = self._clock.now()
        self._execute(
            "UPDATE reports SET state = %s, approver_id = %s, approved_at = %s, content_hash = %s "
            "WHERE id = %s AND org_id = %s",
            ("approved", actor_id, now, content_hash, report_id, session.org_id),
        )

    @auditable(
        action="report_integrity_verified",
        resource_type="report",
        resource_id_param="report_id",
        requires_session=True,
        auditable=True,
        reason="ISO 15189 7.5 nonconformance detection: a mismatch is audit-relevant on its own, not only on export",
        details_builder=_integrity_audit_details,
    )
    @transactional
    def verify_report_integrity(self, session: Session, report_id: uuid.UUID) -> bool:
        """
        Recompute the report's content hash from CURRENT run_document and
        CURRENT reviewer_claims, using _compute_report_content_hash -- the
        same function _approve_report used to produce the stored value --
        and compare.

        Returns True when the stored hash is reproduced, and False when it is not. "Reproduced" means the canonical
        UTC form, or the previous algorithm run in some IANA zone (a hash from an approval made in a non-UTC session;
        see `_classify_report_integrity`, which says which). False means "does not verify", which is what tampering and
        an unrecognised legacy form both look like: do not report it as tampering without investigating.
        The audit row records which it was (`outcome`: match, match_legacy_zone or no_match, plus `legacy_zone`);
        the return value stays a bool so existing callers are unaffected.
        Deliberately a bool,
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
        _INTEGRITY_AUDIT.set(None)  # never inherit a previous call's detail
        outcome, zone = self._classify_report_integrity(session, report_id)
        _INTEGRITY_AUDIT.set((outcome, zone))  # read by `_integrity_audit_details` for this call's audit row
        return outcome in ("match", "match_legacy_zone")

    @auditable(
        action="report_released",
        resource_type="report",
        resource_id_from_result=True,
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

        AUTHORITY (w129 phase B). Release is gated exactly the way approval is
        gated, twice over and for the same reason:

          - `_require_role(session, 'Approver')` gates the SESSION.
          - the identity recorded in `release_events.released_by` -- actor_id
            -- is separately checked to hold Approver itself.

        Before w129 this method checked only that actor_id named a user IN
        THIS ORGANISATION: membership, not authority. That is the identity
        half of the `_approve_report` pattern with the authority half missing,
        and it meant a session holding only Auditor -- a read-only role by
        intent -- could hand an approved report to an arbitrary named consumer
        and record any colleague as having released it. `released_by` is read
        as an authority claim the same way `reports.approver_id` is, so it
        gets the same check; see `_approve_report`'s docstring for the full
        argument, which this method now shares rather than restates.

        Approver, not a new Releaser role: the ruling of 2026-09-14 gives
        Approver "review/approval", release acts only on an already-approved
        report, and inventing an eighth role for one operation would be the
        broad RBAC redesign the same ruling forbids.
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
                f"User {actor_id} does not hold the Approver role and cannot be recorded as releasing this report."
            )

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

        # Preserve a readable retraction warning before recording a delivery.
        retraction = self._read_retraction_row(session, report_id)
        if retraction is not None and not (retraction[3] or "").strip():
            raise BrokenRevisionRecordError(
                f"Report {report_id} has a retraction with no usable reason; delivery is refused."
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

        # w122, human rulings E3 + E6. E3 permits a retracted report to be
        # delivered; E6 says every party that received it is owed a
        # notification. This consumer is receiving it NOW, after the
        # retraction was recorded, so the debt for them arises now -- and if
        # it were not written here, "which retractions have unnotified
        # recipients" would silently miss exactly the people who received the
        # report while it was already retracted. Same deduplication as
        # retract_report's own pass: one debt per consumer, not per delivery.
        if retraction is not None:
            self._record_retraction_notification_debts(session, retraction[0], [consumer], retraction[3], now)

        return release_id

    @auditable(
        action="require_release",
        resource_type="report",
        resource_id_param="report_id",
        requires_session=True,
        auditable=True,
        reason="the release gate is the structural enforcement spec 13.4 depends on; its denials are audit-relevant",
        details_builder=lambda params, result: {"report_id": str(params.get("report_id"))},
    )
    @transactional
    def require_release(self, session: Session, report_id: uuid.UUID) -> bool:
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

        AUTHORITY (w129 phase B). This gate is now an authority gate as well
        as a state gate: the session must hold Approver. Before w129 it
        answered only "is this report approved" and had no opinion about who
        was asking, so the one gate every delivery path calls could be
        satisfied by any authenticated session in the organisation.

        The state refusal is NOT replaced by the role refusal. An Approver
        asking about a draft report still gets the spec 13.4 ValueError; the
        two conditions are independent and both are tested.

        NOTE for whoever wires the first delivery path -- there is none today
        (`require_release` has zero non-test callers at 36fe487, counted). A
        delivery path driven by a principal that is not an Approver will fail
        here, by design: the authority to hand a report to a consumer and the
        authority to release it are the same authority, and splitting them
        would be the second mechanism this wave is forbidden to build.
        """
        self._require_role(session, "Approver")

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

        # A retracted report remains deliverable, but its warning must travel
        # with it. An unreadable reason means the document cannot be released.
        retraction = self._read_retraction_row(session, report_id)
        if retraction is not None and not (retraction[3] or "").strip():
            raise BrokenRevisionRecordError(
                f"Report {report_id} has a retraction with no usable reason; delivery is refused."
            )
        return retraction is not None

    # ─── w156 (WAVE 6): report retraction ───────────────────────────────────
    #
    # WRITTEN FRESH, NOT LIFTED, AND THE DISTINCTION IS THE WAVE'S OWN
    # QUESTION. Migration `0003` lifted DDL from the held branch
    # `w122-retraction` (a4907d3) MECHANICALLY, under an approved schema
    # decision, and its header says so at length. No decision approved
    # importing that branch's APPLICATION code, and application code is more
    # decision-laden than DDL: a4907d3 predates the w129 phase-B authority
    # rework, the `REPORT_NOT_FOUND` constant, the patient `is_retracted`
    # projection this tip already carries, and the whole administrative
    # surface. The held branch was READ (`git show`, never merged, never
    # modified) and is cited below where it CORROBORATES a ruling. Where the
    # shape below matches it, that is because the schema and the recorded
    # rulings force the shape, not because bytes were copied -- and it is
    # said here rather than left for a reader to wonder about.
    #
    # WHAT THE SCHEMA ALREADY DECIDED, so that nothing below is a fresh
    # invention. Every one of these is in THIS tree, in
    # `clinical/migrations/0003_report_retraction.sql` and `clinical/schema.sql`:
    #
    #   E1  `retracted_from_state TEXT NOT NULL CHECK (... IN ('approved',
    #       'released'))` -- which states may be retracted, AND that the row
    #       records which. The precondition below is the CHECK, moved earlier
    #       so the caller gets a sentence instead of an integrity error.
    #   E4  `replacement_report_id UUID` (nullable) plus
    #       `retraction_replacement_is_not_itself` -- a replacement is
    #       optional and may not be the report itself.
    #   E5  "retraction is NOT reversible in this wave and no reversal is
    #       built" -- so there is no `withdraw_retraction` here, and a second
    #       retraction of the same report is refused rather than appended.
    #   E6  `retraction_notifications`, `status CHECK (status =
    #       'notification_owed')`, `UNIQUE (org_id, retraction_id, consumer)`
    #       -- one DEBT per distinct consumer, never a transmission, and it
    #       does not block the retraction.
    #   E7/E3 (`data_access.py` above, lines ~3020-3350) -- a retraction
    #       FLAGS and never FILTERS; the patient surfaces return the single
    #       boolean `is_retracted` and nothing else.
    #   E8  cascade-tombstone with the report; no retention change. Nothing
    #       below touches the retention clock.
    #
    # THE ONE QUESTION THE TREE DOES NOT RECORD, AND HOW IT IS ANSWERED
    # WITHOUT INVENTING A WORKFLOW ENGINE. Which role may retract. This tree
    # names rulings E1 and E3-E8 but not E2. It is NOT left open, and it is
    # not guessed either: the DECISION RULE in force is "the smallest
    # architecture-consistent choice; preserve existing patterns". Every
    # existing verb that acts on an APPROVED report in this file is gated on
    # Approver and on nothing else -- `_approve_report`, `_release_report`,
    # `require_release` and `create_amendment`, all four measured by SYNTAX
    # TREE rather than by eye, and no line numbers are quoted here because a
    # line number in a comment is a claim that rots on the next insertion. A
    # retraction acts on an approved-or-released report, so it takes the same
    # gate. Inventing an eighth role for one operation is what
    # `_release_report`'s own docstring already refused to do, citing the
    # ruling of 2026-09-14. The held branch records this as human ruling E2 --
    # "any Approver in the org may retract; no new role" -- which is
    # CORROBORATION that the human already ruled it, not the authority for it.
    # If the human wants a different gate, this is the one line to change.

    def _read_retraction_row(self, session: Session, report_id: uuid.UUID) -> Optional[tuple]:
        """
        The live retraction of one report in this organisation, or None.

        NOT `@auditable` AND NOT `@transactional`, and both absences are
        load-bearing rather than tidiness. `@auditable` COMMITS after each
        audit row it writes (the standing hazard), so a decorated read called
        from inside `retract_report` would commit mid-operation and split one
        atomic act into two. A plain private helper composes safely; a
        decorated one does not.

        Org-bound from the session, like every other read here. There is no
        `org_id` parameter for a caller to supply.
        """
        return self._query_one(
            "SELECT id, retracted_at, retracted_by, reason, retracted_from_state "
            "FROM report_retractions WHERE org_id = %s AND report_id = %s "
            "ORDER BY retracted_at LIMIT 1",
            (session.org_id, report_id),
        )

    def _record_retraction_notification_debts(
        self,
        session: Session,
        retraction_id: uuid.UUID,
        consumers: List[str],
        reason: str,
        now: "_datetime.datetime",
    ) -> None:
        """
        One `retraction_notifications` row per consumer that already received
        this report (human ruling E6).

        A ROW HERE ASSERTS A DEBT, NEVER A TRANSMISSION. There is no delivery
        transport anywhere in this platform, so `status` has exactly one
        legal value, `'notification_owed'`, and the schema's CHECK says so.
        This method deliberately does NOT borrow `create_amendment`'s
        "Recorded as sent" phrasing, which asserts something the sentence
        before it says did not happen.

        `ON CONFLICT DO NOTHING` on the schema's own
        `uk_retraction_notification_consumer`: re-delivery to the same
        consumer is ONE debt, not two, and saying that here as well as in the
        constraint keeps a second delivery path from raising instead of
        being absorbed.

        Plain and undecorated for the same atomicity reason as
        `_read_retraction_row` above.
        """
        for consumer in consumers:
            self._execute(
                "INSERT INTO retraction_notifications "
                "(org_id, id, retraction_id, consumer, reason_for_retraction, status, created_at, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (org_id, retraction_id, consumer) DO NOTHING",
                (
                    session.org_id,
                    uuid.uuid4(),
                    retraction_id,
                    consumer,
                    reason,
                    "notification_owed",
                    now,
                    session.user_id,
                ),
            )

    @auditable(
        action="report_retracted",
        resource_type="report",
        requires_session=True,
        auditable=True,
        resource_id_param="report_id",
        reason=(
            "retraction is a deliberate safety act on a report that was validly approved; the record must name which"
        ),
        details_builder=lambda params, result: {
            "report_id": str(params.get("report_id")),
            "retraction_id": str(result) if result else None,
            "replacement_report_id": (
                str(params.get("replacement_report_id")) if params.get("replacement_report_id") else None
            ),
        },
    )
    @transactional
    def retract_report(
        self,
        session: Session,
        report_id: uuid.UUID,
        reason: str,
        replacement_report_id: Optional[uuid.UUID] = None,
    ) -> uuid.UUID:
        """
        Record that a report must NOT be acted on. Writes ONE row in
        `report_retractions`, plus one notification debt per prior recipient
        (E6), AND NOT ONE UPDATE ANYWHERE.

        THE APPROVED `reports` ROW IS NOT TOUCHED, AND THAT IS THE DESIGN,
        not an omission. A report approved on 1 March and retracted on 5
        April WAS approved: `state`, `approver_id`, `approved_at` and
        `content_hash` still say exactly what they said,
        `signing_identity_for_report` still retrieves the approver (ISO 15189
        7.4.1.5 c)) after retraction as well as before, and
        `verify_report_integrity` still returns True -- because retraction
        says "do not act on this", not "this is not what was approved". A
        sixth `reports.state` value would have made the record deny an
        approval that happened, and the database forbids it anyway
        (`enforce_content_immutability`).

        PRECONDITIONS, each one traceable to something already in this tree:
          - THE ROLE GATE IS THE FIRST STATEMENT. Approver, per the note
            above this method. It runs before the report is looked up, so an
            unauthorised caller's refusal cannot vary with whether the
            identifier names anything -- the same ordering rule the
            administrative surface's `_target` was rewritten to preserve.
          - E1: the report is 'approved' or 'released', and the row records
            WHICH in `retracted_from_state`. A draft, under_review or
            returned report has no standing to withdraw.
          - `reason` is required and non-empty (the schema says NOT NULL; an
            empty string would satisfy the column and say nothing).
          - E5: a report already retracted is REFUSED rather than retracted
            twice. Not a reversal question -- E5 rules retraction
            irreversible and none is built here -- but a reader question:
            two retraction rows for one report cannot both be stated, so a
            reader would have to pick, and picking is guessing.
          - E4: `replacement_report_id` is OPTIONAL. A report issued against
            the wrong patient's sample has nothing to replace it with. When
            supplied it must name a report IN THIS ORGANISATION, resolved
            through the session's own `org_id`; a cross-organisation
            replacement is refused here AND is structurally impossible,
            because `fk_retraction_replacement` is composite.

        WHAT THE PATIENT SEES, WHICH IS WHY THIS METHOD CHANGES NO READER.
        The patient surfaces already project `is_retracted` as a scalar
        EXISTS sub-select bound on both `org_id` and the patient's own
        report (w148, human decisions 1 and 2). This method makes those rows
        real; it does not touch them. The reason, the retractor, the
        timestamp, the from-state and the replacement identifier are all
        written here and NONE of them reaches a patient surface.

        THE AUDIT ROW NAMES THE REPORT (`resource_id_param="report_id"`) and
        carries the retraction id in `details`. An audit entry about a report
        that named a retraction id as its resource would be the w120 defect
        in a new costume.

        Returns the retraction id.
        """
        self._require_role(session, "Approver")

        report = self._query_one(
            "SELECT state FROM reports WHERE id = %s AND org_id = %s",
            (report_id, session.org_id),
        )
        if report is None:
            raise NotFoundError(f"Report {report_id} not found")
        state = report[0]

        if state not in ("approved", "released"):
            raise ValueError(
                f"Report is in '{state}' state, cannot retract -- human ruling E1: only an approved or "
                "released report has standing to withdraw."
            )

        if not reason or not reason.strip():
            raise ValueError("reason for retraction is required and cannot be empty")

        if self._read_retraction_row(session, report_id) is not None:
            raise ValueError(
                f"Report {report_id} is already retracted; a retraction is append-only and is not "
                "reversible (human ruling E5)."
            )

        if replacement_report_id is not None:
            if replacement_report_id == report_id:
                raise ValueError("A report cannot be its own replacement.")
            replacement = self._query_one(
                "SELECT 1 FROM reports WHERE id = %s AND org_id = %s",
                (replacement_report_id, session.org_id),
            )
            if replacement is None:
                raise NotFoundError(f"Replacement report {replacement_report_id} not found")

        now = self._clock.now()
        retraction_id = uuid.uuid4()
        self._execute(
            "INSERT INTO report_retractions (org_id, id, report_id, retracted_at, retracted_by, reason, "
            "retracted_from_state, replacement_report_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session.org_id,
                retraction_id,
                report_id,
                now,
                session.user_id,
                reason,
                state,
                replacement_report_id,
            ),
        )

        # E6. Every party `release_events` records as having received this
        # report is owed a notification, in the SAME transaction, and the
        # retraction is recorded whether or not there are any. A lab that has
        # discovered a dangerous report must never be prevented from
        # recording that fact because a debt could not be written.
        consumers = [
            row[0]
            for row in self._query(
                "SELECT DISTINCT consumer FROM release_events WHERE org_id = %s AND report_id = %s ORDER BY consumer",
                (session.org_id, report_id),
            )
        ]
        self._record_retraction_notification_debts(session, retraction_id, consumers, reason, now)

        return retraction_id

    # ─── Phase 7 commit 2: Amendments (ISO 15189 7.4.1.8) ──────────────────

    @auditable(
        action="amendment_created",
        resource_type="report",
        # Returns (amendment_report_id, notification_id). The audit subject is the new
        # amendment REPORT; True would record the tuple, notification id included.
        resource_id_from_result=lambda result: result[0] if result else None,
        requires_session=True,
        auditable=True,
        reason="Amendment creation (GEPER rule implementing ISO 15189 7.4.1.8 b)/d)): original never modified, traceability recorded",
    )
    @transactional
    def create_amendment(
        self, session: Session, original_report_id: uuid.UUID, reason: str
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """
        Create an amendment: a new report referencing an existing approved
        report, with the reason recorded and traceability to what it
        replaces maintained by a forward pointer, not a mutated column.

        Spec 15.2 requires (GEPER's own rule -- see GEPER_CLINICAL_PLATFORM_SPEC.md
        #15.2; implements ISO 15189 7.4.1.8 b)/d), the wording below is ours, not
        the standard's):
          - The original report is never modified, never withdrawn from the record
          - Traceability to the original is maintained
          - Ordering clinician is notified

        This method writes three rows and nothing else: a new `reports` row
        (the amendment itself, draft state), one `amendments` row, and one
        `amendment_notifications` row. No UPDATE anywhere -- amendments and
        amendment_notifications are append-only against clinical_app (see
        schema.sql's grants), and this method does not attempt to be the
        exception. "Current amendment" is derived by query (the row named
        in `supersedes_amendment_id` by no other row), not stored as a
        boolean column that a later write would have to keep in sync.

        Delivery mechanism for the notification (email, SMS, etc.) is
        deferred to a later phase; this records the event itself, not the
        transport. Recorded as sent (read=FALSE: may not be read yet, but
        the fact of the notification is recorded).

        Preconditions:
          - Session holds Approver (w129 phase B; see below)
          - Original report must exist in this org
          - Original report must be approved or released (not in draft/under_review/returned)
          - Reason for amendment is NOT NULL

        A second (or later) amendment of the same original chains off the
        current one: this method looks up whatever amendment of
        `original_report_id` nothing yet supersedes and records the new
        row's `supersedes_amendment_id` as that id, exactly the way
        reviewer_claims.supersedes chains. NULL means this is the first
        amendment of this original.

        AUTHORITY (w129 phase B). The session must hold Approver. An
        amendment is the one sanctioned corrective act on a record that has
        ALREADY BEEN SIGNED -- this method refuses any original that is not
        approved or released -- so its authority is the authority that signed
        it. Before w129 it checked neither the session's roles nor anything
        about the actor at all, so any authenticated session in the
        organisation could open an amendment against a colleague's approved
        report and notify the ordering clinician of it.

        Only the session is checked, not a second recorded actor: unlike
        `_approve_report` and `_release_report`, this method takes no
        actor_id. `reports.created_by` on the amendment is written from
        session.user_id, which the session gate has already qualified. The
        amendment it creates is a DRAFT, and must still pass
        `submit_for_review` (Interpreter) and `_approve_report` (Approver on
        both session and recorded actor) before it can itself be released, so
        the signing authority on the amended content is checked there, where
        the signature is actually recorded.

        Returns: tuple of (amendment_report_id, notification_id)
        """
        self._require_role(session, "Approver")

        # retention_days: INHERITED from the original report (which itself
        # inherited it from its interpretation, which inherited it from its
        # vcf) -- human ruling D2 (2026-09-09). An amendment IS a new report
        # row for the same lineage, so it carries the same already-resolved
        # value, never recomputed.
        original = self._query_one(
            "SELECT state, interpretation_id, retention_days FROM reports WHERE id = %s AND org_id = %s",
            (original_report_id, session.org_id),
        )
        if original is None:
            raise NotFoundError(f"Original report {original_report_id} not found")

        state, interpretation_id, retention_days = original

        if state not in ("approved", "released"):
            raise ValueError(
                f"Original report is in '{state}' state; only approved or released reports may be amended."
            )

        if not reason or not reason.strip():
            raise ValueError("reason for amendment is required and cannot be empty")

        # Find the current (non-superseded) amendment using NOT EXISTS instead of NOT IN
        # to reduce lock contention under concurrent amendments.
        current_amendment = self._query_one(
            "SELECT a.id FROM amendments a "
            "WHERE a.org_id = %s AND a.original_report_id = %s "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM amendments b "
            "  WHERE b.org_id = %s AND b.supersedes_amendment_id = a.id"
            ")",
            (session.org_id, original_report_id, session.org_id),
        )
        supersedes_amendment_id = current_amendment[0] if current_amendment else None

        now = self._clock.now()

        # The amendment IS a new report: its own state machine entry, draft
        # to start, linked to the same interpretation as the original.
        amendment_report_id = uuid.uuid4()
        self._execute(
            "INSERT INTO reports (org_id, id, interpretation_id, created_at, created_by, retention_days) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (session.org_id, amendment_report_id, interpretation_id, now, session.user_id, retention_days),
        )

        amendment_id = uuid.uuid4()
        self._execute(
            "INSERT INTO amendments (org_id, id, original_report_id, amendment_report_id, reason, "
            "supersedes_amendment_id, created_at, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                session.org_id,
                amendment_id,
                original_report_id,
                amendment_report_id,
                reason,
                supersedes_amendment_id,
                now,
                session.user_id,
            ),
        )

        notification_id = uuid.uuid4()
        self._execute(
            "INSERT INTO amendment_notifications (org_id, id, amendment_report_id, reason_for_change, "
            "delivered_to_role, created_at, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                session.org_id,
                notification_id,
                amendment_report_id,
                reason,
                "ordering_clinician",
                now,
                session.user_id,
            ),
        )

        return amendment_report_id, notification_id

    # ─── w118: the reader that makes the revision banners reachable ─────────
    #
    # The banner WORDING shipped 2026-09-11 and the rendering merged with it
    # (geper d28da0a), but NOTHING READ THESE TABLES INTO A DOCUMENT: an
    # amended report, the stale original it replaced, and a re-analysis
    # reissue all rendered byte-for-byte like a first-ever report, because
    # `document["report_revision"]` was only ever filled in by hand in tests.
    # This method is that missing read. It returns FACTS ONLY, in the shape
    # `geper/report/clinical_report_builder.py::normalize_report_revision`
    # consumes -- no banner text is produced here and this module imports
    # nothing from the renderer, so the signed-off wording keeps living in
    # exactly one place.

    def _issued_moment(
        self, org_id: uuid.UUID, report_id: uuid.UUID, approved_at: Any
    ) -> tuple[Optional[Any], Optional[str]]:
        """
        HUMAN RULINGS R2 and R4: when a report was ISSUED, and on what basis.

        Returns (timestamp, 'released') using the EARLIEST release event --
        "originally issued on" is the day the report first went out, not the
        day a second consumer asked for a copy. Falls back to (approved_at,
        'approved') for a report approved but never released, and (None, None)
        for one that is neither.

        The basis travels WITH the timestamp, as one value, because the whole
        point of R2 is that the reader must never be handed the second kind of
        date under the first kind of sentence.
        """
        row = self._query_one(
            "SELECT MIN(released_at) FROM release_events WHERE org_id = %s AND report_id = %s",
            (org_id, report_id),
        )
        released_at = row[0] if row else None
        if released_at is not None:
            return released_at, "released"
        if approved_at is not None:
            return approved_at, "approved"
        return None, None

    def _signing_identity_text(self, session: Session, user_id: uuid.UUID) -> str:
        """R1/R10's three fields, as the one string a banner prints for a person."""
        identity = self.get_signing_identity(session, user_id)
        return f"{identity['full_name']} ({identity['registration_number']}), {identity['hospital']}"

    @auditable(
        action="report_revision_read",
        resource_type="report",
        requires_session=True,
        auditable=False,
        reason="read is not a resource action; the amendment and re-analysis it reads are already audited",
    )
    @transactional
    def get_report_revision(self, session: Session, report_id: uuid.UUID) -> dict[str, Any]:
        """
        Every revision fact this report's document must state, read from the
        clinical database: does it amend something, has it been superseded,
        is it a re-analysis, has it been re-analysed since.

        Returns the raw block for `normalize_report_revision` -- `{}` when the
        report is in none of the four states, which renders no banner at all.
        Never `None`: this method HAS looked, so "nothing to say" is a
        positive answer, not the unknown that `None` means to the renderer.

        Raises NotFoundError for an unknown or cross-org report, and
        BrokenRevisionRecordError (R7) when a record exists but cannot be read
        into a true sentence.
        """
        report = self._query_one(
            "SELECT interpretation_id, created_at, approved_at FROM reports WHERE org_id = %s AND id = %s",
            (session.org_id, report_id),
        )
        if report is None:
            raise NotFoundError(f"Report {report_id} not found")
        interpretation_id, report_created_at, report_approved_at = report

        revision: dict[str, Any] = {}
        # w122: FIRST, because the banner it feeds renders first -- "do not
        # act on this document" outranks every other statement, and retraction
        # outranks supersession because a superseded report has a successor to
        # obtain and a retracted one may not.
        retracted = self._read_retracted(session, report_id, report_approved_at)
        if retracted:
            revision["retracted"] = retracted
        amends = self._read_amends(session, report_id)
        if amends:
            revision["amends"] = amends
        superseded_by = self._read_superseded_by(session, report_id)
        if superseded_by:
            revision["superseded_by"] = superseded_by
        reanalysis_of = self._read_reanalysis_of(session, interpretation_id)
        if reanalysis_of:
            revision["reanalysis_of"] = reanalysis_of
        reanalysed_since = self._read_reanalysed_since(
            session, report_id, interpretation_id, report_created_at, report_approved_at
        )
        if reanalysed_since:
            revision["reanalysed_since"] = reanalysed_since
        return revision

    def _read_retracted(
        self, session: Session, report_id: uuid.UUID, report_approved_at: Any
    ) -> Optional[dict[str, Any]]:
        """
        Banner 0 (w122). THIS REPORT HAS BEEN RETRACTED.

        THE DATE BASIS COMES FROM THE STORED FACT, NOT FROM A LOOKUP, and that
        is human ruling E1's whole point. `_issued_moment` derives the basis
        from whatever release_events says AT READ TIME -- correct for the
        amendment banners, wrong here: a report retracted while 'approved' can
        legitimately be released afterwards (E3 permits it), and a reader that
        re-derived the basis would then print "issued on <the release date>"
        over a retraction recorded before any release existed. The retraction
        row records WHICH state it was retracted from, so the sentence keeps
        saying what was true when the lab acted. That is R2's rule -- the basis
        travels with the timestamp, never inferred from which column happens to
        be populated -- applied to the one place where the column would have
        been populated LATER.

        R7 fail-closed, in the shape `_read_amends` uses: a retraction that
        cannot be read into a true sentence is an error, never a marker and
        never a silent omission.
        """
        rows = self._query(
            "SELECT id, retracted_at, retracted_by, reason, retracted_from_state, replacement_report_id "
            "FROM report_retractions WHERE org_id = %s AND report_id = %s "
            "ORDER BY retracted_at, id",
            (session.org_id, report_id),
        )
        if not rows:
            return None
        if len(rows) > 1:
            # retract_report refuses a second retraction for exactly this
            # reason: the banner would have to pick one, and picking is
            # guessing. Asserted here as well because the reader must not
            # depend on the writer having been the only way in.
            raise BrokenRevisionRecordError(
                f"Report {report_id} carries {len(rows)} retraction records; it cannot state which retraction applies."
            )
        _id, retracted_at, retracted_by, reason, retracted_from_state, replacement_report_id = rows[0]

        if not (reason or "").strip():
            raise BrokenRevisionRecordError(
                f"The retraction record for report {report_id} carries no reason; refusing to state the "
                "retraction without it."
            )

        if retracted_from_state == "released":
            row = self._query_one(
                "SELECT MIN(released_at) FROM release_events WHERE org_id = %s AND report_id = %s",
                (session.org_id, report_id),
            )
            issued_at = row[0] if row else None
            issued_basis = "released"
        elif retracted_from_state == "approved":
            issued_at = report_approved_at
            issued_basis = "approved"
        else:
            # The CHECK constraint admits two values. A third is a record this
            # reader has never heard of, not a third meaning to guess at.
            raise BrokenRevisionRecordError(
                f"The retraction record for report {report_id} names state {retracted_from_state!r}, "
                "which is neither 'approved' nor 'released'."
            )
        if issued_at is None:
            raise BrokenRevisionRecordError(
                f"Report {report_id} was retracted from '{retracted_from_state}' but carries no "
                f"{'release' if retracted_from_state == 'released' else 'approval'}; there is no date "
                "on which it was issued."
            )

        block: dict[str, Any] = {
            "issued_at": issued_at,
            "issued_basis": issued_basis,
            "retracted_at": retracted_at,
            # R1/R10: the person, by the three fields a report may print.
            "retracted_by": self._signing_identity_text(session, retracted_by),
            "reason": reason,
        }
        if replacement_report_id is not None:
            replacement = self._query_one(
                "SELECT tombstoned_at FROM reports WHERE org_id = %s AND id = %s",
                (session.org_id, replacement_report_id),
            )
            if replacement is None:
                raise BrokenRevisionRecordError(
                    f"The retraction record for report {report_id} names replacement report "
                    f"{replacement_report_id}, which does not exist in this organisation."
                )
            block["replacement_report_id"] = replacement_report_id
            # R8 generalised: still retracted either way, but the reader is
            # told the replacement is gone rather than sent to fetch it.
            block["replacement_retained"] = replacement[0] is None
        return block

    def _read_amends(self, session: Session, report_id: uuid.UUID) -> Optional[dict[str, Any]]:
        """Banner 1. This report IS an amendment of something."""
        rows = self._query(
            "SELECT id, original_report_id, reason, created_at, created_by FROM amendments "
            "WHERE org_id = %s AND amendment_report_id = %s",
            (session.org_id, report_id),
        )
        if not rows:
            return None
        if len(rows) > 1:
            # One report is the amendment OF one original. Two rows claiming
            # it means the banner would have to pick an original to name, and
            # picking is guessing. R7: refuse.
            raise BrokenRevisionRecordError(
                f"Report {report_id} is named as the amendment report by {len(rows)} amendments rows; "
                "it cannot state which report it amends."
            )
        _amendment_id, original_report_id, reason, created_at, created_by = rows[0]
        if not (reason or "").strip():
            raise BrokenRevisionRecordError(
                f"The amendment record for report {report_id} carries no reason (ISO 15189 7.4.1.8 "
                "requires it to be retrievable); refusing to state the amendment without it."
            )
        original = self._query_one(
            "SELECT approved_at FROM reports WHERE org_id = %s AND id = %s",
            (session.org_id, original_report_id),
        )
        if original is None:
            raise BrokenRevisionRecordError(
                f"The amendment record for report {report_id} names original report {original_report_id}, "
                "which does not exist in this organisation."
            )
        original_issued_at, basis = self._issued_moment(session.org_id, original_report_id, original[0])
        if original_issued_at is None:
            # create_amendment refuses an original that is not approved or
            # released, so an original with neither date is a record that
            # contradicts the method that wrote it.
            raise BrokenRevisionRecordError(
                f"Original report {original_report_id} was amended but carries neither a release nor an "
                "approval; there is no date on which it was issued."
            )
        return {
            "original_report_id": original_report_id,
            "original_issued_at": original_issued_at,
            "original_issued_basis": basis,
            "reason": reason,
            # R1/R10: the person, by the three fields a report may print.
            "amended_by": self._signing_identity_text(session, created_by),
            # The amendment ACT, not an issue date -- amendments.created_at is
            # exactly "when this report was amended", so no basis applies.
            "amended_at": created_at,
        }

    def _read_superseded_by(self, session: Session, report_id: uuid.UUID) -> Optional[dict[str, Any]]:
        """
        Banner 2. Something amended THIS report.

        R3: a DRAFT amendment does not supersede anything. An amendment that
        has not been approved is work in progress -- telling a clinician not
        to act on the report they hold, in favour of a document that may never
        be approved and cannot be obtained, is worse than saying nothing. The
        state filter here IS that ruling.

        R5: the LATEST approved amendment is the one named. Its reason is
        printed only when it is also the amendment that superseded THIS
        document (nothing in front of it in the chain); otherwise its reason
        explains a change from a document this reader has never seen, so the
        chain depth goes out instead.
        """
        rows = self._query(
            "SELECT a.id, a.amendment_report_id, a.reason, a.supersedes_amendment_id, a.created_at, "
            "       r.state, r.approved_at, r.tombstoned_at "
            "FROM amendments a JOIN reports r ON r.org_id = a.org_id AND r.id = a.amendment_report_id "
            "WHERE a.org_id = %s AND a.original_report_id = %s "
            "ORDER BY a.created_at, a.id",
            (session.org_id, report_id),
        )
        if not rows:
            return None
        supersedes_by_id = {row[0]: row[3] for row in rows}
        issued = [row for row in rows if row[5] in ("approved", "released")]
        if not issued:
            return None
        latest = issued[-1]
        amendment_id, amendment_report_id, reason, _supersedes, _created_at, _state, approved_at, tombstoned_at = latest

        amended_at, basis = self._issued_moment(session.org_id, amendment_report_id, approved_at)
        if amended_at is None:
            raise BrokenRevisionRecordError(
                f"Amendment report {amendment_report_id} is in an issued state but carries neither a "
                "release nor an approval; there is no date on which the amended report was issued."
            )

        # How many amendment steps stand between this document and the one
        # being named. 1 means the named amendment is the one that superseded
        # THIS document, so its reason describes a change from what the reader
        # is holding.
        depth = 1
        walker = supersedes_by_id.get(amendment_id)
        seen = {amendment_id}
        while walker is not None:
            if walker in seen:
                raise BrokenRevisionRecordError(
                    f"The amendment chain for report {report_id} contains a cycle at {walker}."
                )
            seen.add(walker)
            if walker not in supersedes_by_id:
                raise BrokenRevisionRecordError(
                    f"Amendment {walker} is superseded in the chain for report {report_id} but is not an "
                    "amendment of that report."
                )
            depth += 1
            walker = supersedes_by_id.get(walker)

        block: dict[str, Any] = {
            "amendment_report_id": amendment_report_id,
            "amended_at": amended_at,
            "amended_at_basis": basis,
            # R8: still superseded, but the reader is told the amended report
            # is gone rather than sent to fetch it.
            "retained": tombstoned_at is None,
        }
        if depth == 1:
            if not (reason or "").strip():
                raise BrokenRevisionRecordError(
                    f"The amendment superseding report {report_id} carries no reason; refusing to state "
                    "the supersession without it."
                )
            block["reason"] = reason
        else:
            block["amendment_count"] = depth
        return block

    def _read_reanalysis_of(self, session: Session, interpretation_id: uuid.UUID) -> Optional[dict[str, Any]]:
        """Banner 3. This report's interpretation branched from another one (spec 15.3)."""
        row = self._query_one(
            "SELECT parent_interpretation_id FROM interpretations WHERE org_id = %s AND id = %s",
            (session.org_id, interpretation_id),
        )
        if row is None:
            raise BrokenRevisionRecordError(
                f"Report names interpretation {interpretation_id}, which does not exist in this organisation."
            )
        parent_id = row[0]
        if parent_id is None:
            return None
        parent = self._query_one(
            "SELECT created_at FROM interpretations WHERE org_id = %s AND id = %s",
            (session.org_id, parent_id),
        )
        if parent is None:
            raise BrokenRevisionRecordError(
                f"Interpretation {interpretation_id} names parent {parent_id}, which does not exist in "
                "this organisation; the report cannot say what it is a re-analysis of."
            )
        return {"parent_interpretation_id": parent_id, "parent_interpreted_at": parent[0]}

    def _read_reanalysed_since(
        self,
        session: Session,
        report_id: uuid.UUID,
        interpretation_id: uuid.UUID,
        report_created_at: Any,
        report_approved_at: Any,
    ) -> List[dict[str, Any]]:
        """
        Banner 4. Re-analyses that exist SINCE this report was issued.

        HUMAN RULING R6, both halves:

          DIRECT children only -- interpretations naming this report's
          interpretation as their parent. A grandchild is a re-analysis of a
          re-analysis; it branched from a document this reader never held.

          Created AFTER this report -- anything earlier is not "since". A
          sibling branch that predates this report would make the word "since"
          a lie, and the banner is entirely built on that word.

        The cutoff is this report's issue moment (R2's basis again), falling
        back to its creation for a report that was never approved: an
        unissued report has no "since" of its own, and its creation is the
        earliest moment anything could be said to come after it.
        """
        cutoff, _basis = self._issued_moment(session.org_id, report_id, report_approved_at)
        if cutoff is None:
            cutoff = report_created_at
        rows = self._query(
            "SELECT id, created_at FROM interpretations "
            "WHERE org_id = %s AND parent_interpretation_id = %s AND created_at > %s "
            "ORDER BY created_at, id",
            (session.org_id, interpretation_id, cutoff),
        )
        return [{"interpretation_id": row[0], "created_at": row[1]} for row in rows]

    @auditable(
        action="notification_read_receipt_recorded",
        resource_type="notification",
        resource_id_from_result=True,
        requires_session=True,
        auditable=True,
        reason="Read receipt recorded: clinician opened the amendment notification (ISO 15189 7.4.1.8)",
    )
    @transactional
    def record_notification_read_receipt(
        self, session: Session, notification_id: uuid.UUID, read_by_user_id: uuid.UUID
    ) -> uuid.UUID:
        """
        Record that a clinician read a notification. A receipt row carries
        WHO read it and WHEN (an append-only record, not a mutable flag).
        Multiple receipts may exist for the same notification (different users
        or the same user reading multiple times).

        Read state is DERIVED: a notification is read if at least one receipt
        row exists for it (EXISTS in amendment_notifications → notification_read_receipts).

        Preconditions:
          - Notification must exist in this org
          - read_by_user_id must exist in this org
          - read_by_user_id must hold a role that can open notifications
            (deferred to a future phase; not validated here)

        Returns: receipt_id (UUID of the read_receipts row)
        """
        notification = self._query_one(
            "SELECT amendment_report_id FROM amendment_notifications WHERE id = %s AND org_id = %s",
            (notification_id, session.org_id),
        )
        if notification is None:
            raise NotFoundError(f"Notification {notification_id} not found in org {session.org_id}")

        reader = self._query_one(
            "SELECT user_id FROM users WHERE user_id = %s AND org_id = %s",
            (read_by_user_id, session.org_id),
        )
        if reader is None:
            raise NotFoundError(f"User {read_by_user_id} not found in org {session.org_id}")

        receipt_id = uuid.uuid4()
        now = self._clock.now()

        self._execute(
            "INSERT INTO notification_read_receipts (org_id, id, notification_id, read_at, read_by, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                session.org_id,
                receipt_id,
                notification_id,
                now,
                read_by_user_id,
                now,
            ),
        )

        return receipt_id

    # ── Phase 8: FASTQ ingestion ─────────────────────────────────────────────
    #
    # These two live here rather than in `ingestion.py` for the reason every
    # other write does: this class owns org scoping, the audit trail and the
    # SQL, and a second module issuing its own INSERTs would be a parallel
    # mechanism rather than a reuse of this one. The scanner decides WHAT is a
    # complete, stable, verified FASTQ set; it does not decide how a row is
    # written or how an organisation is isolated.

    @auditable(
        auditable=False,
        action="find_fastq_set_by_checksums",
        resource_type="fastq_set",
        requires_session=True,
        reason="idempotency probe on every scan; auditing it would bury the detections in noise",
    )
    def find_fastq_set_by_checksums(self, session: Session, r1_checksum: str, r2_checksum: str) -> Optional[uuid.UUID]:
        """
        The existing row for this exact pair of file contents, if any.

        THE CHECKSUM PAIR IS THE IDEMPOTENCY KEY, NOT THE PATH (human ruling,
        2026-09-08). A moved or renamed file with the same content is the SAME
        observation; a reused path with new content is a DIFFERENT one. This is
        why `fastq_sets` deliberately carries no `UNIQUE (org_id, r1_path)`: a
        path-based constraint would have been wrong in both directions.

        w129c: THE ORGANISATION IS READ OFF THE SESSION, NOT OFF AN ARGUMENT.
        This used to take a raw `org_id`, which made the tenant scope a CLAIM
        the caller asserted rather than a property of an authenticated
        principal -- and a checksum probe that can name any organisation is an
        existence oracle over another tenant's file contents. The ruling left
        the READ PATH's AUTHORISATION alone and it is left alone: no role gate,
        still `auditable=False` for the reason above. Only the source of the
        org_id changed, which is what `Session`'s own docstring already
        requires of every call that takes one.
        """
        row = self._query_one(
            "SELECT fastq_set_id FROM fastq_sets WHERE org_id = %s AND r1_checksum = %s AND r2_checksum = %s",
            (session.org_id, r1_checksum, r2_checksum),
        )
        return None if row is None else row[0]

    @auditable(
        action="fastq_set_detected",
        resource_type="fastq_set",
        resource_id_from_result=True,
        requires_session=True,
    )
    def record_fastq_set(
        self,
        session: Session,
        sample_id: uuid.UUID,
        r1_path: str,
        r2_path: str,
        r1_checksum: str,
        r2_checksum: str,
        detected_at: _datetime.datetime,
    ) -> uuid.UUID:
        """
        Append one `fastq_sets` row for a sample IN THIS ORGANISATION.

        Raises `NotFoundError` if the sample does not resolve. That exception
        already carries the property this needs and its docstring says so:
        "deliberately indistinguishable from does not exist at all". ANOTHER
        ORGANISATION'S SAMPLE MUST BE INDISTINGUISHABLE FROM UNKNOWN, not
        merely refused -- a distinct error for "exists but not yours" tells the
        caller which sample ids exist elsewhere. So the resolution below
        filters on org_id and the caller cannot tell the two cases apart.

        w129c: IT TAKES A SESSION AND THE SESSION MUST HOLD `System`.

        This paragraph used to read "No `session` argument: a scheduled scan
        has no logged-in user", and reached for the PROVISIONING audit path
        (org_id off the bound parameters, actor null). That was DRIFT, not
        architecture. "No logged-in user" is precisely the condition the System
        principal exists to answer -- `record_pipeline_result` in this same
        file already has the shape: it takes a `Session` and the worker mints
        it. Two precedents were available and the provisioning one was picked
        in passing, never argued. Three things were wrong as a result: the
        organisation was a caller-supplied CLAIM rather than a property of an
        authenticated principal; every detection was audited with a NULL
        ACTOR; and this was the only `requires_session=False` method in the
        file that writes a tenant-scoped row -- a category of one.

        THE ROLE GATE IS `System`, NOT `Lab technician`. Detection is the
        platform observing a directory, not a member of laboratory staff
        receiving a specimen, and w129 forbids treating an internal principal
        as a normal human staff role. A human session holding Administrator,
        Interpreter, Orderer AND Lab technician is refused here.

        THE SESSION COMES FROM THE CALLER. Neither this method nor
        `clinical/ingestion.py` mints one. `_create_system_session` is an
        authentication bypass whose invariant is about WHERE, not how many:
        every call site sits in a background worker class in `geper/api/`.
        Minting one in the scanner would be a FIFTH site in a non-worker module
        and test_identity.py's
        `test_create_system_session_call_sites_are_the_four_pinned_worker_sites`
        would fail -- correctly. The not-yet-written scheduler is the thing
        that has to be explicit about which principal it runs as.

        The audit row still comes from `@auditable` and only from it: an
        earlier draft called `self._audit` by hand as well and would have
        written TWO rows per detection.
        """
        self._require_role(session, "System")

        sample = self._query_one(
            "SELECT sample_id FROM samples WHERE sample_id = %s AND org_id = %s",
            (sample_id, session.org_id),
        )
        if sample is None:
            raise NotFoundError(f"Sample {sample_id} not found")

        fastq_set_id = uuid.uuid4()
        self._execute(
            "INSERT INTO fastq_sets (fastq_set_id, org_id, sample_id, r1_path, r2_path, "
            "r1_checksum, r2_checksum, detected_at, state) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'detected')",
            (fastq_set_id, session.org_id, sample_id, r1_path, r2_path, r1_checksum, r2_checksum, detected_at),
        )
        return fastq_set_id


# A real bcrypt hash of a value nobody holds, used only to spend verification
# work when no user matched. Generated once, constant thereafter.
_DUMMY_HASH = "$2b$12$C6UzMDM.H6dfI/f/IKcEe.6c1sVQ0S0kzSfKUL5jHkKzE1LpMOxQ2"
