"""
review/clinical_http.py
───────────────────────

The clinical platform, reached over its authenticated HTTPS API instead of a database connection.

WHY THIS EXISTS. The human-operated tools (report-revision refresh, the LIMS export's status lookup,
and, in a later phase, sign-off) used to obtain clinical facts through `DataAccess`, which means a
PostgreSQL connection string on the machine a clinician sits at. That credential is the application's
whole authority -- the database has no row-level security, the application's role may write `sessions`,
and organisation isolation and role gates exist only in `DataAccess` -- so it must not leave the server
network. `HttpClinicalDataAccess` offers the SAME method names those tools already call
(`login`, `get_report`, `get_report_revision`, `get_signing_identity`, and the four sign-off writes
`record_sole_signatory_claim`, `record_disagreement_claim`, `submit_for_review`, `approve_report`), answered by the
API, so it drops into the existing injected `data_access` parameter and the tools above it do not change.

WHAT IT REFUSES TO DO
  * It never opens a database connection and never reads a DSN.
  * It talks to `https://` URLs only. The session cookie is `Secure`, so over plain HTTP it would not even
    work, and a password must not cross an unencrypted network. (An injected client, used by tests, is the
    only way around this check.) Redirects are not followed, so a credential is never re-sent elsewhere.
  * It never turns a failure into an empty answer. A transport error, an unexpected status, a malformed body,
    an authorization refusal and an unreadable revision record each raise; the caller's own fail-closed
    handling (review/revision_status.py) then refuses to produce anything.
  * It can only ask for the CALLER'S OWN signing identity -- the server has no route naming anyone else.
  * It NEVER RETRIES A WRITE. A write whose outcome is unknown (no answer, a dropped connection, a 5xx) may
    have committed; it raises `ClinicalWriteOutcomeUnknown` and the tool above writes no file. Only a
    definitive refusal (400/401/403/404) means "not done".
  * It offers only the four writes a sign-off needs. Release, amendment and retraction are not reachable.

ERROR MAPPING (HTTP -> the exception the direct `DataAccess` would have raised, so callers need no new cases)
  401 on login -> AuthenticationError            401 afterwards -> SessionExpired
  403          -> AuthorizationError             404 (report)   -> None from get_report, NotFoundError from revision
  400 on a write -> ValueError(detail), as the direct DataAccess raises it; 404 on a write -> NotFoundError
  5xx / no answer on a WRITE -> ClinicalWriteOutcomeUnknown
  409 coded `incomplete_clinician_identity` -> IncompleteClinicianIdentityError
  500 coded `revision_record_unreadable`    -> BrokenRevisionRecordError
  anything else, or no answer               -> ClinicalServiceError
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from utils.exceptions import SignoffError


class ClinicalServiceError(SignoffError):
    """The clinical service did not give a usable answer (transport failure, unexpected status or body)."""


class ClinicalWriteOutcomeUnknown(ClinicalServiceError):
    """A sign-off WRITE got no definitive answer, so it may or may not have been recorded. Never retried here."""


@dataclass(frozen=True)
class HttpSession:
    """What the tools need to know about the logged-in caller. The credential itself is the cookie, held
    privately by the adapter's HTTP client and never exposed here."""

    org_id: uuid.UUID
    user_id: uuid.UUID


# What the schema allows (clinical/schema.sql: reports.state CHECK, reviewer_claims.claim_type CHECK). `returned`
# is permitted by the schema although no current code path sets it; it is accepted, not invented.
_SIGNOFF_STATES = frozenset({"draft", "under_review", "approved", "released", "returned"})
_CLAIM_TYPES = frozenset({"accept", "disagree", "variant_added", "variant_not_relevant", "sole_signatory"})


def _text(mapping: Any, key: str) -> str:
    """A required string field. A missing key, a null or any other type is refused (no str() coercion)."""
    if not isinstance(mapping, dict):
        raise TypeError(f"expected an object, got {type(mapping).__name__}")
    value = mapping[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} is not a string")
    return value


def _uuid_field(mapping: Any, key: str) -> uuid.UUID:
    return uuid.UUID(_text(mapping, key))


def _utc_aware(text: str, what: str) -> datetime.datetime:
    value = datetime.datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} has no timezone")
    return value


def _parse_signoff_state(body: Dict[str, Any], report_id: uuid.UUID) -> Dict[str, Any]:
    """Validate and convert the sign-off state answer. Raises KeyError / ValueError / TypeError on any defect."""
    if _uuid_field(body, "report_id") != report_id:
        raise ValueError("the answer names a different report")
    interpretation_id = _uuid_field(body, "interpretation_id")
    state = _text(body, "state")
    if state not in _SIGNOFF_STATES:
        raise ValueError(f"unsupported state {state!r}")
    for flag in ("is_retracted", "has_content_hash"):
        if not isinstance(body[flag], bool):
            raise ValueError(f"{flag} is not a boolean")

    # `approver_id` and `approved_at` must be PRESENT (null is a legitimate value), not merely defaulted.
    raw_approver, raw_approved_at = body["approver_id"], body["approved_at"]
    approver = _uuid_field(body, "approver_id") if raw_approver is not None else None
    approved_at = _utc_aware(_text(body, "approved_at"), "approved_at") if raw_approved_at is not None else None
    has_hash = body["has_content_hash"]
    # reports_approval_complete: approver, time and hash are all set together or not at all.
    if not ((approver is None) == (approved_at is None) == (not has_hash)):
        raise ValueError("approver_id, approved_at and has_content_hash disagree about whether the report is approved")
    # reports_released_states_need_approval: an approved or released report has an approver.
    if state in ("approved", "released") and approver is None:
        raise ValueError(f"a report in state {state!r} carries no approval")

    claims_raw = body["claims"]
    if not isinstance(claims_raw, list):
        raise ValueError("claims is not a list")
    claims = []
    seen_ids = set()
    previous = None
    for entry in claims_raw:
        claim_id = _uuid_field(entry, "claim_id")
        claim_type = _text(entry, "claim_type")
        if claim_type not in _CLAIM_TYPES:
            raise ValueError(f"unsupported claim type {claim_type!r}")
        recorded_at = _utc_aware(_text(entry, "recorded_at"), "recorded_at")
        if claim_id in seen_ids:
            raise ValueError("a claim is listed twice")
        if previous is not None and recorded_at < previous:
            raise ValueError("claims are not in oldest-first order")
        seen_ids.add(claim_id)
        previous = recorded_at
        claims.append(
            {
                "id": claim_id,
                "claim_type": claim_type,
                "actor_id": _uuid_field(entry, "actor_id"),
                "recorded_at": recorded_at,
            }
        )

    return {
        "id": report_id,
        "interpretation_id": interpretation_id,
        "state": state,
        "is_retracted": body["is_retracted"],
        "approver_id": approver,
        "approved_at": approved_at,
        "has_content_hash": has_hash,
        "claims": claims,
    }


def _clinical_errors():
    from clinical import data_access as da

    return da


class HttpClinicalDataAccess:
    """Duck-typed stand-in for the parts of `clinical.data_access.DataAccess` the human tools call."""

    def __init__(self, base_url: Optional[str] = None, *, client: Any = None, timeout: float = 15.0):
        if client is None:
            if not base_url or not base_url.lower().startswith("https://"):
                raise ClinicalServiceError(
                    "The clinical service URL must start with https:// (the session cookie is Secure and a "
                    "password must not cross an unencrypted network). Human-operated tools do not connect "
                    "to the database; they use the clinical service over HTTPS."
                )
            import httpx

            client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, follow_redirects=False)
        self._client = client
        self._session: Optional[HttpSession] = None

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs: Any):
        import httpx

        try:
            return self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # The exception text carries the URL, never the request body.
            raise ClinicalServiceError(f"The clinical service could not be reached ({type(exc).__name__}).") from exc

    @staticmethod
    def _json(response: Any) -> Dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise ClinicalServiceError(
                f"The clinical service answered {response.status_code} with a body that is not JSON."
            ) from exc
        if not isinstance(body, dict):
            raise ClinicalServiceError(f"The clinical service answered {response.status_code} with a non-object body.")
        return body

    def _detail(self, response: Any) -> str:
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("detail"), str):
                return body["detail"]
        except ValueError:
            pass
        return f"HTTP {response.status_code}"

    def _refuse(self, response: Any, what: str):
        """Raise the right exception for a non-success status. Always raises."""
        da = _clinical_errors()
        status = response.status_code
        if status == 401:
            raise da.SessionExpired()
        if status == 403:
            raise da.AuthorizationError(self._detail(response))
        raise ClinicalServiceError(f"The clinical service answered {status} to {what}.")

    def _own(self, session: Any) -> HttpSession:
        if self._session is None or session is not self._session:
            raise ClinicalServiceError("This session was not opened by this adapter; refusing to use it.")
        return self._session

    # ── DataAccess surface ───────────────────────────────────────────────────

    def login(self, email: str, org_id: uuid.UUID, password: str, totp_code: Optional[str] = None) -> HttpSession:
        da = _clinical_errors()
        payload: Dict[str, Any] = {"email": email, "org_id": str(org_id), "password": password}
        if totp_code:
            payload["totp_code"] = totp_code
        response = self._request("POST", "/auth/login", json=payload)
        if response.status_code == 429:
            raise da.AuthenticationError("Too many login attempts; try again later.")
        if response.status_code == 401:
            raise da.AuthenticationError(da.GENERIC_AUTH_ERROR)
        if response.status_code != 204:
            raise ClinicalServiceError(f"The clinical service answered {response.status_code} to the login.")
        me = self._request("GET", "/auth/me")
        if me.status_code != 200:
            raise ClinicalServiceError(f"The clinical service answered {me.status_code} to /auth/me after login.")
        body = self._json(me)
        try:
            session = HttpSession(org_id=uuid.UUID(str(body["org_id"])), user_id=uuid.UUID(str(body["user_id"])))
        except (KeyError, ValueError) as exc:
            raise ClinicalServiceError("The clinical service's /auth/me answer is malformed.") from exc
        if session.org_id != org_id:
            raise ClinicalServiceError("The service reports a different organisation than the one logged in to.")
        self._session = session
        return session

    def logout(self) -> None:
        """End the server-side session. Best effort: failing to log out must not mask the real outcome."""
        if self._session is None:
            return
        try:
            self._client.request("POST", "/auth/logout")
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._session = None

    def close(self) -> None:
        self.logout()
        close = getattr(self._client, "close", None)
        if close:
            close()

    def get_report(self, session: Any, report_id: uuid.UUID) -> Optional[Dict[str, Any]]:
        self._own(session)
        response = self._request("GET", f"/review/reports/{report_id}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            self._refuse(response, f"the read of report {report_id}")
        body = self._json(response)
        try:
            if not isinstance(body["is_retracted"], bool):
                raise ValueError("is_retracted is not a boolean")
            return {
                "id": uuid.UUID(str(body["report_id"])),
                "interpretation_id": uuid.UUID(str(body["interpretation_id"])),
                "state": str(body["state"]),
                "is_retracted": body["is_retracted"],
                "created_at": datetime.datetime.fromisoformat(str(body["created_at"])),
            }
        except (KeyError, ValueError, TypeError) as exc:
            raise ClinicalServiceError(f"The clinical service's report answer for {report_id} is malformed.") from exc

    def get_report_revision(self, session: Any, report_id: uuid.UUID) -> Dict[str, Any]:
        da = _clinical_errors()
        self._own(session)
        response = self._request("GET", f"/review/reports/{report_id}/revision")
        if response.status_code == 404:
            raise da.NotFoundError(f"Report {report_id} not found")
        if response.status_code == 500:
            body = None
            try:
                body = response.json()
            except ValueError:
                pass
            if isinstance(body, dict) and body.get("code") == "revision_record_unreadable":
                raise da.BrokenRevisionRecordError(str(body.get("detail") or "the revision record cannot be read"))
            raise ClinicalServiceError("The clinical service failed while reading the revision (500).")
        if response.status_code != 200:
            self._refuse(response, f"the revision read of report {report_id}")
        body = self._json(response)
        revision = body.get("revision")
        if not isinstance(revision, dict) or str(body.get("report_id")) != str(report_id):
            raise ClinicalServiceError(
                f"The clinical service's revision answer for {report_id} is malformed: only a mapping naming "
                "the same report is a successful answer."
            )
        return revision

    def get_signoff_state(self, session: Any, report_id: uuid.UUID) -> Optional[Dict[str, Any]]:
        """
        Where a report's sign-off stands (`GET /review/reports/{id}/signoff-state`), or None when the service has
        no such report in the caller's organisation. READ-ONLY: exactly one GET. Claims come back without their
        reasons.

        NOTHING MALFORMED OR SELF-CONTRADICTORY IS ACCEPTED. The answer must carry every promised field with the
        right type, a state the schema allows, claim types the schema allows, UTC-aware timestamps, no repeated
        claim, claims oldest first, and an approval that agrees with itself (approver, time and hash all present or
        all absent, and present for an approved or released report). Anything else raises `ClinicalServiceError`
        rather than handing an operator a picture that cannot be true. A draft or a report under review with no
        approval is legitimate and is accepted.
        """
        self._own(session)
        response = self._request("GET", f"/review/reports/{report_id}/signoff-state")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            self._refuse(response, f"the sign-off state read of report {report_id}")
        body = self._json(response)
        try:
            return _parse_signoff_state(body, report_id)
        except (KeyError, ValueError, TypeError) as exc:
            raise ClinicalServiceError(
                f"The clinical service's sign-off state answer for {report_id} is malformed or inconsistent "
                f"({type(exc).__name__}: {exc})."
            ) from exc

    def get_signing_identity(self, session: Any, user_id: uuid.UUID) -> Dict[str, Any]:
        da = _clinical_errors()
        own = self._own(session)
        if user_id != own.user_id:
            raise da.AuthorizationError(
                "Over the HTTP API a tool can read only the caller's own signing identity, not another user's."
            )
        response = self._request("GET", "/auth/me/signing-identity")
        if response.status_code == 409:
            body = None
            try:
                body = response.json()
            except ValueError:
                pass
            if isinstance(body, dict) and body.get("code") == "incomplete_clinician_identity":
                raise da.IncompleteClinicianIdentityError(str(body.get("detail") or "incomplete identity"))
            raise ClinicalServiceError("The clinical service answered 409 to the identity read.")
        if response.status_code != 200:
            self._refuse(response, "the signing-identity read")
        body = self._json(response)
        try:
            return {
                "user_id": uuid.UUID(str(body["user_id"])),
                "full_name": str(body["full_name"]),
                "registration_number": str(body["registration_number"]),
                "hospital": str(body["hospital"]),
            }
        except (KeyError, ValueError) as exc:
            raise ClinicalServiceError("The clinical service's signing-identity answer is malformed.") from exc

    # ── the sign-off WRITES ──────────────────────────────────────────────────
    #
    # Each is exactly one request to a route that is exactly one domain call. Nothing here names an actor, an
    # organisation or a state: those are the server's (the session). The report/interpretation ids come from the
    # signed link and are CHECKED by the domain against the session's organisation; they are not trusted.

    def _write(self, what: str, path: str, body: Optional[Dict[str, Any]], ok: int) -> Any:
        """POST once. Returns the response on `ok`; raises the direct path's exception for a definitive refusal and
        `ClinicalWriteOutcomeUnknown` for anything that leaves the outcome in doubt."""
        import httpx

        da = _clinical_errors()
        unknown = (
            f"The outcome of '{what}' is UNKNOWN: it may or may not have been recorded. It was not retried and no "
            "file has been changed. Check the report's state in the clinical service before running this again."
        )
        try:
            response = (
                self._client.request("POST", path, json=body)
                if body is not None
                else self._client.request("POST", path)
            )
        except httpx.HTTPError as exc:
            raise ClinicalWriteOutcomeUnknown(f"{unknown} ({type(exc).__name__})") from exc
        status = response.status_code
        if status == ok:
            return response
        if status == 401:
            raise da.SessionExpired()
        if status == 403:
            raise da.AuthorizationError(self._detail(response))
        if status == 404:
            raise da.NotFoundError(self._detail(response))
        if status == 400:
            raise ValueError(self._detail(response))
        if status == 422:
            # The service rejected the request's shape: a client defect, and the request was not processed.
            raise ClinicalServiceError(
                f"The clinical service rejected the request for '{what}' (422). Nothing changed."
            )
        raise ClinicalWriteOutcomeUnknown(f"{unknown} (HTTP {status})")

    def record_sole_signatory_claim(
        self, session: Any, interpretation_id: uuid.UUID, report_id: uuid.UUID, reason: str
    ) -> uuid.UUID:
        self._own(session)
        response = self._write(
            "record_sole_signatory_claim",
            f"/review/reports/{report_id}/concurrence",
            {"interpretation_id": str(interpretation_id), "reason": reason},
            201,
        )
        return self._claim_id(response, "record_sole_signatory_claim")

    def record_disagreement_claim(
        self,
        session: Any,
        interpretation_id: uuid.UUID,
        report_id: uuid.UUID,
        variant_key: str,
        new_classification: str,
        reason: str,
    ) -> uuid.UUID:
        self._own(session)
        response = self._write(
            "record_disagreement_claim",
            f"/review/reports/{report_id}/disagreement",
            {
                "interpretation_id": str(interpretation_id),
                "variant_key": variant_key,
                "new_classification": new_classification,
                "reason": reason,
            },
            201,
        )
        return self._claim_id(response, "record_disagreement_claim")

    def submit_for_review(self, session: Any, report_id: uuid.UUID) -> None:
        self._own(session)
        self._write("submit_for_review", f"/review/reports/{report_id}/submission", None, 204)

    def approve_report(self, session: Any, report_id: uuid.UUID) -> None:
        self._own(session)
        self._write("approve_report", f"/review/reports/{report_id}/approval", None, 204)

    @staticmethod
    def _claim_id(response: Any, what: str) -> uuid.UUID:
        """The claim was created (201); an unreadable id does not undo that, so this is an unknown OUTCOME for the
        caller's purposes only in that the id is missing -- never treated as a refusal."""
        try:
            return uuid.UUID(str(response.json()["claim_id"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise ClinicalWriteOutcomeUnknown(
                f"'{what}' was accepted (201) but the answer carries no usable claim id. The claim exists; "
                "no file has been changed. Check the report's claims in the clinical service."
            ) from exc
