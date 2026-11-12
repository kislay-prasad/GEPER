# Genome Analysis Phase 2 — Corrections after the Final Independent Review

Status: **implemented in the working tree, uncommitted, nothing applied to a real database.** Findings are those of
`GENOME_ANALYSIS_PHASE2_FINAL_REVIEW.md`. All evidence below comes from disposable PostgreSQL 16 databases.

Evidence tags: **[ran]** a test in the final suite; **[mutation]** the test was shown to fail when the control is removed;
**[measured]** a synthetic benchmark; **[not verified]** stated so.

## 1. Resolution of each finding

| Finding | Resolution | Evidence |
|---|---|---|
| **H-1** gates | `submit_call_set`, `validate_call_set` and the map reader all enforce: sample QC `passed`; order not `cancelled`; an active consent (latest non-superseded consent for the order's scope not withdrawn - the rule `check_consent` uses); run/sample/order/patient as one chain in the organisation; the file's sample is the run's sample; declared assembly = ordered test and = any live small-variant analysis of the run (both values named in the refusal). One statement reads the chain; the map reader applies the same predicates in its single SQL statement. A parse alone is never eligibility. | [ran] 10 submit/validate/map tests; [mutation] 7 of 7 gate removals fail a named test |
| **M-1** purge/event race | One advisory lock per call set (key = its id), taken by every lifecycle verb, by a replacement that names the call set, by the purge, **and by the database** (`call_sets_before_insert`, `call_sets_immutable`). The purge locks each candidate in a fixed order, then **re-checks eligibility under the lock**. | [ran] 5 deterministic concurrency tests + 1 database-level; [mutation] see §4 |
| **M-2** technician reads | Technicians get `get_call_set_status` / `list_call_sets_for_run`: state, class, assembly, caller, record **counts**, lifecycle with the reason only for rejection or withdrawal, and only for call sets they submitted. `get_call_set_for_review` (document) is Interpreter/Approver only and no longer loads the bytes. No bytes, document, coordinates or variant records are reachable. The only HTTP surface is the map, which refuses technicians; a test asserts no route exposes call-set detail. | [ran] 5 tests; [mutation] role widening fails |
| **M-3** audit | Actions per the approved design: `caller_proposed/approved/retired`, `call_set_submitted/validated/rejected/withdrawn/retraction_requested/retraction_declined/retracted`, `call_set_read`; map reads are `genome_read` (no separate `genome_callset_read`). **No details.** Atomic: `@auditable(atomic=True)` commits the success row with the act (verified by equal `xmin`); a failed success-audit write rolls the act back; a refusal rolls the act back and is then recorded `denied`/`error`. Historical rows are never rewritten. | [ran] 5 tests; [mutation] non-atomic fails |
| **M-4** limits | The 400,000 record ceiling is **removed**. Limits are configuration (`CLINICAL_CALLSET_MAX_BYTES`, `CLINICAL_CALLSET_MAX_RECORDS`, provisional defaults 5 MiB / 40,000); the only code-level bound is the schema's own 64 MiB CHECK. Enforcement is before any decoding or parsing (bytes, then a line count with no record built), and a derived document over the stored limit is a clean refusal. | [ran] 3 tests; [mutation] pre-parse check removal fails; benchmark in §3 |
| **M-5** parser versions | **Neither offered option fits an immutable record, so a third is used:** every rule set that ever wrote a call set stays registered (`PARSER_RULESETS`); validation re-derives with the **recorded** version; new submissions use the current one; an unavailable rule set is a refusal, never a pass. A pending call set is therefore never stranded by a bump and its evidence is never silently regenerated. | [ran] 3 DAO + 4 unit tests; [mutation] validating with the current parser fails |
| **M-6** identity | Four database triggers (`callset_identity_guard`) refuse repointing `sequencing_runs.sample_id`, `samples.order_id`, `orders.patient_id/test_id`, `tests.assembly` (and org) while a call set stands on them; validation reads the chain `FOR SHARE`; the map re-checks the sample (`vcf_sample_name` is the immutable snapshot), assembly, QC, order and consent. **The broad UPDATE grants on those tables were not changed** (see §5). | [ran] 9 tests; [mutation] trigger removal fails |
| **M-7** CI | The lifecycle/security suite is copied to `clinical/tests/test_callset_lifecycle_db.py` (206 tests, collected by the suite). The original design suite (`test_lifecycle.py`) is not included in the repository. | [ran] 206 passed |
| **M-9** tests | The mislabelled cross-run test is replaced by tests that use the same sample on two runs; CNV is covered through the DAO (extreme quality 1.5e22, negative CN, END before POS, non-numeric POS, NaN quality, position off the chromosome); the pure functions have 23 direct tests. | [ran] |
| **Replay** | **Superseded by section 7** (wave 3 defines the behaviour by call-set state and adds a resubmission verb). |

## 2. Changes to the reviewed v3 SQL (migration 0010 was never applied, so it was edited, not followed by an 0011)

1. `call_sets_before_insert`: a replacement takes the predecessor's lifecycle lock.
2. Cross-run duplicate rule: refused for any earlier non-tombstoned submission of the same bytes on another run unless it was retracted with
   `reason_code = wrong_run_placement`.
3. `cse_checks_shape`: a `retracted` event may carry exactly `{"reason_code": "wrong_run_placement"}`.
4. `call_sets_immutable`: a tombstone or redaction takes the call set's lifecycle lock.
5. New `callset_identity_guard()` (15th function) and four `BEFORE UPDATE OF` triggers.

The migration header lists these; `test_callset_migration.py` allows **only** these deltas against the reviewed text. `schema.sql` carries the
identical body and the new checksum stamp.

## 3. Resource measurements (synthetic; not a production limit)

Five synthetic workloads (simple SV, SV with intervals and a long INFO, BND pairs, CNV with copy number, 70 % invalid CNV) at 10k/40k/100k/200k
records, each in its own process (Windows, Python 3.14). Submit = parse + compact document + JSON; validate = fresh parse + comparison.

| Records | Parse (s) | Submit peak RSS | Validate path (re-parse) | Derived document |
|---|---|---|---|---|
| 40,000 | 0.4 - 0.6 | 212 - 291 MB | 1.0 - 1.2 s; whole pipeline ~495 MB | ~29 MB |
| 100,000 | 1.2 - 1.7 | 580 - 732 MB | 2.7 - 3.4 s | ~72 - 75 MB |
| 200,000 | 3.0 - 4.0 | 983 - 1452 MB | 5.6 - 7.3 s; whole pipeline ~2.4 GB | ~145 - 152 MB |

What this does and does not support: memory is about 4.6 - 6.6 KB per record on submit and ~11 KB for submit + verify + read; **time is not the
constraint, memory is**; the derived document is **5 - 11x the source**, so a ~180k-record file exceeds the 128 MiB stored-document limit (now a
clean refusal). The 40,000 default stays **provisional**. These are synthetic records from one machine; real caller output, concurrent requests
and the production memory budget are **not measured**, so no production limit is chosen here.

## 4. Concurrency results (deterministic: wait on a lock condition, never a guessed sleep)

| Scenario | Result |
|---|---|
| Purge in progress, then a retraction request | request waits, is refused as tombstoned; no untombstoned event; no trace of the refused event |
| Retraction request in flight, then purge | purge waits; the committed event is tombstoned by the cascade |
| Replacement in flight, then purge of its predecessor | purge waits, re-checks under the lock, **skips** the predecessor (a live successor now exists) |
| Validation (with supersession) in flight, then purge | purge waits; the successor is redacted with its validated event; predecessor stays superseded and intact |
| Withdrawal in flight, then validation | validation waits, is refused (withdrawn) |
| Raw redaction SQL (no application purge code) vs an event in flight | waits (the **database** lock) |
| Two identical submits at once | one call set, one event |
| Two replacements / two retractions at once | exactly one succeeds |
| Batch of two, failure on the second audit row | both call sets and all events untouched; no audit row |
| Success audit write fails | the act does not exist |

Mutation results: removing both the DAO and database locks fails the race tests; removing only the **purge's explicit lock** fails (the re-check
must happen under the lock); removing only the **database** lock passes the application-path tests - the DAO lock suffices there - and is caught
only by the raw-SQL test above. The database lock therefore protects any purge not written through `RetentionPrincipal`.

## 5. Not done, or only partly, and why

* **Grants not narrowed.** `clinical_app` still has column UPDATE on `tests`, `sequencing_runs`, `samples`, `orders`. Narrowing them changes
  tables outside this feature and could break unrelated tests and code; the triggers, row locks and read-time re-check close the call-set
  consequences instead. A separate grant-narrowing wave remains recommended.
* **Consent policy is conservative and unratified.** Evidence disappears from the map when consent is withdrawn and returns if it is restored.
  Whether withdrawal should instead *retract* is a policy ruling (the platform's own `withdraw_consent` states withdrawal is not retroactive).
* **Audit of a replay.** A replayed identical submit writes an ordinary success row (the audit vocabulary has no way to mark it).
* **Parser rule sets must be kept.** Retiring one strands the pending call sets recorded under it; a test pins that the current version is
  registered and every registered rule set reproduces its own documents.
* **Historical audit rows** from disposable test runs (old action names) are not rewritten; none exist outside those databases.
* **SECURITY DEFINER ownership unchanged** (the `postgres` superuser per `docker-compose.clinical.yml`); a dedicated owner is still a follow-up.

## 6. Documentation status

Corrected: the review document's §0 status, the design SQL header, the storage design §10.2, the Phase 2 document. Still open and marked as
decisions: final retention duration; backup/WAL/replica erasure; metadata that survives redaction; orphan evidence; production size limits;
production SECURITY DEFINER ownership; consent-withdrawal behaviour; the parser retirement procedure.

## 7. Wave 3 (second independent review): H-A, M-A .. M-E

Evidence tags as above. Every item has a regression test that fails when the control is removed (19 mutants, all caught).

| Finding | Resolution |
|---|---|
| **H-A** replay of a terminal call set | Replay is defined **by state**. PENDING or VALIDATED, same submitter and parameters: the existing call set is returned (idempotent; racing identical requests become one). REJECTED or WITHDRAWN: `CallSetConflictError` naming the state to its own submitter and pointing at `resubmit_call_set` - **never a successful return**. RETRACTED or SUPERSEDED: refused, "cannot be resubmitted unchanged". Another technician gets only the generic conflict text. **New verb `resubmit_call_set(session, call_set_id, reason)`** (action `call_set_resubmitted`): submitter only, rejected or withdrawn only, bytes taken from the stored original, a NEW call set with fresh identity, current parser, fresh retention snapshot, every eligibility check and its own validation; lineage in the new `call_sets.resubmission_of`. |
| duplicate protection | The unique index (which made resubmission impossible) is replaced by a **trigger rule under the same content lock**: (1) the same bytes may be live once on a run; (2) bytes the run has already seen need an explicit `resubmission_of` naming a live rejected/withdrawn call set of that run, class and file; (3) unchanged cross-run rule: another run's non-tombstoned submission blocks unless retracted with `wrong_run_placement` - and only the **latest** call set of those bytes on that other run counts, so an older rejected attempt in a chain does not outlive a wrong-run retraction of its successor. Direct SQL is held to the same rules (racing direct inserts leave exactly one). |
| **M-A** FOR SHARE | Tests now call `validate_call_set`, `submit_call_set` and `resubmit_call_set` themselves, leave their transaction open, and show that a writer to each of `tests`, `samples`, `orders` and `sequencing_runs` WAITS until it ends (and does not wait once the method has committed). Removing `lock=True` from either method fails them. |
| **M-B** map integrity | `list_patient_validated_call_sets` loads the stored bytes and, for each call set, requires (a) the bytes still hash to the recorded SHA-256 and (b) a fresh parse with the **recorded** rule set reproduces the stored document exactly. Otherwise the call set is not returned and `call_set_integrity_failure` (id only) is audited: **fail closed**, also for an unavailable or unknown rule set. A positive result is cached per process keyed by id, hash, rule set and a digest of the document actually read, so repeated reads are cheap and a changed document is always re-verified. Cost: one re-parse per call set per process (about 1 s per 40,000 records, measured in section 3). |
| **M-C** consent relationship | `callset_identity_guard` now also covers `orders.required_scope` and `orders.consent_id`, and a fifth trigger covers `consents`: scope, patient, organisation and `recorded_at` never change under a call set, a recorded withdrawal is never undone, a recorded supersession is never re-pointed (recording one, NULL to a value, stays allowed). The scope swap that brought withdrawn-consent evidence back is refused. |
| **M-D** post-conditions | A `MIGRATION-ASSERT` verifies all five triggers: exactly these five call the guard, each on the right table, enabled, row-level BEFORE UPDATE, on exactly the right column list. Seven kinds of damage (dropped, disabled, narrowed, AFTER, statement-level, extra trigger) make it false; it is true when evaluated as `clinical_app`. **Correction of earlier wording:** post-conditions are evaluated by `inspect_state`, i.e. `migrate --verify` (which `docker-compose.clinical.yml` runs after `--migrate`) and `/readyz` - not by `migrate()` while applying. A database missing the triggers is therefore applied and then FAILS verification. |
| **M-E** technician refusals | Every run-dependent refusal at submission (sample, QC, order, consent, assembly, small-variant build) is the one message "This sequencing run cannot accept this call set." Work that does not look at the run (arguments, size, record count, decoding, parsing) now happens first, so an ineligible run answers a bad file exactly as an eligible one does. Validation by an Interpreter still names the failed gate (documented asymmetry). |

### What a reviewer should know

* **Behaviour change:** a technician whose submission is refused no longer learns why; the reason is not recorded either (audit rows carry no details). An operator diagnosing a refusal must inspect the run directly.
* **Resubmission is deliberately narrow:** retracted and superseded call sets cannot be resubmitted unchanged, and a rejected file can be resubmitted repeatedly, one live at a time, each time needing a fresh validation.
* **Migration 0010 was edited again** (never applied to a real database). `test_callset_migration.py` allows only the declared deltas against the reviewed v3 text, and `schema.sql` carries the identical body and the new checksum.

### Pending policy decisions (not decided in this wave)

* **Withdrawn consent is hide-only**: evidence leaves the map while consent is withdrawn and returns if it is recorded again. Whether withdrawal should instead retract the evidence is undecided. Related, also undecided: whether the order's *own* consent (not merely the latest for the scope) must be active.
* **Draft-order eligibility** is not decided: only `cancelled` excludes; a `draft` order with a sample remains eligible.
* Retired tests remain eligible; final retention duration; backup/WAL/replica erasure; metadata surviving redaction; orphan evidence; production size limits; production SECURITY DEFINER ownership; whether to narrow the application role's UPDATE grants on `tests`, `sequencing_runs`, `samples`, `orders`; the parser rule-set retirement procedure.


## 8. Wave 4 (third independent review): H-1, H-2, M-1 .. M-6, L-1 .. L-6

Evidence tags as above. 22 mutants of these controls were run: 21 are caught by a named test and 1 is equivalent (below). The
earlier wave-3 mutants that still apply are still caught; those whose target code moved were re-expressed as wave-4 mutants.

| Finding | Resolution |
|---|---|
| **H-1** lineage and double drawing | A resubmitted **replacement** keeps `supersedes_id` while its predecessor is still the live validated call set (otherwise it is not carried, and `resubmission_of` still records the chain). To make that possible the one-successor **unique index became a rule** (a rejected or withdrawn replacement no longer occupies the slot; a pending or in-force one does). **The database now enforces ONE active validated call set per run, evidence class and assembly**: a second one must replace the first explicitly, and two racing validations leave exactly one (a lock on the run/class/assembly key). Direct SQL is held to the same rule. |
| **H-2** consent revival | Defence in depth. (1) The `consents` trigger now refuses a supersession whose target is not a **newer** consent of the same patient and scope (the reproduced bypass pointed an older active consent at the withdrawn one). (2) The eligibility rule **no longer reads `superseded_by` at all**: the latest consent by `recorded_at` decides, so even an owner-level edit of a supersession pointer cannot change the answer. A later consent (re-consent) is a new consent and does restore evidence; that is documented behaviour, not a revival of the withdrawn one. |
| **M-1** reviewer lineage | `get_call_set_for_review` and `get_call_set_status` (Interpreter/Approver) return the resubmission link, the replacement link and the **lineage**: every call set of the same file on the same run, oldest first, with state and the rejection / withdrawal / retraction events (who, role, when, reason). A technician sees only the link of their own submission. |
| **M-2** parser pin | `clinical/tests/golden/structural_vcf_v1/` freezes two VCFs (every record shape, all four statuses, **every warning the parser can emit**) and the exact documents version 1 wrote, pinned by SHA-256. `test_structural_vcf_v1_golden.py` requires the registered rule set to reproduce them exactly, the documents to verify and rehydrate, and a source scan to fail if a warning is added that no golden record exercises. Editing the version-1 warning text, which previously passed all 206 pure tests, now fails 4 tests. The fixtures were generated once from the rule set as it stands (no earlier stored documents exist anywhere). |
| **M-3** map cost | See the benchmark below. Integrity is **not** weakened: the database computes the SHA-256 of the stored bytes and of the stored document text on every read; the bytes must match the recorded hash, and the document digest must be the one a **full** verification (re-parse with the recorded rule set, exact equality) passed for exactly those bytes. Otherwise the bytes and document are fetched and verified in full. The cache holds the verified, rehydrated call set, bounded by records (`CLINICAL_CALLSET_CACHE_RECORDS`, default 100,000 records; **see section 9 for a corrected memory figure**) and guarded by a lock. |
| **M-4** reviewer access | While consent is withdrawn `get_call_set_for_review` returns metadata, lifecycle and lineage but **no document** (`document_withheld = "consent_withdrawn"`), and validation is refused. Nothing is erased; a later consent makes the document available again. Record **counts** in `get_call_set_status` remain visible (aggregate, no coordinates). |
| **M-5** never attached vs held back | `patient_call_set_evidence` returns `{eligible, excluded}`: validated, live call sets that are not drawn, counted under a fixed vocabulary (`consent_withdrawn`, `sample_qc_not_passed`, `order_cancelled`, `sample_identity_changed`, `assembly_mismatch`, `small_variant_build_mismatch`, `integrity_failure`). The map response carries `held_back_call_sets` (counts only) and three different notes: drawn, held back (with reasons, and "not a finding that no such variants exist"), and never attached. A pending, rejected or withdrawn call set is neither drawn nor "held back". |
| **M-6** cross-class duplicates | Identical bytes cannot be accepted as both CNV and SV: the same-run rule, the content lock and the replay lookup ignore the class, and a resubmission must keep its prior's class. |
| **L-1** | A NUL in the file is a clean input error (it cannot be stored in jsonb). |
| **L-2** | `call_set_integrity_failure` is written at most once an hour per call set; a persistent fault is still re-reported. |
| **L-3** lock order | One hierarchy, shared by the DAO and the triggers: **call-set locks first, then content locks**. The trigger now takes the prior call set's lock before the content lock (it took it after, which deadlocked against a DAO resubmission in a deterministic test); a replacement's predecessor is locked first by the DAO too. The same scenario now completes with a clean "already live" refusal. A deadlock that still reaches the DAO is reported as a retryable conflict. |
| **L-4** | "Latest" is decided by a database-assigned `seq`, not an application timestamp (a backdated row cannot change it). |
| **L-5** audit vocabulary | Pinned by a test that parses the source: `caller_proposed/approved/retired`, `call_set_submitted/resubmitted/validated/rejected/withdrawn/retraction_requested/retraction_declined/retracted/read`, `call_set_integrity_failure`, plus `genome_read` for map reads. `call_set_resubmitted` and `call_set_integrity_failure` are not in the original approved list (design section 9); they exist because the verb and the incident exist, and adding any other action now fails the test. |
| **L-6** | The cross-run refusal is the generic conflict text; it no longer confirms that the bytes exist on another run. |

### Map-read benchmark (3 validated call sets of 40,000 records each, one patient, 82 MB of stored documents)

| | Before | After |
|---|---|---|
| First read (full verification of everything) | 7.0 s | 6.6 s |
| Repeat read | 4.2 s | **0.31 s** (about 14x faster) |
| Repeat read with the cache disabled | - | 7.1 s (every read verifies in full) |

The cold path is unchanged on purpose: it is the full verification. The repeat read now does one metadata query in which the
database hashes the stored bytes and document, and rehydrates nothing. Remaining cost: the first read per call set per
process; a cache entry holds about 0.7 KB per record **(corrected in section 9: about 1.4 KB end to end)**. Not measured: concurrent requests, real caller output, multi-process
deployments (each process warms its own cache).

### Equivalent mutant

Removing the DAO's early lock on a replacement's predecessor is not caught, because the trigger now takes that lock before the
content lock itself. The DAO lock is kept so both paths follow the same hierarchy; no input exists that distinguishes them.

### Changed expectations (not weakened)

* The lifecycle suite gives every state it builds its own run (the new rule forbids two validated call sets on one run) and a
  replacement defaults to its predecessor's run.
* The cross-run refusal tests now expect the generic conflict text; the status test expects `resubmission_of`; the integrity
  incident test expects one row per call set per hour.
* Migration 0010 was edited again (never applied to a real database); the migration test allows only the declared deltas.

### Pending policy decisions (not decided in this wave)

* **Withdrawn consent stays hide-only** (map and reviewer document), with retention unchanged; whether it should instead retract the evidence is undecided. Related and undecided: whether the order's *own* consent, rather than the patient's latest for the scope, must be active (they differ after a second consent is recorded).
* **Draft-order eligibility**, retired tests, and any other order/test state are not decided: only `cancelled` excludes.
* Final retention duration, backup/WAL/replica erasure, metadata surviving redaction, orphan evidence, production size limits, production SECURITY DEFINER ownership, grant narrowing on `tests`/`sequencing_runs`/`samples`/`orders`, the parser rule-set retirement procedure.
* New, for a ruling: whether a technician whose submission is refused should have any path to learn why (today none), and what a reviewer should be told when the document is withheld beyond the fixed reason.


## 9. Wave 5 (fourth independent review): M1 .. M4, L1 .. L6

Evidence tags as above. Everything here is uncommitted. The new tests are `clinical/tests/test_callset_wave5.py` (34 tests).

| Finding | Status | What changed |
|---|---|---|
| **M1** byte integrity untested | Fixed | New tests remove the database CHECK (an owner action), change the bytes under an unchanged hash, and require the map to exclude the call set as `integrity_failure` and audit it, with a warm and a cold cache; a twin shows the original bytes are drawn again. The review-4 mutant is caught. |
| **M2** cache bounds, precedence | Fixed | Tests for the budget (environment, default, zero, invalid), the oversize guard (stores nothing, evicts nothing), LRU eviction and refresh, a 500-put bound, replacement of a key, a hit that cannot outlive a changed document, bytes or rule set, and the fixed exclusion order (all ten pairs and four database scenarios). The reason order is now one small pure function, `_callset_exclusion_reason`, so it can be tested. |
| **M3** order consent vs latest consent | **Policy decision required** | No behaviour changed. See 9.1. |
| **M4** cold reads, thrash, single flight | Partly fixed (9.3) | Profiled. **Single flight** added (measurable: 4 concurrent cold readers 26.1 s -> 12.5 s). A "protect what this read used" eviction variant was built, measured, showed **no benefit**, and was **removed**. The default budget is unchanged. |
| **L1** app-chosen `seq` | Fixed | `clinical_app` no longer holds table-level INSERT on `call_sets`: INSERT is granted column by column for every column except `seq` (built from the catalogue in a DO block, so a later column is covered). `INSERT ... OVERRIDING SYSTEM VALUE` is refused; normal inserts still draw `seq` from the database. Migration 0010 and `schema.sql` carry the same text; the migration test lists it as a declared delta. |
| **L2** near-duplicate bytes | Not changed (decision required) | Behaviour pinned by a test. See 9.2. |
| **L3** retention wording | Fixed | A validated call set whose content retention has removed is counted as `retention_redacted` (count only) and the map note says it was removed under the retention policy, not that nothing was attached. |
| **L4** retired rule set | Fixed | A call set whose rule set is not registered is refused **before** the cache is consulted, so a warm process refuses what a cold one refuses (`integrity_failure`, audited). Historical rule sets are unchanged. |
| **L5** wrong-class replacement | Fixed | A replacement on another run or of another class is a clean `CallSetConflictError` before the insert; the composite foreign key stays as the last barrier (asserted). |
| **L6** negative copy-number mutant | Fixed (the mutant now applies) | The check is `structural.py:188`. The mutant `copy_number < 0` -> `< -1e18` applies and is caught by `TestCnv::test_invalid_records_say_why[...CN=-1-negative]`. |

### 9.1 M3: which consent decides eligibility (decision required; not changed)

**Today.** `_CALLSET_CONSENT_OK_SQL` takes the **patient's latest consent for the order's required scope** (`ORDER BY recorded_at DESC, consent_id DESC LIMIT 1`) and requires it not to be withdrawn. `orders.consent_id`, the consent the order was placed under, is not read. The same constant gates the map and the reviewer document.

| | Policy A (today): latest consent for the scope | Policy B: the consent the order was placed under | Policy C: both must be active |
|---|---|---|---|
| Withdraw the order's own consent while a later one exists | Evidence stays visible | Hidden | Hidden |
| Withdraw the latest consent while the order's own is active | Hidden | Visible | Hidden |
| Re-consent after a withdrawal | Restores the evidence | Does not restore evidence of orders placed under the withdrawn consent | Does not restore it |
| Clinical reading | The patient's current wishes govern everything for that scope | Consent is a property of the act (the order) | Most conservative |
| Technical cost | None | One lookup on `orders.consent_id` | Both conditions |
| Risk | Withdrawing the consent a clinician sees on the order has no effect once a newer one exists | A newer consent cannot restore older evidence; operationally harsh | Hides in the union of both cases |

**Smallest safe change for each.** One constant in `data_access.py` (`_CALLSET_CONSENT_OK_SQL`), used by the map and the reviewer document and nothing else.
B: `(SELECT k.withdrawn_at IS NULL FROM consents k WHERE k.org_id = o.org_id AND k.consent_id = o.consent_id)`.
C: `COALESCE(A AND B, false)`. Either needs the matching consent test expectations updated (`test_callset_review_fixes`, `test_callset_wave4`). No migration is needed.

### 9.2 L2: exact bytes or canonical content

Identity is the SHA-256 of the exact stored bytes; replay, the duplicate rules and the integrity check all rest on that, so a trailing newline makes a different call set. What stops double drawing is the one-validated-per-(run, class, assembly) rule, which the new test pins (the near-duplicate cannot be validated beside the original). The remaining gap is a near-duplicate accepted as the *other* class.

Options: (1) keep exact bytes (today). (2) add a second, canonical-content hash (normalised line endings, trailing whitespace and final newline) used **only** by the duplicate rules, keeping the original-byte hash for integrity and replay. That needs a new column, a trigger rule and a ruling on what "canonical" means (header comments, record order); it changes which resubmissions count as "the same". It was not done because it alters replay semantics, which this wave was told not to change silently.

### 9.3 M4: where the cold read goes, and what was kept

The cold read is Python, not the database: the re-parse of the stored bytes with the recorded rule set and the rehydration of the document dominate; the database digests are about 0.3 s. Nothing in the verification was weakened.

| 3 x 40,000 records, one patient, same machine | Wave 4 tree | Wave 5 tree |
|---|---|---|
| Cold read (full verification of everything) | 6.8 s | 6.4 s |
| Warm read (everything cached) | 0.31 s | 0.31 - 0.32 s |
| Default budget 100,000 vs 120,000 records: repeat reads | 2.5 - 2.7 s (one call set re-verified each read) | 2.6 - 2.7 s |
| Two 40,000 sets vs a budget of 60,000: repeat reads | 2.3 - 2.7 s | 2.4 - 2.6 s |
| 4 concurrent cold readers (threads, one process), wall | 26.1 s | **12.5 s** |
| Budget raised to 200,000 (all fit): repeat reads | 0.31 s | 0.31 s |
| Memory retained, 80,000 records cached | 113 MB | 113 MB (about 1.4 KB per record) |

* **Thrash is bounded by capacity, not by the eviction policy.** Lookups precede stores in a read, so the cache keeps whatever fits; a call set that does not fit is re-verified on every read (about 2.3 s per 40,000 records). The only lever is the budget.
* **Single flight** claims a cache key before verifying, never holds a claim while waiting, and a waiter re-reads the cache with its **own** freshly computed digests, so it is only served what matches its own read of the bytes. A leader that fails leaves nothing cached and the waiter verifies (and refuses) for itself. The CPU work is still done once per call set and the GIL still serialises it, so the gain is avoided duplicate work, not parallelism.
* **Corrected memory figure.** Section 8 said 0.7 KB per record (about 73 MB per 100,000). That measured the rehydrated object alone. Measured end to end (tracemalloc around a real read, 80,000 records retained): **113 MB, about 1.4 KB per record, so about 141 MB for a full default cache per process**; a 200,000-record cache is about 282 MB per process. Choosing the budget is an infrastructure decision.


## 10. M3 resolved: Policy C (approved)

**Rule.** A call set is eligible only if BOTH hold now: (1) the consent the order was placed under (`orders.consent_id`) exists, belongs to the order's patient and organisation, is for the order's scope and is not withdrawn; and (2) the patient's latest consent for that scope (by `recorded_at`, then `consent_id`) is not withdrawn. Either failing holds the call set back as `consent_withdrawn` (no new reason, status or expiry rule). A newer active consent does not revive an order whose own consent was withdrawn. `superseded_by` is still never consulted.

**Where.** One constant, `_CALLSET_CONSENT_OK_SQL` in `clinical/data_access.py`, now built from `_CALLSET_ORDER_CONSENT_OK_SQL` and `_CALLSET_LATEST_CONSENT_OK_SQL`. It gates submission, replacement, resubmission, validation (through `_callset_chain_facts`), the reviewer document, and the Genome Map (`_patient_call_set_evidence`). The expression is never NULL: a missing, foreign-patient, foreign-organisation or wrong-scope order consent is false. The verified cache holds bytes and documents only, never consent, so a warm hit cannot bypass it. `orders.consent_id` is immutable under the identity guard, and the app role cannot un-withdraw or re-time a consent. No migration, no schema change.

**Behaviour change.** Under the old rule a later consent restored evidence after the order's own consent was withdrawn; it no longer does. Re-consent still restores evidence when the order's own consent was never withdrawn (only the latest was). One existing test (`test_callset_wave4.py::TestWithdrawnConsentWithholdsTheDocument`) asserted the old restoration and now asserts the new rule.

**Tests.** `clinical/tests/test_callset_m3_policy_c.py` (35 tests). **Equivalent mutants**, which cannot change the result and so cannot be caught: dropping `oc.org_id = o.org_id` (a consent of another organisation always has another patient, which the patient test catches), and `IS TRUE` -> `IS NOT FALSE` on the latest half (when no consent covers the scope, the order half is already false).
