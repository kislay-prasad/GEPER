"""
clinical/endpoints.py
─────────────────────
Clinical platform exception workflow public API.

Three endpoints for exception management:
1. GET /orders/{id}/exceptions — list all exceptions for an order (open + resolved)
2. GET /exceptions/worklist — worklist of open exceptions by owner
3. PATCH /exceptions/{id} — resolve an exception

Exception workflow vocabulary is immutable after create time:
- category: PRECONDITION_FAILURE | VALIDATION_FAILURE | TRANSIENT_SUBMISSION_FAILURE | INTERPRETATION_FAILURE
- reason_code: closed vocabulary mapping to owners and categories (see models/exception.py)
- owner: orderer | lab_operator (determined by reason_code)
- status: open | resolved

NOTE: These endpoints assume a SessionMiddleware or equivalent has injected
an authenticated session into the request context. The session is used to
enforce organisation isolation (org-scoped queries).
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator, Optional

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, status
from pydantic import BaseModel

from clinical.data_access import (
    GENERIC_AUTH_ERROR,
    AuthenticationError,
    DataAccess,
    NotFoundError,
    Session,
)

router = APIRouter(prefix="/exceptions", tags=["exceptions"])


class ExceptionListItem(BaseModel):
    """Exception summary for list responses."""

    id: str
    order_id: str
    category: str
    reason_code: str
    error_message: Optional[str] = None
    status: str
    owner: str
    created_at: str
    last_resolved_by: Optional[str] = None
    last_resolved_at: Optional[str] = None
    resolution_action: Optional[str] = None
    resolution_note: Optional[str] = None


class ExceptionEventItem(BaseModel):
    """Exception event (open, resolve, reopen)."""

    id: str
    action: str
    actor: str
    timestamp: str
    action_note: Optional[str] = None


class ExceptionDetail(ExceptionListItem):
    """Exception with full event history."""

    events: list[ExceptionEventItem] = []


class WorklistFilters(BaseModel):
    """Optional filters for worklist query."""

    owner: Optional[str] = None
    category: Optional[str] = None


class ResolutionRequest(BaseModel):
    """
    Request body for PATCH /exceptions/{id}.

    THERE IS NO `actor` FIELD, AND ITS ABSENCE IS THE POINT. It used to carry
    one, and whatever the caller typed was written to `exceptions.
    last_resolved_by` and to the `exception_events` row -- so the same
    statement scoped the update by something trusted (`session.org_id`) and
    attributed it by something the caller supplied. Under ISO 15189 the record
    of who performed an action is evidence, and a claimed actor is
    indistinguishable in the log from a verified one, which makes it worse
    than a missing one: the audit trail looks complete.

    The actor is now derived from the authenticated session. Validating a
    caller-supplied actor would not have been enough -- a validated claim is
    still a claim -- so the field is removed rather than checked. This is the
    rule `Session`'s own docstring already states for `org_id`: read from the
    session and never from a caller argument, "which is what makes the
    isolation structural rather than remembered". The actor now obeys the rule
    the organisation already obeyed.
    """

    resolution_action: str  # "retry_check" or "accept_failure"
    resolution_note: str


class ErrorResponse(BaseModel):
    """Error response."""

    detail: str


_SESSION_COOKIE = "clinical_session"
_DSN_ENV = "CLINICAL_DSN"


def _get_data_access() -> Iterator[DataAccess]:
    """
    FastAPI dependency: a DataAccess for the lifetime of ONE request.

    REQUEST-SCOPED, AND THAT IS A RULING, NOT A DEFAULT. This function used to
    raise NotImplementedError under a docstring saying "initialize DataAccess
    with postgres connection pool, cache as singleton". That plan cannot be
    safely executed with `DataAccess` as written, and implementing it
    faithfully would have shipped a correctness defect:

      `data_access.transactional` gates on INSTANCE state -- `if
      self._in_transaction: return func(...)`, documented as "nested calls
      within a transaction pass through". That is right for a call chain
      inside one request. With ONE instance shared across concurrent
      requests it is not: a second request arriving while the first holds the
      flag is treated as a nested call, opens no transaction of its own, and
      request A's `commit()` commits request B's half-finished work -- then
      `finally: self._in_transaction = False` clears the flag while B is
      still running, so the next request misjudges too. On the path whose
      entire purpose is recording who did what.

    Nothing in the suite would have caught that: no test exercises two
    concurrent requests. The decorator is not wrong; the singleton plan was,
    and it sat in a placeholder where it read as a decision someone had made.

    A connection pool is the production form of this same answer --
    `pool.connection()` per request -- so nothing here forecloses one. What
    must not come back is a shared, cached instance.
    """
    dsn = os.environ.get(_DSN_ENV)
    if not dsn:
        # A configuration gap, not a client error: 503 rather than 500 says
        # the service is not ready rather than that the request was bad.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{_DSN_ENV} is not configured; the clinical database is unavailable.",
        )

    import psycopg

    connection = psycopg.connect(dsn, autocommit=False)
    try:
        yield DataAccess(connection)
    finally:
        connection.close()


def _get_session(
    authorization: str = Header(default=""),
    clinical_session: str = Cookie(default="", alias=_SESSION_COOKIE),
    dao: DataAccess = Depends(_get_data_access),
) -> Session:
    """
    FastAPI dependency: the authenticated session, or 401.

    Reads an opaque session id from the `clinical_session` cookie or an
    `Authorization: Bearer <id>` header, and validates it through
    `DataAccess.session_valid`, which refreshes the idle window and raises for
    a session that is terminated, past its absolute cap, or idle too long.

    EVERY FAILURE RETURNS THE SAME MESSAGE. A malformed id, an unknown id, an
    expired one and a terminated one are indistinguishable to the caller --
    `data_access.GENERIC_AUTH_ERROR`, which that module already uses for
    exactly this reason. Distinguishing them would tell an unauthenticated
    caller which session ids exist.

    Depends on `_get_data_access`, so within one request the session lookup
    and the endpoint body share one connection and one transaction: FastAPI
    caches a dependency's result per request, so this does not open a second.
    """
    raw = clinical_session
    if not raw and authorization.lower().startswith("bearer "):
        raw = authorization[len("bearer ") :].strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=GENERIC_AUTH_ERROR,
        )

    try:
        session_id = uuid.UUID(raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=GENERIC_AUTH_ERROR,
        )

    try:
        return dao.session_valid(session_id)
    except AuthenticationError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=GENERIC_AUTH_ERROR,
        )


@router.get(
    "/orders/{order_id}",
    response_model=list[ExceptionDetail],
    responses={
        200: {"description": "List of exceptions (open and resolved)"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        404: {"model": ErrorResponse, "description": "Order not found in this organisation"},
        500: {"model": ErrorResponse, "description": "Server error"},
    },
)
async def get_exceptions_for_order(
    order_id: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> list[ExceptionDetail]:
    """
    Get all exceptions (open + resolved) for an order.

    Returns exceptions in created_at DESC order. Org-scoped: only returns
    exceptions for orders in the session's organisation.

    Args:
        order_id: UUID of the order

    Returns:
        List of ExceptionDetail objects (each with event history)

    Raises:
        401: Unauthenticated
        404: Order not found in this organisation
        500: Database error
    """
    try:
        order_uuid = uuid.UUID(order_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid order_id: {order_id}",
        )

    try:
        # Check order exists
        order = dao.get_order(session, order_uuid)
        if order is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Order {order_id} not found",
            )

        # Get exceptions with event history
        exception_dtos = dao.get_exceptions_for_order(session, order_uuid)

        # Convert DTOs to response models
        result = []
        for exc_dto in exception_dtos:
            result.append(
                ExceptionDetail(
                    id=exc_dto.id,
                    order_id=exc_dto.order_id,
                    category=exc_dto.category,
                    reason_code=exc_dto.reason_code,
                    error_message=exc_dto.error_message,
                    status=exc_dto.status,
                    owner=exc_dto.owner,
                    created_at=exc_dto.created_at,
                    last_resolved_by=exc_dto.last_resolved_by,
                    last_resolved_at=exc_dto.last_resolved_at,
                    resolution_action=exc_dto.resolution_action,
                    resolution_note=exc_dto.resolution_note,
                    events=[
                        ExceptionEventItem(
                            id=evt.id,
                            action=evt.action,
                            actor=evt.actor,
                            timestamp=evt.timestamp,
                            action_note=evt.action_note,
                        )
                        for evt in exc_dto.events
                    ],
                )
            )

        return result

    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving exceptions: {str(e)}",
        )


@router.get(
    "/worklist",
    response_model=list[ExceptionDetail],
    responses={
        200: {"description": "Open exceptions for owner"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        400: {"model": ErrorResponse, "description": "Missing owner parameter"},
        500: {"model": ErrorResponse, "description": "Server error"},
    },
)
async def get_exceptions_worklist(
    owner: str,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> list[ExceptionDetail]:
    """
    Get open exceptions for a given owner role.

    Returns open (not resolved) exceptions owned by the specified role,
    sorted by created_at ASC (oldest first). Org-scoped: only returns
    exceptions from the session's organisation.

    Args:
        owner: Owner role (e.g., "orderer", "lab_operator")

    Returns:
        List of ExceptionDetail objects for open exceptions

    Raises:
        401: Unauthenticated
        400: Missing owner parameter
        500: Database error
    """
    if not owner or not owner.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="owner parameter is required",
        )

    try:
        # Get open exceptions by owner
        exception_dtos = dao.get_open_exceptions_by_owner(session, owner)

        # Convert DTOs to response models
        result = []
        for exc_dto in exception_dtos:
            result.append(
                ExceptionDetail(
                    id=exc_dto.id,
                    order_id=exc_dto.order_id,
                    category=exc_dto.category,
                    reason_code=exc_dto.reason_code,
                    error_message=exc_dto.error_message,
                    status=exc_dto.status,
                    owner=exc_dto.owner,
                    created_at=exc_dto.created_at,
                    last_resolved_by=exc_dto.last_resolved_by,
                    last_resolved_at=exc_dto.last_resolved_at,
                    resolution_action=exc_dto.resolution_action,
                    resolution_note=exc_dto.resolution_note,
                    events=[
                        ExceptionEventItem(
                            id=evt.id,
                            action=evt.action,
                            actor=evt.actor,
                            timestamp=evt.timestamp,
                            action_note=evt.action_note,
                        )
                        for evt in exc_dto.events
                    ],
                )
            )

        return result

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving worklist: {str(e)}",
        )


@router.patch(
    "/{exception_id}",
    response_model=ExceptionDetail,
    responses={
        200: {"description": "Exception resolved"},
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        404: {"model": ErrorResponse, "description": "Exception not found"},
        400: {"model": ErrorResponse, "description": "Invalid exception_id"},
        500: {"model": ErrorResponse, "description": "Server error"},
    },
)
async def resolve_exception(
    exception_id: str,
    body: ResolutionRequest,
    session: Session = Depends(_get_session),
    dao: DataAccess = Depends(_get_data_access),
) -> ExceptionDetail:
    """
    Resolve an exception, recording resolution action and note.

    Transitions exception from open to resolved, creates a RESOLVE event,
    and records the resolution action + note. Org-scoped: can only resolve
    exceptions in the session's organisation.

    Args:
        exception_id: UUID of the exception to resolve
        body: Resolution request with action and note

    Returns:
        Updated ExceptionDetail with resolved status and RESOLVE event

    Raises:
        401: Unauthenticated
        404: Exception not found in this organisation
        400: Invalid exception_id or request body
        500: Database error
    """
    try:
        exc_uuid = uuid.UUID(exception_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid exception_id: {exception_id}",
        )

    if not body.resolution_action:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="resolution_action is required",
        )

    if not body.resolution_note:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="resolution_note is required",
        )

    try:
        # Resolve the exception
        # The actor is the authenticated user, never a caller-supplied
        # string -- see ResolutionRequest for why the field was removed rather
        # than validated. The id and not an email: a display string can be
        # re-pointed at a different person, an id cannot, and this is evidence.
        dao.resolve_exception(
            session,
            exc_uuid,
            body.resolution_action,
            body.resolution_note,
            str(session.user_id),
        )

        # TODO: After resolution, re-run preconditions or submission
        # depending on the resolution_action. This is a separate workflow
        # that may trigger a retry or may mark the order as closed.

        # Retrieve updated exception to return
        resolved_exc = dao.get_exception_by_id(session, exc_uuid)
        if resolved_exc is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Exception resolved but could not retrieve updated state",
            )

        return ExceptionDetail(
            id=resolved_exc.id,
            order_id=resolved_exc.order_id,
            category=resolved_exc.category,
            reason_code=resolved_exc.reason_code,
            error_message=resolved_exc.error_message,
            status=resolved_exc.status,
            owner=resolved_exc.owner,
            created_at=resolved_exc.created_at,
            last_resolved_by=resolved_exc.last_resolved_by,
            last_resolved_at=resolved_exc.last_resolved_at,
            resolution_action=resolved_exc.resolution_action,
            resolution_note=resolved_exc.resolution_note,
            events=[
                ExceptionEventItem(
                    id=evt.id,
                    action=evt.action,
                    actor=evt.actor,
                    timestamp=evt.timestamp,
                    action_note=evt.action_note,
                )
                for evt in resolved_exc.events
            ],
        )

    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Exception {exception_id} not found",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error resolving exception: {str(e)}",
        )
