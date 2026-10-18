# Sign-off over the authenticated API: design (Phase 2)

Status: implemented on `feat/clinical-signoff-api`; **not** production-ready, not deployed, synthetic data only.

## What was inspected, and what it showed

* `geper/review/signoff.py`: `approve`, `override` and `withdraw` each log in, read, and (for approve/override) write
  to the clinical record through an injected `DataAccess`, defaulting to `_connect_clinical_data_access()`, which opens
  `psycopg.connect($CLINICAL_DSN)` on the machine the person is sitting at.
* `clinical/review_endpoints.py`: **the write endpoints already exist** (Stage 3, w168). Each handler is exactly one domain
  call; the organisation is `Session.org_id`, the actor is `session.user_id`, a body naming either is a 422
  (`extra="forbid"`), roles are the domain methods' first statement, a foreign or absent id is the same 404.
* PR #25 added the reads (`GET /review/reports/{id}`, `.../revision`, `GET /auth/me`, `.../signing-identity`).

## The three call paths

| Operation | Clinical reads | Clinical writes | HTTP route that already serves it |
|---|---|---|---|
| `approve` (linked) | signing identity, report, revision | `record_sole_signatory_claim`, `submit_for_review`, `approve_report` (three separate commits, as today) | `POST .../concurrence`, `.../submission`, `.../approval` |
| `override` (linked, before approval) | report, revision | `record_disagreement_claim` | `POST .../disagreement` |
| `withdraw` (linked) | report, revision | **none** (the clinical record cannot un-approve; withdraw only refuses) | reads only (PR #25) |

So no new server route is needed. Adding a composite "approve" endpoint would be a second, parallel implementation of the
sole-signatory sequence, and `review_endpoints.py` documents why a handler must call one decorated method only
(`@auditable` commits after each audit row). The existing three-call sequence and its ordering are kept exactly.

## Endpoints, roles, request/response

| Route | Role (enforced by the domain method, unchanged) | Body | Success | Errors |
|---|---|---|---|---|
| `POST /review/reports/{id}/concurrence` | Interpreter | `{interpretation_id, reason}` | 201 `{claim_id}` | 400 blank reason / report-interpretation mismatch, 401, 403, 404 |
| `POST /review/reports/{id}/disagreement` | Interpreter | `{interpretation_id, variant_key, new_classification, reason}` | 201 `{claim_id}` | 400, 401, 403, 404 |
| `POST /review/reports/{id}/submission` | Interpreter | none | 204 | 400 not a draft / no claim of its own, 401, 403, 404 |
| `POST /review/reports/{id}/approval` | Approver | none | 204 | 400 not under review, 401, 403, 404 |

The signatory must hold both Interpreter and Approver for the one-person sign-off; nothing is widened.

## Organisation ownership and trust

* The organisation is `Session.org_id` from the server-side session. The `org_id` in `clinical_link.json` is used
  only to choose which organisation to log in to; the adapter then refuses a session whose `/auth/me` org differs.
* `report_id` and `interpretation_id` come from the signed link file. They are **not** authorization claims: the domain
  checks that the report belongs to that interpretation **and** to the session's organisation, and a foreign
  organisation's id answers 404, indistinguishable from an absent one.
* No request carries an actor, state or organisation.

## Invariants preserved

* Write order is unchanged: identity read, revision status read and validated, **then** claim, submit, approve, and only
  then any file is touched. A refusal at any step leaves the directory as it was.
* Audit: every write is audited by the domain method with the session user as actor. The tool's own run-directory
  audit log is unchanged.
* Revision/retraction: the revision status is still read and validated (`BrokenRevisionRecordError` fails closed) before
  any write; `{}` stays a successful empty answer only.
* Approved/released reports still refuse override and withdraw before any write.
* Signed-artifact immutability and the LIMS guards are untouched.

## Failure semantics specific to writes

A read that fails changes nothing. A **write whose outcome is unknown** (timeout, connection reset, 5xx) may or may not
have committed. The adapter never retries a write. It raises `ClinicalWriteOutcomeUnknown` (a `ClinicalServiceError`)
whose message tells the operator to check the report's state in the clinical service before re-running; the tool
then writes no files. 401/403/404/400 are definitive "not done" answers and map to the exceptions the direct path
raised (`SessionExpired`, `AuthorizationError`, `NotFoundError`, `ValueError`).

Partial progress is the same as before: the three approve calls are separate commits, so a failure after the claim
leaves a claim and a draft report. This phase does not make approve atomic.

### Unknown client outcome is not a failed operation

| What the tool reports | What it means | Recorded in the clinical service? |
|---|---|---|
| 400 / 401 / 403 / 404 / 422 (ValueError, SessionExpired, AuthorizationError, NotFoundError, `ClinicalServiceError`) | The service answered and refused. | **No** change from that call. |
| `ClinicalWriteOutcomeUnknown` (timeout, dropped connection, 5xx, a success status that is not the contract's) | The client does not know. | **Maybe.** |

A write is never retried automatically, and the tool writes no file after an unknown outcome.

**Reconciliation, using only what exists today.** There is **no** command that shows a report's claims, completes an
interrupted sign-off, or finalises the run directory afterwards; none is invented here.

1. Do **not** run the tool again for that run until the state is known. (It would now refuse a report that is no longer a
   draft, by design.)
2. Read the authoritative state as an authenticated Interpreter, Approver, Auditor or Administrator:
   `GET /review/reports/{report_id}` (state, interpretation, `is_retracted`) and `GET .../revision`. `refresh-revision`
   also reads them, but only for an already-signed run.
3. Decide from the state:
   * `draft`: the approval and submission did not happen. The concurrence claim may or may not have been recorded; claims
     are not served over HTTP, so someone with audit-log access must look for a `record_sole_signatory` row for that
     report and user. A recorded claim does not block a retry (a draft accepts more claims).
   * `under_review`: the claim and submission are recorded; only the approval is missing. An Approver can call
     `POST /review/reports/{report_id}/approval`; **no CLI does this**, and the sign-off tool will refuse the run (it is
     not a draft), so the run directory is **not** finalised by anything that exists. Escalate to the platform owners.
   * `approved`: the clinical sign-off exists but the run directory is unsigned. The tool refuses to sign it again. The
     files and the clinical record now disagree; escalate. Do not edit the signed files by hand.
4. Record what you found and who decided what next (the clinical audit log already holds the service's side).

This gap (no supported way to finish a half-completed sign-off) is a known limitation, listed in the PR as a risk.

## Concurrent requests

Findings, verified on PostgreSQL 16 against the code before the fix:
* Two simultaneous approvals both read `under_review` (plain SELECT, READ COMMITTED). The loser was stopped by the
  `enforce_content_immutability` trigger, but as an `ImmutabilityViolationError`, and only because the attempts carried
  different `approved_at` values. Over HTTP that is a 500, which a client reads as an unknown outcome.
* Simultaneous submissions all succeeded (N state writes, N audit rows).
* A claim could be appended to an approved report, and one in flight during sealing slipped past the content hash, so
  `verify_report_integrity` reported tampering.

Fix, in `clinical/data_access.py`: `_approve_report` and `submit_for_review` read the report row `FOR UPDATE`; the claim
recorders' shared precondition (`_validate_claim_inputs`) reads it `FOR SHARE` and refuses an `approved` or `released`
report. The second approval waits, re-reads the committed row, and gets the domain's own `ValueError`. The role gates,
organisation scoping, hash computation, audit decorators and the trigger are unchanged; the trigger remains the
backstop. `clinical_app` has column-level `UPDATE` on `reports`, which is sufficient for row locks.

## Migration of callers

`approve`, `override` and `withdraw` gain `clinical_url` (CLI `--clinical-url`, env `GEPER_CLINICAL_URL`) and use the same
`_human_tool_access` as refresh/export: an `https://` address is required, otherwise the operation is refused with nothing
changed. **`_connect_clinical_data_access` and its `CLINICAL_DSN` fallback are removed from the human tools.** An injected
`clinical_data_access` (tests, server-side callers) is still used as given. Headless workers
(`api/submission_worker.py`, `api/exception_retry_worker.py`) are untouched and keep their own database access; replacing
that is a separate decision (worker identity).

## Not done here

MFA enforcement, session hardening, worker least-privilege, amendment/retraction-from-tool, and atomic approve.
