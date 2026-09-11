"""
tests/test_exception_models.py
──────────────────────────────
Unit tests for exception workflow vocabulary and enums.
"""

from clinical.models.exception import (
    ExceptionCategory,
    ExceptionReasonCode,
    ExceptionStatus,
    ExceptionEventAction,
    ResolutionAction,
    REASON_CODE_TO_CATEGORY,
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


class TestExceptionCategories:
    """Tests for exception categories."""

    def test_precondition_failure_exists(self):
        """Precondition failure category exists."""
        assert ExceptionCategory.PRECONDITION_FAILURE.value == "precondition_failure"

    def test_validation_failure_exists(self):
        """Validation failure category exists."""
        assert ExceptionCategory.VALIDATION_FAILURE.value == "validation_failure"

    def test_transient_submission_failure_exists(self):
        """Transient submission failure category exists."""
        assert ExceptionCategory.TRANSIENT_SUBMISSION_FAILURE.value == "transient_submission_failure"

    def test_interpretation_failure_exists(self):
        """Interpretation failure category exists."""
        assert ExceptionCategory.INTERPRETATION_FAILURE.value == "interpretation_failure"


class TestExceptionReasonCodes:
    """Tests for all exception reason codes."""

    def test_consent_missing_exists(self):
        """Consent missing reason code exists."""
        assert ExceptionReasonCode.CONSENT_MISSING.value == "consent_missing"

    def test_consent_withdrawn_exists(self):
        """Consent withdrawn reason code exists."""
        assert ExceptionReasonCode.CONSENT_WITHDRAWN.value == "consent_withdrawn"

    def test_sample_not_found_exists(self):
        """Sample not found reason code exists."""
        assert ExceptionReasonCode.SAMPLE_NOT_FOUND.value == "sample_not_found"

    def test_vcf_invalid_exists(self):
        """VCF invalid reason code exists."""
        assert ExceptionReasonCode.VCF_INVALID.value == "vcf_invalid"

    def test_bij_ai_timeout_exists(self):
        """Bij AI timeout reason code exists."""
        assert ExceptionReasonCode.BIJ_AI_TIMEOUT.value == "bij_ai_timeout"

    def test_bij_ai_error_other_exists(self):
        """Bij AI error (other) reason code exists."""
        assert ExceptionReasonCode.BIJ_AI_ERROR_OTHER.value == "bij_ai_error_other"

    def test_clinical_record_write_failed_exists(self):
        """The pipeline ran but its result could not be recorded as clinical
        records (2026-09-12, D0). Not a Bij AI error -- reusing
        bij_ai_error_other would send the lab looking at the engine."""
        code = ExceptionReasonCode.CLINICAL_RECORD_WRITE_FAILED
        assert code.value == "clinical_record_write_failed"
        assert REASON_CODE_TO_OWNER[code] == "lab_operator"
        assert REASON_CODE_TO_CATEGORY[code] == ExceptionCategory.TRANSIENT_SUBMISSION_FAILURE


class TestExceptionStatus:
    """Tests for exception status enum."""

    def test_open_status_exists(self):
        """Open status exists."""
        assert ExceptionStatus.OPEN.value == "open"

    def test_resolved_status_exists(self):
        """Resolved status exists."""
        assert ExceptionStatus.RESOLVED.value == "resolved"


class TestExceptionEventAction:
    """Tests for exception event action enum."""

    def test_open_action_exists(self):
        """Open action exists."""
        assert ExceptionEventAction.OPEN.value == "open"

    def test_resolve_action_exists(self):
        """Resolve action exists."""
        assert ExceptionEventAction.RESOLVE.value == "resolve"

    def test_reopen_action_exists(self):
        """Reopen action exists."""
        assert ExceptionEventAction.REOPEN.value == "reopen"


class TestResolutionAction:
    """Tests for resolution action enum."""

    def test_retry_check_action_exists(self):
        """Retry check action exists."""
        assert ResolutionAction.RETRY_CHECK.value == "retry_check"

    def test_accept_failure_action_exists(self):
        """Accept failure action exists."""
        assert ResolutionAction.ACCEPT_FAILURE.value == "accept_failure"
