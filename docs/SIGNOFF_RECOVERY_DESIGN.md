# Recovering from an interrupted sign-off: design (Stage 2)

**Status: DESIGN ONLY. Nothing in this document is implemented.** No application code, test or API contract was changed
to write it. It does not claim clinical validity or production readiness, and it does not claim that any verification
described as a target exists.

Baseline: `master` at `59295f7` (PR #27 merged: the read-only `GET /review/reports/{id}/signoff-state` exists). Automatic
recovery and resume do **not** exist. See also `docs/SIGNOFF_API_DESIGN.md`.

**Update after the r1a2 experiment (2026-10-18).** Decisions 5 and 6, cases J and K, open items 18 to 24 and sections 13 and 14 were added after a throwaway-database experiment (run tag `r1a2`, section 13). Each statement in them is labelled **CONFIRMED (r1a2)**, **PROPOSED** or **OPEN**. **Everything marked PROPOSED is a design for review. None of it is implemented, and none of it settles or relaxes Decisions 1 to 4.** The 13 approvals and the S5 discrepancy described in section 13 are experimental evidence to be preserved; they are not a recovery.

**Labels used on statements**

| Label | Meaning |
|---|---|
| **CURRENT** | what the repository does today, verified by reading the code (and a test, where named) |
| **TARGET** | a policy or behaviour recovery is meant to achieve; **not built, not enforced today** |
| **PREREQUISITE** | something that must exist before the work that depends on it may be built or claimed |
| **INTERIM (PROPOSED)** | an operational policy proposed to apply until a prerequisite or hardening work lands; **not implemented** by the repository |
| **OPEN** | an unresolved question that needs a human or domain-expert decision; no behaviour is assumed |
| **CONFIRMED (r1a2)** | an observation made in the r1a2 experiment (section 13): real code, real PostgreSQL 16.15, synthetic data, a throwaway cluster. Evidence about that run only; it does not show production behaviour |
| **PROPOSED** | a design decision put forward for review after r1a2; **not settled, not built** |

## 0. Decisions

Four decisions are recorded (1 to 4, settled as TARGET policy); Decisions 5 and 6, after Decision 4, are **PROPOSED** additions and are not settled. Each states what is settled, what the code does today, and what follows. Where a settled
decision cannot be met by what exists, the gap is recorded as a **PREREQUISITE**, never worked around or described as done.

### Evidence classes (used by every decision)

Recovery reasons from three kinds of evidence, which must never be mixed up:

| Class | Examples | What it can establish | What it cannot |
|---|---|---|---|
| **Authoritative server state** | report state, approver id, server `approved_at`, `has_content_hash`, claims (id, type, actor, time), retraction flag, the revision block | what the clinical service has committed | whether the local files match it; the approval hash value (not exposed) |
| **Local journal evidence** (**TARGET** file) | baseline claim ids, step intent and result events, artefact hashes recorded as written | what *this tool* tried and wrote; which claim is attributable to this operation | anything about the server; it is a hint, never an authorisation |
| **Artefact integrity evidence** | the manifest's SHA-256 of both PDFs and the results JSON; the document's own `content_hash` (`compute_content_hash`); the journal's artefact hashes | that a file is the one that was hashed | that the file's *content* is the approved content (see Decision 1) |

Identity facts (organisation, interpretation, report, the revision block, the approver and the server approval time) are
**not** artefact hashes and are never inferred from one.

### Decision 1: content binding

**Settled (TARGET policy).**
- Approved report content is immutable, and recovery never re-approves an already approved report.
- A recovered artefact must be **demonstrably derived from the exact approved content and revision**.
- The report, interpretation, revision, approval identity and approval timestamp are distinguished from artefact hashes.
- **A PDF hash alone does not prove correspondence to approved source content.**
- Content verification is **not claimed to be possible** until an authorised, trustworthy verification mechanism exists.
- That mechanism is recorded below as a **PREREQUISITE** for the phases that depend on it. This stage does not change the API.

**CURRENT behaviour (verified).**
- *Identity.* The run's `clinical_link.json` names the organisation, interpretation and report; the manifest's `clinical` block repeats them; `fetch_report_revision_status` refuses a link that disagrees with the server about which report an interpretation belongs to. There is **no revision counter**: "revision" is the retraction/amendment/re-analysis block read from `GET .../revision`.
- *Two different hashes.* The **local** `content_hash` is `report/models.py::compute_content_hash(document)`: SHA-256 over the clinical content only (excluding review status, timestamps and file paths), stored in `geper_results.json` and the manifest and checked with `verify_content_hash`. The **server's** `reports.content_hash` covers the run document **and every reviewer claim**. They are different values; neither can be recomputed from the other.
- *The server hash cannot be checked from a client.* `DataAccess.verify_report_integrity` has **no HTTP route and no production caller** (`clinical/workflow_endpoints.py` lists it as carded) and writes an audit row; `signoff-state` returns only `has_content_hash`.
- *PDFs are not reproducible.* Both PDFs stamp "PDF Rendered" from `datetime.now()` (`report/summary.py:1027`, `report/summary_short.py:441`) and neither renderer sets reportlab's invariant mode. A regenerated PDF never has the hash of the original.
- *Mismatch between the two sides is invisible, in every path.* Nothing today compares the local document to what the server approved. **This is equally true of the normal sign-off path** (`approve` stamps a local document onto a server report on the strength of the link's ids alone). Recovery's stricter requirement below is a **safety constraint** (recovery acts on an uncertain history and must not add to the doubt); it is **not** a claim that normal sign-off is already bound. Whether normal sign-off should also gain binding is **OPEN** (section 11).

**Consequences (TARGET).**
1. **The local hash proves local self-consistency only.** Passing `verify_content_hash` shows the JSON is unchanged since it was hashed; it does not show it is the content the server approved.
2. **A recovered artefact may be produced only when binding to the server's approved content is verified by a PREREQUISITE mechanism.** Until that mechanism exists, any recovery that would *create or finalise* an artefact after an approval whose files were not completed (cases C, D, E) **stops and requires investigation**. An operator's statement ("I confirm this is the document I approved") is recorded for the audit trail if given, but **does not substitute for verification** and does not unlock finalisation. (This replaces the earlier draft's attestation path.)
3. **Once a verified manifest exists, nothing it lists is regenerated or rewritten.** A missing or mismatching file stops recovery. Re-issuing a report is a quality-system matter (OPEN).
4. **Before a manifest exists**, PDFs may be regenerated only from a document whose binding is verified (consequence 2), and the manifest is then built from the new files' hashes.
5. **The journal is separate from the manifest:** never hashed into it, written into it, or consulted to judge its validity.

**PREREQUISITE P-BIND: a verifiable approval-binding mechanism.** It must be (a) *authorised* (role-gated, organisation-scoped, uniform refusals); (b) *trustworthy*, meaning the evidence is **a comparison the server itself performs against authoritative approved content the server holds**: a client's assertion of any kind (a digest it computed, a statement that content matches) may serve only as the *query* and is never the evidence; (c) specific to the exact organisation, interpretation, report and revision; (d) free of claim text and patient data; and (e) auditable.

**Untrusted client assertion versus server-recomputed comparison.** A digest the client computes and sends is an *assertion*: it shows only what the client claims. It becomes evidence only if the server independently recomputes the corresponding digest from the authoritative approved content it holds, in the same canonical form, and compares the two. A digest that the server merely *stores* on the client's word is still the client's assertion.

**Candidate shapes, assessed (none chosen, none built; the evidence available does not allow choosing):**

| Candidate | Assessment against (a) to (e) |
|---|---|
| (i) an authenticated read returning a server-computed fingerprint of the approved content and claims | The value is server-computed, but it is **unresolved as a verification**: the client cannot recompute it (it covers stored claims whose reasons are not exposed, and the stored run document), so there is nothing to compare it with. Not presented as viable on its own. |
| (ii) store, at approval, a digest of the document the client approved, and return it in the sign-off state | The stored digest is **a client assertion** (fails (b)): it could show later *local drift* from what the client said, not that the client's document equals the server's approved content. **Not viable as stated.** It would become viable only if the server also recomputes and compares at storage time, which is candidate (iii) applied at approval. |
| (iii) a server-side verify: the client sends a digest as the *query*; the server recomputes from authoritative approved content and answers match, mismatch or cannot-verify | The only candidate whose evidence is a server-side comparison against authoritative content, so it can meet (b) **in principle**. **Unresolved:** it needs the server to hold approved content in a form that canonicalises identically to the local document, and nothing shows that the stored run document and the local `geper_results.json` are comparable (**OPEN**). Until that is shown it is **not** a viable solution either. |

No candidate is therefore presented as viable today. A mechanism needs its own design, evidence and acceptance criteria (section 9); each shape is a contract and possibly schema change. Runs signed *before* any mechanism exists cannot be bound retroactively and always stop for investigation.

**Blocks:** Phase 5 (any recovery **approval** step, and finalising or creating files after a lost approval; see section 9 and Decision 4). Does **not** block Phases 0 to 4 (journal, classification, and continuing from a recorded claim to submission).

### Decision 2: operator handover and concurrency

**Settled (TARGET policy).**
- V1 supports **same-operator** recovery only. The original operator authenticates again to reconcile their operation.
- A local journal is **not authorisation**.
- Cross-operator takeover requires a **separate, authorised and auditable workflow** (out of scope; OPEN).
- Concurrent recovery attempts require a **defined concurrency mechanism**.
- Server state is reconciled before any write is retried.

**CURRENT server-enforced concurrency control (verified).**

| Concern | Enforced by the server today? | By what |
|---|---|---|
| Two submissions or two approvals | **Yes** | `FOR UPDATE` on the report row in `submit_for_review` and `_approve_report`; the loser re-reads and gets the domain's `ValueError`. Pinned by `test_w175_concurrent_approval.py` on real PostgreSQL. |
| A claim arriving while the report is approved or being approved | **Yes** | `FOR SHARE` plus a refusal of `approved`/`released` reports in `_validate_claim_inputs` |
| Approver, approval time, hash changing after approval | **Yes** | trigger `enforce_content_immutability` |
| **A second `sole_signatory` claim by the same actor on one report** | **No** | `reviewer_claims` has no uniqueness beyond its primary key |
| "A recovery is in progress" excluding another recovery | **No** | no lease, operation id or lock on the server |

**The defined concurrency mechanism (TARGET), in layers:**
- **L1, server (CURRENT):** at most one submission and one approval can succeed, whoever races. This alone makes the *state transitions* safe under competing recoveries, from any machine.
- **L2, local per-run lock (TARGET, advisory):** an exclusive-create lock file (PID, host, start time, operation id) stops two processes on one machine from interleaving journal lines and files. It is not server-enforced and does not protect against a second machine. A stale lock is never broken automatically.
- **L3, claims (PREREQUISITE P-CLAIM):** a server-enforced rule so a claim cannot be duplicated (a uniqueness constraint or an idempotency key on the claim write). **Until P-CLAIM exists, recovery never issues a claim write** (see Decision 4). P-CLAIM is a schema or contract change and is not part of this stage.

**Stale-lock investigation and resolution (INTERIM human procedure, PROPOSED; locks are never broken automatically).**
1. The tool refuses and prints the holder's details: PID, host, user, start time, age and operation id.
2. The operator checks whether that holder is still running on that host. **Lock age alone never justifies release.**
3. The operator runs the read-only `signoff-status` (Phase 3) to see the server state and the journal's last event.
4. Only if the holder is demonstrably gone **and** the state is understood may the operator release the lock, by a deliberate act: either a documented manual rename of the lock file to `.stale-<UTC time>` or a dedicated explicit command (the mechanism is **OPEN**). Either way a local audit entry `lock_released_by_operator` records who, when, the holder's details and the reason, and the renamed lock is kept, not deleted.
5. If the holder may still be running or the state is unclear: `INVESTIGATE`; the lock stays.

**Blocks:** nothing in Phases 0 to 5 as scoped (they never issue a claim). P-CLAIM would be required before any future feature that re-issues a claim, and for safe concurrent recovery of case A across machines.

### Decision 3: the LIMS export gate

**CURRENT behaviour (verified in `report/export_lims.py`, `review/signoff.py` and the tests named). This is a description of the code, not a statement of desired policy.**

| Question | What the code does today | Pinned by |
|---|---|---|
| Is `review_status` required? | Yes: exactly `"reviewed"`, else `LIMSExportBlockedError` (audited) | `test_an_unsigned_linked_run_is_still_refused_for_review` (read); `test_export_lims.py` and `test_review_status_governance.py` are named by `export_lims.py`'s docstring as guarding this, bodies not read |
| Is the local content hash checked? | Yes, **if present**; a mismatch refuses | `tests/test_content_verification.py` |
| Is a **missing** local content hash refused? | **No: it is allowed**, with audit entry `content_not_verified_no_hash_predates_phase_6` (a deliberate legacy path) | `tests/test_content_verification.py` (~494 to 520) |
| Is the **manifest** checked? | **No.** `export_lims.py` has zero references to it. `list_pending` uses the manifest's presence (per its docstring), so the two disagree on a run with a reviewed JSON and no manifest. | none |
| Is any **PDF** checked? | **No** | none |
| Linked run: is the clinical status required? | Yes, fail closed (no status, failed login, unreadable/inconsistent revision, or a different report/organisation each refuse) | `TestLimsExportCannotShipARetractedReportWithoutItsStatus` |
| Is a **retracted** report refused? | **No: it is allowed**, exported with its retraction state in `report_status` (export version 1.1.0) | `test_a_retracted_report_is_exported_with_its_retraction_and_replacement_state` |
| Is the server's sign-off state compared with the local `review_status`? | **No.** `report_state` is carried into the payload and never compared with `approved`/`released`; a local `reviewed` JSON over a server `draft` report is exported (stating `draft`). | none pins either outcome |
| Does the server integrity check run at export? | **No** | none |

**The current gate does NOT enforce the target policy below, and no claim to the contrary is made.**

**Required evidence: three states that must not be conflated.** "Required" means required by the column that applies; evidence marked "not required" in a column is never an export precondition under that column.

| Evidence | CURRENT (the code today) | INTERIM (PROPOSED; not implemented; until WI-GATE and P-BIND are delivered) | TARGET (after WI-GATE; for binding, also after P-BIND) |
|---|---|---|---|
| `review_status == reviewed` | required | required | required |
| clinical service says `approved`/`released` | not compared | checked by the operator before export (procedural) | required (in code) |
| not retracted | not required: exported with a flag | procedural: a retracted report is not exported while the retraction policy is unresolved (OPEN 2) | required: retracted reports are refused |
| local content hash present and verifies | verifies **if present**; absent is allowed (legacy path) | procedural: expected present; legacy runs per the waiver decision (OPEN 2) | required; absent fails closed (waiver path OPEN) |
| manifest present and verifies | not checked | procedural: a reviewed run **without** a manifest is on HOLD (below) | required; absent or unverifiable fails closed |
| **server approval binding (P-BIND)** | **not checked and not required; exports proceed without it** | **not required**: it cannot be verified, so it cannot be a precondition | required **only** for runs signed after the mechanism exists; for earlier runs, whether a waiver applies is OPEN |

**Reading rule.** Until P-BIND exists, server binding is **not** an export precondition anywhere: not in the code, not in the interim policy. Exports of runs that completed a normal sign-off are governed by the CURRENT gate and the interim controls above, and the absence of binding is a documented limitation. It becomes a precondition only for runs signed after a P-BIND mechanism exists, and only once WI-GATE enforces it.

**TARGET policy (not enforced today).** After WI-GATE, an export is eligible only if **all** hold:
1. the clinical service says the report is `approved` or `released` (a local `review_status` alone never decides it);
2. the report is **not retracted**;
3. the required integrity evidence is present and verifies: a stored local content hash that verifies, a manifest that verifies against the results JSON and PDFs, and, for runs signed after a P-BIND mechanism exists, the server-side approval binding;
4. **missing required evidence fails closed**: an absent hash, an absent or unverifiable manifest, a status that cannot be read, or (for post-P-BIND runs) an unverifiable binding each refuse.

**Consequences and conflicts with CURRENT behaviour (stated, not hidden).**
- Items 3 and 4 end the **tested legacy no-hash path**; runs signed before the hash existed would become unexportable without an explicit, audited waiver (**OPEN**: whether a waiver exists and who may grant it).
- Item 2 reverses the **tested** export-with-retraction behaviour and makes the 1.1.0 `report_status` retraction metadata unreachable for a retracted report. Per the project's status, the LIMS receiver's written confirmation of compatibility with 1.1.0 and the retraction metadata has not been obtained; the change must be sequenced with that confirmation (**OPEN**).
- Item 1 adds a comparison that does not exist today.

**Work item WI-GATE (separate implementation change; NOT part of the recovery phases).** Tightening the gate is its own change with dedicated tests, owner sign-off and the lab's quality owner: tests for each refusal (missing hash, failed hash, absent/mismatching manifest, server state not approved/released, retracted report, status unreadable), an explicit diff to the legacy and retracted-export tests where behaviour intentionally changes, and the waiver path if one is wanted. The server-integrity element of item 3 additionally depends on P-BIND. **This stage does not modify export code or its contract.**

**INTERIM operational policy for a reviewed run without its manifest (PROPOSED; the repository implements none of it).**
- *CURRENT, verified:* a results JSON with `review_status: reviewed` and a valid hash but **no manifest passes the current LIMS export gate** (the gate reads neither the manifest nor the PDFs), while `list_pending` calls it DRAFT. **The code does not block export of such a run.**
- *Detect.* A run directory whose `geper_results.json` says `reviewed` and which has no `geper_signoff_manifest.json` is an **incomplete sign-off**. No command finds these today. Phase 3's `signoff-status` is proposed to flag it prominently for one run; a sweep across a directory tree would be a small read-only listing that is **not built** (OPEN 15).
- *Hold.* Such a run is **on HOLD**: by procedure it must not be exported to the LIMS, delivered or released. This is a **procedural control only**; no code enforces it and the export tool will not refuse it.
- *Escalate.* Report it promptly to the platform owner and the lab's quality owner and record it in the quality system. Do not try to "fix" it with `approve` or `withdraw` (the linked-run tools refuse by design) and do not edit files by hand.
- *Resolve.* Only through the investigation path (Decision 4): reconcile the server state, then a documented decision by the owner.
- *Close.* Phase 1 removes the usual cause for new sign-offs (an interruption between the JSON and the manifest) by publishing the JSON last. WI-GATE would make the gate refuse such a run. **Neither exists today.** Runs interrupted under the CURRENT order may still exist and are covered by this procedure.

**Rules binding on recovery regardless of WI-GATE (TARGET):**
- Recovery **never creates a bypass**: it sets or leaves `review_status: reviewed` only when it has itself checked the full target eligibility above that it can check (server state, not retracted, local hash, manifest), and it publishes the results JSON **last**, so no interruption leaves a state the current gate accepts without a manifest.
- A recovery that cannot verify leaves `review_status` as it was; it never "completes" a file on a best guess.
- Recovery is thus at least as strict as the target even while the gate is not. That is a property of recovery; it is not a claim about the gate.

### Decision 4: ambiguous outcomes

**Settled (TARGET policy).**
- **Reconcile before retrying.** A fresh authoritative read always precedes any further write.
- **Never repeat a write merely because its response was lost.**
- **If safe recovery cannot be proven, stop and require investigation.** Stopping is a first-class outcome (`INVESTIGATE`), not a failure to be papered over.
- Authoritative server state, local journal evidence and artefact integrity evidence are kept distinct (Evidence classes, above).

**When a further write is provably safe (TARGET).**

| Step | Re-issue after reconciliation? | Why |
|---|---|---|
| Claim | **Never by recovery.** Recovery may only *continue from* a claim it can attribute (new sole-signatory claim by this actor, absent from the journal's baseline). With none attributable the outcome is `INVESTIGATE`. | The write is not idempotent and a late commit cannot be excluded; no server rule prevents a duplicate (P-CLAIM). |
| Submission | Only after a fresh read shows `draft` with the attributed claim, and the operator confirms | State-guarded: at most one transition can succeed; a late commit just makes the second attempt a refusal |
| Approval | **Only when** a fresh read shows `under_review`, the operator is the same user and confirms, **and** a P-BIND check is available and passes so that file finalisation can follow. **Otherwise recovery stops at `under_review`.** | State-guarded (at most one approval); and see the ruling below on why an approval that cannot be followed by safe finalisation is not performed. It is a *new* approval of an unapproved report, never a re-approval. |
| File finalisation | Only under Decision 1 (P-BIND) | content must be demonstrably the approved content |

**Ruling: recovery approval versus P-BIND (settled, conservative).** Recovery does **not** perform the approval (`under_review` to `approved`) while P-BIND is unavailable. The approval would have to be followed by file finalisation, which cannot safely proceed without P-BIND, so recovery would leave a server-approved report with no signed files and no safe way to finish it. *Exact safe stopping point:* the report is `under_review`, the claim and submission are recorded, **nothing is approved and nothing is written locally**, and the tool reports `INVESTIGATE` naming that state. *Rationale:* `under_review` can be completed later by a properly equipped recovery or by a documented manual decision, whereas `approved` is irreversible (the immutability trigger). This rule does not claim P-BIND exists.

**Re-read before approving, and the compare-and-set question.** Immediately before any recovery approval (once permitted) recovery re-reads `signoff-state` and aborts if the state or the claim set differs from what the operator was shown. **CURRENT: no compare-and-set protection exists**: the approval endpoint takes no body or precondition and `signoff-state` carries no version token, so a claim recorded between that re-read and the approval would still be sealed by it. That residual race is recorded as **OPEN** (P-CAS, section 9); no protection is claimed.

**Pre-journal runs (PROPOSED handling).** A run interrupted before a journal existed (or whose journal is missing or corrupt) has no baseline, so no claim is attributable. For such runs recovery performs **no write of any kind, in any phase**. The read-only `signoff-status` (Phase 3) still works from the server state and the local files alone and says "no journal: classification limited"; it flags a reviewed JSON with no manifest. Every such case ends in `INVESTIGATE` with the interim HOLD policy (Decision 3).

`INVESTIGATE` output: the tool writes nothing, lists the three evidence classes it saw, states exactly which fact it could not prove, and appends a local audit entry. A person decides the next action. Today the only available decisions are manual and outside the tool (see `SIGNOFF_API_DESIGN.md`, "Unknown client outcome is not a failed operation").

### Decision 5 (PROPOSED): completing a partially signed report, and the aggregate acceptance verdict

**Status: PROPOSED, not settled, not built.** It relaxes none of Decisions 1 to 4: completion never approves (Decision 4 ruling), never repeats a write, never re-approves an approved report (Decision 1), is same-operator (Decision 2), and creates or finalises an artefact only when binding is verified (Decision 1, consequence 2). **Until a P-BIND mechanism exists, check C3 below can only be UNKNOWN, so every completion ends in `INVESTIGATE`.** That is the intended consequence, not a gap.

**What the r1a2 experiment adds (CONFIRMED (r1a2), section 13):** a sign-off can be left in the state *server approved, results JSON `reviewed`, PDFs and manifest absent*; a file/claim divergence can be sealed into an approved report; and a projection MATCH says nothing about either. The proposal below turns those observations into rules; the rules themselves are **PROPOSED**.

**Proposed rules.**
1. **Approval is final.** A partially signed report is completed by a separate operation that never calls `approve()`, never changes server state, and never resets an approved report to draft.
2. **Aggregate verdict.** A run is declared signed off only when every check below passes, each evaluated independently and reported separately. A check that is UNKNOWN counts as not passed.

| Check | Evidence | Passes when |
|---|---|---|
| C1 Approval | server state and revision read | `approved` or `released`; approver is the expected user and still holds the Approver role; not retracted; revision status reads and validates |
| C2 Server hash | versioned recomputation (Decision 6) | the recomputed hash equals the stored `content_hash` |
| C3 Binding | server-side comparison (P-BIND candidate (iii)) of the strict projection | MATCH under the strict experimental policy (section 13, fact 5). **Cannot pass until P-BIND exists** |
| C4 Claims | file override records against the server's `disagree` claims, as multisets and by claim id | equal. Any extra or missing claim is a divergence |
| C5 Artefacts | both PDFs and the manifest | all present; the manifest's SHA-256 of both PDFs and the results JSON match the files; its `clinical` block matches the server |
| C6 File state | results JSON | `review_status: reviewed` and `reviewed_by` consistent with the approver's recorded identity |
| C7 Audit | local audit log | an `approved` line exists, or a reconstructed one marked `reconstructed: true` (never backdated) |

3. **Verdict values.** `COMPLETE` (all pass). `RECOVERABLE` (C1 to C4 and C6 pass; only C5 and/or C7 are missing, and no valid manifest exists). `INVESTIGATE` (anything UNKNOWN, divergent or unverifiable). `INTEGRITY_FAILURE` (C2 fails, or a valid manifest exists and a listed file is missing or changed). Only `RECOVERABLE` permits any write, and only of local artefacts.
4. **A projection MATCH is C3 only.** No report, summary line or exit code may present it as sign-off verification. The first line of any output is the aggregate verdict, followed by each check.
5. **S5 (claim and override divergence) always ends in `INVESTIGATE` and needs human review.** The file is never silently synchronised to the server claim, and the claim is never edited or added to. After approval the claim set is sealed into the hash, so the divergence is permanent evidence. The amendment path is not available from the sign-off tool today.
6. **Authorization (PROPOSED, with an OPEN part).** Only the user who approved, authenticated as themselves and still holding the Approver role, may run completion (Decision 2, no takeover in V1). Whether a quality owner may complete on another person's behalf is **OPEN** (item 20).
7. **What completion may write (PROPOSED).** Staged PDFs, then the manifest built from those files' hashes, then local audit lines; published in the section 5 order with the results JSON last. It never overwrites a verified manifest and never rewrites any existing artefact (Decision 1, consequence 3). Where the results JSON already says `reviewed` (the CURRENT order produces that state, as in r1a2), completion leaves it untouched; only a run whose JSON is not yet `reviewed` (the section 5 order) has it published last.
8. **Idempotency and concurrency (PROPOSED).** Re-running on a `COMPLETE` run is a no-op that returns the verdict. A crash leaves only staged files, and a re-run resumes from a fresh verdict. A per-run advisory lock plus atomic publication prevent two local writers (the server needs no new lock because completion writes nothing there).
9. **The defect that caused r1a2's PDF failures must be fixed before completion can succeed for such runs** (section 13, fact 2). Otherwise completion would fail at the same step and end in `INVESTIGATE`.

### Decision 6 (PROPOSED): versioned, time-zone-independent approval hash

**Status: PROPOSED, not settled, not built.** It does not change any stored hash and proposes no migration.

**CONFIRMED (r1a2), see section 13, fact 4:** the server approval hash serialises each claim timestamp with `datetime.isoformat()`, in the zone of the connection that computes it. A hash computed in a UTC session did not reproduce under three other session zones, for all 13 reports, although nothing stored had changed.

**Proposal.**
1. **Version 2** serialises each claim timestamp as `ts.astimezone(timezone.utc).isoformat()`. For a hash computed in a UTC session this is byte-identical to today's output, so every hash computed in a UTC session remains verifiable without change.
2. Every clinical connection pins its session `TimeZone` to UTC (defence in depth).
3. The algorithm version is recorded with the hash. A hash with no recorded version is a **legacy v1** hash.
4. **Legacy verification:** recompute with the UTC form. If it matches, it verifies. If it does not, the result is `UNVERIFIABLE_LEGACY`, meaning the approval may have been computed in another zone. It is **never** reported as tampering and maps to `INVESTIGATE`.
5. No retroactive migration and no rewriting of stored hashes (they are write-once at the database level in any case).
6. **OPEN (item 19):** the session zone production approvals used is not known. Until it is, "all existing hashes are UTC-computed" cannot be claimed.

## 1. What a sign-off is today - CURRENT (verified by reading the code)

> **Update: crash-safe local publication is IMPLEMENTED (CURRENT).** The file steps F1 to F5 below describe the order
> that existed when this section was written. `review/signoff.py::approve` no longer writes them in that order: see
> section 1A. Steps S1 to S3 (the three server writes) and facts 1, 3 to 7 and 9 to 10 are unchanged. Fact 2 (the
> results JSON written before the PDFs and the manifest) no longer holds for runs approved with this change; runs
> approved before it may still be in that state (interim HOLD, Decision 3).

A linked run's sign-off is **eight separate durable steps**, in this order. No two share a transaction or an atomic filesystem operation.

| # | Step | Where | Durable boundary |
|---|---|---|---|
| S1 | record the `sole_signatory` claim | `POST .../concurrence` | its own commit; the success audit row (`record_sole_signatory`) is a **second** commit after it |
| S2 | submit for review (`draft` to `under_review`) | `POST .../submission` | own commit, audit row after |
| S3 | approve (`under_review` to `approved`; sets approver, time, hash) | `POST .../approval` | own commit, audit row after |
| F1 | rewrite `geper_patient_meta.json` (physician line) | `signoff.py::approve` | `open(path, "w")`: **not atomic** |
| F2 | rewrite `geper_results.json` with `review_status: reviewed`, `reviewed_by`, `reviewed_at`, `content_hash` | `_write_document` | `open(path, "w")`: **not atomic** |
| F3 | render the full and short PDFs from the in-memory document | `generate_pdf`, `generate_short_pdf` | files appear as they are written |
| F4 | write `geper_signoff_manifest.json` (SHA-256 of both PDFs and the results JSON, identity, `approved_at`, `clinical` ids) | `signoff.py::approve` | `open(path, "w")`: **not atomic** |
| F5 | append `approved` to `geper_signoff_audit.log` | `_append_audit_log` | append, **no fsync** |

Facts that shape the design:

1. **The local record of an attempt starts at F5.** `approve` writes no local entry when it starts, fails, or gets an unknown outcome; an interrupted attempt leaves **no local trace**.
2. **The results JSON is written (F2) before the PDFs and the manifest (F3, F4).** The current gate (Decision 3) checks `review_status` and the local hash only, so a run interrupted between F2 and F4 **already passes the export's review gate** with no manifest and possibly no or partial PDFs, while `list_pending` calls it DRAFT.
3. **`approved_at` in the JSON, manifest and local log is the client's clock**, not the server's `reports.approved_at`. The manifest's `clinical` block carries ids only; neither the server's approval time nor its hash.
4. **Two different hashes exist** (Decision 1).
5. **Server immutability:** approver, time and hash are write-once; an approved report cannot be walked back; `reviewer_claims` are append-only with no per-actor uniqueness.
6. **Server concurrency:** see Decision 2.
7. **Client behaviour:** `approve` refuses a report that is not a `draft` before writing anything, and the HTTP adapter never retries a write (`ClinicalWriteOutcomeUnknown` on a timeout, drop, 5xx or unexpected success). An interrupted sign-off **cannot currently be resumed by the tool**.
8. **Precedent for safe publishing:** `refresh_revision` stages files privately and publishes with `os.replace`, the three renderings first and the status record (which lists their hashes) last. Each `os.replace` is atomic for one file; the set is not published atomically.
9. **Callers:** only `review/cli.py` and `report/export_lims.py` use `review/signoff.py`.
10. **Server audit:** `audit_log` is append-only; a success row is written *after* the data commit, so a failure there leaves committed data with an `error`-outcome row (or none).

## 1A. Crash-safe local publication - CURRENT (implemented; tested without a database)

**What changed.** After the claim, the submission and the approval, and after the clinical session is closed, `approve`
publishes its five local files through `_publish_signoff_files` in `geper/review/signoff.py`:

1. a private staging directory is created **inside the run directory** (`.signoff-stage-<random>`), so it is on the same
   filesystem as the destination;
2. patient meta, the reviewed results JSON, the full PDF, the short PDF and the manifest are built there, under names that
   can never be taken for a report run (`*.staged`; never `geper_results.json`, so `list_pending` cannot see a leftover
   staging directory as a second run). The PDFs are rendered from the staged patient meta. The manifest is built from the
   SHA-256 of the **staged bytes**. Every staged file is flushed and `fsync`ed;
3. publication with `os.replace`, in this order: `geper_patient_meta.json`, the full PDF, the short PDF,
   `geper_signoff_manifest.json`, then **`geper_results.json` last**. On POSIX the directory is `fsync`ed before the JSON
   is published (so its rename cannot become durable ahead of the manifest's) and again after;
4. only then the `approved` audit line is appended and `fsync`ed;
5. the staging directory **this call created** is removed. A staging directory left by an earlier, killed process is
   never touched. A failure to remove it is logged as a warning and never masks the result.

**Failure behaviour.** Any failure raises `PublicationError` (a `SignoffError`) with the original exception as its
`__cause__` and `published`, the files already renamed into place. On a linked run its message says that server approval
may already have succeeded. Nothing is rolled back (a rollback could overwrite a newer or concurrent report) and nothing
is repaired automatically.

| Interrupted | On disk | `require_reviewed(…, output_dir)` | `list_pending` | `status` (server approved) |
|---|---|---|---|---|
| while staging or rendering | destination unchanged; a staging directory remains if the process was killed | refuses (JSON not `reviewed`) | DRAFT | `APPROVED_NO_LOCAL_SIGNOFF` (INVESTIGATE) |
| after meta / full PDF / short PDF | those new files; JSON still the draft | refuses | DRAFT | same |
| after the manifest, before the JSON | manifest whose results hash does not match the draft JSON | refuses | **DRAFT** (the manifest does not verify; the earlier disagreement with the gate is closed) | same |
| after the JSON, before the audit line | complete | accepts | REVIEWED | `SIGNED_OFF_AUDIT_ENTRY_MISSING` (INCOMPLETE) |
| after the audit line | complete | accepts | REVIEWED | CONSISTENT |

**What "complete" means now.** `list_pending` reports REVIEWED only if the manifest exists **and verifies**
(`_publication_problems`: it names SHA-256 values for both PDFs and the results JSON, each matches the file on disk, so a
missing, truncated or replaced file is a problem, and its content hash agrees with the results JSON) **and** the results
JSON says `reviewed`. `manifest_problems` on each entry says why a manifest that exists did not verify.
`require_reviewed(document, consumer, output_dir)` applies the same check when it is given the directory and refuses
before it writes any audit line. This is evidence of internal consistency only; **content binding is still not verified**.

**Limitations (stated, not hidden).**
- **Not atomic across the file set.** Each `os.replace` is atomic for one file only. The order makes every interrupted
  state one that the status paths refuse, or a complete sign-off missing only its audit line.
- **Durability depends on the platform.** Windows cannot `fsync` a directory (this step is skipped there), and a filesystem
  that refuses it is tolerated with a warning. Network and bind-mounted filesystems may reorder or lose renames. Power loss
  is not covered by the process-kill tests.
- **Server approval comes first.** If local publication fails, the clinical service may already say `approved`; nothing
  here resumes or repairs that (that is the later recovery work).
- **`require_reviewed` only checks completeness for a caller that gives it the directory.** `report/export_lims.py`
  passes none, so the LIMS export gate still checks only `review_status` and the content hash; wiring the run directory
  through is a separate, deliberate change. A reviewed JSON with no manifest, which the old order could leave behind, is
  refused by the directory-aware gate and is reported by `status`.
- **Runs approved before this change** keep whatever state they have. A manifest that lacks the three SHA-256 keys (all
  three have been written since the first sign-off command) is reported as not verifying, which fails closed.
- **`override` and `withdraw`** still write the results JSON, remove the manifest and re-render PDFs directly; they are not
  covered here.

## 2. Safety rules - TARGET (the contract every phase must meet)

1. **A lost response is never, by itself, a reason to repeat a write** (Decision 4).
2. **The server is the source of truth; the journal is a hint and never an authorisation** (Decision 2).
3. **Never alter an approved report**, never re-approve one, and never rewrite or regenerate any file once a verified manifest exists (Decision 1).
4. **Never bypass authentication, role checks or organisation isolation.** Recovery uses the same authenticated API calls as `approve`, under the operator's own session; it adds no privileged path and no server write of its own.
5. **No takeover in V1.** Only the same authenticated user and organisation as the journal may resume (Decision 2).
6. **Use the server's approval time and approver** wherever recovery writes a file that states them.
7. **Nothing is silently deleted, rewritten or tidied.** A corrupt journal is quarantined (renamed). A reconstructed local audit entry says it is a reconstruction and is never backdated.
8. **Recovery never issues a claim write** (Decision 4; P-CLAIM).
9. **Uncertain is a state, not a guess:** unknown outcomes are reported as unknown, and unprovable safety ends in `INVESTIGATE`.
10. **Stop when anything disagrees** (journal vs server, hash vs file, organisation). Conflicts write nothing and produce a local audit entry.
11. **No export-gate bypass** (Decision 3): recovery sets `review_status: reviewed` only after the checks it can make have passed, and publishes the results JSON last.
12. **Content must be demonstrably the approved content** before any artefact is created or finalised (Decision 1; P-BIND).
13. **Concurrency control** at three layers: the server (CURRENT, with the claim gap), a local per-run lock (TARGET, advisory), and atomic file publication (TARGET).
14. **Re-read before approving** (TARGET): immediately before any recovery approval, re-read the sign-off state and abort on any difference from what the operator was shown. No compare-and-set protection is claimed to exist (Decision 4).
15. **Locks are released only by a person**, by the audited procedure in Decision 2.

## 3. Failure-state matrix - TARGET behaviour (cases A to I kept; changes from the earlier draft follow Decisions 1, 2 and 4)

Notation: *server* = committed clinical state; *local* = what may be in the run directory; "journal" is the TARGET file in section 5 (today there is none, so "determinable" is stated for both).

### A. Claim committed, response lost
- **Server:** `draft`; one more `sole_signatory` claim by the operator than before (its success audit row may be missing or `error`).
- **Local:** nothing changed. Today: no trace. With the journal: `claim: in_flight` and the claim ids that existed *before* the write.
- **Determinable:** the new claim id is attributable by set difference against the journal's baseline. Without a baseline it is **not** attributable.
- **Auto-retry safe?** Retrying the claim: **never**. Continuing to submission from an attributed claim: yes.
- **Human?** If no claim is attributable: yes, `INVESTIGATE` (the write may still be in flight, may have failed, or an earlier attempt's claim may be the one visible). Recovery does not re-issue it.
- **Audit events:** local `recovery_started`, `claim_attributed` (claim id) or `claim_not_attributable`; server rows already exist.
- **Expected outcome:** resume at submission; or `INVESTIGATE`. Any later claim comes from a fresh, deliberate sign-off by the operator, not from recovery.

### B. Submission committed, response lost
- **Server:** `under_review` (the claim stays); or still `draft` if it did not commit.
- **Local:** journal `submit: in_flight`.
- **Determinable:** the state itself. Who submitted and when is **not** exposed (gap, section 6).
- **Auto-retry safe?** After a fresh read: `draft` with the attributed claim, and the operator confirms, then submitting is safe (state-guarded). `under_review`: do not repeat; continue to approval.
- **Human?** Confirmation only, when journal and server agree and the actor is the same user.
- **Audit events:** `submit_confirmed_from_state` or `submit_reissued_after_verified_draft`.
- **Expected outcome:** continue to the verified `under_review` state and then **stop** (Decision 4 ruling): recovery does not approve while P-BIND is unavailable. The tool states the safe stopping point reached.

### C. Approval committed, response lost
- **Server:** `approved`, approver, time and hash set.
- **Local:** no files changed (the file phase never began). Journal `approve: in_flight`.
- **Determinable (authoritative):** `state == approved`, `approver_id == journal actor`, `approved_at` after the journal's start (allowing for skew), `has_content_hash == true`, identity consistent across link, journal and server, revision status reads and validates. **Not determinable:** that the local document is the content the server approved (Decision 1).
- **Auto-retry safe?** Never repeat the approval (it is refused, and recovery never re-approves).
- **Human?** Yes. Without P-BIND the outcome is `INVESTIGATE`: the server approval is recorded as observed, the files are **not** finalised, and `review_status` is left as it was.
- **Audit events:** `approval_observed` (server time, approver id), `recovery_blocked_binding_unverifiable`.
- **Expected outcome:** `INVESTIGATE` now; after P-BIND exists and verification passes, finalise the files using the server's approval time and the approver's own identity, if and only if the operator is that approver.

### D. Approval succeeded but PDF generation failed
- **Server:** `approved`.
- **Local:** under the CURRENT order, `patient_meta.json` and the results JSON may already say `reviewed` with the **client** time (under the section 5 order the JSON is not yet `reviewed` at this point); PDFs missing, truncated or only one present; **no manifest**. The current gate would already accept this run (section 1, fact 2).
- **Determinable:** the document's own `content_hash` verifies (self-consistency only). Binding to the approved content is not provable without P-BIND.
- **Auto-retry safe?** Re-rendering has no server effect and, with no manifest, would not break a hash; but under Decision 1 it is allowed **only after** binding is verified. Without P-BIND: `INVESTIGATE`.
- **Human?** Yes (until P-BIND).
- **Audit events:** `recovery_blocked_binding_unverifiable`; later, with P-BIND: `finalise_started`, `pdf_regenerated`, `finalise_completed`.
- **Expected outcome:** `INVESTIGATE` now. Phase 1 reduces how often D occurs by making the file phase crash-safe. While the manifest is absent the run is subject to the **interim HOLD** (Decision 3). After P-BIND: staged PDFs and manifest published, results JSON last (section 5 order).

### E. PDFs created, manifest generation failed
- **Server:** `approved`.
- **Local:** both PDFs exist; the manifest is missing or truncated (written non-atomically). Under the CURRENT order the results JSON already says `reviewed`; under the section 5 order it does not yet.
- **Determinable:** the PDFs' hashes match the journal's record only if a journal was kept; a match shows the PDFs are the files written, **not** that their content is the approved content. A truncated manifest is treated as absent.
- **Auto-retry safe?** Building a manifest over files whose binding is unverified would create a signed-looking record for unproven content, so under Decision 1 it needs P-BIND. Without it: `INVESTIGATE`.
- **Human?** Yes (until P-BIND).
- **Audit events:** `recovery_blocked_binding_unverifiable`; later `manifest_rebuilt_from_journal` with the file hashes.
- **Expected outcome:** `INVESTIGATE` now; the run is subject to the **interim HOLD** (Decision 3) if its results JSON already says `reviewed`. After P-BIND: manifest published before the results JSON, the results JSON last (section 5). **If a valid manifest already exists and a listed file is missing or mismatches, recovery stops** regardless of P-BIND (it never regenerates).

### F. Final files exist but the local audit entry is missing
- **Server:** `approved`.
- **Local:** results JSON, PDFs and a valid manifest present and consistent; no `approved` line (or a torn last line).
- **Determinable:** the manifest verifies against the files (artefact integrity) and its `clinical` block matches the server. This writes no artefact and asserts nothing about content binding.
- **Auto-retry safe?** Yes: only an append.
- **Human?** No.
- **Audit events:** an `approved` entry with `reconstructed: true`, `binding_verified: false` (until P-BIND), the journal operation id, and the time of reconstruction (never backdated).
- **Expected outcome:** journal marked complete.

### G. Two processes attempt recovery concurrently
- **Server:** transitions are serialised by the row locks (L1); claims are not serialised (no recovery issues one).
- **Local:** two writers could interleave journal lines and partial files.
- **Determinable:** the local lock file (L2) shows another owner.
- **Auto-retry safe?** The second process does not proceed and never breaks a lock itself.
- **Human?** Only for a stale lock, by the human procedure in Decision 2 (never automatic).
- **Audit events:** `recovery_refused_locked` (holder's details).
- **Expected outcome:** exactly one process completes. L2 is advisory and per-machine: a second process on **another machine** is not stopped by it. The server's locks still make submission and approval safe, and since recovery never issues a claim, no duplicate claim can come from recovery.

### H. A different operator attempts to resume
- **Server:** whatever the original operator left.
- **Local:** a journal bound to user A and organisation O.
- **Determinable:** the authenticated `user_id` and organisation (`GET /auth/me`) differ from the journal's.
- **Auto-retry safe?** Not applicable.
- **Human?** Yes: refused (Decision 2). A signed report prints the **approver's** identity and the signing-identity endpoint is self-only, so another operator could not finalise an approved report faithfully either.
- **Audit events:** `recovery_refused_other_operator` naming both user ids (never credentials).
- **Expected outcome:** nothing changes; the message says whom to ask. A takeover needs its own authorised, audited workflow (OPEN).

### I. Server state conflicts with the local journal
Always: no writes, no file changes, journal untouched, `recovery_conflict` appended locally, operator informed (`INVESTIGATE`).

| Conflict | Meaning |
|---|---|
| journal says approve in flight, server `draft` with no new claim | the earlier steps did not commit, or the journal is for another report |
| server `approved` by a different user | someone else completed it; this operator must not finalise it |
| server `released` or `is_retracted` | the report moved on; finalising a stale document would be wrong |
| local `review_status` is `reviewed` but the server is not `approved`/`released` | the local file disagrees with the authority; do not "complete" it |
| local `review_status` is `reviewed` and the manifest is absent | an incomplete sign-off: **HOLD** under the interim policy (Decision 3); recovery does not complete it without P-BIND |
| claim count below the journal baseline | impossible for an append-only table: wrong organisation/report or a restored backup |
| server organisation, interpretation or report differs from the link | wrong target |
| local document's `content_hash` does not verify | content mismatch: stop |
| journal chain broken (not just a torn tail) | local corruption or tampering |
| P-BIND mechanism unavailable or answers "cannot verify" | not a conflict but an unprovable fact: `INVESTIGATE`, never "assume binding" |

### J. Reviewer claims and local override records disagree (PROPOSED addition; observed as S5 in r1a2)
- **Server:** the report carries a `disagree` claim (and, after approval, the `sole_signatory` claim) for a variant; once `approved` the whole claim set is sealed into the hash.
- **Local:** the results JSON has no corresponding override record (the file write failed after the server claim committed), or the reverse.
- **Determinable (CONFIRMED (r1a2)):** a reconciliation of file overrides against server claims (variant key, classification, count, claim ids) shows the divergence. The projection does **not**: it removes overrides from both sides, so it returns MATCH. The sign-off state API today does not expose variant keys or classifications (open item 21).
- **Auto-retry safe?** No. Recovery never issues a claim write (Decision 4), and it never edits an approved report.
- **Human?** Always. Verdict `INVESTIGATE`.
- **Audit events:** `recovery_conflict` locally, naming the claim ids and the variant keys but not the reason text.
- **Expected outcome:** nothing is written or synchronised. After approval the divergence is permanent and a person decides what to do, noting that the amendment path is **not available** from the sign-off tool today. Before approval, the same state should be caught by the journal (intent recorded before the claim, implementation step 6, section 14.5).

### K. Approval-hash verification fails in a non-UTC session (PROPOSED addition; observed in r1a2)
- **Server:** `approved`, hash stored.
- **Local:** not involved.
- **Determinable (CONFIRMED (r1a2)):** recomputation in a UTC session reproduced the stored hash; the same recomputation in three other zones did not, for all 13 reports, with the stored claims and document unchanged. This is a serialisation difference, not a content change (derived from the two facts, not from a fresh query of the claims).
- **Auto-retry safe?** Verification is read-only and may be repeated.
- **Human?** Only if the UTC form also fails.
- **Audit events:** the verification read is audited like `verify_report_integrity`.
- **Expected outcome:** verify with the Decision 6 form. A match is C2 passed. A failure of the UTC form for a legacy hash is `UNVERIFIABLE_LEGACY` and ends in `INVESTIGATE`; a failure for a hash recorded as v2 is `INTEGRITY_FAILURE`.

### Amendments to cases D and E (PROPOSED)
Cases D and E are unchanged. The r1a2 experiment **observed case D** (section 13, fact 2) and adds one requirement: finalising artefacts for such a run needs the rendering defect fixed first, or the same step fails again. Under Decision 5 the outcome stays `INVESTIGATE` until C3 can pass.

## 4. What recovery can see, and what the current state API gives - CURRENT (verified against `SignoffState`)

| Needed for | Provided today? |
|---|---|
| report state; retraction flag | yes (`state`, `is_retracted`) |
| approver id and the server's approval time | yes |
| that an approval hash exists | yes (`has_content_hash`) |
| the claims on the report, with actor, type, time | yes |
| authenticated identity (to bind to the journal) | yes, `GET /auth/me` |
| exact report/interpretation/organisation identity | yes |
| **the approval hash value, or any way to verify content binding** | **no** (**PREREQUISITE P-BIND**) |
| **a server rule preventing a duplicate claim** | **no** (**PREREQUISITE P-CLAIM**) |
| who submitted and when | no |
| a claim's reason, or a digest of it | no |
| an operation id on a write | no |
| a row version or token for compare-and-set | no |
| another approver's printed identity | no (self-only) |
| the server's clock | only the HTTP `Date` header |

**Sufficient for:** classification (Phase 3), journal-assisted continuation from a recorded claim to submission (Phase 4), and local audit reconstruction (case F). **Not sufficient for:** proving that any recovered or finalised artefact corresponds to the approved content, nor for a safe recovery *approval* (its follow-on finalisation needs P-BIND), nor for detecting a claim recorded between a re-read and an approval (no version token). That is P-BIND and P-CAS, and until they exist the tool says so rather than guessing. This stage does not change the API.

## 5. The recovery journal - TARGET

**File:** `geper_signoff_journal.jsonl` in the run directory, next to `clinical_link.json`. One per run. **Separate from the signed set:** not hashed into the manifest, not written into it, never consulted to judge its validity. Kept after completion, not deleted.

**Format:** append-only JSON Lines; each line has `seq`, `ts` (client UTC), `event`, `data`, `prev` (SHA-256 of the previous line's bytes) and `sha256`. A header carries the schema version, a random `operation_id`, `org_id`, `report_id`, `interpretation_id`, the acting `user_id`, the tool version, and the **baseline**: the ids of all claims then on the report.

**Minimum data stored:** step names and outcomes (`in_flight`, `confirmed`, `unknown`), server-returned claim ids, the server's `approved_at` and `approver_id` once observed, SHA-256 of each artefact as written, a SHA-256 of the claim reason (not the text), and any operator confirmations. **Never stored:** passwords, cookies, session ids, TOTP codes, claim reason text, patient data, names or registration numbers, report content, any server hash.

**Permissions:** mode `0600` on POSIX. On Windows the file inherits the directory ACL; the design states that limitation rather than claiming protection.

**Atomicity and crash consistency:**
- Journal: each event is one `O_APPEND` write of a single line then `fsync`; first creation also `fsync`s the directory. Intent is written **before** the action, the result **after**.
- Artefacts: staged privately (the `refresh_revision` pattern), `fsync`ed, hashed into the journal and published with `os.replace`, in **one order, used throughout this document**: (1) `geper_patient_meta.json` (an input to the PDFs; the export gate does not read it); (2) the full and short PDFs; (3) `geper_signoff_manifest.json`; (4) **`geper_results.json` last**, which flips `review_status` to `reviewed` and is the commit marker for the current export gate; (5) the local `approved` audit entry. The manifest records the results JSON's SHA-256, which is known before publication because the JSON bytes are fixed in staging. **This is not atomic multi-file publication.** Each `os.replace` is atomic for one file only and an interruption can fall between any two. What the order provides is that every interruption point leaves the run in a state the current gate refuses, or a complete run missing only its audit entry; it does not make the set appear as one unit. Phase 1 proves this with crash-point tests.
- A torn final line is ignored as incomplete. A break anywhere else halts recovery; the file is renamed `…journal.corrupt-<ts>` and kept; nothing is repaired.

**Interruption points under the section 5 order (TARGET; assumes the JSON's `review_status` was not already `reviewed` before this sign-off).**

| Interrupted after | Files in the run directory | Current export gate (CURRENT behaviour) | `list_pending` (CURRENT, per its docstring) |
|---|---|---|---|
| staging only | previous files, unchanged | refuses (status not `reviewed`) | DRAFT |
| patient meta published | physician line changed; the rest old | refuses | DRAFT |
| PDFs published | new PDFs; no manifest; JSON old | refuses | DRAFT |
| manifest published, JSON not yet | manifest whose results hash does not match the current JSON | refuses (status still not `reviewed`) | **REVIEWED (manifest present): disagrees with the gate**; Phase 1 either makes `list_pending` verify the manifest against the JSON or the owner accepts this documented disagreement (OPEN 14) |
| JSON published, audit entry not yet | complete | accepts (correctly) | REVIEWED; this is case F |

Under this order a reviewed JSON with no manifest can no longer arise from an interruption of the new flow. Runs interrupted under the CURRENT order may exist and are covered by the interim policy (Decision 3).

**Tamper protection (stated honestly):** the chain detects accidental corruption and casual edits. A local attacker who can write the file can recompute it, so the journal is **never an authority** (Decision 2): it can only cause a re-read of the server, never a skipped check. Real tamper-resistance needs an anchor outside the run directory (OPEN).

## 6. Gaps in the current state API - CURRENT (not addressed in this stage)

In order of consequence: the approval-binding mechanism (**P-BIND**); a duplicate-claim rule or operation id (**P-CLAIM**); the submitter and time; a claim-reason digest; a row version for compare-and-set approval; an admin-gated read of another approver's printed identity (handover). None is made here.

## 7. Recovery procedure - TARGET

1. Acquire the per-run lock (exclusive create); fail if held.
2. Load the journal. With none (a run interrupted before journals existed, or a missing or corrupt journal) perform **no recovery write of any kind**; offer read-only `status` only (Decision 4, pre-journal runs).
3. Authenticate over HTTPS; compare `user_id` and organisation to the journal (H).
4. Read `signoff-state` and the revision; check identity and the local document hash; classify against the journal (section 3); on any conflict or unprovable fact, `INVESTIGATE` (I, Decision 4).
5. Print the classification, the evidence by class, and the proposed next step; require explicit confirmation for any write.
6. Perform exactly one step, never a claim, and never an approval unless the Decision 4 conditions hold. **Immediately before any approval re-read `signoff-state` and abort if the state or claims differ from what the operator was shown** (no compare-and-set protection exists; Decision 4). Journal the step; re-read; repeat. Never catch up several steps blind.
7. File finalisation only if no manifest exists **and** P-BIND verification has passed: stage; use the server's `approved_at` and the approver's own identity; publish in the section 5 order (patient meta, PDFs, manifest, **results JSON last**); append the local `approved` entry; mark the journal complete; release the lock.

## 8. Existing tests that bound this work - CURRENT

Server: `test_w168_review_workflow.py`, `test_w175_concurrent_approval.py` (real-thread races, `clinical_app` role), `test_w176_signoff_state.py`, `test_w174_human_tool_read_api.py`, `test_phase_7_commit1_hash_locking.py` and `test_r9_amendment_defects.py` (hash and claims), `test_w122_retraction.py` and `test_w118_report_revision_reader.py`, `test_w117_sole_signatory.py`. Client: `geper/api/tests/test_signoff_over_http.py`, `test_signoff_clinical_link.py`, `test_clinical_http_adapter.py` (including the state stand-in and validation tests), `test_report_revision_refresh.py` (staging precedent and the linked-export gate), and `geper/tests/test_signoff.py`, `test_r10_signoff_identity.py`, `test_export_lims.py`, `test_content_verification.py` (including the legacy no-hash path), `test_review_status_governance.py`.

## 9. Phases (the six phases and Phase 0 are kept; dependencies and acceptance updated)

Each phase is small, independently reviewable and leaves the system working. Failure injection means deterministic faults at named points. **Prerequisites and the separate gate work are not phases of this list**; they are named so blockers are explicit:

| Item | What | Status |
|---|---|---|
| **P-BIND** | verifiable approval-binding mechanism (Decision 1); **no candidate is currently viable** | PREREQUISITE, not designed, not built; its acceptance criteria are defined in its own design work, not here |
| **P-CLAIM** | server rule against duplicate claims (Decision 2) | PREREQUISITE for any future claim re-issue; not needed by Phases 0 to 5 as scoped |
| **P-CAS** | compare-and-set or equivalent protection against a claim recorded between a re-read and an approval (Decision 4) | OPEN whether wanted; does not exist; no protection is claimed |
| **WI-GATE** | export-gate hardening to the target policy (Decision 3) | separate implementation change with dedicated tests; its acceptance criteria are defined in its own design work, not here; not a recovery phase |

**Recommended execution order:** 0, 1, 2, 3, 4, then P-BIND (with its own design and acceptance criteria), then 5. WI-GATE, P-CAS, P-CLAIM and the Phase 6 items proceed on their own timelines. Phases 1 to 4 do not wait for P-BIND, P-CLAIM, P-CAS or WI-GATE; **Phase 5's approval and artefact-creating paths do**. The phase numbers now follow this order.

**Phase 0: decisions, no code.**
- *Scope:* record the settled decisions (section 0), settle the OPEN items (section 11) that gate later phases, and commission the designs of P-BIND, P-CLAIM (if wanted) and WI-GATE.
- *Acceptance:* sections 0 and 11 reviewed and agreed by the owner (and, for the export gate, the re-issue policy and the legacy-hash waiver, by the lab's quality owner too); **P-BIND and WI-GATE each have their own design document with their own acceptance criteria before any path that depends on them is enabled**.
- *Failure injection:* not applicable (no code).
- *Dependencies:* none.

**Phase 1: make the file phase crash-safe** (tool only; no server change; **no export-gate change**).
- *Status:* **implemented** for `approve` (section 1A). `list_pending` now verifies the manifest (open item 14, resolved in that direction); `override` and `withdraw` are not covered; the LIMS export still passes no directory to `require_reviewed`.
- *Scope:* stage files privately, `fsync`, and publish with `os.replace` in the single order of section 5 (patient meta, PDFs, manifest, **results JSON last**); same contents and names. **No atomic multi-file publication is claimed.** Decide `list_pending`'s treatment of the manifest-present/JSON-not-yet-reviewed state (OPEN 14). The export gate is **not** changed.
- *Tests:* a crash-point test at every boundary of the section 5 table, asserting the directory state and the current gate's and `list_pending`'s outcome for each row; a successful sign-off yields files and a manifest equivalent to today's (hashes recomputed; PDFs are not compared byte-for-byte because they stamp the render time).
- *Failure injection:* raise between each pair of publications; truncate each file; simulate a full disk; one real process-kill test.
- *Acceptance:* at every interruption point the current export gate refuses (the JSON is not yet `reviewed`), or the run is complete except the local audit entry; a `reviewed` results JSON never exists before the PDFs and the manifest are published; `list_pending` and the gate agree, or the documented disagreement row is explicitly accepted by the owner; no claim of atomic publication is made; existing sign-off, export, content-verification and revision tests pass unchanged.
- *Dependencies:* none. (It narrows the existing hazard for interruptions; it does not harden the gate.)

**Phase 2: the journal, write-only.**
- *Scope:* create and append the journal during a normal `approve`; baseline, intent and result events, artefact hashes; no behaviour change when everything succeeds.
- *Tests:* format and chain verification; torn-tail and mid-file corruption; mode `0600` on POSIX; a scan proving no secret, reason text or server hash is on disk; unlinked runs write no journal; the manifest is unchanged by the journal.
- *Failure injection:* kill after every journal write; corrupt one byte in each record class.
- *Acceptance:* a successful sign-off yields a complete, verifiable journal; an interrupted one yields a classifiable journal; signed files are equivalent to today's.
- *Dependencies:* Phase 1.

**Phase 3: `signoff-status`, read-only classification.**
- *Scope:* a CLI command combining the journal, `signoff-state`, the revision read and the local hash check; prints which of A to I applies, the evidence by class, and the proposed next step, including `INVESTIGATE`. It writes nothing (not even the journal), states "binding not verifiable" until P-BIND exists, **flags prominently a run whose results JSON says `reviewed` with no manifest** (interim HOLD), works with **no journal** (limited classification, no recovery write), and never describes content as verified.
- *Tests:* one stand-in test per matrix row (journal state × server state); every conflict row in I refuses; H refuses; identity and local-hash checks; an `INVESTIGATE` result for C, D, E.
- *Failure injection:* a transport returning each state; a state that changes between two reads; a malformed answer (must refuse, as the adapter already does).
- *Acceptance:* each cell classifies deterministically; the transport records no write request; no output claims content verification.
- *Dependencies:* Phase 2.

**Phase 4: resume for A and B, up to `under_review`** (reuses the existing write endpoints; no new server write; **never a claim; never an approval**).
- *Scope:* from an attributed claim to submission, after a fresh read and with operator confirmation; then stop at `under_review` and report the safe stopping point (Decision 4 ruling). A non-attributable claim ends in `INVESTIGATE`. Case C and the approval step belong to Phase 5.
- *Tests:* a lost-response test for each of S1 and S2 using a **response-dropping proxy** that forwards the request to the real service and then fails the answer (the write commits, the client sees an error); repeated resume; two operators resuming (real threads); state changing between inspection and action; unauthorised and cross-organisation attempts; the transport never receives an approval request; a run with no journal triggers no write.
- *Failure injection:* drop the response at the claim and at the submission; crash the client between them; a late commit landing during the re-read.
- *Acceptance:* exactly one claim and one submission in every scenario; **recovery issues no claim write and no approval write**; no write is repeated on a lost response alone; an unprovable case ends in `INVESTIGATE` with nothing written; the stopping message names `under_review`.
- *Dependencies:* Phases 2 and 3. **Not** P-BIND, P-CLAIM, P-CAS or WI-GATE.

**Phase 5: P-BIND-gated recovery: the approval step (C), local artefact recovery (D, E) and audit reconstruction (F)** (the approval writes to the server; D, E and F change local files).
- *Scope:* (1) case F (audit reconstruction) needs no binding and may be enabled first. (2) The recovery **approval** step (`under_review` to `approved`) and (3) cases D and E (create or finalise artefacts) are built, and enabled, only when P-BIND exists: finalise from staging **only when no manifest exists**, using the journal's hashes and the server's `approved_at`; publish in the section 5 order (results JSON last); rebuild a truncated manifest; the per-run lock; before any approval re-read `signoff-state` and abort on any difference. If a valid manifest exists and a listed file is missing or mismatches: stop.
- *Tests:* F from every interruption point (no P-BIND needed); the approval step and D and E from every interruption point **with a P-BIND test double that returns verified, mismatch and cannot-verify**; the approval endpoint is never called unless the P-BIND check has passed, and with P-BIND unavailable the run stops at `under_review`; manifest-exists-with-missing-PDF stops and changes nothing; lock contention between real processes and the stale-lock procedure; restart after each step; a binding mismatch changes nothing.
- *Failure injection:* crash during recovery itself, repeatedly (re-runnable and idempotent); the binding check failing, timing out and returning garbage (each must stop); the response dropped at the approval write; a crash between the approval and each publication step.
- *Acceptance:* the final directory equals what an uninterrupted sign-off would have produced apart from `reconstructed` markers; the approved server record is never modified; recovery never sets `review_status: reviewed` without the checks of Decision 3; no artefact is created and no approval is made without a passing binding check. **Release criterion: the approval step and the D and E paths are feature-gated off, and are not enabled in any release, until an integration test against the REAL P-BIND mechanism passes (verified, mismatch, cannot-verify, unauthorised, cross-organisation); a test double alone does not satisfy this.** Case F may ship earlier.
- *Dependencies:* Phases 3 and 4; **the real P-BIND mechanism for the approval step and for D and E**; OPEN decisions 2, 3 and 6 of section 11. P-CAS is desirable; without it the stale-approval residual risk stays documented. WI-GATE is **not** a dependency of recovery, but "recovery produces export-eligible output" can be claimed only once WI-GATE lands.


**Phase 6 (separate decisions): handover, and the separate prerequisite and hardening work (P-BIND, P-CLAIM, P-CAS, WI-GATE).**
- *Scope:* any of: an authorised, audited operator handover workflow; P-CLAIM; P-CAS; and the separate work items P-BIND and WI-GATE, each with its own design and its own acceptance criteria (this document does not define them). P-BIND is listed here with the other separate-decision items but is a **gating prerequisite for Phase 5** and is scheduled before it (execution order above). Each is a contract change with its own design, tests and owner sign-off.
- *Tests:* per change, written with that change. For WI-GATE: a test for each refusal in Decision 3 and an explicit test diff wherever the legacy no-hash and retracted-export behaviours intentionally change. For P-BIND: match, mismatch, cannot-verify, unauthorised, cross-organisation, and no disclosure of claim text. For handover: an unauthorised takeover and a takeover racing the original operator.
- *Failure injection:* for P-BIND, a service that lies, times out or returns partial data; for WI-GATE, each fail-closed case; for handover, a stale journal and a revoked role mid-takeover.
- *Acceptance:* for WI-GATE and P-BIND, defined in their own design work, which must exist before any dependent path is enabled; a design that lacks acceptance criteria does not unblock Phase 5. For handover, P-CLAIM and P-CAS, defined when each is designed; none is assumed here.
- *Dependencies:* owner and domain-expert decisions (section 11). Phases 1 to 4 do not wait for it; Phase 5's approval and D and E paths wait for P-BIND.

## 10. Risks

- **Content binding unprovable until P-BIND:** after a lost approval whose files were not finalised, the tool can only report and stop. That is deliberate; it leaves some runs awaiting investigation.
- **Runs signed before P-BIND exists** can never be bound retroactively.
- **Duplicate claims** can still arise from a fresh sign-off by the operator (P-CLAIM absent); recovery itself never issues one.
- **PDFs are not reproducible:** once a manifest exists a lost or damaged PDF cannot be restored faithfully; recovery stops.
- **Current export gate** permits missing hashes, retracted reports, manifest-less runs and a state mismatch (Decision 3). Until WI-GATE lands these remain, recovery cannot make the gate stricter, and the only control for a manifest-less reviewed run is the **procedural interim HOLD**, which no code enforces.
- **Stale approval (no compare-and-set):** an approval seals every claim present when it commits; recovery re-reads first, but a claim recorded in between would still be sealed. No protection exists (P-CAS, OPEN).
- **Stale locks** rely on a person following the procedure in Decision 2; a wrongly released lock could let two same-operator processes interleave local files (the server's locks still protect submission and approval).
- **Pre-journal runs** have no recovery path at all; they depend on investigation and the interim HOLD.
- **Clock skew** between client and server affects matching an `approved_at` to a journal start; rules use ranges and the HTTP `Date` header.
- **Journal trust:** local only; forgeable by an attacker with write access, which is why it never authorises anything.
- **Windows permissions** do not give `0600` semantics.
- **Locks held across the approval's hash computation** lengthen contention; not measured.
- **Unverified:** real TLS, load, and `fsync`/`os.replace` behaviour on the target filesystems.

## 11. Decisions: settled, and what still needs a person

**Settled in sections 0 and 2 (TARGET):** content binding must be demonstrable and is not claimed possible until a P-BIND mechanism exists, and recovery's stricter requirement is a safety constraint, not a claim that normal sign-off is bound (D1); same-operator V1, journal is not authorisation, layered concurrency, recovery never issues a claim, locks released only by a person (D2); the current gate is documented as it is, the three-state evidence table and interim policy are recorded, and tightening the gate is a separate change (D3); reconcile before retry, never repeat on a lost response alone, stop when safety cannot be proven, **recovery does not approve while P-BIND is unavailable**, one publication order with the results JSON last (D4 and section 5).

**Genuinely open, and who should decide:**

| # | Question | Needs |
|---|---|---|
| 1 | How can any P-BIND shape satisfy "a comparison the server performs against authoritative approved content"? No candidate is viable until the stored run document and the local `geper_results.json` are shown to be comparable in one canonical form | owner and an engineer who owns the clinical service and the hashing |
| 2 | WI-GATE: the exact target policy details; whether a waiver exists for pre-hash legacy runs and who grants it; **sequencing the retracted-report refusal with the LIMS receiver's compatibility confirmation for export 1.1.0 and the retraction metadata (not obtained)** | owner **and the lab's quality/domain owner** |
| 3 | When a valid manifest exists and a PDF is lost or damaged: is a re-issued report permissible, and how must it be marked? | quality/domain owner |
| 4 | Whether P-CLAIM (a server-side duplicate-claim rule) is wanted at all, and its form | owner |
| 5 | Handover (post-V1): whether ever, and how an approver's printed identity may be read by someone else | owner, quality/domain owner |
| 6 | Which clock appears in finalised files: the server's `approved_at` (recommended) versus a client time already in a half-written JSON | owner |
| 7 | Journal anchor: is local-only acceptable, or must the chain head be recorded outside the run directory | owner, records owner |
| 8 | Journal retention: with the run indefinitely (recommended) or under a stated rule | records owner |
| 9 | Test-only fault hooks inside production paths, or an external harness only | engineering |
| 10 | Should an operator's recorded confirmation have any role at all alongside P-BIND (this draft: audit only, never unlocking) | owner |
| 11 | Recovery unattended: recommended never | owner |
| 12 | P-CAS: is protection against a claim recorded between the re-read and the approval wanted, and in what form (no such protection exists)? | owner, engineering |
| 13 | Stale-lock release: a dedicated explicit command or a documented manual rename | engineering, owner |
| 14 | `list_pending` for the manifest-present, JSON-not-yet-reviewed state: make it verify the manifest, or accept the documented disagreement | owner |
| 15 | Detection of reviewed-without-manifest runs across a directory tree (interim HOLD): a small read-only sweep tool, or per-run checks only | owner, quality owner |
| 16 | Acceptance criteria for P-BIND and WI-GATE: to be defined in their own design work before any dependent path is enabled | owner, quality/domain owner |
| 17 | Should normal sign-off also gain content binding (it has the same gap today)? | owner |
| 18 | Numeric policy for check C3 (strict type-and-sign comparison as tested in r1a2, an agreed canonicalisation, or refuse). The r1a2 result bears on item 1 (comparability of the stored run document and the local file) but does not answer it | owner, engineer who owns the hashing |
| 19 | Which session time zone production approvals were computed in, and whether every existing hash is therefore UTC-computed (Decision 6) | engineering, records owner |
| 20 | Who may complete a partially signed report: the original approver only (proposed), or also a quality owner on their behalf | owner, quality/domain owner |
| 21 | A claim-listing read exposing variant key and classification, but not reason text, so that check C4 can run (the sign-off state API does not provide them today) | engineering, owner |
| 22 | Whether the input shapes behind r1a2's PDF failures (consent object of an unexpected shape; ACMG net points without their components) can occur in real pipeline output; only the orchestrator was found to populate `patient_consent`, and the combiner invariant for point components was not examined | engineering |
| 23 | Which clock `reviewed_at` carries on a completed run (extends item 6) | owner |
| 24 | Where the r1a2 evidence is archived: it currently lives in a scratchpad directory outside the repository, which is not durable | owner, records owner |

## 12. What was inspected

`geper/review/signoff.py` (approve, the clinical and file phases, `_write_document`, `_append_audit_log`, `_withdraw_manifest`, `require_reviewed`, `list_pending`, `refresh_revision` staging), `geper/review/cli.py`, `geper/review/clinical_http.py`, `geper/report/models.py` (`compute_content_hash`, `verify_content_hash`), `geper/report/export_lims.py` (`_require_reviewed`, `_require_status_for_linked_run`, the CLI status lookup, CSV/JSON paths, manifest references), `geper/report/summary.py` and `summary_short.py` (the render-time stamp), `clinical/review_endpoints.py`, `clinical/data_access.py` (claim, submission, approval, state read, `verify_report_integrity`, `@auditable`/`@transactional`, hashing), `clinical/workflow_endpoints.py`, `clinical/schema.sql` (reports, reviewer_claims, immutability trigger, grants), `docs/SIGNOFF_API_DESIGN.md`, and the tests named in section 8.

**Verified by reading the code or tests (CURRENT):** the tables of existing behaviour in sections 0, 1 and 4. **TARGET, PREREQUISITE, PROPOSED and OPEN items are proposals and questions: not built, not tested, not enforced.** Nothing in this document has been executed, **except that section 13 records the results of an experiment (run tag `r1a2`) executed against a throwaway cluster with synthetic data.** For that experiment the following were additionally read: `geper/report/summary.py` and `summary_short.py` (consent and ACMG-point rendering), `geper/pipeline/orchestrator.py` (where `patient_consent` is populated), `geper/report/clinical_report_builder.py`, `geper/review/signoff.py` (`approve`, `override`, `withdraw` and the clinical helpers), and `clinical/data_access.py` (`_compute_report_content_hash`, claim recording, approval).

## 13. Evidence from the r1a2 experiment - CONFIRMED (r1a2)

**Scope and limits.** One experiment on 2026-10-18: Windows Python 3.14.7 driving PostgreSQL 16.15 (PGDG packages on Ubuntu 26.04 under WSL2), an isolated cluster on `127.0.0.1:55450`, UTF8, session `TimeZone` `Etc/UTC`, the schema applied from `clinical/schema.sql`. Synthetic records only. Documents were built with the real `JSONResultBuilder` (nine cases), persisted with the real `DataAccess.record_pipeline_result`, and signed off with the real `geper/review/signoff.py` through an in-process `DataAccess` (not the HTTP API, not the submission worker, not the real pipeline). Some synthetic documents were shaped by the experiment's own harness (fact 2). It is **not** production behaviour, **not** the Linux CI container, and the comparison policy used (**STRICT EXPERIMENTAL COMPARISON**: any numeric type, sign or value mismatch is UNVERIFIED; textual form of an identical parsed value is accepted; key order is not compared) has **not been validated against real pipeline output**.

**Facts.**
1. **Order of writes (CONFIRMED FROM SOURCE and the run).** `approve()` on a linked run commits three server steps first (a `sole_signatory` claim, `submit_for_review`, `approve_report`), then writes `geper_patient_meta.json`, then the results JSON already marked `reviewed`, then the full and short PDFs, then the manifest, then the local audit line.
2. **Three partial sign-offs.** Of the 13 approved reports, 10 completed (PDFs, manifest present) and 3 did not: `c07a_unicode_nfc`, `c07b_unicode_nfd` (both `KeyError: 'clinical_reporting'`) and `c08_numeric_edges` (`TypeError`, formatting `None`). For each, the server report is `approved` with a hash, the results JSON says `reviewed`, and the run directory holds only three files (no PDFs, no manifest, no audit line). No partial PDF can exist: the failure occurs while the document is being assembled, before the file is written. The three are exactly the *reviewed-without-manifest* state to which the interim HOLD (Decision 3) applies. **Causes, reproduced in memory against copies:** (a) the two Unicode documents carried `patient_consent` of shape `{"consent_given", "purpose"}`, which the pipeline does not produce (it passes the consent through `_parse_consent`, giving `clinical_reporting`, `research`, `timestamp`); both renderers read the document field raw and index `consent["clinical_reporting"]` for any non-empty object (`report/summary.py` line 981; `summary_short.py` line 424); the other cases had `patient_consent` null, which skips the block. (b) `c08` set `net_points` non-null with `pathogenic_points` and `benign_points` null; line 2052 of `report/summary.py` formats `acmg.get('pathogenic_points', 0)`, and the default applies only when the key is absent, so `format(None, 'g')` raises. These are inputs the experiment's harness built; whether real output can take those shapes is **OPEN** (item 22).
3. **S5.** An override whose results-file write was made to fail after the server `disagree` claim committed left the server with one `disagree` claim and the file with no override. `approve()` then succeeded. Reconciliation reported the divergence; the projection returned MATCH, because it removes `overrides` and `clinician_override` from both sides and the claim lives only in `reviewer_claims`. The T3 report keeps projection, reconciliation, approval state and hash result as separate fields, but it has no aggregate verdict, its console summary printed only the projection verdict and the hash result, and it did not record whether PDFs or manifest existed.
4. **Time-zone dependence of the approval hash.** `_compute_report_content_hash` serialises each claim timestamp with `datetime.isoformat()`; psycopg returns `timestamptz` in the connection's zone; nothing in `clinical/` pins the session zone. Recomputation in a UTC session reproduced the stored hash for 13 of 13 reports; in three other zones (Asia/Kolkata, Europe/London, America/Los_Angeles) it reproduced 0 of 13. The run document contains no timestamps, and the UTC match shows the claims and document were unchanged. The per-report cause was not examined with a fresh query (the cluster was not running when this was analysed).
5. **JSONB number round trip (separate T2 run, same tag).** Eight of nine documents round-tripped with no value, type or token difference. In `c08_numeric_edges`, 13 number tokens differed: `1e+16` came back as the integer `10000000000000000` (five paths), `1.7976931348623157e308` as a 309-digit integer, `-0.0` as `0.0`, and `1e-07`/`5e-324` changed text but not type or value. JSONB does not preserve object key order. Under the strict policy, 12 of 13 signed documents compared MATCH against the stored run document, and `c08` was UNVERIFIED (6 type changes, 1 lost zero sign).
6. **After approval.** `override` and `withdraw` were both refused with an "already approved" message; the results file and the claims were unchanged.

**Preserved evidence.** The 13 approved reports, the three partial run directories, the S5 run, the saved T3 report and the launcher outputs are retained unchanged. They are in a scratchpad directory outside the repository, which is not a durable location (open item 24). Nothing in the repository records them. Completion, recovery or cleanup must be exercised on copies, never on those directories.

## 14. Proposed completion operation - PROPOSED (not built)

### 14.1 Local sign-off states

| From | Event | To |
|---|---|---|
| `DRAFT` | claim, submit, approve (server) | `SERVER_APPROVED_FILES_PENDING` |
| `SERVER_APPROVED_FILES_PENDING` | PDFs and manifest published, audit line appended | `SIGNED_OFF` |
| `SERVER_APPROVED_FILES_PENDING` | PDF or manifest step fails | `PARTIAL` (stays; no automatic action) |
| `PARTIAL` | completion with verdict `RECOVERABLE` | `SIGNED_OFF` |
| `PARTIAL` or `SIGNED_OFF` | any check yields `INVESTIGATE` or `INTEGRITY_FAILURE` | `QUARANTINED` (read-only; a person decides) |
| any approved state | override or withdraw | refused (CURRENT, fact 6) |

There is no transition back to `DRAFT` and no second approval. `QUARANTINED` is a reporting state; it changes nothing on disk or in the database.

### 14.2 Recovery decision table

Rows that need C3 to pass are **unreachable until a P-BIND mechanism exists**; until then C3 is UNKNOWN and those rows resolve to `INVESTIGATE`.

| Server (C1) | Binding (C3) | Claims (C4) | Hash (C2) | Artefacts (C5) | Verdict and action |
|---|---|---|---|---|---|
| approved | MATCH | equal | ok | all present and valid | `COMPLETE`; at most a reconstructed audit line (case F) |
| approved | MATCH | equal | ok | PDF and/or manifest missing, no valid manifest | `RECOVERABLE`: stage and publish (cases D, E) |
| approved | any | any | any | valid manifest, a listed file missing or changed | `INTEGRITY_FAILURE`: stop; never regenerate |
| approved | any | divergent | any | any | `INVESTIGATE` (case J) |
| approved | UNKNOWN, UNVERIFIED or mismatch | any | any | any | `INVESTIGATE` |
| approved | any | any | mismatch for a v2 hash | any | `INTEGRITY_FAILURE` |
| approved | any | any | legacy hash fails the UTC form | any | `INVESTIGATE` (`UNVERIFIABLE_LEGACY`, case K) |
| retracted, released, unreadable or other approver | any | any | any | any | `INVESTIGATE` (case I rules apply) |
| not approved | - | - | - | - | out of scope here: cases A to C |

### 14.3 Operation contract

| Item | Proposal |
|---|---|
| Names | `signoff-verify` (read-only; returns the aggregate verdict and each check) and `signoff-complete` (acts only on `RECOVERABLE`) |
| Inputs | the run directory (its `clinical_link.json` names organisation, interpretation and report) and the operator's own authenticated session |
| Authorization | the original approver only, authenticated as themselves, still holding the Approver role (Decision 2). Takeover: not in V1 (OPEN item 20) |
| Server calls | reads only through the existing authenticated API, plus two that do not exist today: a server-side binding verification (P-BIND candidate (iii)) and a claim-listing read with variant key and classification but no reason text (item 21). The verification read is audited like `verify_report_integrity` |
| Writes | local artefacts only: staged PDFs, the manifest built from their hashes, local audit lines. Never a server write, never `approve()`, never a claim |
| Concurrency | a per-run advisory lock plus atomic publication (section 5 order, results JSON last); no new server lock is needed |
| Idempotency | a run already `COMPLETE` is a no-op returning the verdict; a crash leaves staged files only and a re-run starts from a fresh verdict |
| Local audit events | `completion_started`, `completion_verdict` (every check), `pdf_regenerated`, `manifest_written`, `completion_finished` or `completion_refused`; operator id and a non-backdated time; the original `approved` line, the server approval time and the approver are never altered |
| Never | reset a state, call `approve()` again, add or edit a claim, overwrite a verified manifest, edit the file to match the server, or run unattended |

### 14.4 Failure scenarios the operation must handle

PDF generation fails again after the rendering defect remains (verdict stays `INVESTIGATE`); the manifest write is interrupted; two processes complete the same run (second refused by the lock); the lock is stale (person-only release, Decision 2); the server state changes between verdict and publication (re-read, abort); the approver's role is revoked meanwhile (refuse); a PDF exists but its hash differs from the journal (stop); the clock or time zone of the verifying session differs (Decision 6 form is used); the results JSON was edited after approval (C3 or C6 fails); the claim set differs from the file (case J).

### 14.5 Tests required before any implementation

- Renderer regression tests: consent object of the wrong shape, absent and valid; ACMG point components null while the total is set; assertion that the failure does not leave a `reviewed` JSON ahead of the PDFs once the Phase 1 order exists.
- Hash: version 2 equals version 1 for a UTC session; stable under every session zone; a legacy hash failing the UTC form is `UNVERIFIABLE_LEGACY`, not tampering; version recorded and read back.
- Verdict engine (offline): every row of 14.2; a projection MATCH cannot hide a C2, C4, C5 or C6 failure; UNKNOWN never counts as passed.
- S5: the real override path with fault injection yields `INVESTIGATE` and changes nothing.
- Completion against a real database from each interruption point; crash during completion; two processes racing; non-approver and cross-organisation attempts; a stale lock.
- C3 with the strict policy: type change and `-0.0` yield UNVERIFIED; textual-form-only differences yield MATCH.
- Evidence preservation: completion exercised on copies only.
- An integration test against the real P-BIND mechanism before `signoff-complete` is enabled (Phase 5 release criterion).

### 14.6 Implementation sequence (PROPOSED; none of it authorized or started)

1. Review and accept (or change) this section and Decisions 5 and 6.
2. **Renderer robustness fix** with tests. Independent of everything else; reduces how often case D occurs.
3. **Hash version 2 and UTC session pinning**, with compatibility tests (Decision 6) and a decision on recording the version.
4. **Verdict engine** as a pure, offline-tested module and the read-only `signoff-verify`. Until P-BIND exists it returns `INVESTIGATE` for C3. Fits with Phase 3 (`signoff-status`).
5. **Claim-listing read** (item 21), so that C4 can run.
6. **Journal intent before an override claim**, so that an S5-type divergence is classified when it happens rather than discovered afterwards (builds on Phases 1 and 2).
7. **P-BIND server-side verification** (candidate (iii)), after the numeric-policy decision (item 18).
8. **`signoff-complete`**, feature-gated off until an integration test against the real P-BIND passes. This is the P-BIND-gated part of Phase 5; Phases 1 to 4 do not wait for steps 5 to 8.

**Relation to the existing phases.** Steps 4 to 6 extend Phases 1 to 3; step 8 is part of Phase 5 and inherits its release criterion. Nothing here changes Phase 0 to 6 as written.
