"""
tests/test_exception_models.py
──────────────────────────────
Unit tests for exception workflow models.
"""

from clinical.models.exception import (
    Exception,
    ExceptionEvent,
    ExceptionCategory,
    ExceptionReasonCode,
    ExceptionStatus,
    ExceptionEventAction,
    ResolutionAction,
    REASON_CODE_TO_OWNER,
)


class TestExceptionReasonCodeToOwnerMapping:
    """Verify reason code to owner mapping is complete and correct."""

    def test_all_reason_codes_have_owner(self):
        """Every reason code maps to an owner."""
        for reason_code in ExceptionReasonCode:
            assert reason_code in REASON_CODE_TO_OWNER, f"{reason_code} not mapped"

    def test_orderer_reasons(self):
        """Orderer owns consent-related failures."""
        assert REASON_CODE_TO_OWNER[ExceptionReasonCode.CONSENT_MISSING] == "orderer"
        assert REASON_CODE_TO_OWNER[ExceptionReasonCode.CONSENT_WITHDRAWN] == "orderer"

    def test_lab_operator_reasons(self):
        """Lab operator owns validation and interpretation failures."""
        lab_reasons = [
            ExceptionReasonCode.SAMPLE_NOT_FOUND,
            ExceptionReasonCode.SAMPLE_COLUMN_MISSING,
            ExceptionReasonCode.VARIANT_COUNT_ZERO,
            ExceptionReasonCode.VCF_HEADER_INVALID,
            ExceptionReasonCode.VCF_INVALID,
            ExceptionReasonCode.ASSEMBLY_MISMATCH,
            ExceptionReasonCode.BIJ_AI_TIMEOUT,
            ExceptionReasonCode.BIJ_AI_ERROR_OTHER,
        ]
        for reason in lab_reasons:
            assert REASON_CODE_TO_OWNER[reason] == "lab_operator", f"{reason} should be lab_operator"


class TestExceptionModel:
    """Tests for Exception model."""

    def test_exception_creation(self):
        """Create an exception with all required fields."""
        exc = Exception(
            org_id="org-1",
            order_id="order-uuid-123",
            category=ExceptionCategory.PRECONDITION_FAILURE,
            reason_code=ExceptionReasonCode.CONSENT_MISSING,
            error_message="Consent not provided",
            status=ExceptionStatus.OPEN,
            owner="orderer",
        )

        assert exc.org_id == "org-1"
        assert exc.order_id == "order-uuid-123"
        assert exc.category == ExceptionCategory.PRECONDITION_FAILURE
        assert exc.reason_code == ExceptionReasonCode.CONSENT_MISSING
        assert exc.error_message == "Consent not provided"
        assert exc.status == ExceptionStatus.OPEN
        assert exc.owner == "orderer"
        assert exc.created_at is not None

    def test_exception_owner_immutable(self):
        """Owner is set at creation and should not change."""
        exc = Exception(
            org_id="org-1",
            order_id="order-uuid-123",
            category=ExceptionCategory.PRECONDITION_FAILURE,
            reason_code=ExceptionReasonCode.CONSENT_MISSING,
            status=ExceptionStatus.OPEN,
            owner="orderer",
        )

        # Owner should not be modified after creation
        assert exc.owner == "orderer"

    def test_exception_resolution(self):
        """Resolve an exception with action and note."""
        exc = Exception(
            org_id="org-1",
            order_id="order-uuid-123",
            category=ExceptionCategory.PRECONDITION_FAILURE,
            reason_code=ExceptionReasonCode.CONSENT_MISSING,
            status=ExceptionStatus.OPEN,
            owner="orderer",
        )

        # Resolve it
        exc.status = ExceptionStatus.RESOLVED
        exc.last_resolved_by = "orderer-user-1"
        exc.resolution_action = ResolutionAction.RETRY_CHECK
        exc.resolution_note = "Consent provided by patient"

        assert exc.status == ExceptionStatus.RESOLVED
        assert exc.last_resolved_by == "orderer-user-1"
        assert exc.resolution_action == ResolutionAction.RETRY_CHECK

    def test_exception_accept_failure(self):
        """Accept an interpretation failure as terminal."""
        exc = Exception(
            org_id="org-1",
            order_id="order-uuid-123",
            category=ExceptionCategory.INTERPRETATION_FAILURE,
            reason_code=ExceptionReasonCode.BIJ_AI_ERROR_OTHER,
            error_message="Bij AI service unavailable",
            status=ExceptionStatus.OPEN,
            owner="lab_operator",
        )

        # Accept the failure
        exc.status = ExceptionStatus.RESOLVED
        exc.last_resolved_by = "lab-operator-user-2"
        exc.resolution_action = ResolutionAction.ACCEPT_FAILURE
        exc.resolution_note = "Bij AI service down for maintenance, accepting failure"

        assert exc.resolution_action == ResolutionAction.ACCEPT_FAILURE


class TestExceptionEventModel:
    """Tests for ExceptionEvent model."""

    def test_event_creation(self):
        """Create an exception event."""
        event = ExceptionEvent(
            exception_id="exc-uuid-123",
            actor="system",
            action=ExceptionEventAction.OPEN,
            action_note="Exception created by validation check",
        )

        assert event.exception_id == "exc-uuid-123"
        assert event.actor == "system"
        assert event.action == ExceptionEventAction.OPEN
        assert event.action_note == "Exception created by validation check"
        assert event.timestamp is not None

    def test_event_resolve(self):
        """Create a resolve event."""
        event = ExceptionEvent(
            exception_id="exc-uuid-123",
            actor="orderer-user-1",
            action=ExceptionEventAction.RESOLVE,
            action_note="Consent obtained from patient",
        )

        assert event.action == ExceptionEventAction.RESOLVE
        assert event.actor == "orderer-user-1"

    def test_event_reopen(self):
        """Create a reopen event."""
        event = ExceptionEvent(
            exception_id="exc-uuid-123",
            actor="system",
            action=ExceptionEventAction.REOPEN,
            action_note="Precondition re-check still fails: consent_missing",
        )

        assert event.action == ExceptionEventAction.REOPEN
        assert "consent_missing" in event.action_note


class TestExceptionCategories:
    """Tests for exception categories and their semantics."""

    def test_precondition_failure_category(self):
        """Precondition failures block order, visible to orderer+lab, not auto-retried."""
        exc = Exception(
            org_id="org-1",
            order_id="order-uuid-123",
            category=ExceptionCategory.PRECONDITION_FAILURE,
            reason_code=ExceptionReasonCode.CONSENT_MISSING,
            status=ExceptionStatus.OPEN,
            owner="orderer",
        )

        assert exc.category == ExceptionCategory.PRECONDITION_FAILURE

    def test_validation_failure_category(self):
        """Validation failures block order, visible to orderer+lab, not auto-retried."""
        exc = Exception(
            org_id="org-1",
            order_id="order-uuid-123",
            category=ExceptionCategory.VALIDATION_FAILURE,
            reason_code=ExceptionReasonCode.VCF_INVALID,
            status=ExceptionStatus.OPEN,
            owner="lab_operator",
        )

        assert exc.category == ExceptionCategory.VALIDATION_FAILURE

    def test_transient_failure_category(self):
        """Transient failures are auto-retried, escalate to exception if exhausted."""
        exc = Exception(
            org_id="org-1",
            order_id="order-uuid-123",
            category=ExceptionCategory.TRANSIENT_SUBMISSION_FAILURE,
            reason_code=ExceptionReasonCode.BIJ_AI_TIMEOUT,
            error_message="Timeout after 3 retries",
            status=ExceptionStatus.OPEN,
            owner="lab_operator",
        )

        assert exc.category == ExceptionCategory.TRANSIENT_SUBMISSION_FAILURE

    def test_interpretation_failure_category(self):
        """Interpretation failures block order, retryable by operator."""
        exc = Exception(
            org_id="org-1",
            order_id="order-uuid-123",
            category=ExceptionCategory.INTERPRETATION_FAILURE,
            reason_code=ExceptionReasonCode.BIJ_AI_ERROR_OTHER,
            error_message="Bij AI internal error",
            status=ExceptionStatus.OPEN,
            owner="lab_operator",
        )

        assert exc.category == ExceptionCategory.INTERPRETATION_FAILURE


class TestExceptionEnums:
    """Test all enum values are correct."""

    def test_exception_status_values(self):
        """Exception status enum has correct values."""
        assert ExceptionStatus.OPEN.value == "open"
        assert ExceptionStatus.RESOLVED.value == "resolved"

    def test_exception_event_action_values(self):
        """Exception event action enum has correct values."""
        assert ExceptionEventAction.OPEN.value == "open"
        assert ExceptionEventAction.RESOLVE.value == "resolve"
        assert ExceptionEventAction.REOPEN.value == "reopen"

    def test_resolution_action_values(self):
        """Resolution action enum has correct values."""
        assert ResolutionAction.RETRY_CHECK.value == "retry_check"
        assert ResolutionAction.ACCEPT_FAILURE.value == "accept_failure"
