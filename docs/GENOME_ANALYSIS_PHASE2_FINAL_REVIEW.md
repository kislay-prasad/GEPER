# Genome Analysis Phase 2 — Independent Final Implementation Review

Reviewed: the uncommitted working tree on `feat/genome-analysis-phase2-cnv-sv` (migration 0010, `schema.sql`,
`data_access.py`, `retention.py`, `genome_endpoints.py`, `genome_analysis/structural*.py`, all new/modified tests, the
Phase 2 and call-set documents).

**Review only.** No implementation code, SQL, grants, ownership, Docker or CI file was changed; nothing was committed,
pushed or deployed. Reproductions ran on disposable databases only (`127.0.0.1:55433`, created and dropped by the probe;
the cluster holds only `postgres` afterwards). The two Phase 2 images and the test container are untouched.

How to read the evidence tags: **[measured]** = reproduced on a disposable database in this review; **[read]** = established
by reading code or documents; **[inferred]** = reasoned, not reproduced. Nothing is stated as checked that was not.

---

## 1. Executive verdict

**NOT READY FOR COMMIT as it stands.** There are no BLOCKERs in the database layer: the migration, grants, ownership,
`TEMP` and search-path controls hold as designed. But the DAO **departs from the approved design in ways my own earlier
report did not disclose**, one of them a clinical-integrity gate:

* **H-1:** `submit_call_set` does not check sample QC status, order cancellation, or consent, all of which the approved design
  (§6 gates 4 and 5) required. A call set was accepted for a QC-failed sample on a cancelled order [measured].
* Several MEDIUM items (a measured purge/event race, a role-scope inconsistency, audit content that contradicts the approved
  text, a CPU/memory ceiling not supported by the measurements, a test that does not test what its name claims, and a DB-level
  security suite that is not in CI).

Recommended path: fix H-1 (and its tests), decide M-2/M-3, then commit; the remaining MEDIUMs may follow as a hardening
commit if you accept the stated residual risk. Every item is listed with its fix in section 10.

**Correction to my earlier report.** I said the baseline GEPER suite had 2 failures and that there were "no deviations".
Both statements were wrong or unproven: the pristine-HEAD GEPER baseline is **1** failure, identical to now (section 7), and
the DAO deviates from the approved design in the ways listed in H-1, M-3 and L-7.

---

## 2. Findings

Severity is about clinical integrity and security of *this change*. Pre-existing conditions are labelled as such.

### HIGH

**H-1 — Submission skips three approved gates (QC, order state, consent) and the small-variant assembly cross-check.**
*File:* `clinical/data_access.py` `submit_call_set` (line 4500); `_callset_run_chain` (line 4284).
*Evidence:* The approved design (`GENOME_ANALYSIS_CALLSET_STORAGE_DESIGN.md` §6) lists: "(4) sample `qc_status='passed'` and
order not `cancelled`; (5) consent for the order's `required_scope` is `consent_ok`; (6) … equals the ordered test's
`tests.assembly` **and, if the run already has a small-variant analysis, its assembly**". The code implements only the
ordered-test comparison. **[read]** `_callset_run_chain` selects `sample_id, patient_id, tests.assembly` and nothing about QC,
order state or consent; no consent helper is called. **[measured]** with a sample set to `qc_status='failed'` and its order
`cancelled`, `submit_call_set` returned a call set id.
*Impact:* Genomic evidence can be attached to a patient whose consent was withdrawn, whose sample failed QC, or whose order was
cancelled, and it can reach the map after validation. The same gap applies at validation (no re-check of consent or QC).
*Note:* `validate_call_set` and the map read do not consult consent or QC either, so a consent withdrawn *after* submission
also has no effect on what the map shows. Whether the map should honour withdrawal is a policy question (see M-8 below), but
refusing new submissions is not optional under the approved design.

### MEDIUM

**M-1 — Purge and a concurrent lifecycle event can leave a live event on a redacted call set (measured race).**
*Files:* `clinical/retention.py` `_purge_call_sets` (line 643); trigger `call_set_events_before_insert` (migration 0010).
*Evidence:* **[measured]** With the purge transaction open and uncommitted, a retraction request committed from another
connection without blocking; after the purge committed the call set was tombstoned and redacted, but the new
`retraction_requested` event had `tombstoned_at = NULL`. Cause **[read]**: the event trigger serialises on
`pg_advisory_xact_lock(hashtextextended(call_set_id::text, 1))` and reads `tombstoned_at` under READ COMMITTED; the purge takes
no such lock, and its `UPDATE call_set_events … WHERE tombstoned_at IS NULL` cannot see a row committed after its snapshot.
*Impact:* the "tombstoned set has only tombstoned events" invariant is not guaranteed; the map is unaffected (it filters on the
call set). It also shows the same window exists for `validated` events (a validation committing just before the purge
commits is accepted and then redacted).

**M-2 — Lab technicians can read full call-set documents; Phase 1 policy says they may not read genomic content.**
*Files:* `clinical/data_access.py` `CALLSET_READ_ROLES` (line 4243), `get_call_set_for_review` (line 4827),
`list_call_sets_for_run` (line 4803).
*Evidence:* **[read]** Phase 1's `list_patient_genome_analyses` docstring excludes Administrator, Lab technician and Auditor from
genomic reads ("a technician sees samples and QC … not clinical content"). `get_call_set_for_review` returns the entire parsed
document (coordinates, breakpoints, quality) to any Lab technician, for any call set in the organisation, by id, with no
relationship to the technician's own submissions.
*Impact:* inconsistent role model; broader read access than the platform's stated rule.

**M-3 — Audit content and action names contradict the approved design (§9); the audit table cannot be corrected.**
*Files:* `clinical/data_access.py` (`@auditable` on `submit_call_set`, the caller verbs, `list_patient_validated_call_sets`).
*Evidence:* The approved design says "no `details_builder` — nothing genomic, no file name, caller, assembly or counts" and names
the actions `caller_proposed/approved/retired`, with map reads staying `genome_read`. **[measured]** the success row for a
submit has details `{caller_name, caller_version, record_class, supersedes_id, sequencing_run_id}`; the actions are
`caller_version_*`; the map read writes a separate `genome_callset_read` row in addition to Phase 1's `genome_read`.
*Impact:* none of this is genomic content, but `audit_log` is append-only by grant, so a wrong choice cannot be undone. Either the
design or the code should change; today they disagree silently.

**M-4 — The DAO's hard ceiling on records (400,000) is not supported by the measurements.**
*File:* `clinical/data_access.py` `CALLSET_CEILING_RECORDS` (line 4242).
*Evidence:* The design's own measurements are ~75 µs and ~3 KB per record: 200,000 SV records took 17.5 s and 629 MB; 400,000 is
therefore ≈ 35 s and ≈ 1.2 GB **[inferred by extrapolation]** for one request, run in the web process, and `validate_call_set`
parses the same bytes again. The ceilings are only the upper clamp (the default is 40,000), but an operator who raises the
environment variable to "the ceiling" gets a request that can exhaust memory.
*Impact:* availability, not integrity. Lower the ceiling to what was measured, or move parsing off the request path first.

**M-5 — A parser-version change strands every pending call set.**
*File:* `clinical/data_access.py` `validate_call_set` (line 4639): refuses when `parser_version != CALLSET_PARSER_VERSION`.
*Evidence:* **[read]** there is no re-derivation path; the only way out is withdraw-and-resubmit by the original submitter.
Validated call sets are unaffected (the map does not compare versions; it validates the document through the models).
*Impact:* operational dead end after any parser change. Needs a decision (re-derive under the recorded version, or keep both
rule sets) before the first parser bump.

**M-6 — Validation and the map read rely on unlocked reads of identity and assembly; the application role can change them.**
*Files:* `clinical/data_access.py` `validate_call_set`, `list_patient_validated_call_sets`; grants in `schema.sql` (pre-existing).
*Evidence:* **[measured]** `clinical_app` holds UPDATE on `tests.assembly`, `sequencing_runs.sample_id`, `samples`, `orders` and
`role_assignments` (all columns). No shipped DAO verb changes `tests.assembly` or `sequencing_runs.sample_id` **[read: only
`tests.status`, `samples.qc_*`, `orders.state` are updated]**, so the window is not reachable through the shipped code. But
**[measured]** after a validated call set existed, repointing the run to another sample (as `clinical_app`) left the call set on the
map although its `vcf_sample_name` no longer equals the run's sample; the map read re-checks assembly (and did hide the set after an
assembly change) but not sample identity. Validation reads the chain with no `FOR SHARE`, so a concurrent change between the
read and the event insert is not excluded; the database trigger re-checks role, state and caller approval but cannot check
sample identity or assembly.
*Caller approval is safe:* the trigger requires the approval row to be the latest at insert time under an advisory lock, so a
retirement between read and insert is refused **[read; design suite covers it]**.
*Impact:* needs an application defect or compromise; the broad UPDATE grants are a pre-existing condition and out of scope to change
here, but the new feature depends on those facts staying put without saying so.

**M-7 — The ~200 database-level lifecycle/security tests are not in CI, so the 20 skipped variant tests are not adequately replaced.**
*File:* `docs/design/genome_callsets/test_lifecycle.py` (its docstring: "NOT part of the repository test suites … not collected by
any CI job").
*Evidence:* **[read]** the real-schema trigger, immutability, redaction and shadowing tests live under `docs/`. The repository tests
cover the DAO paths (44), the migration properties (17) and the pins/`TEMP`. Not covered in CI: the individual trigger rules reached
by direct SQL (event ordering, advisory-lock behaviour, content-state CHECKs, redaction finality, the deferred "must have submitted
event" trigger, 4 shadowing attacks). The 20 skipped tests proved that *each* defence layer alone stops the attack; the replacement
(4 attacks against the final objects, run only from `docs/`) proves only the combined result, and not from CI.
*Impact:* a future edit to a trigger can regress without a CI failure.

**M-8 — Consent/QC changes after validation have no effect on the clinical map (policy question to decide, not decided).**
*File:* `clinical/data_access.py` `list_patient_validated_call_sets`. **[read]** no consent or QC predicate. Distinct from H-1 (which
is about intake); recorded so it is decided rather than discovered.

**M-9 — Test coverage that does not exercise what its name says.**
*File:* `clinical/tests/test_callset_dao.py` `test_the_same_file_twice_on_one_run_or_live_on_another_run_is_refused` (line 494).
*Evidence:* **[read]** the "live on another run" half builds a file for a *different* sample, so the bytes differ; its closing
assertion only counts distinct hashes. **[measured, review probe]** the guard does work through the DAO when the same bytes are
submitted to a second run of the same sample ("the same file is already live on another sequencing run"), so this is a test defect,
not a code defect. Also: the pure-module additions (`to_document`, `callset_from_document`, `verify_document`, `max_records`,
the `from_dict` constructors) have no direct unit tests; the DAO tests exercise only the SV path for validation (CNV through the
database was exercised only by this review's probe, which passed, including a 1.5e22 quality value).

### LOW

* **L-1** `list_patient_validated_call_sets` writes a `call_set_integrity_failure` audit row on *every* read of a corrupt document
  (audit growth per request). `data_access.py` line 4856.
* **L-2** `get_call_set_for_review` loads `source_vcf` (up to the byte limit) and discards it; select only the document.
* **L-3** `CallSetConflictError` carries the database's primary message verbatim, including a constraint name (measured:
  `duplicate key value violates unique constraint "callsets_no_duplicate"`). No HTTP route exists yet; map it to a fixed message
  before one does.
* **L-4** The database cannot verify the `checks` object or the DAO-only checks (sample, assembly, integrity re-parse). A caller that
  inserts events directly as `clinical_app` can record `validated` without them. This is the documented "separation of recorded
  actors" model, restated here because the DAO is the only place those checks live.
* **L-5** No scheduler calls `purge_expired(…, 'call_set')` (none exists for any class **[read]**), so redaction happens only when an
  operator runs it. Operational, not a defect.
* **L-6** Batch purge rollback is tested for one call set; the property is a single-transaction one, but a two-call-set batch with
  a failure on the second was not exercised.
* **L-7** Exact replay of an identical file on the same run is a conflict **[measured]**; the design (§6 gate 9) says it is idempotent.
* **L-8** Re-reading the approved design shows `retraction_declined` as an action name and a `call_set_read` read action; the code
  matches the latter, not the caller action names (see M-3).

### INFORMATIONAL

* Orderer, Interpreter and Approver can read validated CNV/SV evidence for **any** patient of the organisation (same as Phase 1).
* The sample-identity rule requires the VCF's sample column to equal the platform sample UUID; the calling pipeline must be
  configured to name samples that way (an operational dependency, not a defect).
* The advisory-lock key spaces do not collide with the migration runner (it uses one fixed integer key; call-set locks hash text).

---

## 3. Security and privilege assessment

**Measured on a database built from `schema.sql` (superuser `postgres` as migrating role):**

| Check | Result |
|---|---|
| `clinical_app`, `clinical_retention` | not superuser, no CREATEROLE/CREATEDB/BYPASSRLS, no role memberships |
| `CREATE` on schema `public` for `clinical_app` | denied |
| Owner of all 3 tables and 14 functions | `postgres` (the migrating role); none owned by an application role |
| `call_sets_require_redaction_audit` ACL | `postgres=X/postgres` only; `clinical_app` calling it directly: *permission denied* |
| Other helper functions | executable by PUBLIC (read-only lookups; no write path) |
| App roles on call-set tables | `clinical_app` SELECT/INSERT only; `clinical_retention` SELECT + column UPDATE on tombstone/redaction columns only (re-asserted by the migration's own post-conditions) |
| Migration `MIGRATION-ASSERT` lines evaluated as `clinical_app` (how `/readyz` runs them) | all six true |
| `TEMP` | revoked from PUBLIC and both roles; `CREATE TEMP TABLE` refused under both `SET ROLE`s |
| search_path | all 14 functions `pg_catalog, public, pg_temp`; the four shadowing attacks fail against the repository objects |
| Ordering/transaction | forward-only, one transaction; a mid-file failure leaves no object, no history row, original `TEMP` state |

**The SECURITY DEFINER function and the migrating role — is the current ownership acceptable?**
The deployment model is not hypothetical: `docker-compose.clinical.yml` runs `clinical-init` with `CLINICAL_BOOTSTRAP_DSN`, and
`scripts/clinical-secrets.sh` builds that secret as `postgresql://postgres:<pw>@…`, i.e. the **`postgres` superuser** (the
documentation's "not verifiable from the repository" in §16.3 item 7 is therefore out of date: it *is* visible). So in the real
deployment:

* All tables and the definer function are owned by a **superuser**, and the function body therefore runs with superuser rights
  whenever the deferred redaction trigger fires.
* **Acceptable for first release, with conditions.** The function is small and fixed; it returns `TRIGGER` (not callable from SQL),
  EXECUTE is not granted to anyone but the owner, its search_path is pinned with `pg_temp` last, every name is schema-qualified,
  `clinical_app` cannot `CREATE` in `public` (so it cannot plant a shadowing operator or function), and it performs one read of
  `audit_log`. I found no route by which an application role can change what it does **[read + measured]**. The risk is
  maintenance-time: any future edit to that function body is a superuser-privileged change.
* **Not least-privilege.** A dedicated no-login owner holding only `SELECT` on `audit_log` would shrink the blast radius from
  "superuser" to "reads one table". I recommend it as a follow-up (it needs a role-creation step in bootstrap and a migration to
  change the owner), but I do not consider it a commit blocker, and I did not change ownership or grants.
* **What the migrating role retains afterwards (excessive?).** Everything: it is a superuser, owns every table, and can disable
  triggers (`ALTER TABLE … DISABLE TRIGGER`), run `ALTER … OWNER`, bypass every `REVOKE`, and change `TEMP`. Append-only-by-grant and
  every trigger rule bind the application roles, not this role. This is the **pre-existing** model for every table in the schema; the
  call-set work does not widen it, but it does mean the immutability story is "immutable to the application", never "immutable".
  The secret is mounted only into the init job, which is the right scoping.

**Trigger bypass possibilities.** (a) The owner/superuser (above). (b) `session_replication_role = replica` and trigger disabling
need superuser/owner. (c) An application role inserting directly can choose any actor id (documented; the `clinical.actor_id`
guard catches an accidental mismatch only). (d) The deferred triggers fire at commit; a client cannot skip them. I found no
application-role bypass of the immutability, redaction-audit or state-machine rules.

**`TEMP` revocation is database-wide.** It removes `TEMP` from any other role in that database that held it only through PUBLIC. No
repository code creates temporary tables (grep-verified in the design pass); operators with ad-hoc sessions must grant it back
explicitly. The migration header says so. Acceptable; flagged because it is operationally visible.

**Equivalence.** `test_w126m_schema_equivalence` (catalogs from schema.sql vs migrations) passes, and two new tests assert the
promoted body appears verbatim in both files and equals the reviewed v3 text **[read + ran]**.

---

## 4. DAO and clinical-integrity assessment

Traced: (no HTTP route exists for submission/validation; the only endpoint is the map) → `DataAccess` method → database.

| Control | Result |
|---|---|
| Role gate first, organisation from `Session` | Yes for every verb **[read; tested]**. |
| Authenticated actor | `session.user_id` is the only actor ever recorded; `clinical.actor_id` is set from it before each write. |
| Org isolation | Run, call set and patient lookups all bind `org_id`; another organisation's id and a missing id give the same message **[tested]**. |
| Run ownership / patient–sample chain | run → sample → order → test in the caller's organisation; the file's single sample column must equal the run's sample **[tested]**. |
| Caller approval / retirement | checked in the DAO and again by the trigger (latest approval row, advisory-locked); proposer ≠ approver in the trigger **[tested + mutation: removing the DAO check is still caught by the trigger]**. |
| Assembly | declared = header = ordered test (hard refusal, no liftover); re-checked at validation and in the map read. **Missing:** the small-variant-analysis cross-check (H-1). |
| SHA-256 and fresh parse at validation | Recomputed hash, then `verify_document` (fresh parse of the stored bytes must equal the stored document) **[tested, including a tampered-document case]**. |
| Parser version | recorded at submission; validation refuses a different one (M-5). |
| Rejected / withdrawn | refused files are not stored (an `error` audit row only); rejected/withdrawn call sets are kept as evidence until retention, and without a release they sit in the unresolved queue indefinitely (policy). |
| Replacement / supersession | predecessor must be validated, same run and class; one successor (unique index); supersession event written atomically by trigger; concurrent replacement: exactly one wins **[tested with threads]**. |
| Retraction | Interpreter requests, Approver retracts/declines, one open request, concurrent retraction: exactly one wins **[tested with threads]**. |
| Map eligibility | see section 6. |

**Time-of-check / time-of-use, specifically asked:**

* *Caller approval:* safe (trigger re-check under advisory lock).
* *Source evidence:* safe (the bytes and document are immutable; the DB forbids changing them; the only removal is redaction, after
  which events are refused).
* *Run/sample identity and assembly:* **not locked** (M-6). The validate method runs inside the `@transactional` wrapper at READ
  COMMITTED with no `SELECT … FOR SHARE` on run/sample/test rows, and the database cannot check these facts. The shipped code never
  changes them, so exposure requires a defect or compromise.
* *Tombstone/redaction:* safe against a *validation that starts after* the purge commits (the trigger refuses); **unsafe** for the
  commit-ordering race in M-1.
* *Consent/QC/order state:* never consulted at any step (H-1, M-8).

---

## 5. Retention and redaction assessment

Traced `RetentionPrincipal.purge_expired(org, 'call_set')` → `_purge_call_sets` → trigger → audit.

* **Eligibility:** latest release across the run's VCFs' reports + the call set's own `retention_days` ≤ now, and no live successor
  **[read; tested before/after the boundary, successor blocking, and release moving a set out of the queue]**.
* **Snapshot immutability:** `retention_days` is `NOT NULL`, set once, immutable by trigger, not UPDATE-able by either application
  role **[tested]**; a later VCF value change does not alter an existing row **[tested]**.
* **No-VCF / no-release:** never purged, never eligible; returned by `unresolved_call_sets` and `PurgeResult.unresolved_ids`
  **[tested, with the positive twin]**. No retention period is invented.
* **Atomicity:** one UPDATE tombstones and removes both content columns; the table CHECK makes "one removed, one kept" impossible
  **[design suite + DAO tests]**. Events are tombstoned afterwards (see M-1 for the race).
* **Same-transaction audit:** a deferred, `SECURITY DEFINER` trigger requires a `retention_purge` success row from the same
  transaction; with the audit write stubbed out, commit was refused and everything rolled back **[tested]**; with it failing, the
  rollback left bytes and document intact **[tested]**.
* **Batch rollback:** single-transaction by construction; tested with one call set (L-6).
* **Concurrency with reads and validation:** a map read sees either the pre-commit row or nothing after commit; a validation after
  commit is refused; M-1 is the exception.
* **Is redacted content reachable elsewhere?** Checked in the schema and code **[read]**: the bytes and the document exist only in the
  two `call_sets` columns. *Survives by design (policy item still open):* `source_filename`, `vcf_sample_name`, `caller_in_header`
  (≤200 chars from the file header), the SHA-256, `call_set_events.reason` (free text), and `audit_log` rows (ids and, per M-3,
  caller name/version). The deferred-trigger error text and `CallSetInputError` messages can name header contig text, but they are not
  logged (no logging in the modules) and are not stored.
* **Not claimed:** removal from old row versions, TOAST, WAL, replicas, backups or dumps. Nothing in this review or the code asserts it.

---

## 6. API and map exposure assessment

* **Eligibility (all five required by the brief)** is enforced in one SQL statement: patient bound to the session organisation through
  the shared run chain; state exactly `validated`; `tombstoned_at IS NULL`; `content_redacted_at IS NULL`; `tests.assembly =
  call_sets.assembly`. Pending, rejected, withdrawn, superseded and retracted sets are excluded **[tested one by one]**. Each document is
  rebuilt through the validating models; failure excludes it and writes an id-only audit row (L-1). Not enforced: sample identity at
  read time (M-6), consent (M-8).
* **Patient routes:** `patient_routes_enabled` is true only for `1/true/yes` in `CLINICAL_ENABLE_PATIENT_GENOME`; unset/empty/other = off
  **[read]**. Not modified by this work; the Phase 1 tests that pin it ran green in the final suite.
* **Errors:** the map endpoints translate only `AuthorizationError` and `NotFoundError` (generic bodies); anything else becomes the
  framework's generic 500. **Logs:** `genome_endpoints.py` imports no logging (a standing guard; I made the data layer audit instead of
  log). **Query strings:** only `start`/`end` integers; the existing access-log redaction covers them. **Audit events:** the map read writes
  `genome_callset_read` on the patient with empty details; no genomic content in any call-set audit row (M-3 concerns metadata only).
* **Frontend:** I checked only that `explorer.js` assigns `innerHTML` solely through its own escaping builders (its header says so and a
  grep found no other sink); I did not re-audit the escaping. File-derived strings (VCF ID, caller name, parser warnings) do reach the
  clinician API response, so that escaping matters.

---

## 7. Regression analysis

| Suite | Pristine HEAD (`git archive HEAD`) | Working tree | Verdict |
|---|---|---|---|
| Clinical (`clinical/tests`, disposable Postgres, run alone) | 2518 passed, **38 failed, 5 errors** | 2680 passed, **38 failed, 5 errors** | The 43 failing/erroring test **ids are identical**; no new failure and none fixed. +162 passing = the new tests. |
| GEPER (`geper/tests`, `geper/api/tests`) | 3575 passed, 190 skipped, **1 failed** | 3575 passed, 190 skipped, **1 failed** | Identical. |

* **The GEPER Python 3.14.7 failure is a genuine pre-existing baseline failure.** `test_main_help_names_the_component` fails identically
  on a pristine export of HEAD; the interpreter here is 3.14.7 and GEPER requires `3.11 <= python < 3.13`. It is environmental, not a
  compatibility problem introduced by this work. (A Python 3.12.10 is installed on this machine; I did *not* run the suite under it, so
  "would be 0 failures on a supported interpreter" is untested.) My earlier "baseline 2" was not reproduced: I never recorded the second
  failure, so I withdraw it.
* The 43 clinical baseline failures (MFA `test_w164*`, compose/secrets `test_w171*`, others) were not investigated in this review; they
  also mean those test areas cannot detect a regression. The changed code does not touch authentication or compose handling.
* Intermediate full runs earlier in the work showed extra errors; they were traced to my concurrent and aborted design-suite runs
  (cluster-wide roles, leftover databases) and the final run is clean. Two declared edits to existing tests were version bumps
  (migration 9 → 10, table census 31 → 34); a structural guard (`test_w135`) was satisfied by refactoring the join text, not editing the test.
* **Skipped:** 20 lifecycle variant tests (see M-7) and the 190/29 GEPER skips/deselects, identical at HEAD.

---

## 8. Test coverage gaps

1. Submission refusals for QC-failed, cancelled-order and consent-withdrawn (none exist because the gates do not) — H-1.
2. A purge racing an in-flight lifecycle event (M-1); a two-call-set purge batch failing on the second (L-6).
3. Validation racing a change of run/sample/test (M-6); map read after a sample repoint.
4. Direct unit tests for `to_document`, `callset_from_document`, `verify_document`, the `from_dict` constructors and `max_records`.
5. A CNV call set through the DAO end to end (only SV is in `test_callset_dao.py`).
6. The cross-run duplicate guard through the DAO with the same sample (M-9).
7. The DB-level lifecycle suite in CI (M-7), and a per-layer "remove one defence → attack succeeds" check against the repository schema.
8. Role-scope test for Lab technician reading documents (M-2) — currently nothing asserts the intended rule either way.
9. Tests that pass for reasons worth noting: `test_a_failure_in_the_middle_of_the_file_leaves_nothing_behind` accepts any
   `psycopg.Error`/`MigrationError` (it proves the rollback, which it asserts explicitly, but not *which* statement failed);
   the migration/DAO tests use a superuser connection for fixture building and a separate `SET ROLE` connection for the code under test,
   which is the intended production-path coverage.

---

## 9. Documentation discrepancies

* `docs/GENOME_ANALYSIS_CALLSET_IMPLEMENTATION_REVIEW.md` lines 19–29 (§0 status block): "Implementation is still NOT approved or started",
  "code not built" (also lines 69, 531 "specification; not built", 613 "not built"). Superseded by §17 but still present.
* Same file §6 title "(nothing implemented)" and §16.3 item 7 ("not verifiable from the repository"): the deployment model is visible
  (migrating role = `postgres` superuser) — update.
* `docs/GENOME_ANALYSIS_CALLSET_STORAGE_DESIGN.md` §10.2 "specified, **not built**"; and §§6, 9 describe gates and audit content that the
  code does not follow (H-1, M-3, L-7). Either amend the design (with your approval) or fix the code.
* `docs/design/genome_callsets/0010_call_sets.proposed.sql` line 2 still says "NOT APPLIED. NOT IN clinical/migrations"; `test_lifecycle.py`
  docstring still says it applies the proposed SQL on top and is run against a bare base.
* §17.4 of the review states "None [deviations] in the statements" — true for the SQL — but §17 does not list the DAO-level deviations above.
* Policy items **are** still marked unresolved and provisional: final retention duration, backup/WAL/replica erasure, metadata surviving
  redaction, orphan evidence, production size limits (the code labels 5 MiB / 40,000 as provisional defaults), and production SECURITY
  DEFINER ownership (§17.4 item 3). No provisional value was presented as approved policy. The one correction is that the ownership item is
  now answerable from the compose/secrets files (above) and should say so.

---

## 10. Recommended fixes and implementation order

(None applied. Each needs your approval.)

1. **H-1 — add the missing submission gates** in `submit_call_set`: sample `qc_status = 'passed'`, order not `cancelled`, consent for the
   order's `required_scope` is `consent_ok` (reuse the existing consent helper), and, when the run already has a small-variant analysis,
   assembly agreement with it (name both values in the refusal). Add the refusal tests. Decide whether `validate_call_set` also re-checks
   consent/QC (recommended: yes, cheap, same query).
2. **M-1 — serialise purge with events:** in `_purge_call_sets`, take `pg_advisory_xact_lock(hashtextextended(call_set_id::text, 1))` (the
   same key the event trigger uses) before the UPDATE; add a two-connection test (event commits first → purge tombstones it; purge first →
   event refused).
3. **M-6 — close the identity window cheaply:** in `validate_call_set` read run/sample/order/test with `FOR SHARE`; add
   `s.sample_id::text = cs.vcf_sample_name` to the map read's WHERE. Record, as a separate hardening wave, narrowing `clinical_app`'s column
   UPDATE grants on `tests`, `sequencing_runs`, `samples`, `orders`.
4. **M-2 — decide the technician rule;** recommended: technicians get metadata/state only (and only their own submissions); the document
   goes to Interpreter/Approver. Add a test either way.
5. **M-3 / L-7 — reconcile audit and replay with the approved design** (decide which side changes), including whether the extra
   `genome_callset_read` row is wanted beside `genome_read`.
6. **M-4 — lower `CALLSET_CEILING_RECORDS` to the measured range** (or move parsing out of the request) and state the memory bound.
7. **M-5 — decide the parser-version policy** before any parser change.
8. **M-7 / M-9 / gaps 4–8 — tests:** relocate the DB-level lifecycle suite into `clinical/tests` (or wire it into CI) with repo-schema as its
   only mode; fix the cross-run duplicate test; add the pure-module unit tests and a CNV end-to-end DAO test.
9. **L-1, L-2, L-3** — small clean-ups in the same change as item 1.
10. **Documentation (section 9)** — after the code decisions, so the documents describe the final state.
11. **Follow-up, not blocking:** dedicated no-login owner for the SECURITY DEFINER function; decide M-8 (consent/QC effect on the map).

Suggested commit grouping once approved: (a) items 1–3 + their tests; (b) items 4–9; (c) docs. Items 1–10 are DAO, retention-code, test or documentation changes, so the migration file and its stamped checksum stay as they are. The
grant-narrowing in item 3 and the owner change in item 11 would each need a new migration and are deliberately separate.
