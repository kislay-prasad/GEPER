# GEPER Consent Decision Brief

**As of commit 72e706d447e70e9ca6a4829c89010dadcf1eb457.** Line numbers in this document refer to that commit and will
drift as the code changes. Names (files, functions, tests) are the durable references.

> **Every decision in this document is PENDING.** No consent policy has been approved. Nothing here is a legal opinion,
> a clinical standard, or a statement of what the law or any accreditation body requires. The document makes **no claim
> of DPDP compliance, NABL compliance, clinical validation or regulatory approval.** Founder approval of a decision is a
> business decision; it is **not** legal approval and it is **not** clinical approval. Where a question needs one of
> those, the document says so and leaves the answer blank.

## 1. Status and how to use this document

**What this is.** A neutral description of how the GEPER clinical service treats consent today, the twelve open
questions about what it *should* do (D1 to D12), and a form to record each decision later.

**What this is not.** It does not recommend an outcome, does not choose which consent rule is authoritative, and does
not change any behaviour. It does not replace `docs/CONSENT_ENFORCEMENT_MATRIX.md`, which describes the code stage by
stage and is unchanged by this document.

**Who reads what.**

| Reader | Start at | Purpose |
|---|---|---|
| Founder (non-engineer) | Sections 2, 3, 4, then each decision's "In plain terms" part | Understand the choices and record product decisions |
| Indian legal and privacy counsel | Section 6.2, then the "Questions for counsel" in each decision | Answer the legal and privacy questions. This document supplies facts, not legal conclusions |
| Clinical genetics or laboratory reviewer | Section 6.3, then the "Questions for the clinical reviewer" in each decision | Answer the workflow and patient-safety questions |
| Engineers | The "Technical notes" under each decision and Appendix A | Check the facts and later implement an approved decision |

**How statements are labelled.**

| Label | Meaning |
|---|---|
| **[FACT]** | Verified in the code or the tests at the commit above, with a reference. |
| **[ENGINEERING INFERENCE]** | An engineer's reading of the code that has not been demonstrated by a test. It may be wrong. It is never a legal conclusion. |
| **[PRODUCT]** | A decision for the founder as a business and product matter. |
| **[CLINICAL]** | A decision that needs a qualified clinical genetics or laboratory reviewer. |
| **[LEGAL/PRIVACY]** | A question for Indian legal and privacy counsel. |

**How to record a decision.** Use the template in Section 7, one copy per decision. A decision is only "approved" in the
sense of its type: a [PRODUCT] decision by the founder, a [CLINICAL] decision by a qualified clinical reviewer, a
[LEGAL/PRIVACY] question by counsel. A decision that needs more than one type stays pending until all of them have
signed.

## 2. Executive overview for the founder

**The short version.** GEPER records a patient's consent and lets staff withdraw it. Today a withdrawal updates the
consent record and writes an audit event. It does not update the associated order, sample, sequencing run,
interpretation, or report. Whether that is enough is exactly what is undecided. The software looks at
consent in only three places; everywhere else, work that began under a consent carries on after that consent is
withdrawn. This is how the code behaves today, and the document does not say it is right or wrong.

**The three places consent is looked at [FACT].**

1. When an order is created (the order must name a consent that is currently active).
2. When the processing service starts an analysis run (a check made once, before the analysis begins).
3. When staff view a patient's genome or call-set data (the data is hidden if the order's consent, or the patient's
   newest consent, has been withdrawn).

**Where it is not looked at [FACT].** Order placement and cancellation, sample receipt and quality control, the web
address that accepts a submission, recording the analysis result, review, approval, release, amendment, retraction,
and the patient's own portal. A withdrawn consent therefore does not stop any of these.

**Three things the audit found that are easy to miss.**

* **The submission entry point does no clinical checking [FACT].** The service that accepts a submission
  (`POST /interpretations`) stores whatever order, sample and "consent reference" text the caller sends. The
  "consent reference" is never checked or used. Only the later worker checks anything. See D11.
* **There is a window during an analysis run [FACT, demonstrated by tests].** Consent is checked before the run starts
  and not again. A test (`test_a_withdrawal_while_the_engine_runs_is_currently_recorded_as_a_completed_result`) shows
  a withdrawal that happens during the run is not noticed, and the result is recorded normally. See D2 and D3.
* **Two possible "checked, then changed" races [ENGINEERING INFERENCE].** In both the consent is read and the action is
  written as separate steps, with no lock on the consent, so a withdrawal in between could be missed:
  (a) creating an order, and (b) recording the result. Race (b) is the window above, which tests demonstrate in
  sequence. Race (a), at the same instant in two connections, has not been demonstrated by a test. The system's default
  database isolation level (READ COMMITTED) is assumed here; that is an inference from the code. See Section 3.4.

**The twelve decisions at a glance.**

| # | Decision | Needs |
|---|---|---|
| D1 | What withdrawal does to an order that already exists | Clinical, legal/privacy, product |
| D2 | Whether an analysis already running may finish | Clinical, product |
| D3 | Whether a result may be recorded after withdrawal | Clinical, legal/privacy |
| D4 | Whether review, approval and release may continue | Clinical, legal/privacy |
| D5 | Whether a released report stays visible to the patient | Legal/privacy, product |
| D6 | Whether staff genome access should stop immediately, and the patient's own genome read | Clinical, legal/privacy |
| D7 | Whether a newer consent can authorise an order tied to an older one | Clinical, legal/privacy |
| D8 | How a superseded consent should behave | Clinical, legal/privacy |
| D9 | What happens to amended or retracted reports | Clinical, legal/privacy |
| D10 | Which consent rule is authoritative at each stage | Clinical, legal/privacy, engineering |
| D11 | Whether the submission entry point should check the order and consent | Product |
| D12 | Whether a withdrawal can ever be undone | Clinical, legal/privacy |

**What can proceed without these decisions.** Engineering could prepare a shared, locking way to read consent with no
change in behaviour. That is a preparation step only. It would not enforce anything and should not be mistaken for a
policy.

**What this document gives you for the next step.** A way to hand the right questions to the right person (Section 6)
and a record form (Section 7). It is written so that you can send Section 6.2 and the "Questions for counsel" to a
prospective lawyer, and Section 6.3 and the "Questions for the clinical reviewer" to a clinical reviewer, without them
having to read code.

## 3. The current consent workflow

### 3.1 In plain terms: the path of one test

1. **A clinician registers the patient** and records the patient's consent for a stated purpose (the "scope"), for
   example "testing".
2. **The clinician creates an order** for a test and names the consent it relies on. The system refuses if that consent
   is not currently active.
3. **The order is placed**, a **sample is received**, and the lab records **quality control (QC)**.
4. **A submission is made** to the processing service, which queues it.
5. **A worker starts the run.** Before the analysis begins it checks the consent again (the "preflight").
6. **The analysis runs** (it can take a long time) and its result is **recorded** as a sequencing run, a VCF
   (the variant file), an interpretation and a **draft report**.
7. **Review:** an interpreter and an approver move the report through review, approval and **release** to the patient
   portal.
8. **Afterwards** the report can be **amended** or **retracted**.

**A withdrawal** can happen at any moment between steps 1 and 8. It marks the consent as withdrawn. It does not move
the order, the sample, the run or the report.

### 3.2 Stage table: does the stage look at consent? [FACT]

This repeats the facts in `docs/CONSENT_ENFORCEMENT_MATRIX.md`, with call-path detail added in Appendix A.

| Stage | Looks at consent? | Which rule (Section 4) |
|---|---|---|
| Patient registration | No (no consent exists yet) | none |
| Record or withdraw a consent | It is the act itself | none |
| Order creation | **Yes** | Rule 1 |
| Order placement | No | none |
| Order cancellation | No | none |
| Sample receipt | No | none |
| Quality control | No | none |
| Submission entry point (`POST /interpretations`) | No | none |
| Start of an analysis run (preflight) | **Yes** | Rule 2 |
| The analysis run itself | No | none |
| Recording the result | No | none |
| Review, approval, release | No | none |
| Amendment, retraction | No | none |
| Patient portal: reports, trends | No | none |
| Patient's own genome read (route off by default) | No | none |
| Staff genome and call-set reads, call-set submit and validate | **Yes** | Rule 3 (Policy C) |
| Staff patient, order, sample reads | No | none |
| Staff consent read | Shows the status only; decides nothing | none |

### 3.3 What a withdrawal changes [FACT]

`DataAccess.withdraw_consent` (`clinical/data_access.py` line 3723) stamps the withdrawal time and the withdrawing user
on the consent record (the update is at line 3764) and writes one audit entry, `consent_withdrawn`, naming the consent.
Its own comment says that what withdrawal means for existing orders "IS NOT DECIDED ANYWHERE IN THIS TREE". The
project's platform specification, section 6.3 (`GEPER_CLINICAL_PLATFORM_SPEC.md` line 308), describes an intent:
existing reports are not deleted, but withdrawal "stops further processing and flags the patient record", and it marks
what happens to data already held as a decision that "needs counsel". The statement of intent is the project's own; this
document does not treat it as law. I searched the code for any mechanism that flags the patient record on withdrawal
and found none (a search, so it may have missed something) **[ENGINEERING INFERENCE]**. Further
statements that withdrawal is undecided are in `docs/CLINICAL_DEPLOYMENT.md` (lines 491 to 499, 528 to 531 and 1102).

### 3.4 The "checked, then changed" findings [ENGINEERING INFERENCE unless stated]

* **Race (a): order creation.** `create_order` (line 3217) reads the consent (the filter is at line 3284) and, in a
  later statement, inserts the order. Nothing locks the consent between the two. If a withdrawal committed between
  them, an order could be created on a consent that was just withdrawn. Not demonstrated by a concurrent test.
* **Race (b): result recording.** `record_pipeline_result` (line 7536) reads no consent. The only consent check is the
  preflight, which runs before an analysis that may take a long time. **Demonstrated in sequence [FACT]** by the tests
  named in D2 and D3.
* **Isolation assumption.** The code uses the database's default transaction isolation and no consent row is locked
  anywhere on the order or analysis path. The two staff genome paths lock run, sample, order and test rows
  (`_callset_chain_facts`, line 4418, option `lock=True`), not consent rows. Whether this matters in practice depends on
  concurrency that has not been measured.

## 4. The three existing consent rules and their differences

There is no single consent rule in the code. There are three, written at different times for different purposes. This
document does not choose among them (see D10).

### 4.1 The rules in plain terms [FACT]

| | Rule 1: order creation | Rule 2: analysis-run preflight | Rule 3: Policy C (staff genome and call sets) |
|---|---|---|---|
| Where | `create_order` (line 3217) | `check_consent` (line 8440), called from `validate_order_for_submission` (line 8796) | `_CALLSET_CONSENT_OK_SQL` (lines 4351 to 4359) |
| Which consent is judged | The one consent the clinician names | The patient's **newest consent for that purpose that has not been superseded** | **Both**: the consent the order was created under, **and** the patient's newest consent for the purpose |
| "Withdrawn" | The consent is treated as not found | Judged on the newest unsuperseded consent only | Either consent withdrawn means not eligible |
| "Superseded" (replaced by a newer record) | Treated as not active | Skipped; the newer consent is the one judged | **Ignored on purpose** |
| Ties on the recorded time | Not applicable | **No tiebreak** in the query (line 8452) | Broken by the consent id |
| What happens on failure | A generic "not found" | An exception is opened for the ordering clinician and the run fails | Staff are refused with a fixed message (HTTP 403); nothing is shown |

### 4.2 How they answer the same patient history [FACT, pinned by a test]

`TestTheThreeConsentRulesAreNotTheSameRule` in `clinical/tests/test_consent_characterization.py` (line 320) records
these answers. Each row starts with one order placed under the patient's first consent.

| What happened to the patient's consents afterwards | Rule 1 (can a new order use the first consent?) | Rule 2 | Rule 3 |
|---|---|---|---|
| Nothing | Yes | OK | Eligible |
| First consent withdrawn | No | Withdrawn | Not eligible |
| First withdrawn, then a **newer active** consent recorded | No | **OK** | **Not eligible** |
| First **superseded** by a newer active consent | No | OK | **Eligible** |
| First withdrawn, then superseded by a newer active one | No | **OK** | **Not eligible** |
| A newer consent recorded and then withdrawn; first still active | Yes | Withdrawn | Not eligible |

### 4.3 Where they disagree, and why it matters

* **Rows 3 and 5:** Rule 2 lets the work run (the newer consent covers it) while Rule 3 hides the result from staff
  (the order's own consent was withdrawn). A run could therefore be started and recorded, and then be invisible to the
  clinicians who need it.
* **Row 4:** the rules disagree in the opposite direction in what they say about the first consent: Rule 1 refuses to
  base a *new* order on it, Rule 3 still treats an *existing* order on it as eligible.
* **Ties:** only Rule 2 has no tiebreak. Two consents recorded at the identical instant could be judged in either
  order **[ENGINEERING INFERENCE; untested]**.

### 4.4 What changing a rule would affect [ENGINEERING INFERENCE]

* Making Rule 2 behave like Rule 3 would start refusing runs that now start (rows 3 and 5). Queued and retried
  submissions for patients who re-consented would fail at preflight, and exceptions already resolved might reopen.
* Making Rule 3 behave like Rule 2 would show staff the genome in rows 3 and 5 again. That would reverse the behaviour
  the code calls "Policy C (approved)" (comment above line 4351). Whether that "approval" is clinical or legal is not
  recorded anywhere I could find; it should be treated as an engineering label.
* Either change alters tests in `clinical/tests/test_callset_m3_policy_c.py`,
  `clinical/tests/test_phase_5d_orchestration.py` and the Phase 1 rule table.

## 5. Decision register D1 to D12

Each decision uses the same layout. "Technical notes" are for engineers and reviewers who want them; the rest is meant
to be read without code. Blank "Decision record" boxes are at the end of each entry.

---

### D1. What withdrawal does to an order that already exists

**In plain terms.** A clinician orders a test relying on a patient's consent. Later the patient withdraws that consent.
The order is already in the system, perhaps with a sample on the bench. What should happen to that order?

**Current behaviour [FACT].**

* Withdrawal changes only the consent record. The order keeps its state (`withdraw_consent`, line 3723).
* Creating a *new* order on the withdrawn consent is refused with a generic "not found" (`create_order`, line 3284).
* Placing, cancelling, receiving a sample, and recording QC do not consult consent at all
  (`place_order` line 3328, `cancel_order` line 3365, `receive_sample` line 3596, `record_qc` line 3677). Tests:
  `TestStagesBetweenOrderCreationAndSubmissionAfterWithdrawal`
  (`clinical/tests/test_consent_characterization.py`, line 445).
* The analysis run is the next place consent is read (D2).

**The exact unresolved question.** After a withdrawal, which of the order's actions, if any, must be stopped, and does
the order itself need a visible state (held, cancelled, flagged) so staff know why?

**Available choices (none recommended).**

| Choice | What it means | Technical consequence [ENGINEERING INFERENCE] |
|---|---|---|
| A. Nothing changes | Orders carry on until a stage that already checks consent evaluates them. Today, the run-start check uses Rule 2, which may refuse the run or allow it depending on the patient's newest unsuperseded consent. Staff genome and call-set access separately follow Rule 3. | No change. Retry and worker behaviour unchanged. |
| B. Block *new* work on the order | Placement, sample receipt and QC refuse once the consent is withdrawn; work already done stays | Add a consent check at those methods, ideally inside the same transaction as the change. Audit: a refusal would be an audited error with a fixed reason code. No schema change. |
| C. Hold or cancel the order | The order moves to a held or cancelled state automatically at withdrawal | Needs either a new order state or a hold flag (schema change and a migration), plus changes to every reader of order state. Cancelling an order already in the lab has sample-handling consequences. |
| D. Freeze everything downstream | As C, and also stop result recording, review and release (see D3 and D4) | The largest change; touches most of Section 9's list. |

**Affected stages.** Placement, cancellation, sample receipt, QC, run start, result recording, review.
**Code areas.** `clinical/data_access.py` order and sample methods; possibly `clinical/schema.sql` and a new migration
(only for C or D).
**Schema implications.** A, B: none. C, D: a new state or flag.
**Retry behaviour.** A: unchanged. B: refusal at placement does not interact with the worker. C, D: a held order must
also stop requeues (`requeue_resolved_exceptions`, line 9635).
**Audit implications.** All options can audit using identifiers and a fixed reason code only, not patient detail.

**Timeline for a worked example (fictional).** Patient `PAT-EX-0001` consents (`CON-EX-A`, scope "testing"). Order
`ORD-EX-0001` is created on it.

| When the patient withdraws `CON-EX-A` | What happens today [FACT] |
|---|---|
| Before the order is created | Creation is refused. |
| After creation, before placement | The order can still be placed or cancelled. |
| After placement, before the sample arrives | The sample can still be received. |
| After the sample arrives, before QC | QC can still be recorded. |
| After QC, before the run | The run is refused at the preflight; an exception is opened for the ordering clinician. |
| During the run | See D2. |
| After the run | See D3. |

**Privacy implications [LEGAL/PRIVACY question].** Continuing to handle a sample or data after withdrawal may or may not
be permitted; this document does not say. **Clinical implications [CLINICAL question].** A sample already collected may
be unrecoverable if the order is cancelled; a held order may need a documented way to resume.
**Operational implications.** Choices B to D create more refusals for lab staff and more exceptions to resolve.

**Questions for counsel.** What does the law require or permit for an order, a collected sample and associated data
after a patient withdraws consent for the scope the order relies on? Does the answer depend on the scope, on whether
processing has begun, or on the purpose of the consent?
**Questions for the clinical reviewer.** For an order at each stage in the table, is stopping safe, and what must happen
to a sample already received? Who is notified, and how is the patient told?
**Reviewer types.** Legal/privacy counsel and clinical reviewer; the founder decides product behaviour only after both.

**Technical notes.** Tests: `TestOrderCreationAfterWithdrawal` (line 427);
`test_a_withdrawn_consent_cannot_back_an_order` (`clinical/tests/test_w166_catalogue_and_orders.py`, line 287);
`TestAWithdrawnConsentCannotBeOrderedOn` (same file, line 407). Uncovered: withdrawal concurrent with order creation.

**Decision record.** Decision: ______ Rationale: ______ Reviewer(s) and role: ______ Approval date: ______

---

### D2. Whether an analysis run already in progress may finish

**In plain terms.** The analysis can take a long time. Consent is checked only when it starts. If the patient withdraws
consent while it is running, should it be allowed to finish?

**Current behaviour [FACT].** The worker checks consent at the start (`_clinical_preflight`,
`geper/api/submission_worker.py` line 443, which calls `validate_order_for_submission` at line 493). It then starts the
engine (`spawn_tracked`, line 809) and does not check again. After the engine it re-checks only that the VCF file was not
changed (`_vcf_changed_since_preflight`, defined at line 394, used at line 846). A withdrawal during the run is not seen.
Tested with a real worker, a real database and an engine stand-in that withdraws the consent while it runs:
`test_a_withdrawal_while_the_engine_runs_is_currently_recorded_as_a_completed_result`
(`geper/api/tests/test_worker_consent_withdrawal_during_run.py`, line 181). That test only documents the current
behaviour; it is not an endorsement.

**The exact unresolved question.** Once a withdrawal is recorded, must the system stop a run that has already begun, let
it finish and decide separately about its result (D3), or treat the two cases differently depending on how far it got?

**Available choices (none recommended).**

| Choice | What it means | Technical consequence [ENGINEERING INFERENCE] |
|---|---|---|
| A. Let it finish | As today | No change. The result is then governed by D3. |
| B. Stop at the next safe point | The worker re-checks consent after the engine and before recording, and abandons the run if withdrawn | A worker re-check between lines 846 and 570, like the VCF re-check. The process still runs to the end; only the recording is skipped (a true mid-run kill would be a separate, larger change). The submission fails with the existing `consent_withdrawn` reason, which the retry flow already understands. No schema change. |
| C. Finish, but do not use the result | The run completes and its result is set aside | Needs somewhere to keep an unused result or a decision to discard it; see D3. |

**Affected stages.** Engine run, result recording, retries. **Code areas.** `geper/api/submission_worker.py`
(`process_queued_submission` line 657, `_record_run` line 570).
**Schema implications.** A, B: none. C: possibly a marker for a set-aside result.
**Retry behaviour.** After B, a later retry runs the full preflight again, so it would fail for as long as the consent
stays withdrawn. After a fresh consent, a retry would depend on D7. `reconcile_interrupted_submissions` (line 612) adopts
an already recorded result and does not run the preflight, so it would not see a withdrawal unless changed **[FACT from
the code; the effect is an inference]**.
**Audit implications.** Stopping a run would be visible as a failed submission with the existing reason code and an
order exception; no patient detail is needed.

**Timeline for a worked example (fictional).** Submission `SUB-EX-0001` for `ORD-EX-0001` passes the preflight at 10:00;
the engine runs until 10:40.

| Withdrawal at | Today [FACT, tested] |
|---|---|
| 09:59, before the preflight | The preflight fails with `consent_withdrawn`; the engine never runs. Test: `test_a_withdrawal_before_the_attempt_starts_is_refused_at_preflight_and_the_engine_never_runs` (line 219) |
| 10:20, during the run | Not noticed; the result is recorded at 10:40 and the submission completes |
| 10:41, after recording | Recorded already; D3 and later decisions apply |
| After completion, then the submission is requeued | The requeue fails at the preflight. Test: `test_a_requeue_after_a_completed_run_and_a_later_withdrawal_is_refused_at_preflight_not_adopted` (line 241) |

**Privacy implications [LEGAL/PRIVACY question].** Whether processing already started may continue is a legal question;
none is answered here. **Clinical implications [CLINICAL question].** An analysis that is nearly done may be costly to
repeat; discarding it may be required or may be unnecessary. **Operational implications.** B and C produce more failed
runs and exceptions for the ordering clinician (the exception owner for `consent_withdrawn` is the orderer:
`clinical/models/exception.py` line 140).

**Questions for counsel.** Is a processing activity that began lawfully before a withdrawal permitted to complete? Does
completing it differ from using its output?
**Questions for the clinical reviewer.** Is a partly completed run clinically reusable? What is the cost of repeating a
run if consent is later restored?
**Reviewer types.** Clinical reviewer and counsel; product decides how failures are surfaced.

**Decision record.** Decision: ______ Rationale: ______ Reviewer(s) and role: ______ Approval date: ______

---

### D3. Whether a result may be recorded after withdrawal

**In plain terms.** When the analysis finishes, the system saves the result as a draft report. If consent was withdrawn
before the save, should the save be allowed?

**Current behaviour [FACT].** `record_pipeline_result` (`clinical/data_access.py` line 7536) finds the sample, order and
patient (the lookup at line 7579 selects no consent) and checks the VCF hash (line 7593). It takes no consent argument and
reads no consent. It writes a sequencing run, VCF, interpretation and draft report in one transaction and audits each
(`pipeline_result_recorded`). Tested at the data layer: `TestTheSubmissionToResultWindowAtTheDataLayer`
(`clinical/tests/test_consent_characterization.py`, line 498) validates the order, withdraws the consent, records the
result, and observes that the draft report exists, that a fresh validation would now fail, and that staff genome access is
refused. The worker-level version is the test cited in D2.

**The exact unresolved question.** After a withdrawal, may the system record an analysis result, and if the result
already exists, must it be removed, hidden, kept but not used, or kept unchanged?

**Available choices (none recommended).**

| Choice | What it means | Technical consequence [ENGINEERING INFERENCE] |
|---|---|---|
| A. Record as today | The result is saved; later stages and readers decide | No change. Staff genome reads are already hidden under Rule 3. Review and release continue unless D4 says otherwise. |
| B. Refuse to record | The save checks consent inside the same transaction and raises an error | Closest to a "no gap" check: the read and write share a transaction. The completed analysis is lost; an exception is opened for the ordering clinician. Needs a locking read to be race-safe. No schema change. |
| C. Record, then withhold | The result is saved but marked unavailable | Needs a marker (schema change) and every reader to respect it. The evidence is kept. |
| D. Record and delete later | Save, then purge on a schedule | Interacts with retention rules and the retention role; not analysed here. |

**Affected stages.** Result recording, review, release, all readers of interpretations and reports.
**Code areas.** `record_pipeline_result`; `geper/api/submission_worker.py::_record_run` (line 570); the report readers.
**Schema implications.** A, B: none. C, D: a marker or a purge path.
**Retry behaviour.** After B, the worker's existing "recording failed" path runs
(`clinical_record_write_failed`); an operator then decides. Whether a rerun after re-consent should be automatic is part
of D7 and D12.
**Audit implications.** B: a failed recording is already audited as an error. C: the withholding itself would need an
audit entry. Neither needs patient detail.

**Timeline for a worked example (fictional).** Same submission as D2; result ready at 10:40.

| Withdrawal | Today [FACT] |
|---|---|
| Before 10:00 | Run refused; no result exists |
| 10:00 to 10:40 | Result recorded at 10:40 as a draft report; Rule 2 would now say "withdrawn" and Rule 3 hides it from staff |
| After 10:40 | The draft exists; review and release are not blocked (D4) |

**Privacy implications [LEGAL/PRIVACY question].** Whether a result produced from a sample while consent was in force
may be kept, and for how long, is a legal and retention question. **Clinical implications [CLINICAL question].** A
refused or hidden result may delay care; a retained result may be a record the clinic needs. **Operational
implications.** B wastes the compute; C needs reader changes; D needs a purge process.

**Questions for counsel.** May a result that finished after withdrawal be kept, used or deleted? Does the answer differ
if the analysis began before the withdrawal?
**Questions for the clinical reviewer.** What is clinically unacceptable about keeping an unused draft? What is lost if it
is discarded?
**Reviewer types.** Counsel and clinical reviewer.

**Decision record.** Decision: ______ Rationale: ______ Reviewer(s) and role: ______ Approval date: ______

---

### D4. Whether review, approval and release may continue after withdrawal

**In plain terms.** A draft report exists. Can clinicians still review, approve and release it to the patient after the
patient has withdrawn consent?

**Current behaviour [FACT].** None of these methods reads consent: `record_sole_signatory_claim` (line 10102),
`record_disagreement_claim` (line 10181), `submit_for_review` (line 10568), `_approve_report` (line 10897), `_release_report`
(line 11016); public wrappers `approve_report` (line 10239) and `release_report` (line 10279). Tests:
`TestReviewAndReleaseAfterWithdrawal` (`clinical/tests/test_consent_characterization.py`, line 551).

**The exact unresolved question.** Must any of these steps refuse after a withdrawal, and is release (the step that
delivers to the patient and records a delivery) treated differently from review?

**Available choices (none recommended).** A. Continue (today). B. Block each step. C. Block release only; allow review.
D. Block release and require a documented override.

**Technical consequences [ENGINEERING INFERENCE].** B, C, D: a check inside `_approve_report` and `_release_report`.
`_approve_report` already locks the report row (`FOR UPDATE`, line 10945), so a check there can sit in the same
transaction as the state change. `_release_report` reads the report state at line 11083 without a row lock in that
statement, so a race-safe check there would need its own locking decision. No schema change for B or C; D needs an override record. Release writes a delivery
record. It is not undone by a later consent check; the separate after-the-fact mechanism is retraction (D9).
**Affected stages.** Review, approval, release. **Code areas.** `clinical/data_access.py` review methods.
**Retry behaviour.** None; review is a human flow. **Audit.** A refusal would be an audited error with a fixed reason.
**Privacy [LEGAL/PRIVACY].** Delivering a report after withdrawal is the sharpest privacy question in the workflow; no
position is taken. **Clinical [CLINICAL].** A refused release of a clinically significant finding is a patient-safety
question. **Operational.** Reports stuck in review need an owner.

**Questions for counsel.** May a report be delivered to the patient, or to a clinician, after withdrawal? **Questions for
the clinical reviewer.** If a significant finding exists, what is the duty and the process? **Reviewer types.** Counsel
and clinical reviewer.

**Decision record.** Decision: ______ Rationale: ______ Reviewer(s) and role: ______ Approval date: ______

---

### D5. Whether a released report stays visible to the patient

**In plain terms.** After a report is released and the patient later withdraws consent, can the patient still see it in
their portal?

**Current behaviour [FACT].** The patient portal shows reports that were released and not retracted, with no consent
condition: `list_my_released_reports` (line 4058), `get_my_released_report` (line 4130), `list_my_report_trend`
(line 5827). A retracted report remains listed with a flag. Test:
`test_after_release_and_withdrawal_each_surface_behaves_as_recorded` (class
`TestWhatEachSurfaceShowsAfterReleaseWithdrawalAndRetraction`, line 582).

**The exact unresolved question.** Does a patient lose access to, keep access to, or keep access with a notice to a
released report after they withdraw consent?

**Available choices.** A. Keep (today). B. Hide. C. Keep and show a notice. **Technical consequences
[ENGINEERING INFERENCE].** B and C change the portal reader SQL in `clinical/data_access.py` (lines 4058 to 4180, 5827).
No schema change. Hiding a patient's own record could be a different privacy issue from hiding it from staff; this is a
question, not a conclusion. **Audit.** Unchanged. **Operational.** Support questions from patients.

**Questions for counsel.** What is the patient entitled to see of their own results after withdrawal? **Questions for the
clinical reviewer.** Can a patient safely lose sight of a result they already received? **Reviewer types.** Counsel;
clinical reviewer for patient-safety input; the founder for product behaviour.

**Decision record.** Decision: ______ Rationale: ______ Reviewer(s) and role: ______ Approval date: ______

---

### D6. Staff genome access and the patient's own genome read

**In plain terms.** Staff genome access already stops when consent is withdrawn. The patient's own genome view (a route
that is switched off by default) does not follow that rule. Should it, and is the staff behaviour right?

**Current behaviour [FACT].** Staff reads follow Rule 3 (`list_patient_genome_analyses`, line 4226, refusal at line
4278; `get_call_set_for_review`, line 5396; the call-set submit and validate paths at lines 4882 and 5076). The refusal
text is `GENOME_CONSENT_WITHHELD` (line 4168). The patient's own read, `list_my_genome_analyses` (line 4180), has no
consent condition, and its route is mounted only when a flag is set (`clinical/genome_endpoints.py::patient_routes_enabled`).
Tests: `TestTheConsentGate` (`clinical/tests/test_genome_analysis_endpoints.py`, line 663);
`TestBothConsentsMustBeActive` (`clinical/tests/test_callset_m3_policy_c.py`, line 104);
`test_the_patients_own_genome_read_after_release_and_withdrawal_is_currently_served` (Phase 1 file).

**The exact unresolved question.** Is immediate staff refusal the intended long-term behaviour, and should the patient's
own genome read be treated the same way?

**Available choices.** A. Keep both as they are. B. Apply Rule 3 to the patient's own read. C. Relax staff access for some
roles or purposes. **Technical consequences [ENGINEERING INFERENCE].** B: add the Rule 3 predicate to
`list_my_genome_analyses`; no schema change. C: needs a role/purpose model that does not exist. **Operational.**
Clinicians will notice that one withdrawn consent can hide all of a patient's analyses, as `docs/CLINICAL_DEPLOYMENT.md`
(lines 543 to 556) already warns.

**Questions for counsel/clinical reviewer.** Does withdrawal of one consent scope justify hiding all analyses of that
patient? Is the patient's own access treated differently? **Reviewer types.** Clinical reviewer and counsel.

**Decision record.** Decision: ______ Rationale: ______ Reviewer(s) and role: ______ Approval date: ______

---

### D7. Whether a newer consent can authorise an order tied to an older consent

**In plain terms.** A patient consents (consent A) and an order is created on it. A withdraws A and later gives a new
consent (B), or A is replaced by B. Does B now cover the existing order, or does the order need to be made again?

**Current behaviour [FACT].** The three rules answer differently (Section 4.2). Rule 2 lets the run proceed once B
exists, because it judges only the newest unsuperseded consent. Rule 3 keeps hiding the result because the order's own
consent (A) was withdrawn: the code comment above line 4351 says a newer consent "cannot revive an order whose own consent
was withdrawn". Rule 1 never reaches this case because it is only used when creating an order. Tests:
`TestTheThreeConsentRulesAreNotTheSameRule` (Phase 1, line 320) rows "withdrawn then newer active" and "withdrawn then
superseded"; `test_a_newer_active_consent_cannot_revive_a_withdrawn_order_consent`
(`clinical/tests/test_genome_analysis_endpoints.py`, in `TestTheConsentGate`).

**The exact unresolved question.** If the consent an order was created under is withdrawn, can a later consent allow work
on that same order, or must a new order be made under the new consent?

**Available choices (none recommended).**

| Choice | What it means | Technical consequence [ENGINEERING INFERENCE] |
|---|---|---|
| A. The order's own consent governs | A withdrawal ends the order for good; a new order is needed | Make Rule 2 follow the order's own consent. Runs for patients who re-consented now fail at preflight; tests change. No schema change. |
| B. The newest consent governs | A new consent revives the order | Make Rule 3 follow the newest consent. Staff would see data again in the divergent rows; this reverses the current Policy C behaviour. |
| C. Both must hold | Require both the order's consent and the newest consent | This is what Rule 3 does; extend to Rule 2. |
| D. Keep the rules different | Different rules at different stages | See D10; the divergence stays and must be documented per stage. |

**Affected stages.** Run start, result recording, staff reads, retry. **Code areas.** `check_consent` (line 8440),
`_CALLSET_*` constants (4351 to 4359), `validate_order_for_submission`. **Schema implications.** None.
**Retry behaviour.** Directly affected: after a new consent, an operator's "retry" resolution
(`requeue_resolved_exceptions`, line 9635) would succeed or fail depending on the choice.
**Audit.** Unchanged in kind.

**Timeline for a worked example (fictional).** Patient `PAT-EX-0002`; consent `CON-EX-A` (order `ORD-EX-0002` created on
it), then withdrawal, then `CON-EX-B`.

| Step | Rule 2 says | Rule 3 says [FACT] |
|---|---|---|
| Before withdrawal | OK | Eligible |
| `CON-EX-A` withdrawn | Withdrawn | Not eligible |
| `CON-EX-B` recorded (active) | **OK** | **Not eligible** |
| `CON-EX-B` later withdrawn | Withdrawn | Not eligible |

**Privacy [LEGAL/PRIVACY question].** Whether a fresh consent can retroactively cover work or data connected to an
earlier, withdrawn consent. **Clinical [CLINICAL question].** Whether a clinician can re-use a sample and analysis for a
patient who has re-consented, or must order again. **Operational.** A choice that forces new orders increases lab work;
a choice that revives orders needs a clear record of which consent covered which step.

**Questions for counsel.** Can a later consent authorise processing connected to an order created under a consent that
was withdrawn? Does the answer depend on what the later consent says (scope, wording, date)? **Questions for the
clinical reviewer.** Is re-using a collected sample after re-consent acceptable, and what record is needed?
**Reviewer types.** Counsel and clinical reviewer.

**Decision record.** Decision: ______ Rationale: ______ Reviewer(s) and role: ______ Approval date: ______

---

### D8. How a superseded consent should behave

**In plain terms.** A consent can be replaced by a newer one ("superseded"). Does a replaced consent still count?

**Current behaviour [FACT].** Rule 1 treats a superseded consent as inactive (`create_order`, filter at line 3284). Rule 2
skips superseded consents (`check_consent`, line 8452 query). Rule 3 ignores supersession on purpose; the comment above
line 4351 gives the reason: a supersession pointer is mutable and letting it decide could allow a withdrawn consent to be
revived by re-pointing it. Recording a superseding consent sets the old record's pointer even if the old record is
withdrawn (`record_consent`, line 3139; update at line 3196). The database prevents a withdrawal or supersession from
being undone or re-pointed while a call set stands on that patient's order (trigger at `clinical/schema.sql` line 2992,
function at line 2928) **[FACT]**; without a call set, nothing at the database level prevents it, though no application
method does so **[ENGINEERING INFERENCE]**.

**The exact unresolved question.** Should a superseded consent be treated as active, inactive, or irrelevant, and may a
withdrawn consent be superseded?

**Available choices.** A. Keep the three behaviours. B. Make all rules ignore supersession. C. Make all rules filter on
supersession. D. Refuse to supersede a withdrawn consent. **Technical consequences [ENGINEERING INFERENCE].** B/C change
Rule 1 or 2 or 3 (see Section 4.4). D changes `record_consent` and needs an audit and error path. **Schema.** None, though
a database-level guard against undoing a withdrawal in all cases would be a schema change.

**Questions for counsel/clinical reviewer.** What does "replacing" a consent mean in law and in practice, and can a patient
replace a withdrawal? **Reviewer types.** Counsel; clinical reviewer for the operational meaning.

**Decision record.** Decision: ______ Rationale: ______ Reviewer(s) and role: ______ Approval date: ______

---

### D9. What happens to amended or retracted reports

**In plain terms.** A released report can be corrected (amended) or withdrawn from use (retracted). Does consent
withdrawal interact with either?

**Current behaviour [FACT].** `create_amendment` (line 11480) requires the Approver role and a report in approved or
released state; `retract_report` (line 11337) writes a retraction row and creates a notification debt for every prior
recipient. Neither reads consent. A retracted report stays listed for the patient with a retraction flag; the retraction
reason is not shown to the patient. Test: `test_after_retraction_the_portal_still_shows_the_report_flagged` (Phase 1,
class at line 582).

**The exact unresolved question.** After withdrawal, may a report be amended, must a released report be retracted,
and what notices go to prior recipients?

**Available choices.** A. Independent of consent (today). B. Block amendment after withdrawal. C. Automatic retraction on
withdrawal. D. Retraction by a person, prompted by an alert. **Technical consequences [ENGINEERING INFERENCE].** C would
create notification debts for each prior recipient and needs a trigger path from withdrawal to retraction (a new
dependency from the consent code to the report code). D needs an alert or work-queue surface. **Schema.** C/D may need an
alert record. **Audit.** Retraction is already audited; consent-driven retraction would need its cause recorded without
patient detail.

**Questions for counsel/clinical reviewer.** Is a report issued under a consent that is later withdrawn a report that must
be retracted, or only one that must not be re-issued? Who must be told? **Reviewer types.** Counsel and clinical reviewer.

**Decision record.** Decision: ______ Rationale: ______ Reviewer(s) and role: ______ Approval date: ______

---

### D10. Which consent rule is authoritative at each stage

**In plain terms.** There are three consent rules. Which one, if any, should decide at each stage?

**Current behaviour [FACT].** Section 4. No stage is designated.
**The exact unresolved question.** One rule everywhere, a chosen rule per stage, or the current mix documented and kept.
**Available choices.** A. One rule everywhere (needs D7 and D8 first). B. A chosen rule per stage. C. The current mix, with
the matrix kept up to date. **Technical consequences [ENGINEERING INFERENCE].** A: one shared predicate in the data layer
used by every check; the largest test impact. B: a documented table, one predicate per stage. C: no behaviour change.
This document does not choose.
**Reviewer types.** Counsel and clinical reviewer to decide the principle; engineering to implement it.

**Decision record.** Decision: ______ Rationale: ______ Reviewer(s) and role: ______ Approval date: ______

---

### D11. Whether the submission entry point should check the order and consent

**In plain terms.** The web address that accepts a submission does no clinical checking. Should it?

**Current behaviour [FACT].** `post_interpretation` (`geper/api/main.py` line 539) resolves the VCF path and stores the
submission, including the caller-supplied `order_id`, `sample_id` and `consent_ref` (request model at line 312, the
`consent_ref` field at line 319, stored at line 588). The worker comment at `geper/api/submission_worker.py` lines 720 to
727 states that `consent_ref` is stored and deliberately not passed on. No clinical check happens until the worker's
preflight (line 443). **[ENGINEERING INFERENCE]** The `consent_ref` is therefore free text that nothing verifies against a
real consent.

**The exact unresolved question.** Should the entry point reject a submission whose order, sample or consent is not
acceptable at the time of the request, and should `consent_ref` be removed, validated, or left as an unchecked note?

**Available choices.** A. Leave as is. B. Validate the order and consent at the door (fail fast). C. Drop or validate
`consent_ref`. **Technical consequences [ENGINEERING INFERENCE].** B needs the processing service to query the clinical
data store, which today it reaches only through the worker; a failed check at the door does not remove the need for the
later check, because consent can change in between. C changes an API field (a compatibility question).
**Reviewer type.** Product (founder); counsel only if `consent_ref` is meant to carry legal meaning.

**Decision record.** Decision: ______ Rationale: ______ Reviewer(s) and role: ______ Approval date: ______

---

### D12. Whether a withdrawal can ever be undone

**In plain terms.** If a patient changes their mind, can the withdrawal be reversed, or must a new consent be recorded?

**Current behaviour [FACT].** No application method reverses a withdrawal. A new consent is a new record. At the database
level, while a call set stands on the patient's order, a withdrawal cannot be undone or re-pointed (trigger at
`clinical/schema.sql` line 2992); otherwise the database does not forbid it **[ENGINEERING INFERENCE]**.

**The exact unresolved question.** Is a withdrawal final, with re-consent only as a new record, or can a withdrawal be
reversed in some circumstances (for example, recorded in error)?

**Available choices.** A. Final; new consent only (today's application behaviour). B. Allow a reversal for a recorded
error, with a reason and audit. C. Make finality a database rule everywhere. **Technical consequences
[ENGINEERING INFERENCE].** B needs a new method, reason capture and audit. C is a schema change. Interacts with D7.
**Reviewer types.** Counsel and clinical reviewer.

**Decision record.** Decision: ______ Rationale: ______ Reviewer(s) and role: ______ Approval date: ______

---

## 6. Reviewer guides

### 6.1 Founder and product decisions

You can decide on your own authority only product matters, and only as a business position that stays subject to legal
and clinical review. Points that are yours to settle or sequence:

* **D11** (entry-point checking) and the behaviour of `consent_ref`.
* How refusals and exceptions are surfaced to lab staff and clinicians once a clinical or legal decision is made.
* In what order to put the questions to a lawyer and a clinical reviewer (suggested by cost of getting it wrong: D1, D3,
  D4, D5, then D7).
* Whether to commission a documentation-only update of the consent matrix and a preparation step with no behaviour
  change while decisions are pending.

Do not treat your approval of D1 to D10 or D12 as settling them; the others need qualified review first.

### 6.2 Indian legal and privacy counsel

**What this document gives you.** Facts about the data flows (Sections 3 and 4), one decision per page with the current
behaviour, and the exact questions. It gives no legal analysis and cites no statute or rule, deliberately.

**Facts counsel will probably want and that this document does not contain.** The data fields held per patient, retention
and deletion behaviour (see `docs/CLINICAL_DEPLOYMENT.md` and the retention code), who the deployment's operators are,
where data is stored, and the wording of the consent presented to patients. These should be supplied separately.

**Questions, by decision.** D1: effect on orders, samples and associated data. D2: running processing when consent is
withdrawn. D3: keeping or deleting results finished after withdrawal. D4: delivering reports after withdrawal. D5:
patient access to their own released results. D6: scope of what a withdrawal hides. D7: whether a later consent can cover
earlier processing. D8: meaning of replacing a consent. D9: reports issued under a later-withdrawn consent. D12: whether
a withdrawal can be reversed. The project's specification already marks withdrawal handling as needing counsel
(`GEPER_CLINICAL_PLATFORM_SPEC.md`, section 6.3); `docs/CLINICAL_DEPLOYMENT.md` line 1102 records the same.

### 6.3 Clinical genetics or laboratory reviewer

**What this document gives you.** The workflow in clinical terms (Section 3), the points at which a withdrawal can arrive,
and the effects of each option on the lab.

**Questions, by decision.** D1: what should happen to a collected sample and an order in progress. D2: whether a nearly
finished analysis should be stopped. D3: whether an unused draft or result is acceptable to keep. D4: the process if a
significant finding exists. D5: the clinical safety of removing a delivered result from the patient. D6: whether hiding all
of a patient's analyses for one withdrawn scope is workable. D7: whether a sample can be re-used after re-consent. D9: the
clinical handling of a report issued under a later-withdrawn consent, and who is told. D12: when a withdrawal might be
reversed.

**Boundary.** Nothing here has been clinically validated. The clinical reviewer's role is to decide or advise on workflow
and safety; the software documented here has no clinical validation status.

## 7. Decision and approval record template

Use one copy per decision. A decision stays pending until every required reviewer type has signed.

```
Decision ID:                     D__
Title:
Status:                          PENDING / APPROVED / REJECTED / DEFERRED
Question as finally worded:
Choice selected (A / B / C / D / other, described):
Rationale:
Facts relied on (cite this brief's sections or code references):
Assumptions and their owners:
Conditions or limits on the decision:
Product decision by (founder):   name ____ date ____
Clinical reviewer:               name ____ qualification ____ date ____
Legal/privacy counsel:           name ____ firm ____ date ____
Engineering impact estimated by: name ____ date ____
Tests that change:
Matrix rows that change (docs/CONSENT_ENFORCEMENT_MATRIX.md):
Next review date:
Superseded by decision:
```

**Change log.**

| Date | Decision | Change | By |
|---|---|---|---|
| | | | |

## 8. Source references

All as of commit 72e706d447e70e9ca6a4829c89010dadcf1eb457.

### 8.1 Code

* `clinical/data_access.py`: `create_patient` 3056; `record_consent` 3139 (supersession update 3196); `create_order` 3217
  (consent filter 3284); `place_order` 3328; `cancel_order` 3365; `receive_sample` 3596; `record_qc` 3677;
  `withdraw_consent` 3723 (update 3764); `GENOME_CONSENT_WITHHELD` 4168; `list_my_released_reports` 4058;
  `get_my_released_report` 4130; `list_my_genome_analyses` 4180; `list_patient_genome_analyses` 4226 (refusal 4278);
  `_CALLSET_ORDER_CONSENT_OK_SQL`, `_CALLSET_LATEST_CONSENT_OK_SQL`, `_CALLSET_CONSENT_OK_SQL` 4351 to 4359;
  `_callset_chain_facts` 4418; `_callset_assert_eligible` 4467 (refusal 4477); `_submit_call_set_impl` 4882;
  `validate_call_set` 5076; `list_call_sets_for_run` 5315; `get_call_set_for_review` 5396 (refusal 5412);
  `_patient_call_set_evidence` 5434; `list_my_report_trend` 5827; `record_pipeline_result` 7536 (lookup 7579, hash check
  7593); `check_consent` 8440 (ordering 8452); `validate_order_for_submission` 8796 (consent check 8845);
  `create_or_reopen_exception` 9409; `requeue_resolved_exceptions` 9635; `record_sole_signatory_claim` 10102;
  `record_disagreement_claim` 10181; `approve_report` 10239; `release_report` 10279; `submit_for_review` 10568;
  `_approve_report` 10897; `_release_report` 11016; `retract_report` 11337; `create_amendment` 11480.
* `geper/api/submission_worker.py`: `_vcf_changed_since_preflight` 394; `_clinical_preflight` 443 (validation call 493);
  `_adopt_existing_record` 558; `_record_run` 570; `reconcile_interrupted_submissions` 612; `process_queued_submission` 657;
  engine started 809; VCF re-check 846; comment on `consent_ref` 720 to 727.
* `geper/api/main.py`: `InterpretationSubmissionRequest` 312 (`consent_ref` 319); `post_interpretation` 539 (stored at 588).
* `clinical/schema.sql`: `consents` table 492; `consents_patient_scope_latest` index 525; `callset_identity_guard` 2928;
  `trg_callset_identity_consents` 2992.
* `clinical/models/exception.py`: `CONSENT_WITHDRAWN` 67; owner mapping `"orderer"` 140.
* `clinical/genome_endpoints.py`: `patient_routes_enabled`.

### 8.2 Tests

* `clinical/tests/test_consent_characterization.py`: `TestTheThreeConsentRulesAreNotTheSameRule` 320;
  `TestOrderCreationAfterWithdrawal` 427; `TestStagesBetweenOrderCreationAndSubmissionAfterWithdrawal` 445;
  `TestTheSubmissionToResultWindowAtTheDataLayer` 498; `TestReviewAndReleaseAfterWithdrawal` 551;
  `TestWhatEachSurfaceShowsAfterReleaseWithdrawalAndRetraction` 582; `TestTheAuditTrailAroundAWithdrawal` 654.
* `geper/api/tests/test_worker_consent_withdrawal_during_run.py`: the three tests at lines 181, 219 and 241.
* `clinical/tests/test_phase_5d_orchestration.py::test_withdrawn_consent_creates_exception` (line 197).
* `clinical/tests/test_w166_catalogue_and_orders.py`: `test_a_withdrawn_consent_cannot_back_an_order` 287;
  `TestAWithdrawnConsentCannotBeOrderedOn` 407.
* `clinical/tests/test_genome_analysis_endpoints.py::TestTheConsentGate` 663.
* `clinical/tests/test_callset_m3_policy_c.py::TestBothConsentsMustBeActive` 104.
* `clinical/tests/test_w162_registration_and_consent.py::test_withdrawal_is_audited_to_the_withdrawing_clinician` 1277.
* `clinical/tests/test_staff_read_endpoints.py::test_a_withdrawn_consent_displays_as_withdrawn` 419.

### 8.3 Documents

* `docs/CONSENT_ENFORCEMENT_MATRIX.md` (unchanged by this document).
* `docs/CLINICAL_DEPLOYMENT.md`: lines 491 to 499 (an order on a withdrawn consent; withdrawal's effect on existing orders is
  unresolved); 528 to 531 (the same as a policy decision needing counsel); 543 to 556 (Policy C description and its effect
  on clinicians); 1102 (the blocker table row).
* `GEPER_CLINICAL_PLATFORM_SPEC.md`: section 6.3 "Withdrawal", line 308.

### 8.4 Not verified, or verified only partly

* The "Policy C (approved)" label (comment above `clinical/data_access.py` line 4351) is in the code; I found no record
  of who approved it or in what capacity.
* Concurrent behaviour (races) has not been demonstrated; see Section 3.4.
* No legal source has been consulted or cited anywhere in this document.

## 9. Glossary

| Term | Meaning here |
|---|---|
| Consent | A record that a patient agreed to a stated purpose (the scope), kept in the `consents` table. |
| Scope | The purpose a consent covers, for example "testing". |
| Withdrawal | A consent marked withdrawn, with time and user. The record is kept. |
| Supersession | A consent marked as replaced by a newer one. |
| Order | A clinician's request for a test, created on one named consent. |
| Order's own consent | The consent named when the order was created. It never changes. |
| Preflight | The checks the worker runs before an analysis begins. |
| Engine / run | The analysis program and one execution of it. |
| Result recording | Saving a finished analysis as a run, VCF, interpretation and draft report. |
| VCF | The file of genetic variants analysed. |
| Draft / approved / released | Report states; release delivers to the patient portal. |
| Retraction | A record that a report must not be acted on. The report is not deleted. |
| Amendment | A new report that corrects an earlier one. |
| Policy C / Rule 3 | The rule for staff genome and call-set access (Section 4). |
| Exception | A work item opened for a person when a precondition fails. |
| Tenant / organisation | One hospital or laboratory customer; data of different organisations is kept apart. |

## 10. What this document does not do

* It does not decide any of D1 to D12, and it does not recommend an outcome.
* It does not choose an authoritative consent rule.
* It does not state, interpret or predict any law, regulation or accreditation requirement, and it cites none.
* It does not claim DPDP compliance, NABL compliance, clinical validation or regulatory approval of any kind.
* It does not describe how any other provider handles consent.
* It does not change application behaviour, tests, or `docs/CONSENT_ENFORCEMENT_MATRIX.md`.
* It does not make a founder's approval equivalent to legal or clinical approval.
* Its engineering inferences are not facts and may be wrong; they are listed so that they can be tested or withdrawn.
