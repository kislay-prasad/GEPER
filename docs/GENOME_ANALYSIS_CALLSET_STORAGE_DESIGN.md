# Genome Analysis — CNV/SV call-set storage and ingestion: design (v2, for approval)

> **CORRECTION (v3, see `GENOME_ANALYSIS_CALLSET_IMPLEMENTATION_REVIEW.md` §0 and §15).** Wherever this document says
> "database-enforced", read: the database enforces **separation of recorded actors** (the actor ids the application
> writes must hold the claimed roles and the recorded validator must differ from the recorded submitter). It does **not**
> authenticate a human; the application supplies every actor id as a single login. The retention, tombstone, redaction
> and `TEMP`/search-path decisions in §8 and the SQL are superseded by the v3 SQL and the review document.

**Status: DESIGN ONLY.** Nothing is migrated, no DAO or route exists, no production data
or configuration is touched, nothing is committed. The proposed SQL lives under
`docs/design/genome_callsets/` and is **not** part of
`clinical/migrations`, `clinical/schema.sql` or any CI job. Its verification tests
(`test_lifecycle.py`, the design-verification suite) were written alongside it and are
**not included in the repository**; their collected successor is
`clinical/tests/test_callset_lifecycle_db.py`.

Companion files (the SQL is canonical; this document does not repeat it, so the two cannot drift):

* `docs/design/genome_callsets/0010_call_sets.proposed.sql` — final DDL, every trigger and constraint, grants.
* `test_lifecycle.py` (the design-verification suite; **not included in the repository**) — 155 database-level tests when
  this design was written (a historical count; details in section 10). Its collected successor is
  `clinical/tests/test_callset_lifecycle_db.py` (206 tests when last run).

## 1. Decisions as approved, and how each is realised

| | Decision | Realisation in this design |
|---|---|---|
| D1 | Two-table immutable evidence + append-only lifecycle | `call_sets` (immutable) + `call_set_events` (append-only); a third append-only table, `caller_approvals`, governs callers (D3) |
| D2 | Permission matrix; validator ≠ submitter, database-enforced | Triggers enforce role, separation of duties and every transition; DAO gates are a second layer (sections 5–6). **Triggers are tested; DAO gates are specified, not yet built** |
| D3 | Approved-callers allow-list | Section 3; unapproved submissions are **quarantined**, never refused, never validatable, never drawn |
| D4 | Approver-only retraction | Plus a documented Interpreter **retraction-request** mechanism (section 4.3) |
| D5 | VCF retention provisionally; final duration subject to ruling | Section 8 — no new duration invented |
| D6 | Hard refusal on assembly disagreement | DAO gate; no liftover, no coordinate reinterpretation anywhere |
| D7 | Configurable size limit, measured | Section 7 — measured; **the earlier 8 MiB / 16 MiB pairing was wrong** |
| D8 | Clinical interpretation outside this migration | No column, event or check holds clinical meaning (section 2) |

Also required and met by design: supersession is an explicit linked lifecycle event;
original bytes, hashes, parser version, rejected records and reasons are preserved;
caller-level FILTER status stays separate from interpretation; patient genome access stays
disabled; frontend/auth/CSRF/proxy/CSP/CI is a separate milestone (section 11).

## 2. Four separate acts, four separate records

| Act | Actor | Record | Asserts | Does not assert |
|---|---|---|---|---|
| Ingestion | Lab technician (+ machine parse) | `call_sets` row + `submitted` event | This file, from this caller, declared as this build/class, belongs to this run; it parses to these outcomes | That it is right, or clinically meaningful |
| Caller approval | Administrator proposes, a different Approver approves | `caller_approvals` rows | The lab has validated **this exact version** of this caller for this class | Anything about any particular patient's file |
| Validation | Interpreter ≠ submitter | `validated`/`rejected` event with three technical attestations | Sample identity, assembly and caller are as declared | Clinical significance |
| Interpretation / clinical approval | (future table) / Approver | **Not in this design.** A future table must reference `call_set_id` and a finding id and never alter these records | — | — |

**FILTER is the caller's, not ours.** `detected`/`uncertain` inside `parsed_document` are
derived only from the caller's `FILTER` column (PASS = `detected`; anything else, including
`.`, = `uncertain`). Nothing may map them to clinical meaning. The UI must label them
"caller FILTER: PASS / not PASS", never "detected" as a diagnosis, and must show
*validation state* as a separate badge. The `checks` column accepts exactly three boolean
attestations (`sample_identity`, `assembly`, `caller`) and nothing else, enforced by a CHECK,
so free-text or clinical content cannot be smuggled into an event.

## 3. Schema summary (full text: the proposed SQL)

| Object | Purpose | Key constraints |
|---|---|---|
| `call_sets` | Immutable evidence: original bytes, hash, parser version, parsed outcomes, lineage | Composite FKs `(org_id, sequencing_run_id)`, `(org_id, created_by)`; `source_content_hash = sha256(source_vcf)` CHECK; JSONB envelope CHECK (type, schema_version, assembly and class agree with columns, counts = records, size ceiling); unique `(org_id, run, class, hash)`; at most one successor per set; supersession FK forces same run **and** class; tombstone-pair completeness |
| `call_set_events` | Append-only lifecycle | Role-per-event CHECK; reason required where the design says; attestations shape CHECK; `related_call_set_id` iff `superseded`; `caller_approval_id` iff `validated`; one `submitted`, one decision, one `superseded`, one `retracted`, one answer per request (partial unique indexes) |
| `caller_approvals` | Append-only approval log; the **latest row** for `(name, exact version, class)` is its state | Role-per-event CHECK; non-blank trimmed name/version/basis |
| Functions | `call_set_state`, `open_retraction_request`, `caller_state`, `actor_holds_role` | Read-only (`STABLE`) |
| Triggers | See the transition tables | `call_sets` BEFORE INSERT / BEFORE UPDATE / deferred "must have its submitted event"; `call_set_events` BEFORE INSERT / AFTER INSERT (supersession) / BEFORE UPDATE; `caller_approvals` BEFORE INSERT / BEFORE UPDATE |
| Grants | Same pattern as `vcfs`, `reviewer_claims` | `clinical_app`: `SELECT, INSERT` only; `UPDATE, DELETE` revoked from it and `PUBLIC`; `clinical_retention`: `SELECT` + **column-level** `UPDATE (tombstoned_at, tombstoned_by)` on `call_sets`/`call_set_events`, no `DELETE`; `caller_approvals` is configuration, never tombstoned |

**Linking** — organisation by composite FK and `Session.org_id` (never the request); run by
composite FK; patient and sample **derived** through the run via the existing patient chain
and never stored; assembly stored, tied to the document by CHECK, and gated against the
ordered test and the run's small-variant analysis (DAO); caller name+version stored beside
the header's `##source`; the VCF sample column bound at submission and attested at validation.

### 3.1 Caller approval governance

* **Who:** an **Administrator proposes**; a **different Approver approves** (proposer ≠ approver
  is a database rule even when one person holds both roles); an **Approver retires**. An
  Administrator cannot approve; an Approver cannot propose. (The existing `tests` catalogue
  stamps approval at creation by the Administrator; caller approval is deliberately stricter —
  confirm this is wanted.)
* **Versions:** approval is per **exact** `(caller_name, caller_version, record_class)`; no
  ranges or wildcards; names/versions are trimmed and case-sensitive. A new version is a new
  proposal and a new approval — approving 1.0 says nothing about 1.1. `basis` is mandatory and
  names the lab's validation record for that version.
* **Retirement:** blocks new validations. Already-validated sets stay validated; the map shows a
  derived "caller version since retired" flag, and the Approver may retract if warranted.
  Re-use of a retired version needs a fresh proposal and approval.
* **Unapproved callers — quarantine:** the submission is **accepted and preserved in full**
  (bytes, hash, parse outcomes) with `caller_approved_at_submission = false` set by the
  database. The set sits in `pending_validation`, is listed in the review queue marked
  "awaiting caller approval", cannot be validated (trigger), and is never drawn. When the
  exact caller version is later approved, the **untouched** set becomes validatable; the
  `validated` event cites the approval row relied on (it must be the latest row in force; a
  stale, proposed, retired or other-version row is refused). Verified: the call-set row is
  byte-identical before and after.

## 4. Lifecycle

### 4.1 Call-set transition table (the complete set; everything else is forbidden)

| From | Event | Actor (role, and rule) | Further conditions | To | Layer |
|---|---|---|---|---|---|
| — | `submitted` | Lab technician = the creator | `call_sets` row in the same transaction; deferred trigger refuses commit without it | `pending_validation` | trigger |
| `pending_validation` | `validated` | Interpreter **≠ submitter** | cited approval is `approved`, same name/version/class, latest in force; three attestations all true | `validated` | trigger |
| `pending_validation` | `rejected` | Interpreter **≠ submitter** | reason required | `rejected` (terminal) | trigger |
| `pending_validation` | `withdrawn` | **the submitter** (Lab technician) | reason required | `withdrawn` (terminal) | trigger |
| `validated` | `retraction_requested` | Interpreter | reason; no request already open | `validated` (annotation) | trigger |
| `validated` | `retraction_declined` | Approver | reason; `responds_to` = the open request | `validated` (annotation) | trigger |
| `validated` | `retracted` | Approver | reason; optional `responds_to` = the open request | `retracted` (terminal) | trigger |
| `validated` | `superseded` | **System** (written by the AFTER trigger as the consequence of the successor's validation); actor = the successor's validator | successor names this set and is validated; `related_call_set_id` = successor | `superseded` (terminal) | trigger |

Terminal states (`rejected`, `withdrawn`, `retracted`, `superseded`) accept no event. A
`superseded` event cannot be written by hand. The role of every actor is checked **against
`role_assignments` at the moment of the event**, so a revoked role stops working at once.

**Verified exhaustively:** 6 states × 8 event types = 48 cases, each attempted by a correctly
roled actor, outcome must equal this table; a refused event leaves no trace and no state change.

### 4.2 Call-set lineage and representation

| State (derived from the latest state-changing event) | On the clinician map | Listed? | Evidence kept? |
|---|---|---|---|
| `pending_validation` | Not drawn ("N submitted, not yet validated"; "awaiting caller approval" if quarantined) | Yes | Yes |
| `validated` | **Drawn**; flagged if a retraction request is open or the caller version was since retired | Yes | Yes |
| `rejected` | Not drawn | Yes, with reason | Yes (full) |
| `withdrawn` | Not drawn | Yes, with reason | Yes |
| `superseded` | Not drawn (its successor is); pointer to successor | Yes | Yes |
| `retracted` | Not drawn; prominent | Yes, with reason | Yes |

**Supersession** is explicit: a replacement names `supersedes_id` at submission (DB: same run,
same class, predecessor must be **currently validated**, one successor per set). Nothing
changes while the replacement is pending. When it is **validated**, the database writes a
`superseded` event on the predecessor (same transaction, actor = validator, `related_call_set_id`
= successor), so history is auditable from either side. If the replacement is rejected or
withdrawn, the predecessor stays validated. If the predecessor was retracted meanwhile, the
successor may still be validated, no `superseded` event is written (it is already terminal),
and `supersedes_id` keeps the lineage.

### 4.3 Retraction-request mechanism (Interpreter → Approver)

1. An Interpreter files `retraction_requested` on a validated set with a reason (e.g. suspected
   sample swap). It appears in the Approver's queue and as a flag on the map; **the set stays
   drawn until an Approver decides** — a request is not a retraction.
2. One request may be open at a time (database-enforced).
3. The Approver answers with `retracted` (set leaves the map, terminal) or `retraction_declined`
   (reason mandatory, cites the request). A declined request may be requested again with new
   reasons; every step is an immutable event.
4. An Approver may also retract directly without a request.

### 4.4 Caller-approval transition table

| From (latest state of the exact key) | Event | Actor | Rule | To |
|---|---|---|---|---|
| none or `retired` | `proposed` | Administrator | basis required | `proposed` |
| `proposed` | `approved` | Approver, **≠ the proposer** | basis required | `approved` |
| `approved` | `retired` | Approver | basis required | `retired` |

All other pairs are forbidden (12 cases verified: 4 states × 3 events), plus the role and
separation rules above.

## 5. Role-by-role permission matrix

`DB` = enforced by trigger/CHECK/grant (tested). `DAO` = enforced by the data-access layer
(specified here, **not yet built**).

| Action | Lab technician | Interpreter | Approver | Orderer | Administrator | Auditor | Patient |
|---|---|---|---|---|---|---|---|
| Submit a call set (DB role; DAO gates §6) | ✔ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ |
| Withdraw a pending set | ✔ **own only** | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ |
| Validate / reject a pending set | ✖ | ✔ **not own** | ✖ | ✖ | ✖ | ✖ | ✖ |
| Request retraction of a validated set | ✖ | ✔ | ✖ | ✖ | ✖ | ✖ | ✖ |
| Decline / grant (retract) a retraction | ✖ | ✖ | ✔ | ✖ | ✖ | ✖ | ✖ |
| Propose a caller version | ✖ | ✖ | ✖ | ✖ | ✔ | ✖ | ✖ |
| Approve / retire a caller version | ✖ | ✖ | ✔ (approve: **not own proposal**) | ✖ | ✖ | ✖ | ✖ |
| Modify or delete any evidence | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ | ✖ — no path exists (grants + triggers) |
| Read a call set's content (pending/rejected/validated) — DAO | own submissions' **status** only | ✔ | ✔ | ✖ | ✖ | ✖ | ✖ |
| See validated findings on the map — DAO | ✖ | ✔ | ✔ | ✔ | ✖ | ✖ | ✖ (patient routes stay off) |
| Read audit rows (no genomic content) | ✖ | ✖ | ✖ | ✖ | ✖ | ✔ | ✖ |
| Tombstone (retention principal, separate DB role) | — | — | — | — | — | — | — |

Roles are additive, so one person may hold two. **The database rule still holds:** a person
who submitted a set cannot validate or reject it; a person who proposed a caller cannot approve it.

## 6. DAO authorization gates (specification)

Every verb: `@auditable`, role gate **first** (a refusal says nothing about whether anything
exists), organisation from `Session`, one generic refusal body per surface.

**Submission gates, in order:** (1) Lab technician; (2) run exists in the caller's
organisation; (3) supplied `sample_id` equals the run's sample; (4) sample `qc_status='passed'`
and order not `cancelled`; (5) consent for the order's `required_scope` is `consent_ok`;
(6) **assembly agreement — hard refusal, no liftover:** declared assembly equals the ordered
test's `tests.assembly` and, if the run already has a small-variant analysis, its assembly,
with both values named in the refusal; (7) file passes `parse_structural_vcf` header check and
the declared `vcf_sample_name` is a column of the file; (8) size/record limits (§7);
(9) duplicate handling — exact replay is idempotent; same bytes live on another run is refused;
(10) `replaces_call_set_id`, if given, is on the same run/class and validated. **An unapproved
caller is not a refusal:** the set is accepted and quarantined (§3.1).

The database independently enforces the role, duplicate, lineage and caller-snapshot rules, so
a bypassed or buggy DAO cannot produce an illegal state.

## 7. Size limits — measured, and the earlier proposal corrected

Measured on **synthetic** VCFs (no real caller output exists in the repo; record shapes were
modelled on typical short-read SV and CNV callers and must be re-measured on real output):

| Class | Records | Source | Parse time | Peak memory | Parsed document (full) | × source |
|---|---|---|---|---|---|---|
| SV | 1,000 | 0.13 MB | 0.06 s | 3 MB | 1.1 MB | 8.4 |
| SV | 10,000 | 1.35 MB | 0.6 s | 31 MB | 11 MB | 8.4 |
| SV | 50,000 | 6.8 MB | 3.8 s | 157 MB | 57 MB | 8.4 |
| SV | 200,000 | 27 MB | 17.5 s | 629 MB | 228 MB | 8.3 |
| CNV | 2,000 | 0.30 MB | 0.1 s | 6 MB | 2.4 MB | 8.0 |
| CNV | 20,000 | 3.0 MB | 1.1 s | 63 MB | 24 MB | 8.0 |
| CNV | 100,000 | 15 MB | 6.9 s | 314 MB | 120 MB | 8.0 |

**Findings.** (a) The parsed document is ~8× the source, so the earlier "8 MiB source / 16 MiB
document" pair was inconsistent: a 2 MB source already exceeds a 16 MiB document. (b) Parsing is
~linear (~75 µs and ~3 KB per record), done in the request; above ~50k records it is multi-second
and hundreds of MB. (c) Every map read deserialises the validated documents, so a 50–100 MB JSONB
per set is a read-latency problem, not just a storage one.

**Design response.**
* **Store a compact document**: drop the per-record `evidence` object (measured 5× rather than 8×)
  and keep `line_no`. The original text is the single authoritative copy in `source_vcf` (also
  data minimisation); the finding-detail route re-parses that one line on demand.
  `verify_call_set_integrity` re-parses the bytes and must equal the stored document.
* **Limits are configuration, not schema:** `CLINICAL_CALLSET_MAX_BYTES` and
  `CLINICAL_CALLSET_MAX_RECORDS`, enforced in the DAO before parsing (bytes) and during parsing
  (records). **Provisional defaults: 5 MiB and 40,000 records** (≈ 40k SV records, ≈ 3.5 s,
  ≈ 130 MB peak, ≈ 20–25 MB compact document). **The production values are not chosen** until real
  caller output is measured.
* **Database hard ceilings** only guard against absurd input and leave room to raise the
  configuration without another migration: source ≤ 64 MiB, document ≤ 128 MiB.
* If real callers routinely exceed the defaults, the next step is asynchronous parsing and/or a
  per-record table (so reads do not load whole documents). That is a separate design.
* Plain-text VCF only (`.gz` must be decompressed first), so the stored bytes are exactly what was parsed.

## 8. Retention (provisional)

`retention_days` is resolved at creation by the existing `_resolve_vcf_retention_days` for the
run (age-conditional for minors) — i.e. the **VCF period, provisionally**. No duration is seeded
or invented; the final duration is **subject to an explicit policy ruling**. Clock, eligibility and
descendant blocking follow D3/D6 (anchored on release of a report on the run; a set with a live
successor or future interpretation record cannot expire; nothing unreleased expires); tombstone,
not delete (D4), with events cascading in one transaction and every purge audited (D7).
**Two limitations to rule on:** (i) `retention_days` is immutable once written (as on `vcfs`), so a
later ruling that changes the duration cannot reach existing rows without a retention-principal
procedure — the alternative is to derive eligibility from the run's VCF at purge time;
(ii) D4's open question about redacting content inside tombstoned rows (here the VCF bytes) is
inherited from `vcfs`/`interpretations` and is not decided here.

## 9. Audit

`@auditable` on every verb, `resource_type='call_set'` (or `'caller_approval'`),
`resource_id` = the record id, **no `details_builder`** — nothing genomic, no file name, caller,
assembly or counts. Actions: `call_set_submitted/validated/rejected/withdrawn/retraction_requested/
retraction_declined/retracted`, `caller_proposed/approved/retired`, `call_set_read`; map reads stay
`genome_read`. Refusals are recorded as `denied`. Supersession is recorded by its lifecycle event
(and the validation that caused it). Reasons and attestations live in the events, not in the
append-only audit table.

## 10. Test inventory

### 10.1 Database level — **built and passing: 155 tests** (historical count, when this design was written; the design suite `test_lifecycle.py` is not included in the repository, and its collected successor is `clinical/tests/test_callset_lifecycle_db.py`)

| Area | Tests |
|---|---|
| Every call-set state × every event type (48) | the transition table, including "refused leaves no trace" |
| Wrong role per event; role not held; role revoked | 12 + 3 + 1 |
| Separation of duties (self-validate/reject even with both roles; withdraw by non-submitter; submit by non-Lab-tech; submitted-event-by-creator at commit) | 4 |
| Attestation shape; mandatory reasons | 6 + 6 + 2 + 1 |
| Retraction requests (open limit, decline must answer, retract cites open request, none on non-validated, decline-then-re-request, retract with/without request) | 7 |
| Supersession (linked event, rejected/withdrawn successor, retraction mid-flight, only validated predecessor ×5, one successor, same run/class, not hand-writable) | 10 |
| Caller approvals (12-case transition matrix; proposer ≠ approver; role separation; field validation ×6; approved row in force; borrowed approval; quarantine and later release; per-version/class; stale approval; no caller version) | 27 |
| Duplicates (replay; cross-run live/terminal ×5; org-scoped) | 7 |
| Immutability and grants (app cannot update/delete ×3; owner blocked by triggers; retention-only tombstone; incomplete pair; DB clock) | 7 |
| Evidence envelope and hash | 9 |
| Basics (submit snapshot of caller status; validation changes nothing in the evidence; rejection/withdrawal keep evidence and reason) | 3 |
| Races (decisions; successors) | 2 |

**Mutation check:** 18 deliberately broken copies of the SQL were run; **17 were caught**. The one
miss was an equivalent mutant (a clause made redundant by the foreign key and the other checks).
The exercise also found and fixed two real defects before this review: a NULL-snapshot bug for
unapproved callers, and a redundant approval-matching expression.

### 10.2 Written with the implementation (see `clinical/tests/test_callset_*.py`; originally specified as **not built**)

* **Migration mechanism:** `schema.sql` ⇄ `0010` equivalence; idempotence; concurrency; declaration guards.
* **DAO gates:** each submission gate 1–10 (accept and refuse); role-before-existence for every verb
  (identical 403 for real, invented, foreign-organisation ids); cross-organisation and cross-patient
  isolation; assembly disagreement names both values and never converts; consent withdrawn refused;
  size/record limits; quarantine path end-to-end; `verify_call_set_integrity` catches a tampered
  document; compact document round-trip; evidence-on-demand re-parse; audit rows with no genomic content.
* **Retention:** eligibility, D6 blocking by a live successor, cascade to events, audit per purge.
* **Map:** only validated, non-superseded, non-retracted sets drawn; quarantined/pending/rejected listed
  with reasons; retraction-request and retired-caller flags; FILTER label separated from validation badge.
* **Declared updates** to the route-census, probe-list and refusal-parity guards, exactly as Phases 1–2 did.
* Everything run **sequentially on one test database**.

## 11. Frontend and deployment — a separate milestone (not part of storage)

Out of scope for this approval, and nothing here is implemented: staff login page with TOTP;
same-origin reverse-proxy serving of `frontend/chromosome-explorer/` and proxying of `/clinic`, `/auth`,
`/lab`, `/review` (required by `SameSite=Strict`; CORS and `SameSite=None` are rejected); CSP and HSTS
as proxy headers; an **Origin-check ruling before any POST-based ingestion or validation screen**;
CI job for the frontend tests (Node ≥ 18, no packages); and testing against the real service with a
real session and MFA. Patient-facing genome access stays disabled throughout.

## 12. Rulings still needed (not decided here)

1. Final retention duration for the `call_set` class, and whether eligibility should derive from the run's VCF at purge time (§8).
2. D4's open question: redaction of content inside tombstoned rows (§8).
3. Whether caller approval should be this strict relative to the existing test catalogue (§3.1).
4. Production size/record limits, after measuring real caller output (§7).
5. For a call set retracted because it was attached to the wrong run: this design lets the same bytes
   be resubmitted to another run (as with rejected/withdrawn), relying on validation to re-attest
   identity. Confirm, or require an Administrator step.

## 13. Implementation order once approved

1. Migration `0010` and `schema.sql` together, from the proposed SQL (with its tests promoted into `clinical/tests` against the real schema).
2. Pure module: compact document, `from_dict`, integrity verification, evidence-on-demand.
3. DAO verbs and gates, then routes, then the map's DAO-backed source.
4. Declared census/probe updates; full sequential suite; Docker builds.
