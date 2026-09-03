"""
clinical/models/exception.py
────────────────────────────
Exception workflow — clinical records for blocked orders.

Exceptions are work items (owned, stateful, visible), not log lines.
Each exception tracks why an order is blocked, who owns resolution, and the
resolution history. Reason codes map to owners; reopening on same reason is
tracked per exception to show repeated failures.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, DateTime, Enum, ForeignKey, String, Text, UUID
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class ExceptionCategory(str, enum.Enum):
    """Why the order is blocked."""

    PRECONDITION_FAILURE = "precondition_failure"
    VALIDATION_FAILURE = "validation_failure"
    TRANSIENT_SUBMISSION_FAILURE = "transient_submission_failure"
    INTERPRETATION_FAILURE = "interpretation_failure"


class ExceptionReasonCode(str, enum.Enum):
    """Closed vocabulary of failure reasons, each mapping to an owner."""

    # Precondition failures (orderer owns)
    CONSENT_MISSING = "consent_missing"
    CONSENT_WITHDRAWN = "consent_withdrawn"

    # Validation failures (lab_operator owns)
    SAMPLE_NOT_FOUND = "sample_not_found"
    SAMPLE_COLUMN_MISSING = "sample_column_missing"
    VARIANT_COUNT_ZERO = "variant_count_zero"
    VCF_HEADER_INVALID = "vcf_header_invalid"
    VCF_INVALID = "vcf_invalid"
    ASSEMBLY_MISMATCH = "assembly_mismatch"

    # Submission failures (lab_operator owns)
    BIJ_AI_TIMEOUT = "bij_ai_timeout"
    BIJ_AI_ERROR_OTHER = "bij_ai_error_other"


# Reason code to owner mapping
REASON_CODE_TO_OWNER = {
    ExceptionReasonCode.CONSENT_MISSING: "orderer",
    ExceptionReasonCode.CONSENT_WITHDRAWN: "orderer",
    ExceptionReasonCode.SAMPLE_NOT_FOUND: "lab_operator",
    ExceptionReasonCode.SAMPLE_COLUMN_MISSING: "lab_operator",
    ExceptionReasonCode.VARIANT_COUNT_ZERO: "lab_operator",
    ExceptionReasonCode.VCF_HEADER_INVALID: "lab_operator",
    ExceptionReasonCode.VCF_INVALID: "lab_operator",
    ExceptionReasonCode.ASSEMBLY_MISMATCH: "lab_operator",
    ExceptionReasonCode.BIJ_AI_TIMEOUT: "lab_operator",
    ExceptionReasonCode.BIJ_AI_ERROR_OTHER: "lab_operator",
}


class ExceptionEventAction(str, enum.Enum):
    """What happened to the exception."""

    OPEN = "open"
    RESOLVE = "resolve"
    REOPEN = "reopen"


class ExceptionStatus(str, enum.Enum):
    """Current state of the exception."""

    OPEN = "open"
    RESOLVED = "resolved"


class ResolutionAction(str, enum.Enum):
    """How the exception was resolved."""

    RETRY_CHECK = "retry_check"
    ACCEPT_FAILURE = "accept_failure"


class Exception(Base):
    """
    An exception is a work item: an order blocked on a specific reason,
    owned by someone, with resolution history.

    Owner is derived from reason_code at creation and immutable.
    Reopening on the same reason increments the reopen count and logs
    the event in exception_events.
    """

    __tablename__ = "exceptions"

    id = Column(UUID, primary_key=True, default=uuid.uuid4)
    org_id = Column(String(255), nullable=False, index=True)
    order_id = Column(UUID, ForeignKey("orders.id"), nullable=False, index=True)

    category = Column(Enum(ExceptionCategory), nullable=False)
    reason_code = Column(Enum(ExceptionReasonCode), nullable=False)
    error_message = Column(Text, nullable=True)

    status = Column(Enum(ExceptionStatus), nullable=False, default=ExceptionStatus.OPEN)
    owner = Column(String(50), nullable=False)  # "orderer" or "lab_operator"

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_resolved_by = Column(String(255), nullable=True)
    last_resolved_at = Column(DateTime, nullable=True)
    resolution_action = Column(Enum(ResolutionAction), nullable=True)
    resolution_note = Column(Text, nullable=True)

    # Relationships
    events = relationship("ExceptionEvent", back_populates="exception", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Exception {self.id} {self.reason_code} owner={self.owner} status={self.status}>"


class ExceptionEvent(Base):
    """
    Audit trail for an exception: every open, resolve, reopen with actor,
    timestamp, and note. Tracks who resolved it and what they thought they fixed.
    """

    __tablename__ = "exception_events"

    id = Column(UUID, primary_key=True, default=uuid.uuid4)
    exception_id = Column(UUID, ForeignKey("exceptions.id"), nullable=False, index=True)

    actor = Column(String(255), nullable=False)  # "system" or user_id
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)
    action = Column(Enum(ExceptionEventAction), nullable=False)
    action_note = Column(Text, nullable=True)

    # Relationships
    exception = relationship("Exception", back_populates="events")

    def __repr__(self):
        return f"<ExceptionEvent {self.id} {self.action} by {self.actor}>"


@dataclass
class ExceptionDTO:
    """Data transfer object for exception responses."""

    id: str
    order_id: str
    category: str
    reason_code: str
    error_message: Optional[str]
    status: str
    owner: str
    created_at: str
    last_resolved_by: Optional[str]
    last_resolved_at: Optional[str]
    resolution_action: Optional[str]
    resolution_note: Optional[str]
    events: list["ExceptionEventDTO"]


@dataclass
class ExceptionEventDTO:
    """Data transfer object for exception events."""

    id: str
    action: str
    actor: str
    timestamp: str
    action_note: Optional[str]
