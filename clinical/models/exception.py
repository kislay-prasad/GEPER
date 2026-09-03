"""
clinical/models/exception.py
────────────────────────────
Exception workflow — vocabulary and enums for blocked orders.

Exceptions are work items (owned, stateful, visible), not log lines.
Each exception tracks why an order is blocked, who owns resolution, and the
resolution history. Reason codes map to owners; reopening on same reason is
tracked per exception to show repeated failures.

Schema:
  CREATE TABLE exceptions (
    id UUID PRIMARY KEY,
    org_id TEXT NOT NULL,
    order_id UUID NOT NULL REFERENCES orders(id),
    category TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    error_message TEXT,
    status TEXT NOT NULL,
    owner TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    last_resolved_by TEXT,
    last_resolved_at TIMESTAMPTZ,
    resolution_action TEXT,
    resolution_note TEXT
  );

  CREATE TABLE exception_events (
    id UUID PRIMARY KEY,
    exception_id UUID NOT NULL REFERENCES exceptions(id),
    actor TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    action TEXT NOT NULL,
    action_note TEXT
  );

Exception methods belong in clinical/data_access.py following the existing
org-isolation pattern (Session-based, org_id from session, no raw queries).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


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


# Reason code to owner mapping (immutable after creation)
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

# Reason code to category mapping (immutable after creation)
REASON_CODE_TO_CATEGORY = {
    # Precondition failures (consent-related)
    ExceptionReasonCode.CONSENT_MISSING: ExceptionCategory.PRECONDITION_FAILURE,
    ExceptionReasonCode.CONSENT_WITHDRAWN: ExceptionCategory.PRECONDITION_FAILURE,
    # Validation failures (data quality issues)
    ExceptionReasonCode.SAMPLE_NOT_FOUND: ExceptionCategory.VALIDATION_FAILURE,
    ExceptionReasonCode.SAMPLE_COLUMN_MISSING: ExceptionCategory.VALIDATION_FAILURE,
    ExceptionReasonCode.VARIANT_COUNT_ZERO: ExceptionCategory.VALIDATION_FAILURE,
    ExceptionReasonCode.VCF_HEADER_INVALID: ExceptionCategory.VALIDATION_FAILURE,
    ExceptionReasonCode.VCF_INVALID: ExceptionCategory.VALIDATION_FAILURE,
    ExceptionReasonCode.ASSEMBLY_MISMATCH: ExceptionCategory.VALIDATION_FAILURE,
    # Submission failures
    ExceptionReasonCode.BIJ_AI_TIMEOUT: ExceptionCategory.TRANSIENT_SUBMISSION_FAILURE,
    ExceptionReasonCode.BIJ_AI_ERROR_OTHER: ExceptionCategory.INTERPRETATION_FAILURE,
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
