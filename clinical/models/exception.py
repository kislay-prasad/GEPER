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

    # Validation failures (lab_operator owns) — identity checks
    PATIENT_UNRESOLVED = "patient_unresolved"
    SAMPLE_UNRESOLVED = "sample_unresolved"

    # Validation failures (lab_operator owns) — order checks
    INDICATION_MISSING = "indication_missing"
    ORDER_CANCELLED = "order_cancelled"

    # Validation failures (lab_operator owns) — QC checks
    QC_PENDING = "qc_pending"
    QC_FAILED = "qc_failed"

    # Validation failures (lab_operator owns) — VCF file checks
    VCF_MISSING = "vcf_missing"
    VCF_UNREADABLE = "vcf_unreadable"
    VCF_FORMAT_INVALID = "vcf_format_invalid"

    # Validation failures (lab_operator owns) — VCF header checks
    VCF_HEADER_MISSING = "vcf_header_missing"
    VCF_HEADER_MALFORMED = "vcf_header_malformed"

    # Validation failures (lab_operator owns) — VCF assembly checks
    VCF_BUILD_NOT_DECLARED = "vcf_build_not_declared"
    VCF_BUILD_NOT_RECOGNISED = "vcf_build_not_recognised"
    VCF_BUILD_MISMATCH = "vcf_build_mismatch"

    # Validation failures (lab_operator owns) — VCF sample column checks
    VCF_SAMPLE_COLUMN_MISSING = "vcf_sample_column_missing"

    # Validation failures (lab_operator owns) — VCF variant count checks
    VCF_NO_VARIANTS = "vcf_no_variants"

    # Submission failures (lab_operator owns)
    BIJ_AI_TIMEOUT = "bij_ai_timeout"
    BIJ_AI_ERROR_OTHER = "bij_ai_error_other"


# Reason code to owner mapping (immutable after creation)
REASON_CODE_TO_OWNER = {
    # Precondition failures (orderer)
    ExceptionReasonCode.CONSENT_MISSING: "orderer",
    ExceptionReasonCode.CONSENT_WITHDRAWN: "orderer",
    # Validation failures (lab_operator)
    ExceptionReasonCode.PATIENT_UNRESOLVED: "lab_operator",
    ExceptionReasonCode.SAMPLE_UNRESOLVED: "lab_operator",
    ExceptionReasonCode.INDICATION_MISSING: "lab_operator",
    ExceptionReasonCode.ORDER_CANCELLED: "lab_operator",
    ExceptionReasonCode.QC_PENDING: "lab_operator",
    ExceptionReasonCode.QC_FAILED: "lab_operator",
    ExceptionReasonCode.VCF_MISSING: "lab_operator",
    ExceptionReasonCode.VCF_UNREADABLE: "lab_operator",
    ExceptionReasonCode.VCF_FORMAT_INVALID: "lab_operator",
    ExceptionReasonCode.VCF_HEADER_MISSING: "lab_operator",
    ExceptionReasonCode.VCF_HEADER_MALFORMED: "lab_operator",
    ExceptionReasonCode.VCF_BUILD_NOT_DECLARED: "lab_operator",
    ExceptionReasonCode.VCF_BUILD_NOT_RECOGNISED: "lab_operator",
    ExceptionReasonCode.VCF_BUILD_MISMATCH: "lab_operator",
    ExceptionReasonCode.VCF_SAMPLE_COLUMN_MISSING: "lab_operator",
    ExceptionReasonCode.VCF_NO_VARIANTS: "lab_operator",
    # Submission failures (lab_operator)
    ExceptionReasonCode.BIJ_AI_TIMEOUT: "lab_operator",
    ExceptionReasonCode.BIJ_AI_ERROR_OTHER: "lab_operator",
}

# Reason code to category mapping (immutable after creation)
REASON_CODE_TO_CATEGORY = {
    # Precondition failures (consent-related)
    ExceptionReasonCode.CONSENT_MISSING: ExceptionCategory.PRECONDITION_FAILURE,
    ExceptionReasonCode.CONSENT_WITHDRAWN: ExceptionCategory.PRECONDITION_FAILURE,
    # Validation failures (data quality issues) — identity
    ExceptionReasonCode.PATIENT_UNRESOLVED: ExceptionCategory.VALIDATION_FAILURE,
    ExceptionReasonCode.SAMPLE_UNRESOLVED: ExceptionCategory.VALIDATION_FAILURE,
    # Validation failures (data quality issues) — order
    ExceptionReasonCode.INDICATION_MISSING: ExceptionCategory.VALIDATION_FAILURE,
    ExceptionReasonCode.ORDER_CANCELLED: ExceptionCategory.VALIDATION_FAILURE,
    # Validation failures (data quality issues) — QC
    ExceptionReasonCode.QC_PENDING: ExceptionCategory.VALIDATION_FAILURE,
    ExceptionReasonCode.QC_FAILED: ExceptionCategory.VALIDATION_FAILURE,
    # Validation failures (data quality issues) — VCF file
    ExceptionReasonCode.VCF_MISSING: ExceptionCategory.VALIDATION_FAILURE,
    ExceptionReasonCode.VCF_UNREADABLE: ExceptionCategory.VALIDATION_FAILURE,
    ExceptionReasonCode.VCF_FORMAT_INVALID: ExceptionCategory.VALIDATION_FAILURE,
    # Validation failures (data quality issues) — VCF header
    ExceptionReasonCode.VCF_HEADER_MISSING: ExceptionCategory.VALIDATION_FAILURE,
    ExceptionReasonCode.VCF_HEADER_MALFORMED: ExceptionCategory.VALIDATION_FAILURE,
    # Validation failures (data quality issues) — VCF assembly
    ExceptionReasonCode.VCF_BUILD_NOT_DECLARED: ExceptionCategory.VALIDATION_FAILURE,
    ExceptionReasonCode.VCF_BUILD_NOT_RECOGNISED: ExceptionCategory.VALIDATION_FAILURE,
    ExceptionReasonCode.VCF_BUILD_MISMATCH: ExceptionCategory.VALIDATION_FAILURE,
    # Validation failures (data quality issues) — VCF sample column
    ExceptionReasonCode.VCF_SAMPLE_COLUMN_MISSING: ExceptionCategory.VALIDATION_FAILURE,
    # Validation failures (data quality issues) — VCF variant count
    ExceptionReasonCode.VCF_NO_VARIANTS: ExceptionCategory.VALIDATION_FAILURE,
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
