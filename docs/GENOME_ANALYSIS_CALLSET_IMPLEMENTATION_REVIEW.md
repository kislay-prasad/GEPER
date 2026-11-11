# Genome Analysis — call-set storage: final pre-implementation review

Scope: documentation only. Nothing was migrated, no application code or proposed SQL was
changed, no Docker or CI configuration was touched, nothing is committed. The only
experiments run were on **disposable** PostgreSQL 16 databases (dropped afterwards); the
repository schema and every real database were untouched.

Evidence base: the real `clinical/schema.sql`, `clinical/migrations/`, `clinical/migrate.py`,
`clinical/retention.py`, `clinical/data_access.py`, `clinical/tests/test_w126m_schema_equivalence.py`,
`test_w151_migration_declaration_guards.py`, `test_privilege_boundary.py`, `docs/CLINICAL_DEPLOYMENT.md`,
and a one-off probe that applied the **real** `schema.sql` (via the repository's own `apply_schema`)
and then `0010_call_sets.proposed.sql` on top, built a real organisation/patient/order/sample/run
through `DataAccess`, and attacked the result as the real `clinical_app` and `clinical_retention` roles.

---

## 0. Status after the v3 resolution pass (supersedes the decision in §1 where they differ)

**SUPERSEDED (see §17 and `GENOME_ANALYSIS_PHASE2_REVIEW_CORRECTIONS.md`): implementation was later approved and built. What follows describes the state when this section was written.** *(Original:)* Implementation was still NOT approved or started. No migration, trigger, DAO method, Docker, CI,
frontend or patient-access change was made. The proposed SQL (`0010_call_sets.proposed.sql`, now **v3**) and the
design-verification suite (`test_lifecycle.py`, now **220 tests**) were updated and re-run against a
disposable PostgreSQL 16 database built from the **real** `clinical/schema.sql` with the real roles.

| Item | Status now |
|---|---|
| **B-1** search_path / `pg_temp` shadowing | **RESOLVED in the proposed SQL and verified** (§13). Pinned search path with `pg_temp` last, schema-qualified references, `TEMP` revoked from `PUBLIC` and the application roles. Both original attacks reproduced and then blocked. |
| **B-2** redaction (Option B) | **DECIDED and verified at the database level** (§14): the retention role may redact both the original bytes and the parsed document, atomically, only through the purge transaction, with a same-transaction audit row; a redacted record is final. **Open policy items remain** (§16): backups/physical erasure, and whether other file-derived fields are redacted. |
| **H-1** actor identity | **Documentation corrected** (§15); optional `SET LOCAL` actor-context guard added and explicitly **not** a security boundary. |
| **H-2** call-set purge path | **Specified and its database half verified** (§14); the application code (`_purge_call_sets`) is **not built**. |
| **H-3** retention snapshot | **RESOLVED in the SQL**: `NOT NULL`, inherits the run's VCF value; retention remains **provisional** pending a policy ruling. |
| M-1, M-3 … M-11, L-*, H-4 | **Not addressed in this pass** (unchanged; see §2 and §16). One item outside the approved list was included because redaction requires it: **events are refused on a tombstoned call set (M-2a)** — confirm. |

**Remaining blockers before the migration can be written:** none technical. The migration and DAO still need your
explicit approval, and §16 lists the policy decisions that are unresolved (none of which the live-row redaction
mechanism itself depends on, but the erasure/backup claims do).

---

## 1. Executive decision (ORIGINAL review — superseded by §0 where it differs)

**NOT READY for implementation.** Two blockers must be resolved first, one of which is a
defect in the proposed SQL that the earlier 155 tests could not see.

* **B-1** — the database-enforced role check and the **validator ≠ submitter rule can be
  defeated by the application's own database role** through `pg_temp` shadowing (reproduced on the real
  schema; the fix is known and was verified on a scratch copy). The "database-enforced" claim in the v2
  design is therefore currently false.
* **B-2** — the genomic-content **redaction/erasure ruling is unresolved**. The existing policy
  tombstones but does not redact; for call sets that leaves the original VCF bytes and the parsed JSONB
  readable after tombstone, and nothing governs backups. You asked that this be flagged as blocking.

Everything else is fixable inside the approved design. The proposed SQL **applies cleanly to the real
schema** and its grants, hardening and tombstone paths behave as intended on the real roles.

**Correction to my own earlier claims.** The v2 design said the lifecycle rules were "database-enforced"
and that 155 tests passed. Those tests ran against stub parent tables as a superuser-created fixture and
never asked whether the application role could subvert the trigger helpers. That was an overstatement and
this review replaces it (B-1, H-1).

---

## 2. Findings register

| ID | Severity | Object | Finding | Resolution |
|---|---|---|---|---|
| **B-1** | BLOCKER → **RESOLVED in v3 (verified, §13)** | All 12 helper/trigger functions in `0010_call_sets.proposed.sql` | Functions are `SECURITY INVOKER` with **no pinned `search_path`**. `clinical_app` holds `TEMP` on the database (verified: `has_database_privilege = true`). A session can `CREATE TEMP TABLE role_assignments / call_sets / call_set_events` and the trigger functions resolve unqualified names to `pg_temp` first. **Reproduced on the real schema:** a user with *no* Lab technician role submitted a call set; and a person holding both Lab technician and Interpreter **validated their own submission** by shadowing `call_sets.created_by`. | Pin `SET search_path = public, pg_temp` on every function (verified: both attacks then refused). Optionally also schema-qualify names and `REVOKE TEMP ON DATABASE … FROM clinical_app` (repository code uses no temp tables — grep-verified — but this is a deployment change needing approval). Add the shadowing attacks as permanent tests. |
| **B-2** | BLOCKER (decision) → **DECIDED: Option B, verified at DB level (§14); policy items open (§16)** | `call_sets.source_vcf`, `call_sets.parsed_document`, `call_set_events.reason`, backups | Existing retention **tombstones only**: `retention.py` states content redaction "is NOT implemented". Probe: after tombstone the application role can still read `source_vcf`. No code removes `vcfs.vcf_path` files either. Backups are a manual `pg_dump`, unscheduled, containing everything. | Rule, **before** the DDL is final, on one of: (A) keep status-quo semantics and state in policy that tombstone ≠ erasure (counsel on DPDP erasure); (B) allow the retention role to NULL `source_vcf`/`parsed_document` on tombstone (columns become nullable, column-level `UPDATE` grant, trigger exception); (C) move bytes to a separate `call_set_sources` table. B/C change the DDL, so this blocks. |
| **H-1** | HIGH → **documentation corrected; guard added, not a boundary (§15)** | `call_set_events.actor_id`, `role_assignments` | The database sees one login role (`clinical_app`). `actor_id` is **supplied by the application**; the trigger proves the *named* person holds the role and differs from the submitter, not that the named person acted. Probe: an event recorded under another real Interpreter's id was accepted. Separately `clinical_app` holds `INSERT/UPDATE` on `role_assignments` by design, so a compromised application can grant itself any role. | State the true guarantee: separation of duties between **recorded** identities, with the DAO deriving every `actor_id` from `Session.user_id` only (never request data). Reword all "database-enforced" claims. Optional defence against *bugs* (not malice): DAO sets `SET LOCAL clinical.actor_id`, trigger requires equality. Add DAO tests that no verb accepts an actor argument. |
| **H-2** | HIGH → **purge path specified, DB half verified; code not built (§14)** | `clinical/retention.py` | `RetentionPrincipal.purge_expired` dispatches only `report`, `run_document`, `vcf`; there is no call-set purge, cascade to events, D6 guard or purge audit. The eligibility anchor (latest release of a report of an interpretation of *the run's VCF*) does not exist for a run that has call sets but no VCF/interpretation, so such sets would **never** expire. | Specify and build a `_purge_call_sets` with the D3/D6/D7 behaviour; decide the anchor for runs without a VCF (see §8). Not buildable until B-2 and the snapshot-source decision are made. |
| **H-3** | HIGH → **RESOLVED in the SQL (§14)** | `call_sets.retention_days` | Nullable and accepted as NULL (probe). A NULL is purge-safe (`retention.py`: "absence … blocks a purge"), but the approved requirement is a **snapshot at creation**; the schema does not guarantee one. | `NOT NULL CHECK (retention_days > 0)`; DAO resolves before insert; snapshot source decided in §8. |
| **H-4** | HIGH | `structural_vcf.parse_structural_vcf`, future `submit` verb | The parser has **no record cap and no time bound**; it parses the whole text in memory (measured ≈157 MB peak at 50,000 SV records, 3.8 s). Parsing is synchronous in the request. Concurrent uploads multiply this. | Enforce the byte cap before parsing, add an in-parser record cap that aborts, bound concurrent ingestions, and measure on real output. |
| M-1 | MEDIUM | `actor_holds_role` | Ignores `users.disabled` and `organisations.disabled` (probe: a disabled user's id was accepted). The repository's rule is point-in-time (sessions are ended when a user is disabled; session validation deliberately does not re-read the row), so this is defence in depth only. | Add `NOT disabled` for the user and organisation to the function. |
| M-2 | MEDIUM | `call_set_events` / `call_sets` triggers | (a) Events can be written onto a **tombstoned** set (probe). (b) The database permits tombstoning a **pending** set and a **validated** predecessor with a live successor (probe); D6 and eligibility exist only in retention code (same as `vcfs`). | (a) Refuse events and supersession of a tombstoned set. (b) Either add a DB guard for D6 or accept the existing convention explicitly and test the retention code. |
| M-3 | MEDIUM | `@auditable` / `@transactional` | Domain work is committed by `@transactional` **before** `@auditable` writes and commits the audit row (decorator order verified), so a crash between them leaves committed work with no audit row; and `transactional` *commits* on `AuthorizationError`/`NotFoundError`. | Keep the repository convention; treat the lifecycle events as the authoritative record; the role gate must precede any write; DAO must never convert a DB error into those two exception types after a partial write. |
| M-4 | MEDIUM | Assembly canonicalisation | Two maps exist: `data_access.ASSEMBLY_CANONICAL` (accepts `GRCh38.p13`, no `b38`) and `genome_analysis.canonical_assembly` (accepts `b38`, not `.p13`). Gate 6 compares `tests.assembly` with the declared value. | Canonicalise **both** sides through one function; test `GRCh38.p13` and `b38`. |
| M-5 | MEDIUM | Existing guard tests | `test_privilege_boundary.py` hard-codes table lists and arities (nineteen/seventeen tables, `RETENTION_TABLES`, append-only lists); `test_w126m_schema_equivalence` diffs tables, columns, constraints, indexes, grants, **function definitions and triggers**; `test_w151` requires ≥1 `-- MIGRATION-ASSERT:`; `test_w120/121` audit-id guards; route-census tests. | Declared updates exactly as Phases 1–2 did; no guard weakened (§9). |
| M-6 | MEDIUM | `parse_structural_vcf` output | The pure module produces the **full** 8×-source document with `evidence`. The v2 design requires the compact document, a `parser_version`, `from_dict` and an integrity re-parse; none exists. | Build them first (they are prerequisites, §12). |
| M-7 | MEDIUM | Map reader | Must exclude tombstoned sets, select only `state = validated`, and **never select `source_vcf`**. | Specified in §6, op 12; tested. |
| M-8 | MEDIUM | Custom error mapping | Every rule raises plain `P0001` with free text; the DAO would have to parse messages. | Use `USING ERRCODE` per rule category so the DAO maps by code. |
| M-9 | MEDIUM | Decision | Submissions refused at a gate store **nothing** (not even bytes); only parse-level rejections are preserved. You asked to "record rejected submissions". | Confirm: refused submissions leave an audit `denied`/`error` row (no content) and a machine reason code; no evidence row. |
| M-10 | MEDIUM | `frontend/chromosome-explorer/explorer.js` | The page takes `?patient=<uuid>` in its **query string**; page URLs reach proxy access logs and browser history (the API's own log redaction covers only the service's access log). | Carry the identifier in the URL fragment or an in-page choice; set `Referrer-Policy`. |
| M-11 | MEDIUM | `call_sets.created_at` | Application-supplied, whereas `occurred_at` is database-stamped. | Stamp in the BEFORE INSERT trigger. |
| L-1 | LOW | `call_set_events_before_insert` | Message grammar: "may not **validated** their own call set". | Format with a verb. |
| L-2 | LOW | `callset_doc_envelope` | `(…->>'records')::int` raises a cast error (not a CHECK failure) for a non-numeric count. | Guard with `jsonb_typeof(...)='number'`. |
| L-3 | LOW | Lifecycle | A superseded set can carry a dangling **open** retraction request. | Map/review flags must be state-aware (only a `validated` set shows one). |
| L-4 | LOW | `call_sets_before_insert` | A successor can name a predecessor retracted a moment earlier (no lock on the predecessor at submit). Harmless by design (no `superseded` event is written; lineage keeps intent). | Document; concurrency test. |
| L-5 | LOW | `octet_length(parsed_document::text)` | Materialises the document as text at every insert (up to 128 MiB). | Measure; the real cap is the application limit. |
| L-6 | LOW | `retraction_requested` | Any Interpreter (including the validator/submitter holding both roles) may request. | Acceptable; note it. |
| I-1 | INFO | Functions | **No `SECURITY DEFINER`** anywhere (probe: 0), so no privilege elevation through the triggers themselves. | — |
| I-2 | INFO | Hardening | As the real `clinical_app`: `session_replication_role`, `TRUNCATE`, `ALTER TABLE … DISABLE TRIGGER`, `DROP TRIGGER`, `CREATE OR REPLACE FUNCTION`, and `INSERT … ON CONFLICT DO UPDATE` were all refused; `public` `CREATE` is false. | Keep as permanent tests. |
| I-3 | INFO | Compatibility | The proposed SQL applied to the real schema without error; the retention role's tombstone worked with the real grants; `IDENTITY` columns need no sequence grant (verified inserting as `clinical_app`). | — |
| I-4 | INFO | Locking | Lock-order analysis: validating a successor locks successor then predecessor (via the AFTER trigger); retraction locks only its own set; one successor per set ⇒ no cycle. | Add concurrency tests (§9). |

---

## 3. Actual-schema compatibility matrix

| Proposed object | Actual repository object | Compatible? | Required change |
|---|---|---|---|
| `users (org_id, user_id)` target of FKs | `users_org_user_unique UNIQUE (org_id, user_id)` | **Yes** | None |
| `actor_holds_role(org, user, role)` over `role_assignments(org_id, user_id, role, revoked_at)` | Same columns; active = `revoked_at IS NULL`; also `assigned_by`, `assigned_at`, `basis`; `Approver` rows require `basis` (`role_assignments_approver_needs_basis`) | **Yes, with gaps** | Add `users.disabled` / `organisations.disabled` (M-1); `clinical_app` has `SELECT` so invoker execution works (verified) |
| Role strings `Lab technician`, `Interpreter`, `Approver`, `Administrator` | `role_assignments_role_known` CHECK lists exactly these (+ `Orderer`, `Auditor`, `System`) | **Yes** | None |
| `sequencing_runs (org_id, id)` | `uk_sequ_run_org_id UNIQUE (org_id, id)`; columns `org_id,id,sample_id,created_at,created_by` | **Yes** | None |
| Patient/sample linkage derived, not stored | `sequencing_runs → samples → orders → patients` by composite keys; `patient_id` deliberately only on four tables | **Yes** | Readers anchor on `patients` (existing `_PATIENT_INTERPRETATION_CHAIN` style) |
| Logins `clinical_app`, `clinical_retention` | Created by `bootstrap.ensure_roles`; grants in `schema.sql` | **Yes** | New `GRANT/REVOKE` lines go in **both** `schema.sql` and `0010` |
| Tombstone pair + `has_column_privilege(current_user,…)` trigger | Same pattern in `enforce_content_immutability` for `vcfs/interpretations/reports` | **Yes** | New functions, not an edit of the existing one (equivalence test compares function bodies) |
| `retention_days INTEGER CHECK (… > 0)` | `vcfs.retention_days` identical; downstream artefacts inherit the VCF's persisted value | **Partly** | Make `NOT NULL` (H-3); decide snapshot source (§8) |
| `audit_log` rows via `@auditable` | `audit_log(org_id,user_id,actor_role,action,resource_type,resource_id,outcome,details)`; no CHECK on `action` | **Yes** | New action names need no schema change; each verb needs `resource_id_param`/`_from_result` (`test_w120/121`) |
| `gen_random_uuid()` | Already used throughout (`pgcrypto` created in `schema.sql`) | **Yes** | None |
| `GENERATED ALWAYS AS IDENTITY` | Existing `audit_log.log_id BIGSERIAL` | **Yes** | Equivalence test compares `sequences`; identical on both paths |
| `pg_advisory_xact_lock` | Not used elsewhere; executable by `PUBLIC` | **Yes** | Locks only serialise; no privilege issue |
| `GRANT SELECT, INSERT / REVOKE UPDATE, DELETE` | Same shape as `vcfs`, `reviewer_claims`, `report_retractions` | **Yes** | Add the three tables to `test_privilege_boundary` lists |
| `clinical_retention` column-level `UPDATE (tombstoned_*)` | Identical for `reviewer_claims`, `release_events`, `amendments`, … | **Yes** | None |
| Purge of call sets | `RetentionPrincipal.purge_expired` supports only `report`, `run_document`, `vcf`; `RETENTION_ARTEFACT_CLASSES = ("vcf",)` | **No** | New purge path (H-2) |
| Migration declaration | `migrate.py` requires ≥1 `-- MIGRATION-ASSERT:` per migration; `schema.sql` stamps `schema_migrations` with `(version, name, sha256)` | **Needs work** | `0010` file with assertions; `schema.sql` gets `(10,'call_sets',<checksum>)` and the identical DDL |
| Schema-equivalence | Diffs tables, columns, constraints, indexes, table/column privileges, sequences, **`pg_get_functiondef`, `pg_get_triggerdef`** | **Needs care** | Function text must be byte-identical on both paths; `search_path` settings included |
| `tests.assembly` vs declared assembly | `tests.assembly TEXT`, canonicalised at write by `ASSEMBLY_CANONICAL` | **Partly** | M-4 |
| Existing privilege test lists | `test_privilege_boundary.py` literals (arity-checked) | **No** (will fail) | Declared updates |
| Production Postgres | `postgres:16@sha256:…` pinned in compose | **Yes** | `sha256(bytea)` needs ≥ 11, trigger/JSONB features ≥ 13 |

**Assumptions that cannot be verified from repository code:** the production table owner / migration role (the
scratch database was owned by `postgres`); whether production revokes `TEMP` or otherwise differs from the
bootstrap defaults (the probe used the repository's own bootstrap and found `TEMP` granted); backup,
replication and snapshot practices beyond the documented manual `pg_dump`; the production login role used by
the service (compose passes a DSN by environment); and any extension or provider restrictions in a managed
database service (the deployment guide says these were not tried).

---

## 4. SQL security and correctness review (line by line, by object)

**`actor_holds_role`, `caller_state`, `call_set_state`, `open_retraction_request`** — read-only `STABLE` SQL
functions, invoker rights, **no pinned search path** → B-1. Correct logic otherwise. `actor_holds_role` omits
disabled accounts (M-1).

**`caller_approvals`** — append-only; role-per-event CHECK; trimmed non-empty name/version/basis; index for
latest-row lookup. The trigger serialises per exact key with an advisory lock, stamps the clock, requires
the actor currently hold the claimed role, and enforces `proposed → approved → retired`. Proposer ≠
approver uses the latest `proposed` row — correct. Verified exhaustively (12 transitions). Subject to B-1/H-1.

**`call_sets`** — composite organisation FKs (`fk_callset_run`, creator, tombstoner); `sha256` CHECK ties
bytes to hash; envelope CHECK ties document to the row's assembly and class; supersession FK forces same
run **and** class; unique `(org, run, class, hash)`; one successor per set; self-supersession refused.
`caller_approved_at_submission` is overwritten by the trigger (correct; informational only).
Findings: `retention_days` nullable (H-3); `created_at` app-supplied (M-11); cast in the envelope (L-2);
`::text` materialisation (L-5). The deferred constraint trigger correctly refuses a set without its
`submitted` event by the same creator at commit.

**`call_set_events`** — every state/role/reason/attestation rule is a CHECK or partial unique index where
expressible (single `submitted`, single decision, single `superseded`, single `retracted`, single answer per
request). The BEFORE INSERT trigger takes a per-set advisory lock (used instead of `FOR UPDATE`, which would
need an `UPDATE` privilege the application lacks), stamps the clock, requires the actor currently hold the
claimed role, and applies the transition table. Validator ≠ submitter, submitter-only withdrawal,
Approver-only retraction and decline, and the open-request rule are all present and tested; the AFTER trigger
writes `superseded` **in the same statement and transaction**, so replacement validation and old-set
supersession are atomic by construction. Findings: B-1, H-1, M-1, M-2, M-8, L-1, L-3, L-4.

**Grants** — `clinical_app`: `SELECT, INSERT` only; `UPDATE, DELETE` revoked from it and `PUBLIC`;
`clinical_retention`: `SELECT` plus column-level `UPDATE (tombstoned_at, tombstoned_by)` on the two evidence
tables, no `DELETE`. Verified on the real roles (I-2/I-3). `TRUNCATE` is not granted to anyone but the
owner. Nothing grants the application `INSERT` on `caller_approvals` beyond what the DAO gate uses —
**but** the application *can* insert there directly with any actor id (H-1).

**Direct-write bypasses attempted as `clinical_app`:** `UPDATE`/`DELETE` (refused), `ON CONFLICT DO UPDATE`
(refused), `TRUNCATE` (refused), `DISABLE TRIGGER`/`DROP TRIGGER` (refused: not owner), replace a function
(refused: no `CREATE` on `public`), `session_replication_role` (refused). **Succeeded:** shadow via `pg_temp`
(B-1), record another person's identity (H-1), insert into `role_assignments` (existing design).

**Trigger execution context** — invoker rights; each function reads tables the application can already
`SELECT`; a function that elevated privileges would be a worse risk and none does (I-1).

---

## 5. Role-by-role permission matrix

`DB` = enforced by constraint/trigger/grant (subject to B-1/H-1). `DAO` = enforced by the data-access layer.

| Action | Lab technician | Interpreter | Approver | Orderer | Administrator | Auditor | Patient | Retention role |
|---|---|---|---|---|---|---|---|---|
| Submit (incl. replacement) | ✔ DB+DAO | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ |
| Withdraw pending | ✔ own only, DB | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ |
| Validate / reject pending | ✖ | ✔ not own, DB | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ |
| Request retraction | ✖ | ✔ DB | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ |
| Retract / decline | ✖ | ✖ | ✔ DB | ✖ | ✖ | ✖ | ✖ | ✖ |
| Propose caller version | ✖ | ✖ | ✖ | ✖ | ✔ DB | ✖ | ✖ | ✖ |
| Approve / retire caller version | ✖ | ✖ | ✔ (approve: not own proposal) DB | ✖ | ✖ | ✖ | ✖ | ✖ |
| Read call-set content (any state) | own submissions' status | ✔ DAO | ✔ DAO | ✖ | ✖ | ✖ | ✖ | SELECT (grant) |
| Read validated findings (map) | ✖ | ✔ DAO | ✔ DAO | ✔ DAO | ✖ | ✖ | ✖ (patient routes off) | ✖ |
| Tombstone | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ | ✔ column-level |
| Update/delete any evidence | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ |

Roles are additive; the DB rules compare **recorded identities** (H-1). `clinical_app` can alter
`role_assignments` by design, so the DB role check is only as trustworthy as that table.

---

## 6. DAO operation specification (nothing implemented)

Common rules for every verb: `@auditable` + `@transactional` (existing order); role gate is the **first
statement**; `org_id` and the actor id come **only** from `Session`; no verb accepts an organisation or actor
argument; one decorated call per HTTP handler; DB errors are mapped by SQLSTATE (M-8) and never converted
into `AuthorizationError`/`NotFoundError` after a write (M-3); every miss on an id (absent, foreign,
tombstoned) is the same `NotFoundError`.

| # | Operation | Role | Actor identity | Organisation isolation | Transaction | Audit event | Failure behaviour |
|---|---|---|---|---|---|---|---|
| 1 | **Submit** a call set (also a replacement) | Lab technician | `Session.user_id` = `created_by` and the `submitted` actor | Run looked up by `(org_id, id)` from the session; `sample_id` argument must equal the run's sample; supplied ids never widen scope | One transaction: gates 2–10 (§ below), parse, INSERT `call_sets`, INSERT `submitted` event; deferred trigger checks at commit | `call_set_submitted` (resource = new id); no details | Gate refusal → generic refusal, **nothing stored**, audit `denied`/`error`; oversize/unparseable header → `CallSetRejected`-style refusal, nothing stored; duplicate replay → returns the existing set (idempotent); live same-bytes on another run → refusal |
| 2 | **Verify run ownership and patient/sample identity** | (inside 1) | — | Single statement from `sequencing_runs` joined `samples → orders → patients` bound by `org_id`; patient never accepted from input; returns patient id only for the audit-free response | Same transaction | none (part of 1) | Not found ⇒ same generic body as an invented id |
| 3 | **Assembly consistency** | (inside 1) | — | `tests.assembly` of the run's order; run's small-variant analysis assembly if one exists | Same transaction | none | **Hard refusal** naming both canonical values; no liftover; both sides through one canonicaliser (M-4) |
| 4 | **Caller approval snapshot** | (inside 1) | — | `caller_approvals` by org + exact name/version/class | Same transaction (trigger computes the snapshot) | none | Not approved ⇒ **accepted and quarantined**, flag returned, never validatable/drawn until the exact version is approved |
| 5 | **Store bytes + parsed representation** | (inside 1) | — | — | Same INSERT | none | Hash CHECK / envelope CHECK failure ⇒ internal error, rollback; parser version recorded; rejected/unsupported/invalid records preserved in the compact document with reasons |
| 6 | **Withdraw** | Lab technician | must equal the submitter | Set by `(org_id, id)` | One INSERT of `withdrawn` event | `call_set_withdrawn` | Not pending / not own ⇒ trigger refusal mapped to a conflict; same `NotFound` for foreign ids |
| 7 | **Validate** | Interpreter | `Session.user_id`; ≠ submitter by trigger; cites the approval row in force | Set by org; approval id read server-side, never from input | One INSERT of `validated`; AFTER trigger inserts `superseded` for the predecessor in the **same statement** | `call_set_validated` | Any trigger refusal rolls the whole statement back: **a failed replacement validation leaves the old validated set unchanged**; caller not approved ⇒ conflict with reason code |
| 8 | **Reject** | Interpreter (≠ submitter) | as above | as above | One INSERT of `rejected` | `call_set_rejected` | reason mandatory; not pending ⇒ conflict |
| 9 | **Request retraction** | Interpreter | `Session.user_id` | as above | One INSERT of `retraction_requested` | `call_set_retraction_requested` | One open request at a time; not validated ⇒ conflict. **Does not change state or map eligibility** |
| 10 | **Retract** | Approver | `Session.user_id` | as above | One INSERT of `retracted` (optionally citing the open request) | `call_set_retracted` | reason mandatory; not validated ⇒ conflict |
| 11 | **Decline** a request | Approver | `Session.user_id` | as above | One INSERT of `retraction_declined` citing the open request | `call_set_retraction_declined` | Must answer the open request; **state unchanged** |
| 12 | **Read for the map** | Orderer, Interpreter, Approver | `Session.user_id` | Anchored on `patients` with org + patient bound (existing chain); `tombstoned_at IS NULL`; `call_set_state = 'validated'`; selects `parsed_document` only — **never `source_vcf`** | One read | `genome_read` (existing; no payload) | Retraction requests, retired callers: shown as **flags**, not removed; pending, quarantined, rejected, withdrawn, superseded, retracted: listed, never drawn |
| 13 | **Propose / approve / retire** a caller version | Administrator / Approver (≠ proposer) / Approver | `Session.user_id` | Org from session | One INSERT each | `caller_proposed/approved/retired` | Trigger refusals mapped to conflict; exact-version match only |
| 14 | **Review read** (queue, full content, evidence line) | Interpreter, Approver | `Session.user_id` | Org + patient chain | One read; evidence line re-parsed from `source_vcf` on demand | `call_set_read` | Tombstoned ⇒ not found |

**Submission gates, in order:** (1) Lab technician; (2) run in the caller's organisation; (3) `sample_id`
equals the run's sample; (4) QC passed and order not cancelled; (5) consent `consent_ok` for the order's
scope; (6) assembly agreement; (7) header check and the declared sample column exists; (8) configured byte and
record limits (§ H-4); (9) duplicate rules; (10) replacement target on the same run and class and validated.
Gate 4/5 use existing verbs (`check_consent`, QC state). The DAO also strips path components from the file
name before insert.

**The DAO cannot bypass the database rules**, but the converse is the limit of H-1: the database enforces
consistency of what the DAO records, so the DAO tests must pin that no verb takes an actor argument.

---

## 7. Lifecycle transition tables and the supersession transaction

### 7.1 Call sets (complete)

States (derived from the latest state-changing event): `pending_validation`, `validated`, `rejected`,
`withdrawn`, `superseded`, `retracted`. Annotations (never change state): `retraction_requested`,
`retraction_declined`.

| From \ Event | submitted | validated | rejected | withdrawn | superseded | retraction_requested | retraction_declined | retracted |
|---|---|---|---|---|---|---|---|---|
| *(none)* | ✔ → pending | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ |
| pending_validation | ✖ | ✔ → validated | ✔ → rejected | ✔ → withdrawn | ✖ | ✖ | ✖ | ✖ |
| validated | ✖ | ✖ | ✖ | ✖ | ✔ system only → superseded | ✔ annotation | ✔ only if a request is open | ✔ → retracted |
| rejected / withdrawn / superseded / retracted | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ |

(Verified: 48 cells.) Actors and conditions per cell are in `GENOME_ANALYSIS_CALLSET_STORAGE_DESIGN.md` §4.1.
**Gap (M-2):** none of these cells is refused for a *tombstoned* set.

### 7.2 Caller approvals

| From \ Event | proposed | approved | retired |
|---|---|---|---|
| *(none)* | ✔ (Administrator) | ✖ | ✖ |
| proposed | ✖ | ✔ (Approver ≠ proposer) | ✖ |
| approved | ✖ | ✖ | ✔ (Approver) |
| retired | ✔ (Administrator; a fresh proposal) | ✖ | ✖ |

**Retired callers:** validation is refused (the cited approval must be the latest row for the exact key and
`approved`); already-validated sets remain validated and are flagged; pending sets of a retired caller cannot be
validated and are closed by rejection or withdrawal. **No policy permits validating newly under a retired caller.**
Quarantine: an unapproved caller's set is stored, never validatable, never drawn; it becomes validatable
without being modified once the exact version is approved (verified: row digest unchanged).

### 7.3 Supersession and atomicity

A replacement names `supersedes_id` at submission; allowed only if the predecessor is **currently validated**,
on the same run and class, and has no other successor (unique index). Nothing changes while the replacement is
pending. Validating the replacement is **one INSERT**; its AFTER trigger inserts the linked `superseded` event on
the predecessor in the same statement. If any rule in either insert fails, the statement aborts and **both**
events vanish, leaving the predecessor validated. Rejecting or withdrawing the replacement leaves the
predecessor untouched. If the predecessor was retracted meanwhile, the replacement may be validated, no
`superseded` event is written, and the lineage column keeps the intent (L-4). Only one successor can exist
(`callsets_one_successor`, `cse_one_superseded`). A decline preserves the validated state; a request never
changes map eligibility. **Not yet tested:** forced failure inside the AFTER trigger and concurrent
validate-vs-retract (§9).

---

## 8. Retention and redaction analysis (from the real implementation)

* **Calculation.** `resolve_vcf_retention_days`: ten years if the patient was under 18 at sample collection,
  five otherwise (defaults `1825`/`3650` days, described in code as a floor the product chose, *not a legal
  finding*), unless the organisation set its own `vcf`-class override (`retention_policies`). The age
  threshold of 18 is the code author's own decision, not a ruling.
* **Snapshot.** Resolved **once** in `create_vcf`, stored in `vcfs.retention_days`; interpretations, reports
  and amendments inherit it at their own creation and never recompute. NULL means "never purge".
* **Start.** The latest `release_events.released_at` across every report of every interpretation of the VCF; no
  release ⇒ no expiry.
* **Eligibility.** `RetentionPrincipal._purge_vcfs`: anchor + `retention_days` ≤ now, `tombstoned_at IS NULL`,
  and **no live interpretation** (D6). Performed by the separate `clinical_retention` login; one audit row per
  purge attributed to the system principal; no scheduler exists.
* **Tombstone vs redaction.** Tombstoning sets `tombstoned_at/by` only. The module states that redaction
  inside a tombstoned row "is NOT implemented". Nothing deletes the file at `vcf_path`, and
  `interpretations.run_document` stays in the row. The staff readers added in Phase 1/2 exclude tombstoned
  interpretations, but the existing portal readers do not filter on tombstone.
* **Derived fields.** For call sets: `source_vcf` (bytes), `parsed_document` (JSONB), and free-text
  `call_set_events.reason` can all carry genomic detail and persist after tombstone. Probe: the application role
  can still read `source_vcf` of a tombstoned set.
* **Backups and audit.** Backups are a documented manual `pg_dump`; nothing schedules, encrypts or ships them, so
  tombstoned content persists in any dump. `audit_log` is exempt from retention by ruling D7; the proposed audit
  rows carry only ids and outcomes, no genomic content.
* **Snapshot source for call sets (decision).** The approved text says "snapshot the applicable retention policy
  at creation". D2 says downstream artefacts **inherit the VCF's already-resolved value**. A run may have no
  VCF row. Recommended rule: if the run has a non-tombstoned VCF, copy its persisted `retention_days`; otherwise
  resolve with the same pure function at creation. Either way the value is immutable afterwards.
* **Anchor for call sets (decision).** Recommended: the latest release of any report on an interpretation of any
  VCF of the same run; a run with none never expires (consistent with "nothing unreleased expires"). D6: a
  call set with a live successor must not expire.
* **No new period is invented.** The final duration stays subject to the policy ruling; changing it later cannot
  reach already-written rows (immutable snapshot, as on `vcfs`) unless a retention-principal procedure is
  approved — an alternative is deriving eligibility from the run's VCF at purge time.

**B-2 options (rule before the DDL is final):** (A) status quo, documented as "tombstone is not erasure"
(needs counsel on DPDP erasure); (B) retention role may NULL `source_vcf` and `parsed_document` on tombstone;
(C) a separate source table. Whatever is chosen also needs a statement about backups.

---

## 9. Test coverage gaps and the integration plan

### 9.1 Gap analysis of the 155 existing tests (docs-only, stub schema, superuser fixture)

| Requested area | Existing | Gap |
|---|---|---|
| Actual-schema compatibility | none (stub tables) | Apply `0010` to the real schema; equivalence test; this review's probe was one-off |
| Organisation boundary violations | duplicates are org-scoped; FKs exist | Cross-org `sequencing_run_id`, `supersedes_id`, `caller_approval_id`, `responds_to`, `actor_id`; foreign-org ids give identical refusals |
| Role escalation | wrong-role per event, revoked role | Self-grant via `role_assignments` (real), role revoked **between** submit and validate, disabled user/org, dual-role users |
| Trigger bypass attempts | none | `pg_temp` shadowing of every referenced table, `search_path`, `session_replication_role`, `DISABLE/DROP TRIGGER`, `TRUNCATE`, `ON CONFLICT`, function replacement — **B-1 would have been caught** |
| Concurrent replacement | one trivial test | Two successors racing the unique index; successor validation racing predecessor retraction; two validations of different successors |
| Concurrent retraction | none | Two simultaneous requests; decline racing retract; retract racing validate |
| Caller approval changes between submit and validate | later approval, retire, stale id | Retire/re-approve **during** a validation (race); approval of a different version in between |
| Retired-caller behaviour | basic | Pending sets of a retired caller; already-validated sets flagged |
| Duplicate-submission races | sequential only | Same bytes, two runs, two threads; same run, two threads |
| Retention-snapshot immutability | update blocked | NOT NULL; DAO value equals the VCF's persisted value; minor vs adult |
| Tombstone / redaction | tombstone by retention role only | Events on tombstoned sets; readers exclude; redaction per the B-2 ruling; D6 |
| Transaction rollback | none | Failure inside the AFTER trigger rolls back the `validated` event; failed deferred check leaves no rows |
| SQL privilege escalation | app update/delete refused | Real **login** roles (not `SET ROLE` from a superuser): permissions, `TEMP`, `CREATE`, function privileges |
| Unauthorized direct writes | update/delete, retention insert refused | Direct inserts of `superseded`, of events with foreign actors, into `caller_approvals` by non-gated paths |

### 9.2 Integration plan against the real schema (CI and the docs-only tests are **not** changed now)

1. Promote the lifecycle tests into `clinical/tests/test_callset_lifecycle.py` against the real schema using the
   repository's world fixtures (organisations, users with real role rows, real runs); keep the exhaustive
   state × event matrix.
2. New `test_callset_security.py` using **real login connections** exactly as `test_privilege_boundary.py` does
   (exact `InsufficientPrivilege` classes, proven-live connection, a refusal proven capable of failing): all
   bypass attempts above, with the `pg_temp` attacks as permanent regressions.
3. New `test_callset_concurrency.py`: thread-per-connection races for every row in the gap table.
4. DAO tests (`test_callset_dao.py`): each operation in §6, role-before-existence parity, cross-org/cross-patient
   isolation, no verb takes an actor, audit rows without genomic content, error-code mapping, limits.
5. Migration tests: `test_w126m_schema_equivalence` passes with `0010`; `0010` declares assertions
   (`-- MIGRATION-ASSERT:`); `schema.sql` stamp row; migration idempotence/concurrency like `0007`/`0009`.
6. Declared updates to `test_privilege_boundary` lists, route census and probe lists — never weakened.
7. Re-run a **mutation pass** (add mutants for removing each pinned `search_path`, the disabled-user check, the
   tombstone guard, `NOT NULL`).
8. Run **sequentially on one database**; Docker slim/full builds last.

---

## 10. Frontend deployment security requirements (a separate milestone; nothing deployed)

Verified facts: the session cookie is `HttpOnly; Secure; SameSite=Strict`; the service sets **no CORS, no CSRF
token, no Origin check, no security headers**; a TLS-terminating proxy is required and none is provided; there
is **no staff login page** (login is API-only with a TOTP step); `auth_endpoints.py` records that an Origin check
"needs a ruling".

Requirements before any deployment:
1. **Same-origin serving** behind the TLS reverse proxy (static files and `/clinic`, `/auth`, `/lab`, `/review`
   on one host). CORS plus `SameSite=None` is rejected; it weakens the existing control.
2. Keep the existing cookie flags; never expose the cookie to script; no token in `localStorage`.
3. A staff login page with MFA, built on the existing endpoints, with lockout and throttling behaviour preserved.
4. **Origin/CSRF protection for every state-changing request** (ingestion, validation, retraction, caller
   approval) — needs the Origin-check ruling first; the current Explorer is GET-only and does not depend on it.
5. Proxy-set headers: `Content-Security-Policy` (including `frame-ancestors 'none'`), HSTS,
   `X-Content-Type-Options`, `Referrer-Policy: no-referrer`, and `Cache-Control: no-store` on every API response
   (the service sets none).
6. **Patient-level authorisation on every API request** (already true of the routes: org-scoped, role-gated,
   audited); the page must never cache or persist genomic data.
7. **No public unauthenticated access**; the static page itself reveals nothing but must still sit behind the
   authenticated origin.
8. Do not put the patient identifier in the **query string** (M-10).
9. Frontend tests (`node --test`, Node ≥ 18, no packages) added to CI **before** deployment; plus a test against
   the real service with a real session and MFA (not yet done: the page has only been exercised against a local
   harness serving the synthetic fixture).
10. Do not change the clinical Docker image: static files are served by the proxy, preserving the slim image's
    allow-list guard.

---

## 11. Blocking decisions and unresolved assumptions

**Blocking (human approval needed before implementation):**
1. **B-1 fix** — approve pinning `search_path = public, pg_temp` on all functions (verified), and decide whether
   to also schema-qualify and revoke `TEMP` from `clinical_app` (deployment change).
2. **B-2** — the redaction/erasure rule (A, B or C) including backups, with counsel on DPDP erasure.
3. **H-1** — accept the honest guarantee (separation of recorded identities) and reword the claim; decide
   whether to add the `SET LOCAL` actor guard.
4. **H-2/H-3** — retention snapshot source (inherit the VCF's persisted value, else resolve), the anchor for runs
   without a VCF, NOT NULL, and authority to build the call-set purge path.

**Decisions to confirm (non-blocking):** custom SQLSTATE per rule category (M-8); disabled-user/org check (M-1);
events and supersession refused on tombstoned sets and whether to add a D6 guard in the database (M-2);
refused submissions store nothing (M-9); one assembly canonicaliser (M-4); `created_at` stamped by the trigger
(M-11); production size and record limits (still provisional 5 MiB / 40,000 records, DB ceilings 64 MiB /
128 MiB, none production-validated).

**Rulings already given and recorded:** resubmission of identical bytes after a wrong-run retraction is
allowed as a new submission needing fresh identity and run validation; supersession atomic with replacement
validation; unapproved callers preserved but quarantined; retraction requests never change map eligibility;
original bytes and SHA-256 authoritative, parsed data derived; parser version, validation provenance, rejected
records and reasons preserved.

**Unresolved assumptions** are listed under the compatibility matrix.

---

## 12. Recommended implementation sequence (after approval of §11)

1. Amend the proposed SQL (pinned `search_path`, `NOT NULL retention_days`, tombstone guards, disabled check,
   error codes, `created_at`, message fix) and **re-prove it** with the new bypass tests on a scratch copy of
   the **real** schema; settle B-2 and finalise the DDL.
2. Build the pure-module prerequisites: compact document, `parser_version`, `from_dict`, integrity re-parse,
   in-parser record cap, evidence-on-demand.
3. Migration `0010` and `schema.sql` together (assertions, checksum stamp, identical function text); declared
   updates to the privilege-boundary lists; equivalence test green.
4. DAO verbs and gates with the test files in §9.2; sequential full suite.
5. Retention: `_purge_call_sets`, anchor and D6 behaviour, tests.
6. Routes, then the map's DAO-backed call-set source; declared census updates.
7. Docker slim/full builds.
8. Frontend deployment as its own milestone (§10).

---

## 13. B-1 resolution (v3): exact SQL security changes, and what was verified

### 13.1 Exact changes to `0010_call_sets.proposed.sql`

1. **Every function pins its search path:** `SET search_path = pg_catalog, public, pg_temp` — `pg_catalog` first,
   `pg_temp` **explicitly last**. Applied to all 14 functions (5 helpers, 8 trigger functions, `caller_approvals_no_update`).
2. **Every reference to a sensitive table or to one of our functions is schema-qualified** (`public.role_assignments`,
   `public.call_sets`, `public.call_set_events`, `public.caller_approvals`, `public.vcfs`, `public.audit_log`,
   `public.users`, `public.sequencing_runs`, `public.call_set_state(...)`, `public.actor_holds_role(...)`, and so on),
   including `%ROWTYPE` declarations, trigger definitions and grants.
3. **`TEMP` is revoked** with `REVOKE TEMPORARY ON DATABASE <current> FROM PUBLIC`, **and** from `clinical_app` and
   `clinical_retention`, inside a `DO` block using `current_database()`. The revoke from `PUBLIC` is what matters: the
   privilege is inherited from the default `PUBLIC` grant, so revoking from `clinical_app` alone changes nothing (verified).
4. **One `SECURITY DEFINER` function is now present, deliberately and narrowly** (`call_sets_require_redaction_audit`,
   §14): trigger-returning (cannot be called from SQL), pinned path, qualified names, reads one table, `REVOKE ALL … FROM
   PUBLIC`. A test asserts it is the **only** definer function.
5. Added `MIGRATION-ASSERT` lines for the promoted migration (`to_regclass(...)`, and `NOT has_database_privilege(... 'TEMP')`
   for both application roles). The repository's schema-equivalence test compares table/column privileges but **not** the
   database ACL, so the assert is what protects the `TEMP` revoke on the migration path; the fresh-database path
   (`schema.sql`) must carry the same `DO` block.

### 13.2 Effective privilege verification (real schema, real bootstrap, real login roles)

| | Before the SQL | After v3 |
|---|---|---|
| `datacl` of the database | `NULL` (defaults) | `{=c/postgres, postgres=CTc/postgres}` — `PUBLIC` keeps **CONNECT only** |
| `has_database_privilege('clinical_app','TEMP')` | true | **false** |
| `… ('clinical_retention','TEMP')` | true | **false** |
| A role that is a **member of** `clinical_app` | true | **false** (inheritance checked) |
| `PUBLIC` holds `TEMP` | yes | **no** |
| `clinical_app` role memberships | none | none (so no other inheritance route) |
| `clinical_app` attributes | no super/createdb/createrole, `inherit` | unchanged |
| Real login, `CREATE TEMP TABLE` | succeeded (both roles) | **refused: permission denied to create temporary tables** (both roles) |
| Control: put the default `PUBLIC` grant back | — | check flips to true and the temp table can be created (the check can fail) |

Unverifiable from the repository: whether production roles differ from the bootstrap defaults, and who owns production objects.

### 13.3 The two original attacks, reproduced and re-tested

Four attacks were run through real `clinical_app` logins against **five variants** of the SQL, each with a layer removed
mechanically from the proposed SQL (a 20-case matrix):

| Variant | no Lab role submits | submitter validates own set (both roles) | forged caller approval | skip VCF inheritance |
|---|---|---|---|---|
| **Control** — unqualified, unpinned, `TEMP` granted (the original B-1) | **SUCCEEDS** | **SUCCEEDS** | **SUCCEEDS** | **SUCCEEDS** |
| Pinned search path only | blocked | blocked | blocked | blocked |
| `TEMP` revoked only | blocked | blocked | blocked | blocked |
| Schema-qualified only | blocked | blocked | blocked | blocked |
| **Final** — qualified + pinned + `TEMP` revoked | blocked | blocked | blocked | blocked |

The control reproduces **both original findings** (a user with no Lab technician role submitting; a person holding both
roles validating their own submission), proving the attacks are real; **each layer on its own** stops them; the final
configuration stops them with three independent layers. Two static tests guard the property: every function has the
pinned path and only one is a definer; no function body names a sensitive table or helper unqualified.

---

## 14. B-2 (Option B) and H-2/H-3: the redaction, retention and purge design as verified

### 14.1 Redaction (Option B)

* `call_sets.source_vcf` and `parsed_document` become **nullable only for redaction**; `content_redacted_at/by` mark
  the record. A **content-state CHECK** allows exactly two shapes: content present and not redacted, **or** redacted with
  **both** content columns NULL **and** the record tombstoned.
* **Only the retention role** holds `UPDATE` on those columns (column-level); `clinical_app` holds none.
* The immutability trigger allows content to leave **only as a complete redaction** of a tombstoned record, by the
  privileged principal, with the actor-context guard; it refuses partial redaction (bytes only / document only),
  redaction without tombstone, "replace instead of remove", and any change to a redacted record. A tombstone is one-way.
* **Audit linkage enforced by the database**: a deferred constraint trigger refuses the commit unless a
  `retention_purge` audit row for **that** call set from the **same transaction** exists. It is a narrowly hardened
  `SECURITY DEFINER` function because the retention role may insert audit rows but, by existing design, cannot read them.
  **Caveat:** it identifies the row by the writing transaction's xid, so purge code **must not** wrap the audit `INSERT`
  in a savepoint; if it does, the redaction is refused (data is kept), which is tested.
* After redaction: bytes and parsed document are NULL (**no genomic content remains in any column of the row**, tested by
  rendering the whole row to text); the SHA-256, parser version, retention snapshot, tombstone/redaction attribution and
  all lifecycle events are retained (events are tombstoned with the set, not erased); audit rows persist and the retention
  role cannot alter or delete them.

### 14.2 Retention (H-2/H-3)

* **Snapshot:** `retention_days INTEGER NOT NULL CHECK (> 0)`, immutable for everyone (privilege for the application and
  retention roles; trigger for the owner).
* **Inheritance, enforced in the database:** if the run has any non-tombstoned VCF with a resolved `retention_days`,
  the call set must carry the **longest** such value (several VCFs: the maximum; tombstoned VCFs ignored). If the run has
  no VCF, the DAO resolves the value with the existing pure function (age at collection and the organisation's `vcf`-class
  override) before insert; the database only requires it be positive.
* **Retention anchor.** Same rule as VCFs: the **latest release** of any report of any interpretation of any VCF of the
  call set's sequencing run, plus the snapshot. **A run with no VCF has no report path in the schema, so its call sets have
  no anchor and never become eligible** — a deliberate, conservative consequence of "nothing unreleased expires", flagged as
  a policy decision (§16). A VCF with no release is the same.
* **D6:** a call set with a live (non-tombstoned) successor is never eligible; once the successor is purged the
  predecessor becomes eligible. Already-tombstoned sets are never selected again.
* **The final retention duration stays provisional** — nothing here sets or implies a period.

### 14.3 The dedicated purge path (specification; not built)

`RetentionPrincipal._purge_call_sets(org_id, now)`, dispatched from `purge_expired(org, 'call_set')`, in the existing
class's style (**no internal commit; the caller commits**, one transaction per call):

1. Select eligible ids with the verified eligibility query (anchor + snapshot ≤ now; not tombstoned; no live successor).
2. For each id, in that single transaction: `SET LOCAL clinical.actor_id = <system principal>`; one `UPDATE` setting
   tombstone **and** `source_vcf = NULL`, `parsed_document = NULL`, `content_redacted_*`; one `UPDATE` tombstoning its events;
   one `INSERT` into `audit_log` (`action='retention_purge'`, `resource_type='call_set'`, `resource_id=<id>`,
   `outcome='success'`, `actor_role='System'`, `details={"artefact_class":"call_set"}`) — the same shape the existing
   purge writes; **no savepoint around that insert**.
3. Any exception aborts the whole transaction (verified for a fault after redaction, after the events cascade, after the
   audit insert, and on the second set of a batch — the first set is left untouched).
4. The deferred trigger independently refuses a commit that lacks the audit row, so the audit cannot be skipped by a bug.

### 14.4 Test results (real schema, real logins, disposable database)

| Area | Result |
|---|---|
| Whole suite | **220 passed** |
| Effective `TEMP` privileges incl. `PUBLIC` inheritance and role membership; real-login `CREATE TEMP` refused; control flips | passed |
| Temp-table shadowing: 4 attacks × 5 variants (20 cases); control succeeds, every layer blocks | passed |
| Self-validation by an actor holding both roles (matrix + dedicated tests) | passed |
| Unauthorised direct writes as `clinical_app` (update/delete/truncate/`ON CONFLICT`/disable or drop trigger/replace function/`session_replication_role`/redaction columns) | passed |
| Retention snapshot: NOT NULL/positive; inherits VCF (single, several, tombstoned ignored); immutable for app, retention role and owner | passed |
| Purge eligibility: no VCF, no release, not yet due, due, boundary, live successor, already purged | passed |
| Atomic redaction of **both** columns; no content in any column; hash/parser/snapshot retained; events retained and tombstoned; audit row correct | passed |
| Post-redaction: application reads NULLs; app, retention role and owner all fail to restore or alter; no further events | passed |
| Redaction preconditions: complete, tombstoned, redactor named, privileged, actor context, audit row **in the same transaction, for that record, with that action** (other txn / other record / other action all refused; savepoint caveat refused safely) | passed |
| Audit metadata preserved; retention role cannot update or delete audit rows | passed |
| Rollback: failure at redaction / events / audit steps; failure on the second set of a batch | passed |
| CHECK constraints hold on their own with every trigger disabled | passed |
| Races (real threads, real logins): decisions; two successors; successor validation vs predecessor retraction; two retraction requests; decline vs retract; validation vs caller retirement; same bytes on two runs and on one run | passed |
| **Mutation pass** (23 deliberately broken SQL copies) | **22 caught, 1 equivalent** — the "redacted record is final" rule is redundant with the one-way tombstone and remove-only content rules and is kept as belt-and-braces |

The mutation exercise also exposed three weaknesses in my **tests** (envelope cases failing for the wrong reason, no
same-transaction wrong-resource audit case, no trigger-independent CHECK test); all were fixed and re-run.

---

## 15. H-1 corrected statement: what the database enforces and what it trusts

**The database enforces** (subject to the owner/superuser caveat below):
* that every recorded actor id belongs to the call set's organisation and **currently holds the role claimed** for the event;
* that the **recorded** validator/rejector differs from the **recorded** submitter; that only the **recorded** submitter
  withdraws; that a proposer's recorded id differs from the approver's;
* the transition table, uniqueness (one decision, one successor, one answer per request), immutability, append-only
  grants, redaction rules and the same-transaction audit linkage.

**The database does not and cannot enforce:**
* **who the human actually is.** The application connects as one login (`clinical_app`) and **supplies every actor id**; a
  malicious or compromised application role can record any actor who holds the role (demonstrated by
  `test_the_actor_context_is_not_a_security_boundary`), and can already insert/update `role_assignments` by design.
* anything against the table owner, a superuser, or a role that can disable triggers.

**The actor-context guard** (`SET LOCAL clinical.actor_id`, compared with the actor in the row) catches a DAO **bug** that
forgets or mismatches the actor. It is **defence in depth only** and must never be cited as a security control against a
malicious application role. The DAO must derive every actor id from `Session.user_id` only, and no verb may accept an
actor argument.

---

## 16. Backups, physical erasure, remaining blockers and unresolved policy decisions

### 16.1 Backup retention and deletion — documented separately

* **What exists:** `docs/CLINICAL_DEPLOYMENT.md` documents a **manual `pg_dump`** procedure that was tried against the
  bundled database only; "nothing schedules, encrypts, ships or monitors backups". Nothing in the repository defines
  backup retention, deletion, WAL archiving, replicas or point-in-time recovery.
* **What live-row redaction does and does not do.** Setting `source_vcf`/`parsed_document` to NULL removes the content from
  the **live row**. It does **not**, by itself, remove it from: older row versions until vacuumed and the table's
  out-of-line (TOAST) storage; write-ahead log segments; replicas; base backups or `pg_dump` files taken before the
  redaction; or restores of any of these. **Nothing in this design claims that tombstoning or redaction removes content from
  existing backups.**
* **Therefore unresolved (policy, not code):** how long dumps and base backups are kept; how and when a redacted record is
  removed from them (or whether restored backups are re-redacted); WAL and replica handling; whether and when the table
  is vacuumed after a purge; who attests deletion. These require a ruling and counsel before any erasure statement
  (including under DPDP) is made.

### 16.2 Remaining blockers

* **None technical.** B-1 is resolved and verified; B-2's mechanism is decided and verified. The migration, DAO,
  `RetentionPrincipal` extension, pure-module prerequisites, and the route/census updates are **not built** and need your
  explicit approval.

### 16.3 Unresolved policy decisions (not decided here)

1. **Final retention duration** for call sets (still provisional; the VCF rule is only the snapshot source).
2. **Backups, WAL, replicas, vacuum and deletion attestation** (§16.1) — blocks any claim of physical erasure.
3. **Other file-derived or identifying fields that survive redaction:** `source_filename`, `vcf_sample_name`,
   `caller_in_header` (text from the file header), `call_set_events.reason` (free text), and the retained **SHA-256**
   (provenance value versus confirmation-attack value). They are kept in this design as "non-genomic metadata"; that
   classification needs a ruling.
4. **Orphan evidence:** call sets on runs with no VCF or no release never expire; rejected/withdrawn sets of unreleased
   runs are kept indefinitely. Confirm, or define a manual or alternative anchor.
5. **Production size/record limits** (still provisional 5 MiB / 40,000 records; DB ceilings 64 MiB / 128 MiB).
6. **`REVOKE TEMP` as a production change:** needs the database owner; affects any other tooling that uses temporary
   tables (none in the repository); the `schema.sql` fresh-database path and the migration path must both carry it.
7. **Owner of the `SECURITY DEFINER` function in production** (preferably a non-superuser migration role) — not verifiable
   from the repository.
8. **Confirm the one inclusion beyond the approved list:** lifecycle events are refused on a tombstoned call set (M-2a),
   and a tombstoned predecessor is never superseded.
9. **Not addressed this pass** (still open from §2): disabled-user/organisation check in the role helper (M-1), audit-row
   commit ordering (M-3), assembly canonicaliser unification (M-4), declared updates to the privilege-boundary and other
   guard tests (M-5), the compact-document/integrity prerequisites in the pure module and the parser's missing record cap
   (M-6, H-4), SQLSTATE codes for DAO error mapping (M-8), refused-submission storage (M-9), the Explorer's patient id in the
   URL (M-10), and `created_at` stamping (M-11).

### 16.4 How to reproduce (disposable database only)

```
CALLSET_DESIGN_DSN=postgresql://postgres:***@127.0.0.1:55433/postgres \
    python -m pytest docs/design/genome_callsets/test_lifecycle.py -p no:cacheprovider -q
```
The suite builds and drops its own databases (`callset_design` and five variant databases) and touches no repository file.


---

## 17. Implementation record: migration 0010, the DAO, retention and redaction (uncommitted; nothing applied to a real database)

**Status: IMPLEMENTED IN THE WORKING TREE ONLY.** Not committed, not pushed, not deployed, not applied to any real or
shared clinical database. Every result below comes from disposable PostgreSQL 16 databases (`127.0.0.1:55433`), using
the real `clinical_app` / `clinical_retention` roles. Patient genome routes remain off; no Docker, interpretation-engine
or frontend change was made.

### 17.1 Migration and dependency order

* **File:** `clinical/migrations/0010_call_sets.sql` (version 10, after 0009). `clinical/schema.sql` carries the identical
  DDL before its `schema_migrations` block, plus the stamp row `(10, 'call_sets', <checksum of the file>)`. Equivalence is
  enforced by `test_w126m_schema_equivalence` and by two new tests that require the promoted body to appear verbatim in
  both the migration and `schema.sql`, and to equal the reviewed v3 SQL.
* **Depends on (all created by the 0002 baseline):** `organisations`, `users(org_id, user_id)` (unique), `role_assignments`,
  `sequencing_runs(org_id, id)` (unique), `vcfs`, `audit_log`; and the login roles from `clinical.bootstrap`. Nothing in
  0003-0009 is touched. A preflight block refuses, and changes nothing, if a role or parent table or composite key is
  missing, if the migrating role neither owns the database nor is a superuser, or if a call-set table already exists.
* **Migration role and ownership (measured):** every new table and function is owned by the role that ran the migration; the
  post-conditions fail `migrate --verify` and `/readyz` (NOT `migrate()` itself, which applies and records; corrected in the wave-3 notes) if any is owned by `clinical_app` or `clinical_retention`, if `clinical_app` can
  UPDATE/DELETE a call-set table, or if `clinical_retention` can DELETE. The single `SECURITY DEFINER` function
  (`call_sets_require_redaction_audit`) is owned by that migrating role, is not executable by either application role or
  `PUBLIC`, and all 14 functions pin `search_path = pg_catalog, public, pg_temp`.
* **`TEMP`:** revoked from `PUBLIC`, `clinical_app` and `clinical_retention` for the database. Verified before and after
  (PUBLIC held it before; nobody in scope holds it after), and `SET ROLE clinical_app|clinical_retention` followed by
  `CREATE TEMP TABLE` is refused. The four shadowing attacks run against the repository's own objects and all fail.
* **Rollback (measured):** a failure part-way through the file (an object of the same name created outside the mechanism)
  leaves no table, no history row and the original `TEMP` privilege; a pre-existing table is refused up front; a
  non-owner, non-superuser role is refused. The migration is forward-only; there is no down-migration.

### 17.2 DAO methods and their authorization boundaries

The organisation is always the session's. A patient or sequencing-run id from a caller is verified against that
organisation and the run's own chain (run -> sample -> order -> test/patient); a miss is the same `NotFoundError` as a
missing id. The role gate is the first statement. Every method is `@auditable` (a refusal is a `denied`/`error` row).

| Method | Role | What it enforces beyond the role |
|---|---|---|
| `propose_caller_version` | Administrator | exact name/version/class; reason required |
| `approve_caller_version` / `retire_caller_version` | Approver | proposer may not approve their own proposal (DB); only a proposed version can be approved, only an approved one retired |
| `list_caller_versions` | Admin/Approver/Interpreter/Lab tech | configuration data; documented non-audited read |
| `submit_call_set` (incl. replace via `supersedes_id`) | Lab technician | run in this org; file's single sample column equals the run's sample; declared assembly = header = ordered test's assembly (**hard refusal, no liftover**); size and record caps **before** parsing; UTF-8; whole-file reject; SHA-256, parser version, compact derived document, **retention snapshot** stored; 'submitted' event in the same transaction |
| `validate_call_set` | Interpreter (not the submitter) | pending only; **re-verifies** SHA-256, a fresh re-parse of the stored bytes against the stored document, sample identity, assembly, and that the caller version is **currently approved**; records the exact checks and the approval row used; supersedes the predecessor atomically (trigger) |
| `reject_call_set` | Interpreter (not the submitter) | pending only; reason required |
| `withdraw_call_set` | the submitter (Lab technician) | pending only; reason required |
| `request_call_set_retraction` | Interpreter | validated only; one open request at a time |
| `retract_call_set` / `decline_call_set_retraction` | **Approver only** | retract needs a validated set; decline answers the open request |
| `list_call_sets_for_run`, `get_call_set_for_review` | Lab tech / Interpreter / Approver | metadata and the derived document only; **never the bytes** |
| `list_patient_validated_call_sets` | Orderer / Interpreter / Approver | the only clinical-map reader: state exactly `validated`, not tombstoned, not redacted, assembly equal to the ordered test's, reached through this organisation's patient; each document re-validated, and one that fails is not returned and is recorded as `call_set_integrity_failure` (id only) |

`GET .../genome/map` now reads this through `DaoCallSetSource` (no SQL, no logging and no organisation or patient decision
in the endpoint module). No new HTTP route was added; the patient routes are unchanged and still off.

### 17.3 Retention and redaction

* **Snapshot at creation, `NOT NULL`, never recomputed:** the run's own non-tombstoned VCF `retention_days` (the database
  requires equality with it); when the run has no VCF, the platform's existing age-conditional rule
  (`_resolve_vcf_retention_days`). No period is invented for anything else.
* **`RetentionPrincipal.purge_expired(org, 'call_set')` -> `_purge_call_sets`:** eligible = latest release across the run's
  VCFs' reports + the call set's own `retention_days` has passed, and no live successor. Per eligible call set, **inside the
  caller's transaction (no internal commit, no savepoint)**: one UPDATE tombstones it and removes the original bytes and
  the parsed document together; its events are tombstoned; the `retention_purge` audit row is written. The database
  refuses the redaction at commit unless that audit row is in the same transaction (measured: audit failure -> full
  rollback; missing audit row -> refused at commit and rolled back).
* **Unresolved queue:** a live call set whose run has **no VCF** (`no_vcf`) or whose VCFs have **no released report**
  (`no_release`) is never purged and never treated as eligible. `RetentionPrincipal.unresolved_call_sets(org)` lists them and
  `PurgeResult.unresolved_ids` carries them. Releasing a report moves one out of the queue and into eligibility (tested).
* **Preserved after redaction:** ids, run, assembly, caller name/version, SHA-256, parser version, retention snapshot,
  who/when, every lifecycle event, the audit row. **Not claimed:** removal from superseded row versions, TOAST, WAL,
  replicas, backups or dumps (section 16).

### 17.4 Deviations from approved SQL v3

1. **None in the statements:** the 14 functions, 3 tables, triggers and grants are the reviewed v3 text verbatim (test-enforced).
2. **Added around it:** the migration header, the preflight refusal block, six catalog `MIGRATION-ASSERT` lines.
3. **Not done, stated:** the `SECURITY DEFINER` function is owned by the migrating role, not by a dedicated no-login role
   (section 16.3 item 7 remains a deployment decision).
4. `REVOKE TEMP` is database-wide and is documented in the migration header.

### 17.5 Existing tests updated (declared; none weakened)

* `test_w167_catalogue_integrity_migrations.py`: four assertions that hard-coded "the last migration is 9" now say 10 (and 0009's
  own upgrade test applies `(9, 10)`).
* `test_w147_migration_0003_retraction.py`: the table census 31 -> 34 (three new tables), docstring figures re-measured
  (`clinical_retention` sees 16 of 34).
* `test_w135` joins: the patient->run join text is now one shared constant that both the interpretation chain and the call-set
  reader extend, so the guard that the chain is written once still holds.
* `test_genome_analysis_endpoints`: unchanged; the endpoint module still imports no logging.

### 17.6 Findings from section 2 closed or left open by this pass

Closed: M-6 and H-4 (compact document, `parser_version`, `from_dict` rehydration, in-parser record cap, integrity re-parse);
M-8 (SQLSTATE mapping to `CallSetConflictError`, which rolls back); M-5 (guard updates above). Left open: M-1, M-3, M-4,
M-9, M-10, M-11 (unchanged from section 16.3 item 9), plus every item in 16.3 that is a policy decision.

### 17.7 Operational notes

* A suite that creates or drops roles must never run concurrently with another on the same cluster (`tuple concurrently
  updated`), and an aborted design-suite run leaves `callset_*` databases that make later bootstrap tests fail. Both
  happened during this work and were cleaned from the disposable cluster.
* `docs/design/genome_callsets/test_lifecycle.py` now defaults to the repository schema (`CALLSET_REPO_SCHEMA=1`): the
  proposed SQL is no longer applied on top, the 20-case weakened-variant matrix is skipped (it was green 20/20 before
  promotion), and four attacks run against the real objects instead.

### 17.8 Test results (disposable PostgreSQL 16 only; run one at a time)

| Suite | Result |
|---|---|
| Lifecycle design suite, against the repository schema (`CALLSET_REPO_SCHEMA=1`) | **204 passed, 20 skipped.** The 20 skipped are the weakened-variant matrix, which re-applies the *proposed* SQL and is not applicable once the objects live in `schema.sql`; it was 220/220 green before promotion, and four of its attacks now run against the real objects. |
| `test_callset_migration.py` (order, dependencies, ownership, `TEMP`, rollback, verbatim body) | 17 passed |
| `test_callset_dao.py` (authorization, org isolation, caller approval/retirement, retention snapshot, atomic redaction, rollback, **concurrent replacement**, **concurrent retraction**) | 44 passed |
| `test_w126m` schema-equivalence and migration-mechanism/concurrency, `test_w151` | 62 passed |
| Pure-module and map tests (`test_genome_structural`, `test_genome_analysis`, `test_genome_map_endpoints`) | 213 collected; passed |
| **Full clinical suite, clean final run** | **2680 passed, 38 failed, 5 errors.** The 43 failing/erroring test ids are **identical** to the recorded pre-existing baseline (43); **no test fails that did not fail before, and none was fixed.** The baseline failures remain (test_w164 MFA family, test_w171 compose/secrets, and the others on the recorded list). |
| GEPER suite (`geper/tests`, `geper/api/tests`) | 3575 passed, 190 skipped, **1 failed** (`test_main_help_names_the_component`: the interpreter is Python 3.14.7 and GEPER requires <3.13 - environmental). The recorded baseline had 2; I did not reconcile which, so treat "no new GEPER failure" as unproven beyond this single failure's cause. |

Earlier in this work two intermediate full runs showed extra setup errors; both were traced to my own concurrent/aborted
design-suite runs (section 17.7), not to the code, and the final run above is clean.

Mutation checks of the DAO (each restored afterwards): removing the organisation scope from the run chain, the
integrity re-parse, the `validated`-state filter on the map read, and the submitter pre-check each fail a named test. One
mutant (skipping the DAO's own "caller currently approved" pre-check) is **not** caught by a different test, because the
database trigger refuses the same act with the same message; that is the intended second layer, not a gap.

