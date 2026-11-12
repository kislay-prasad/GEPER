# Consent enforcement matrix

**Status: a description of the code as it is. Not a legal or clinical policy.**

This document records, stage by stage, where the clinical service consults consent and where it does not, as of the
commit that adds it. It exists so that a decision about what consent withdrawal should mean can be made against
facts. It makes **no** decision of its own, and it makes **no claim** of DPDP or NABL compliance, of clinical
validation, or of production readiness. Where a row says a stage does not consult consent, that is an observation of
the implementation, not a statement that it should or should not.

The matching behaviour is pinned by characterization tests (listed in section 7). A test named
`..._is_currently_accepted` documents the current implementation only. When a human decides that a stage must
behave differently, the code, the test and this document change together in one commit.

## 1. Terms

| Term | Meaning in this document |
|---|---|
| Consent record | A row in `consents`: patient, scope, recorded time, optionally `withdrawn_at` and `superseded_by`. |
| Active | Not withdrawn (`withdrawn_at IS NULL`); where a rule also requires it, not superseded (`superseded_by IS NULL`). |
| Withdrawn | `withdrawn_at` is set by `DataAccess.withdraw_consent`. The row is kept. |
| Superseded | `superseded_by` points at a newer consent, set by `DataAccess.record_consent(supersedes_id=...)`. |
| Order consent | The consent id stored on the order when it was created (`orders.consent_id`). |
| Latest consent | The patient's most recent consent for the order's scope. Each rule orders them slightly differently (section 4). |

Status labels used below:

* **ENFORCED**: the stage consults consent and can refuse or hold because of it.
* **NOT ENFORCED**: the stage does not consult consent. A withdrawal does not change what it does.
* **DISPLAY ONLY**: a read shows the consent's status but does not decide anything from it.
* **POLICY-OPEN**: what the stage should do after a withdrawal has not been decided. Every NOT ENFORCED row is
  POLICY-OPEN. The text of `DataAccess.withdraw_consent` says retroactivity "IS NOT DECIDED ANYWHERE IN THIS TREE".

## 2. Stage matrix

Evidence is written as `path::symbol` and is checked by `clinical/tests/test_consent_matrix_document.py`.

| Stage | Status | What the code does | Enforcement point or function | Route | Tests |
|---|---|---|---|---|---|
| Patient registration | NOT ENFORCED (no consent exists yet) | Registers a patient. Consent is recorded afterwards. | `clinical/data_access.py::create_patient` | `POST /clinic/patients` | `clinical/tests/test_w162_registration_and_consent.py::test_the_clinician_withdraws_a_consent_and_the_withdrawer_is_recorded` |
| Record consent | n/a | Orderer only. Optionally supersedes an earlier consent (the pointer is set even if the older one is withdrawn). | `clinical/data_access.py::record_consent` | `POST /clinic/patients/{patient_id}/consents` | `clinical/tests/test_consent_characterization.py::TestTheThreeConsentRulesAreNotTheSameRule` |
| Withdraw consent | n/a (the act itself) | Orderer only. Stamps `withdrawn_at` and `withdrawn_by` on the consent. Touches no other table: no order, sample or report moves. | `clinical/data_access.py::withdraw_consent` | `POST /clinic/consents/{consent_id}/withdrawal` | `clinical/tests/test_w162_registration_and_consent.py::test_withdrawal_is_audited_to_the_withdrawing_clinician` |
| Order creation | ENFORCED (rule 1) | The named consent must be active, belong to the patient and organisation, and match the required scope. A withdrawn, superseded, foreign or absent consent is one generic not-found; a scope mismatch is a value error. | `clinical/data_access.py::create_order` | `POST /clinic/orders` | `clinical/tests/test_w166_catalogue_and_orders.py::test_a_withdrawn_consent_cannot_back_an_order`, `clinical/tests/test_consent_characterization.py::TestOrderCreationAfterWithdrawal` |
| Order placement | NOT ENFORCED | Moves a draft order to placed after a withdrawal. | `clinical/data_access.py::place_order` | `POST /clinic/orders/{order_id}/placement` | `clinical/tests/test_consent_characterization.py::TestStagesBetweenOrderCreationAndSubmissionAfterWithdrawal` |
| Order cancellation | NOT ENFORCED | Cancels after a withdrawal. | `clinical/data_access.py::cancel_order` | `POST /clinic/orders/{order_id}/cancellation` | same class |
| Sample receipt | NOT ENFORCED | Receives a sample for an order whose consent was withdrawn. | `clinical/data_access.py::receive_sample` | `POST /lab/orders/{order_id}/samples` | same class |
| Sample QC | NOT ENFORCED | Records a passed or failed QC after a withdrawal. | `clinical/data_access.py::record_qc` | `POST /lab/samples/{sample_id}/qc` | same class |
| Start of a pipeline attempt | ENFORCED (rule 2) | `check_consent` is the first check of `validate_order_for_submission`. A withdrawn consent opens a `consent_withdrawn` exception for the Orderer and the attempt fails before the engine runs. Runs at every attempt, including a re-queue. | `geper/api/submission_worker.py::_clinical_preflight`, `clinical/data_access.py::validate_order_for_submission`, `clinical/data_access.py::check_consent` | worker, not a route | `clinical/tests/test_phase_5d_orchestration.py::test_withdrawn_consent_creates_exception`, `geper/api/tests/test_worker_consent_withdrawal_during_run.py::test_a_withdrawal_before_the_attempt_starts_is_refused_at_preflight_and_the_engine_never_runs` |
| Engine run | NOT ENFORCED | Nothing consults consent while the engine process runs. | `geper/api/submission_worker.py::process_queued_submission` | worker | `geper/api/tests/test_worker_consent_withdrawal_during_run.py::test_a_withdrawal_while_the_engine_runs_is_currently_recorded_as_a_completed_result` |
| Result recording (run, VCF, interpretation, draft report) | NOT ENFORCED | Records the result in one transaction. Takes no consent argument and reads no consent. | `clinical/data_access.py::record_pipeline_result` | worker | same test; `clinical/tests/test_consent_characterization.py::TestTheSubmissionToResultWindowAtTheDataLayer` |
| Review, concurrence, submission, approval | NOT ENFORCED | Proceed after a withdrawal. | `clinical/data_access.py::record_sole_signatory_claim`, `clinical/data_access.py::submit_for_review`, `clinical/data_access.py::approve_report` | `POST /review/reports/{report_id}/concurrence`, `/submission`, `/approval` | `clinical/tests/test_consent_characterization.py::TestReviewAndReleaseAfterWithdrawal` |
| Release | NOT ENFORCED | Releases an approved report after a withdrawal. | `clinical/data_access.py::release_report` | `POST /review/reports/{report_id}/release` | same class |
| Retraction | NOT ENFORCED | Independent of consent. | `clinical/data_access.py::retract_report` | `POST /admin/reports/{report_id}/retraction` | `clinical/tests/test_consent_characterization.py::TestWhatEachSurfaceShowsAfterReleaseWithdrawalAndRetraction` |
| Staff genome and call-set reads | ENFORCED (rule 3, Policy C) | If any analysis fails Policy C the whole read is refused with a fixed message; never filtered to a shorter list. Call-set submission uses the same predicate. | `clinical/data_access.py::list_patient_genome_analyses`, `clinical/data_access.py::get_call_set_for_review`, `clinical/data_access.py::_CALLSET_CONSENT_OK_SQL` | `GET /clinic/patients/{patient_id}/genome/...` | `clinical/tests/test_genome_analysis_endpoints.py::TestTheConsentGate`, `clinical/tests/test_callset_m3_policy_c.py::TestBothConsentsMustBeActive` |
| Staff patient, order and sample reads | NOT ENFORCED | Rows stay readable after a withdrawal. | `clinical/data_access.py::read_order`, `clinical/data_access.py::read_patient`, `clinical/data_access.py::read_sample` | `GET /clinic/patients`, `/clinic/orders`, `/lab/samples` and their detail routes | `clinical/tests/test_consent_characterization.py::TestWhatEachSurfaceShowsAfterReleaseWithdrawalAndRetraction` |
| Staff consent reads | DISPLAY ONLY | `status` is `active`, `withdrawn` or `superseded`, with withdrawn taking precedence. It decides nothing. | `clinical/data_access.py::read_consent` | `GET /clinic/consents/{consent_id}`, `GET /clinic/patients/{patient_id}/consents` | `clinical/tests/test_staff_read_endpoints.py::test_a_withdrawn_consent_displays_as_withdrawn` |
| Patient portal reports and trends | NOT ENFORCED | A released report stays visible to its patient after a withdrawal and after a retraction (flagged). | `clinical/portal_endpoints.py::list_my_reports`, `clinical/portal_endpoints.py::get_my_report`, `clinical/portal_endpoints.py::list_my_trends` | `GET /portal/me/reports`, `/portal/me/reports/{report_id}`, `/portal/me/trends` | `clinical/tests/test_consent_characterization.py::TestWhatEachSurfaceShowsAfterReleaseWithdrawalAndRetraction` |
| Patient portal genome read | NOT ENFORCED | Applies the released-and-not-retracted rule and no consent predicate. The route is mounted only when the patient-genome flag is set; off by default. | `clinical/data_access.py::list_my_genome_analyses`, `clinical/genome_endpoints.py::patient_routes_enabled` | `GET /portal/me/genome/chromosomes` (flag) | same class |

Not covered: report amendments and notifications, and any export or retention path. Retention and erasure are outside
this document.

## 3. Withdrawal timing, scenario by scenario

What happens next for each moment at which a consent can be withdrawn. "Pinned by" names the test; none of these
rows says what should happen.

| # | Withdrawal happens | Next action | Current result | Pinned by |
|---|---|---|---|---|
| 1 | Before the order is created | Create the order | Refused (generic not-found) | `test_a_withdrawn_consent_cannot_back_an_order` and `TestOrderCreationAfterWithdrawal` |
| 2 | After creation, before placement | Place, or cancel | Accepted | `TestStagesBetweenOrderCreationAndSubmissionAfterWithdrawal` |
| 3 | After placement, before sample receipt | Receive the sample | Accepted | same class |
| 4 | After receipt, before QC | Record QC (passed or failed) | Accepted | same class |
| 5 | After QC, before the attempt starts | Start the attempt | Attempt fails, exception for the Orderer, no engine run | `test_withdrawn_consent_creates_exception`, `test_a_withdrawal_before_the_attempt_starts_is_refused_at_preflight_and_the_engine_never_runs` |
| 6 | While the engine runs | The engine finishes | Result is recorded and the submission completes | `test_a_withdrawal_while_the_engine_runs_is_currently_recorded_as_a_completed_result` |
| 7 | After the pre-run check, before recording | Record the result | Accepted | `test_result_recording_after_a_passed_validation_and_a_withdrawal_is_currently_accepted` |
| 8 | After recording, before review | Claim, submit, approve | Accepted | `TestReviewAndReleaseAfterWithdrawal` |
| 9 | After approval, before release | Release | Accepted | same class |
| 10 | After release | Patient and staff reads | Portal shows the report; staff genome read refused (rule 3); consent shows withdrawn | `TestWhatEachSurfaceShowsAfterReleaseWithdrawalAndRetraction` |
| 11 | After retraction | Patient reads | Report still listed, flagged retracted | `test_after_retraction_the_portal_still_shows_the_report_flagged` |
| 12 | After a completed run, then the submission is re-queued | Re-queue | Fails at the pre-run check; the earlier record is untouched | `test_a_requeue_after_a_completed_run_and_a_later_withdrawal_is_refused_at_preflight_not_adopted` |

Rows 6 and 7 are the same window seen from the worker and from the data layer.

## 4. The three rules compared

The code has three different consent predicates. They agree when a patient has one active consent and part ways in
the cases below. `clinical/tests/test_consent_characterization.py::TestTheThreeConsentRulesAreNotTheSameRule` pins
the answers; **this document does not say which rule is right.**

| | Rule 1: order creation | Rule 2: `check_consent` | Rule 3: Policy C |
|---|---|---|---|
| Code | `clinical/data_access.py::create_order` | `clinical/data_access.py::check_consent` | `clinical/data_access.py::_CALLSET_CONSENT_OK_SQL` |
| Which consent | The one id the caller names | The latest not-superseded consent for the scope, by `recorded_at` | The order's own consent **and** the latest consent for the scope, by `recorded_at` then `consent_id` |
| Scope | Consent scope must equal the required scope (mismatch is a value error) | The order's required scope | The order's required scope |
| Withdrawn | Not found | `consent_withdrawn` | Either consent withdrawn: refused |
| Superseded | Not found | Skipped, so the newer consent is the one judged; if all are superseded, `consent_missing` | Ignored on purpose (`superseded_by` is mutable bookkeeping) |
| On failure | Generic not-found | Returns a reason; the caller opens an exception for the Orderer | Fails closed: the staff read is refused with a fixed message; call-set submission is refused |

Recorded divergences (patient history, then rule 2 / rule 3 for the order placed under the first consent):

| History | Rule 1 on the first consent | Rule 2 | Rule 3 |
|---|---|---|---|
| Active | accepted | `consent_ok` | eligible |
| Withdrawn | not found | `consent_withdrawn` | not eligible |
| Withdrawn, then a newer active consent | not found | `consent_ok` | **not eligible** |
| Superseded by an active consent | not found | `consent_ok` | **eligible** |
| Withdrawn, then superseded by an active consent | not found | `consent_ok` | **not eligible** |
| A newer consent recorded and withdrawn, older still active | accepted | `consent_withdrawn` | not eligible |

An observation that is not tested: rule 2 orders by `recorded_at` alone, with no tiebreak, so two consents with an
identical timestamp could be judged in either order. Rule 3 breaks ties by `consent_id`.

## 5. The submission-to-result window

`geper/api/submission_worker.py::process_queued_submission` calls `_clinical_preflight` (rule 2) once, then runs
the engine, then calls `_record_run`, which calls `clinical/data_access.py::record_pipeline_result`. That method reads
no consent. A withdrawal between the pre-run check and the recording is therefore not seen, and the result is
recorded. Afterwards, rule 2 says `consent_withdrawn` and the staff genome read is refused under rule 3, so the two
disagree about a record that now exists.

The same path already re-checks one thing at record time: the VCF content hash (`_vcf_changed_since_preflight` in
the worker, and the `expected_vcf_hash` check inside `record_pipeline_result`). The tests are
`geper/api/tests/test_worker_consent_withdrawal_during_run.py` (real worker, real database, stubbed engine) and
`TestTheSubmissionToResultWindowAtTheDataLayer`.

Places a future check could go (listed, **none selected**; each also needs a decision on what happens to a completed
analysis: discard, quarantine or keep):

1. Inside `record_pipeline_result`, in its transaction.
2. In the worker, between the engine and `_record_run`, like the VCF re-check.
3. At the first review step or at release.
4. A database-level constraint or trigger.
5. Read-time withholding with the evidence kept, as rule 3 does for staff genome reads.

## 6. Decisions this document does not make

| # | Open question | Owner |
|---|---|---|
| D1 | What withdrawal should do at each NOT ENFORCED stage | Clinical and legal review |
| D2 | Whether a released report stays visible to its patient after a withdrawal | Clinical and legal review |
| D3 | Whether the three rules stay separate or become one | Clinical and engineering, after D1 |
| D4 | What happens to in-flight and completed results after a withdrawal | Clinical and legal review |
| D5 | Whether the patient's own genome read should follow rule 3 | Clinical and legal review |

Consent shown on a report is rendered from the run's patient metadata (`geper/report/summary.py::_parse_consent`)
and not read from `consents`. This is an observation that there are two sources of consent information; it is not
tested here.

## 7. Evidence index and change control

Characterization tests added with this document:

* `clinical/tests/test_consent_characterization.py` (data layer and HTTP, disposable PostgreSQL)
* `geper/api/tests/test_worker_consent_withdrawal_during_run.py` (real worker, real database, stubbed engine)
* `clinical/tests/test_consent_matrix_document.py` (checks that the references in this file still exist)

Existing tests relied on and not duplicated: `clinical/tests/test_phase_5d_orchestration.py`,
`clinical/tests/test_w166_catalogue_and_orders.py`, `clinical/tests/test_w162_registration_and_consent.py`,
`clinical/tests/test_genome_analysis_endpoints.py`, `clinical/tests/test_callset_m3_policy_c.py`,
`clinical/tests/test_staff_read_endpoints.py`.

Change control: a change to any consent behaviour updates, in the same commit, the code, the affected
characterization tests and this matrix. A characterization test that fails because behaviour was changed on purpose
is edited to the new decided behaviour; one that fails by accident is a regression.
