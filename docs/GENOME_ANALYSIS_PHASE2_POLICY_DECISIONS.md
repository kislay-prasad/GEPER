# Genome Analysis Phase 2: policy decisions awaiting a ruling

Status: **for review. Only items marked APPROVED below are decided; nothing here is implemented.** Each item says what the code does today, what the options are, the smallest change for each, and what it costs. Clinical decisions are separated from infrastructure decisions. Where a recommendation is given it is only a recommendation.

Reference points: `clinical/data_access.py` (consent: `_CALLSET_CONSENT_OK_SQL`; map: `_patient_call_set_evidence`; reviewer read: `get_call_set_for_review`; status: `get_call_set_status`, `list_call_sets_for_run`), `docs/GENOME_ANALYSIS_PHASE2_REVIEW_CORRECTIONS.md` sections 9 and 10.

## Already decided (for context)

- **Consent eligibility is Policy C** (approved): the order's own consent AND the patient's latest consent for the scope must both be active. A newer consent never revives an order whose own consent was withdrawn.
- **C1 is APPROVED: Option A, hide only** (see C1 below). The legal-basis condition is NOT yet satisfied.

---

## Part 1: Clinical decisions

### C1. Withdrawal: hide only, or retract? — APPROVED (Option A, hide only)

> **DECISION (approved by the user): C1 Option A, hide only.**
> - When consent is withdrawn, genomic evidence must no longer be visible through protected evidence views.
> - Original genomic files, parsed documents and lifecycle history remain stored.
> - No `retracted` event is created.
> - The call-set lifecycle status is not changed solely because consent is withdrawn.
> - **CONDITION: a documented and legally reviewed basis for retaining genomic data after withdrawal is REQUIRED before production release. This condition is NOT yet satisfied.**
>
> Implementation status: no change required to satisfy the behaviour; the legal-basis condition is open. The checked code paths and open items were reported in the review session and are not stored in this repository.

- **Today.** Withdrawal hides the genomic document and removes the call set from the map. The bytes, the parsed document and the lifecycle history are kept. Nothing is retracted and no event is recorded on the call set.
- **Options.** (a) Keep hide-only. (b) Record a `retracted` event on withdrawal so the call set leaves the validated state. (c) Redact content on withdrawal.
- **Consequences.** (a) is reversible only by a new order under a new consent (see C3), and the data stays in the database after the patient withdrew. (b) makes the lifecycle say what happened, but it must be undone if the withdrawal was a mistake, and the database forbids un-withdrawing. (c) removes the data, in tension with retention duties and the integrity design.
- **Smallest change.** (a) none. (b) a system actor writes the event inside `withdraw_consent`; it needs a new trigger path because call-set events are written by role-checked acts. (c) a retention-side change.
- **Needed from you.** Which of (a), (b), (c), and whether data held after withdrawal needs a legal basis statement.

### C2. What do status views show while consent is withdrawn?

- **Today.** `get_call_set_status` and `list_call_sets_for_run` show state, class, assembly, caller and record **counts** by caller filter, to the submitting technician and to reviewers, regardless of consent. The document is withheld; the counts are not.
- **Options.** (a) Keep. (b) Hide counts as well when consent is withdrawn. (c) Hide all of the status except "held back".
- **Consequences.** (a) leaks the size of the finding set (not coordinates) to people who may no longer have a basis to see it. (b) and (c) make a technician unable to see why their submission appears stuck.
- **Smallest change.** (b) apply `_callset_chain_facts` consent to `get_call_set_status` and null `counts`. About ten lines plus tests.
- **Needed from you.** Whether counts count as genomic content.

### C3. After the order's own consent is withdrawn, can evidence ever return?

- **Today (Policy C).** No. A newer consent does not restore it. The route back is a new order under the new consent, with a new sample and run.
- **Question.** Is that operationally acceptable for a re-consent after a mistaken withdrawal? If not, the alternatives are a "consent re-instated" act that records an explicit link from a new consent to the withdrawn one, or allowing a clinician to re-point the order.
- **Smallest change for a re-instatement link.** A new column and an audited act; the identity guards would have to allow it deliberately. This weakens the "no revival" property unless it is tightly gated.

### C4. Should validation be blocked while a withdrawal is in flight?

- **Today.** Validation and withdrawal do not wait on each other. If the withdrawal commits between validation's consent check and its commit, the call set ends up `validated` while the consent is withdrawn. Every read re-checks consent, so nothing is exposed. Reproduced across separate connections in Review 5.
- **Options.** (a) Accept (the read-time check is the control). (b) Make validation lock the order's consent rows and make withdrawal take the same lock.
- **Consequences.** (b) removes the window; it adds a lock-order rule that must fit the existing hierarchy (call-set locks first, then content locks).
- **Smallest change for (b).** `FOR SHARE` on the consent rows in `_callset_chain_facts(lock=True)`, and a matching row lock in the withdrawal. Needs a deadlock test.

### C5. One consent rule or two?

- **Today.** `check_consent` (used when an order is checked for automatic submission) uses a different rule: the latest consent with `superseded_by IS NULL`, and it ignores the order's own consent. Call-set eligibility uses Policy C and ignores `superseded_by`.
- **Question.** Should order submission use Policy C too?
- **Consequence.** Two definitions of "active consent" can disagree for the same patient, for example after a supersession.

### C6. Near-duplicate VCF bytes (L2)

- **Today.** Identity is the SHA-256 of the exact stored bytes. A file with an extra trailing newline is a different call set. The one-validated-per-(run, class, assembly) rule stops it being drawn twice. The gap is a near-duplicate accepted under the other class.
- **Options.** (a) Keep exact bytes. (b) Add a second, canonical-content hash used only by the duplicate rules, keeping the exact-byte hash for integrity and replay.
- **Consequences.** (b) needs a new column, a trigger rule and a ruling on what "canonical" ignores (line endings, trailing whitespace, header comments, record order). It changes which resubmissions count as "the same".
- **Needed from you.** (a) or (b), and if (b), the normalisation list.

### C7. Which orders and tests may have their evidence drawn?

- **Today.** A call set is drawn when the sample passed QC, the order is not cancelled, consent holds and the assembly matches. A **draft** order and a **retired** test are not excluded.
- **Question.** Should draft orders and retired tests be eligible? (a) Yes, as today. (b) Draft orders no. (c) Retired tests no.
- **Smallest change.** One extra condition in the map query and in `_callset_assert_eligible`, plus a new held-back reason.

### C8. Who may see the held-back counts?

- **Today.** The map reports how many validated call sets are held back and why, in words, to clinicians who may read the map (including Orderers).
- **Question.** Should an Orderer see "held back because consent is withdrawn", or only "no genome evidence shown"?
- **Consequence.** The current wording tells an Orderer that evidence exists. Hiding it removes the explanation for an apparently empty map.

### C9. What can a technician learn from a refusal?

- **Today.** A refused submission says only "This sequencing run cannot accept this call set." whatever the reason (consent, QC, order state, assembly), by design, so the message does not reveal clinical state.
- **Question.** Is that acceptable to the people submitting, or should they be told a category (for example "eligibility")?

---

## Part 2: Infrastructure decisions

### I1. Cache budget

- **Today.** 100,000 records per process, about 1.4 KB per record, about 141 MB per full cache. A call set larger than the budget is re-verified on every read (about 2.3 s per 40,000 records). Caches are per process, never shared.
- **Question.** Keep the default, or set it per deployment (`CLINICAL_CALLSET_CACHE_RECORDS`)? A 200,000-record budget is about 282 MB per process and made repeat reads 0.31 s in the benchmark.

### I2. Retention

Open: the retention duration, whether backups are erased, which metadata survives redaction, how orphaned evidence is handled, and size limits.

### I3. Database ownership and grants

- **SECURITY DEFINER ownership.** Who owns the pinned functions and how they are rotated.
- **Grant narrowing on other tables.** `call_sets` is now column-granted without the ordering column; `tests`, `sequencing_runs`, `samples` and `orders` still use broad grants.

### I4. Parser rule-set retirement

- **Today.** A call set whose parser rule set is not registered is refused as `integrity_failure` (warm and cold alike). No retirement procedure exists.
- **Needed.** A written procedure for retiring a rule set without making historic evidence unreadable.

---

## Suggested order for a ruling

1. C1, C2 and C3 together (they define what withdrawal means).
2. C6 (it changes storage if (b) is chosen, so decide it before any release).
3. C4, C5, C7, C8, C9 (smaller, independent).
4. I1 to I4.
