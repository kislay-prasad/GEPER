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

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from clinical.data_access import DataAccess, NotFoundError, Session

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
    """Request body for PATCH /exceptions/{id}."""

    resolution_action: str  # "retry_check" or "accept_failure"
    resolution_note: str
    actor: str  # Who is resolving (username, role, system principal)


class ErrorResponse(BaseModel):
    """Error response."""

    detail: str


def _get_session() -> Session:
    """
    FastAPI dependency: inject authenticated session.

    IMPLEMENTATION NOTE: This is a placeholder. Actual implementation should:
    - Extract session_id from cookie or Authorization header
    - Call DataAccess.session_valid(session_id) to get Session object
    - Return Session or raise HTTPException(401)

    For now, raises NotImplementedError to make the signature explicit.
    """
    raise NotImplementedError(
        "SessionMiddleware not yet implemented. This endpoint requires authenticated session injection."
    )


def _get_data_access() -> DataAccess:
    """
    FastAPI dependency: inject DataAccess instance.

    IMPLEMENTATION NOTE: This is a placeholder. Actual implementation should:
    - Initialize DataAccess with postgres connection pool
    - Cache as singleton
    - Return instance

    For now, raises NotImplementedError to make the signature explicit.
    """
    raise NotImplementedError("DataAccess dependency not yet configured. This endpoint requires database connection.")


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

    if not body.actor:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="actor is required",
        )

    try:
        # Resolve the exception
        dao.resolve_exception(
            session,
            exc_uuid,
            body.resolution_action,
            body.resolution_note,
            body.actor,
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
