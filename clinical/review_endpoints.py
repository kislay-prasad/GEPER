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
* IT DOES NOT READ A REPORT BACK. Staff report/claim reads are a separate
  stage; the patient portal already serves a released report to its patient.
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

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

from clinical.admin_endpoints import _forbidden, _not_found, _target
from clinical.data_access import AuthorizationError, DataAccess, NotFoundError, Session
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
