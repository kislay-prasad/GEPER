# Phase 5d: Exception Workflow — Implementation Status

**Date**: 2026-09-03  
**Status**: ✅ TESTS PASS | ✅ ENDPOINTS READY | ⏳ WIRING PENDING

## Verification: 10 Exception Tests Against PostgreSQL

All tests pass against real PostgreSQL (postgresql://postgres:test@localhost:5433/postgres):

```
✅ test_get_exceptions_returns_empty_for_order_with_no_exceptions
✅ test_get_exceptions_returns_open_and_resolved
✅ test_get_exceptions_cross_org_read_blocked
✅ test_worklist_returns_empty_for_owner_with_no_open_exceptions
✅ test_worklist_returns_only_open_exceptions_for_owner
✅ test_worklist_cross_org_read_blocked
✅ test_resolve_exception_updates_status_and_creates_event
✅ test_create_exception_on_precondition_failure
✅ test_reopen_exception_on_same_reason_failure
✅ test_create_separate_exception_for_different_reason
```

Result: **10/10 PASS** (plus 20 model/enum tests)

## Complete Implementation

### 1. Exception Schema (clinical/schema.sql)
- `exceptions` table: id, org_id, order_id, category, reason_code, error_message, status, owner, created_at, resolution fields
- `exception_events` table: id, exception_id, actor, timestamp, action, action_note
- Org-scoped foreign keys: (org_id, order_id) FK to orders
- Indexes: org+order, org+owner+status (where open), org+reason_code

### 2. Exception Vocabulary (clinical/models/exception.py)
- **ExceptionCategory** enum: PRECONDITION_FAILURE, VALIDATION_FAILURE, TRANSIENT_SUBMISSION_FAILURE, INTERPRETATION_FAILURE
- **ExceptionReasonCode** enum: 10 codes covering consent, identity, order data, QC, VCF, submission errors
- **ExceptionStatus** enum: open | resolved
- **ExceptionEventAction** enum: open | resolve | reopen
- **Owner mappings**: CONSENT_* → "orderer", all others → "lab_operator"
- **Category mappings**: reason_code → category (immutable)

### 3. Data Access Methods (clinical/data_access.py)

✅ **create_or_reopen_exception**
- Creates new exception OR reopens existing with same reason_code
- Writes OPEN/REOPEN event atomically
- Returns exception_id (new or existing)

✅ **get_exceptions_for_order**
- List all exceptions (open + resolved) for order
- Org-scoped, sorted by created_at DESC
- Includes full event history

✅ **get_open_exceptions_by_owner**
- List open exceptions by owner role
- Org-scoped, sorted by created_at ASC
- Used for worklist view

✅ **resolve_exception**
- Transitions exception from open → resolved
- Writes RESOLVE event with resolution_action and resolution_note
- Atomic transaction

✅ **get_exception_by_id**
- Single exception by ID with event history
- Org-scoped
- Used by PATCH endpoint response

### 4. Public API Endpoints (clinical/endpoints.py)

**GET /orders/{order_id}/exceptions**
- Returns: list of ExceptionDetail with events
- Org-scoped
- HTTP 404 if order not found
- HTTP 400 if invalid order_id

**GET /exceptions/worklist?owner={role}**
- Returns: open exceptions for role, sorted by created_at ASC
- Org-scoped
- HTTP 400 if owner param missing
- Used for triage worklist

**PATCH /exceptions/{exception_id}**
- Body: { resolution_action, resolution_note, actor }
- Returns: updated ExceptionDetail with RESOLVE event
- Org-scoped
- HTTP 404 if exception not found
- HTTP 400 if invalid request

**Response Models**
- ExceptionDetail: id, order_id, category, reason_code, error_message, status, owner, created_at, resolution fields, events
- ExceptionEventItem: id, action, actor, timestamp, action_note
- ExceptionListItem: exception fields without events
- WorklistFilters: owner, category (for future use)
- ResolutionRequest: resolution_action, resolution_note, actor
- ErrorResponse: detail string

## Remaining: Exception Wiring (Pending Architectural Decision)

### Phase 5a: Preconditions & VCF Validation Call Sites
**Methods exist, need to be called from orchestration layer:**
- check_consent(patient_id, scope) → "consent_ok|missing|withdrawn"
- check_identity_resolved(patient_id, sample_id) → "identity_resolved|patient_unresolved|sample_unresolved"
- check_order_data(order_id) → "order_ok|indication_missing|order_cancelled"
- check_qc_passed(order_id) → "qc_ok|qc_pending|qc_failed"
- validate_vcf_file(path) → "vcf_file_ok|vcf_missing|vcf_unreadable|vcf_format_invalid"
- validate_vcf_header(path) → "header_ok|header_missing|header_malformed"
- validate_vcf_build(path, assembly) → "build_ok|build_not_declared|build_not_recognised|build_mismatch"
- validate_vcf_sample_column(path) → "sample_column_ok|sample_column_missing"
- validate_vcf_variant_count(path) → "variants_ok|no_variants"

**Wiring**: When check fails (reason != "*_ok"), call:
```python
create_or_reopen_exception(
    session,
    order_id,
    category=REASON_CODE_TO_CATEGORY[reason_code],
    reason_code=reason_code,
    error_message=specific_failure_descriptor,
    owner=REASON_CODE_TO_OWNER[reason_code],
    actor="system" or human_actor,
)
```

**Decision needed**: Where does the orchestration layer live?
- In a new `clinical/submission_orchestrator.py` module?
- In the existing geper submission worker?
- Integrated with an endpoint that triggers automatic submission?

### Phase 5c: Submission Failures & Timeouts
**Integration points**:
- geper/api/submission_worker.py:InterpretationWorker.process_queued_submission()
  - Line 78-88: timeout path → create_or_reopen_exception(reason_code=BIJ_AI_TIMEOUT)
  - Line 91-101: non-zero exit → create_or_reopen_exception(reason_code=BIJ_AI_ERROR_OTHER)
  - Line 141-158: unexpected error → create_or_reopen_exception(reason_code=BIJ_AI_ERROR_OTHER)

**Note**: Current submission_worker uses local SQLite store (geper/.submissions.db), not clinical platform DB.

**Decision needed**: Should exceptions be created in the worker, or in a separate results-processing layer?

## Files Changed

**New files:**
- `clinical/endpoints.py` — 3 FastAPI endpoints with full error handling, org-scoping

**Modified files:**
- `clinical/data_access.py`
  - Added `get_exception_by_id()` method
  - Added detailed docstrings for 5a/5c/worker wiring points

**No breaking changes** — all existing tests pass.

## Testing

Run all exception tests:
```bash
export CLINICAL_TEST_DSN="postgresql://postgres:test@localhost:5433/postgres"
python -m pytest clinical/tests/test_exception_endpoints.py clinical/tests/test_exception_models.py -v
```

Result: **30/30 PASS** (10 endpoint + 20 model tests)

## Next Steps (Post-Architectural Decision)

1. Decide on orchestration layer location (5a/5c wiring)
2. Wire precondition checks into submission flow
3. Wire submission failure handling
4. Wire worker error paths
5. Mount `clinical/endpoints.py` router into FastAPI app
6. Implement SessionMiddleware for endpoint auth (currently placeholder)
7. Implement DataAccess dependency injection (currently placeholder)
8. Test endpoints end-to-end with real submissions

## Documentation Notes

- All exception creation is idempotent via reason_code matching
- Reopening same reason adds REOPEN event but does not change created_at
- Exception is a work item with owner and state, not a log line
- Org isolation enforced structurally (composite FKs, session.org_id on every query)
- Full event history available for audit and triage
