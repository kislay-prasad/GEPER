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
(`login`, `get_report`, `get_report_revision`, `get_signing_identity`), answered by the API, so it drops
into the existing injected `data_access` parameter and the tools above it do not change.

WHAT IT REFUSES TO DO
  * It never opens a database connection and never reads a DSN.
  * It talks to `https://` URLs only. The session cookie is `Secure`, so over plain HTTP it would not even
    work, and a password must not cross an unencrypted network. (An injected client, used by tests, is the
    only way around this check.) Redirects are not followed, so a credential is never re-sent elsewhere.
  * It never turns a failure into an empty answer. A transport error, an unexpected status, a malformed body,
    an authorization refusal and an unreadable revision record each raise; the caller's own fail-closed
    handling (review/revision_status.py) then refuses to produce anything.
  * It can only ask for the CALLER'S OWN signing identity -- the server has no route naming anyone else.
  * It does not provide the sign-off WRITES (claim, submission, approval). Those still need a direct
    connection in this phase; asking this adapter for one raises, so a sign-off operation can never
    half-run against it.

ERROR MAPPING (HTTP -> the exception the direct `DataAccess` would have raised, so callers need no new cases)
  401 on login -> AuthenticationError            401 afterwards -> SessionExpired
  403          -> AuthorizationError             404 (report)   -> None from get_report, NotFoundError from revision
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


@dataclass(frozen=True)
class HttpSession:
    """What the tools need to know about the logged-in caller. The credential itself is the cookie, held
    privately by the adapter's HTTP client and never exposed here."""

    org_id: uuid.UUID
    user_id: uuid.UUID


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

    # ── the sign-off WRITES are not offered over HTTP in this phase ─────────

    def _not_offered(self, name: str):
        raise ClinicalServiceError(
            f"'{name}' is not available through the HTTP adapter in this phase: the sign-off write path still "
            "needs direct clinical access. Nothing has been changed."
        )

    def submit_for_review(self, *args: Any, **kwargs: Any):
        self._not_offered("submit_for_review")

    def record_sole_signatory_claim(self, *args: Any, **kwargs: Any):
        self._not_offered("record_sole_signatory_claim")

    def record_disagreement_claim(self, *args: Any, **kwargs: Any):
        self._not_offered("record_disagreement_claim")

    def approve_report(self, *args: Any, **kwargs: Any):
        self._not_offered("approve_report")
