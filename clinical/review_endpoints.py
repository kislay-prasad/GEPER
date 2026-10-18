"""
clinical/review_endpoints.py
────────────────────────────
Stage 3: THE REVIEW SURFACE -- the human acts between a draft report and a
released one, over HTTP, and the FOURTH prefix this service serves.

    POST /review/reports/{report_id}/concurrence    Interpreter  record_sole_signatory_claim
    POST /review/reports/{report_id}/disagreement   Interpreter  record_disagreement_claim
    POST /review/reports/{report_id}/submission     Interpreter  submit_for_review
    POST /review/reports/{report_id}/approval       Approver     approve_report
    POST /review/reports/{report_id}/release        Approver     release_report  (to the patient portal)
    GET  /review/reports/{report_id}                Interpreter, Approver, Auditor, Administrator   read_report_status
    GET  /review/reports/{report_id}/revision       (same roles)                                    read_report_revision
    GET  /review/reports/{report_id}/signoff-state  (same roles)                                    read_signoff_state

EVERY ROUTE IS ONE DOMAIN CALL. `@auditable` commits after each audit row it
writes, so a handler that called two decorated methods ("record the claim, then
read the report back") could commit the first act's audit row and then fail.
Each handler therefore calls exactly one method and returns what it returned.

THE PREFIX NAMES THE SURFACE, NOT THE GATE -- as `/admin`, `/lab` and `/clinic`
already do. The gates are the DOMAIN METHODS', and each is that method's first
executable statement: `_require_role(session, "Interpreter")` for the claim
recorders and `submit_for_review`, `"Approver"` for approval and release. No
role is checked, restated or widened here, and the Approver is not accepted as
a substitute for the Interpreter (nor the reverse): the two-role control spec
13.3 rests on is the domain's.

WHAT THIS SURFACE DOES NOT DO, AND WHY IT IS NOT A GAP IN THE SURFACE:

* IT DOES NOT RUN THE INTERPRETATION. The pipeline run is the background
  worker's (`geper/api/submission_worker.py`), as the organisation's System
  principal, through `record_pipeline_result`. That method takes a server
  filesystem path and a hash; exposing it to an HTTP caller would let a client
  name a path for the server to read and hash. The worker records the draft
  report; this surface starts where the draft exists.
* IT DOES NOT LET A CALLER NAME A RECIPIENT. The patient portal is the only
  delivery path that exists, so `release` takes no body: it releases the
  approved report to the portal (`release_events.consumer` =
  `PORTAL_RELEASE_CONSUMER`), and a request that sends a body is simply not
  read. An external-recipient release (a LIMS, a referring clinician) is a
  separate decision and a separate route; the domain's `_release_report`
  still accepts any consumer for it.
* IT DOES NOT SERVE A REPORT'S CONTENT OR ITS CLAIMS. The two GET routes added for the human
  tools (sign-off, revision refresh, LIMS export) return a report's STATE, its interpretation,
  whether it is retracted, and its authoritative REVISION facts -- what a person's tool needs
  in order to refuse or to state a status, so that tool never holds a database credential. The
  report document itself is rendered outside this schema, and the patient portal serves a
  released report to its patient.
* IT DOES NOT ADD OR REMOVE ANY REPORT STATE. Transitions are the domain's
  (draft -> under_review -> approved -> released) and an out-of-order call is a
  400 carrying the state the report was actually in.

NO BODY CARRIES AN ORGANISATION, AN ACTOR OR A STATE. The organisation is
`Session.org_id`; the actor is `session.user_id`, fixed inside each domain
method (`approve_report`, `release_report` and both claim recorders take no
`actor_id`). A request body naming one is refused with 422 (`extra="forbid"`).
A report, interpretation or user in another organisation reads exactly like one
that does not exist: 404, same body.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from clinical.admin_endpoints import _forbidden, _not_found, _target
from clinical.data_access import (
    AuthorizationError,
    BrokenRevisionRecordError,
    DataAccess,
    IncompleteClinicianIdentityError,
    NotFoundError,
    Session,
)
from clinical.endpoints import _get_data_access, _get_session

router = APIRouter(prefix="/review", tags=["review"])


# ─── request and response models ─────────────────────────────────────────────
#
# `str` / `Optional[str]` ONLY, with no default but None and no constraint, for
# the reason w162 gives: Pydantic runs before the role gate, so a value rule
# applied here would answer a caller whose authorisation has not been evaluated.
# Every value rule (a blank reason, a missing classification, a blank consumer)
# is the domain method's and runs after its gate.


class ConcurrenceRequest(BaseModel):
    """`interpretation_id` is the interpretation the report belongs to. It is
    sent, and not derived, because the domain method checks the pair and refuses
    a report that is not a report of that interpretation (400)."""

    model_config = ConfigDict(extra="forbid")

    interpretation_id: str
    reason: str


class DisagreementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interpretation_id: str
    variant_key: str
    new_classification: str
    reason: str


class ClaimRecorded(BaseModel):
    claim_id: str


class ReleaseRecorded(BaseModel):
    release_id: str


class ErrorResponse(BaseModel):
    detail: str


_COMMON = {
    401: {"model": ErrorResponse, "description": "Not authenticated"},
    403: {"model": ErrorResponse, "description": "Not permitted"},
    404: {"model": ErrorResponse, "description": "Not found, or in another organisation"},
}


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ─── the routes ──────────────────────────────────────────────────────────────


@router.post(
    "/reports/{report_id}/concurrence",
    status_code=status.HTTP_201_CREATED,
    response_model=ClaimRecorded,
    summary="Record the signing clinician's concurrence with a draft report (Interpreter)",
    responses={
        **_COMMON,
        400: {"model": ErrorResponse, "description": "Blank reason, or report/interpretation mismatch"},
    },
)
async def record_concurrence(
    report_id: str,
    body: ConcurrenceRequest,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> ClaimRecorded:
    """`DataAccess.record_sole_signatory_claim`, and nothing else. The signatory
    is the session; there is no field by which another user could be named."""
    target = _target(report_id)
    interpretation = _target(body.interpretation_id)
    try:
        claim_id = dao.record_sole_signatory_claim(session, interpretation, target, body.reason)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    except ValueError as exc:
        raise _bad_request(exc)
    return ClaimRecorded(claim_id=str(claim_id))


@router.post(
    "/reports/{report_id}/disagreement",
    status_code=status.HTTP_201_CREATED,
    response_model=ClaimRecorded,
    summary="Record a disagreement with one variant's classification (Interpreter)",
    responses={**_COMMON, 400: {"model": ErrorResponse, "description": "Missing variant, classification or reason"}},
)
async def record_disagreement(
    report_id: str,
    body: DisagreementRequest,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> ClaimRecorded:
    """`DataAccess.record_disagreement_claim`, and nothing else. The original
    classification is never overwritten; the claim is appended beside it."""
    target = _target(report_id)
    interpretation = _target(body.interpretation_id)
    try:
        claim_id = dao.record_disagreement_claim(
            session, interpretation, target, body.variant_key, body.new_classification, body.reason
        )
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    except ValueError as exc:
        raise _bad_request(exc)
    return ClaimRecorded(claim_id=str(claim_id))


@router.post(
    "/reports/{report_id}/submission",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Submit a draft report for review: draft -> under_review (Interpreter)",
    responses={**_COMMON, 400: {"model": ErrorResponse, "description": "Not a draft, or no reviewer claim of its own"}},
)
async def submit_for_review(
    report_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> Response:
    """`DataAccess.submit_for_review`, and nothing else."""
    target = _target(report_id)
    try:
        dao.submit_for_review(session, target)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    except ValueError as exc:
        raise _bad_request(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/reports/{report_id}/approval",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Approve a report under review: under_review -> approved (Approver)",
    responses={**_COMMON, 400: {"model": ErrorResponse, "description": "Not under review"}},
)
async def approve_report(
    report_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> Response:
    """`DataAccess.approve_report`, and nothing else. The approver recorded on
    the report, and the content hash sealed with it, are the domain's."""
    target = _target(report_id)
    try:
        dao.approve_report(session, target)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    except ValueError as exc:
        raise _bad_request(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/reports/{report_id}/release",
    status_code=status.HTTP_201_CREATED,
    response_model=ReleaseRecorded,
    summary="Release an approved report to the patient portal (Approver)",
    responses={**_COMMON, 400: {"model": ErrorResponse, "description": "Not approved, or already released"}},
)
async def release_report(
    report_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> ReleaseRecorded:
    """`DataAccess.release_report`, and nothing else. No body: the recipient is
    the patient portal and the releaser is the session user, both fixed by the
    domain method."""
    target = _target(report_id)
    try:
        release_id = dao.release_report(session, target)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    except ValueError as exc:
        raise _bad_request(exc)
    return ReleaseRecorded(release_id=str(release_id))


# ─── Staff reads for the human tools ─────────────────────────────────────────


class ReportStatus(BaseModel):
    """A report's state and identity within the CALLER'S organisation. No content, no claims."""

    report_id: str
    interpretation_id: str
    state: str
    is_retracted: bool
    created_at: str


class ReportRevision(BaseModel):
    """
    `revision` is the authoritative revision block. THE KEY IS ALWAYS PRESENT ON A 200, and `{}` means
    the lookup SUCCEEDED and found nothing to say. Every failure is a non-200 status, so an empty
    object can never be mistaken for "the lookup failed".
    """

    report_id: str
    revision: dict[str, Any]


class SignoffClaim(BaseModel):
    """One claim on the report, WITHOUT its reason, variant, classification or evidence."""

    claim_id: str
    claim_type: str
    actor_id: str
    recorded_at: str


class SignoffState(BaseModel):
    """Where a report's sign-off stands. Facts only: no claim text, no report content, no hash value."""

    report_id: str
    interpretation_id: str
    state: str
    is_retracted: bool
    approver_id: Optional[str] = None
    approved_at: Optional[str] = None
    has_content_hash: bool
    claims: list[SignoffClaim]


class CodedErrorResponse(BaseModel):
    detail: str
    code: str


REVISION_UNREADABLE = "revision_record_unreadable"


@router.get(
    "/reports/{report_id}",
    response_model=ReportStatus,
    summary="A report's state, interpretation and retraction flag in the CALLER'S OWN organisation",
    responses=_COMMON,
)
async def read_report_status(
    report_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> ReportStatus:
    """`DataAccess.read_report_status`, and nothing else. The organisation is `Session.org_id`; a report in
    another organisation is the same 404 as one that does not exist."""
    target = _target(report_id)
    try:
        report = dao.read_report_status(session, target)
    except AuthorizationError:
        raise _forbidden()
    if report is None:
        raise _not_found()
    return ReportStatus(
        report_id=str(report["id"]),
        interpretation_id=str(report["interpretation_id"]),
        state=str(report["state"]),
        is_retracted=bool(report["is_retracted"]),
        created_at=report["created_at"].isoformat(),
    )


@router.get(
    "/reports/{report_id}/revision",
    response_model=ReportRevision,
    summary="A report's authoritative revision facts in the CALLER'S OWN organisation",
    responses={
        **_COMMON,
        500: {
            "model": CodedErrorResponse,
            "description": "The revision record cannot be read (code revision_record_unreadable)",
        },
    },
)
async def read_report_revision(
    report_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
):
    """`DataAccess.read_report_revision`, and nothing else.

    NO FAILURE BECOMES AN EMPTY ANSWER. An authorization refusal is 403, an unknown or cross-organisation
    report is 404, a revision record that cannot be read into a true sentence is a 500 carrying
    `code: revision_record_unreadable`, and any other failure is the service's ordinary 500. Only a
    lookup that SUCCEEDED returns 200, and then `revision` may legitimately be `{}`."""
    target = _target(report_id)
    try:
        revision = dao.read_report_revision(session, target)
    except AuthorizationError:
        raise _forbidden()
    except NotFoundError:
        raise _not_found()
    except (BrokenRevisionRecordError, IncompleteClinicianIdentityError) as exc:
        # The second case is a retraction (or amendment) whose author's printed identity is incomplete: the
        # revision cannot be STATED truthfully, which is exactly what "unreadable" means to a tool.
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": str(exc), "code": REVISION_UNREADABLE},
        )
    return JSONResponse(content=jsonable_encoder({"report_id": str(target), "revision": revision}))


@router.get(
    "/reports/{report_id}/signoff-state",
    response_model=SignoffState,
    summary="Where a report's sign-off stands, in the CALLER'S OWN organisation (read-only)",
    responses=_COMMON,
)
async def read_signoff_state(
    report_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> SignoffState:
    """`DataAccess.read_signoff_state`, and nothing else. READ-ONLY: it records no claim, moves no report and
    writes no audit row. The organisation is `Session.org_id`; another organisation's report is the same 404 as
    one that does not exist, and a caller without a reading role gets the uniform 403 before any lookup."""
    target = _target(report_id)
    try:
        sign_off = dao.read_signoff_state(session, target)
    except AuthorizationError:
        raise _forbidden()
    if sign_off is None:
        raise _not_found()
    return SignoffState(
        report_id=str(sign_off["id"]),
        interpretation_id=str(sign_off["interpretation_id"]),
        state=str(sign_off["state"]),
        is_retracted=bool(sign_off["is_retracted"]),
        approver_id=str(sign_off["approver_id"]) if sign_off["approver_id"] is not None else None,
        approved_at=sign_off["approved_at"].isoformat() if sign_off["approved_at"] is not None else None,
        has_content_hash=bool(sign_off["has_content_hash"]),
        claims=[
            SignoffClaim(
                claim_id=str(c["id"]),
                claim_type=str(c["claim_type"]),
                actor_id=str(c["actor_id"]),
                recorded_at=str(c["recorded_at"]),
            )
            for c in sign_off["claims"]
        ],
    )
