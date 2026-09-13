# GEPER Clinical Platform — Build-Ready Specification

**Version:** 0.1 — first consolidated specification
**Date:** 2026-09-03
**Status:** Source of truth for implementation. Not a proposal.

---

## 0. How to read this document

Every section marks each item with one of six states:

| Mark | Meaning |
|---|---|
| **IMPLEMENTED** | Exists in the codebase today, verified by audit |
| **PARTIAL** | Exists but incomplete or incorrect in a stated way |
| **ABSENT** | Does not exist. Not a gap in this document — a gap in the system |
| **DECISION** | Requires a product decision from Kislay Prasad before build |
| **EXPERT** | Requires qualified clinical or regulatory validation |
| **VENDOR** | Requires external licensing or vendor confirmation |

Claims about the existing codebase come from Kelly's state audit (2026-09-02) and
Pam's six-item audit (2026-09-03), both read-only investigations with file and line
citations. Where this document states that something does not exist, that is a
finding, not an omission.

**This specification does not claim clinical validation, ISO certification,
production readiness, or medical safety.** Bij AI is in development, is not
publicly available, and is not clinically validated. Every report it produces is a
draft requiring qualified human review.

**On the ISO 15189 clause citations in this document, added 2026-09-08:** every
ISO 15189 clause citation below is tagged `[UNVERIFIED-AGAINST-ISO-TEXT]`. These
citations came from cards written without access to the standard's actual text —
the only machine-readable ISO 15189 text on disk in this repository is three
OCR'd pages containing none of the clauses cited here. The first citation anyone
checked against a human reading of the real standard (7.4.1.5 c) for reviewer/
approver identity, at the two sites below) was wrong — a one-for-one failure rate
on the only sample checked so far — and has been corrected to 7.4.1.6 j). The tag
does not mean a citation is wrong; it means nobody on this floor has been able to
check it, which the 7.4.1.5 c) case shows is a live risk, not a formality.
**Scope of the tag, added 2026-09-08:** it applies to *every* ISO 15189 clause
citation in this document, not only the six originally reviewed for the 7.4.1.5 c)
correction, and for a reason that does not require inspecting any citation
individually: no one who wrote any of them had the standard's own text. That is a
fact about this floor's access to the source, uniform across every citation, not
a per-citation judgement — so a citation appearing later in this document without
the tag is an anomaly to be checked, not a citation that is assumed clean by
default.

---

## 1. Boundaries

### 1.1 What the Bij AI variant-interpretation component (GEPER) is

The genomic interpretation engine. VCF in, evidence assembled from public
databases and pretrained models, ACMG/AMP criteria evaluated, draft classification
out. **IMPLEMENTED.**

GEPER:
- Evaluates 19 of the 28 ACMG/AMP criteria. The remaining nine have no integrated
  data source and are reported as not evaluated.
- Integrates existing pretrained models as published. No model is retrained or
  fine-tuned.
- Produces a draft classification requiring qualified human review and final
  sign-off before any clinical use.

### 1.2 What the Bij AI variant-interpretation component (GEPER) is not

**It does not refuse on clinical grounds.** There is no input gate on sample
quality, consent, or patient identity. The disclaimer is a caveat in the output,
not a precondition on the input. GEPER will run on anything handed to it.

This is the single most important architectural fact in this document. **Every
clinical precondition lives in the Clinical Platform, and none of them can be
delegated downward.**

### 1.3 What GEPER Clinical is

The infrastructure around the engine: identity, consent, ordering, sample
tracking, lineage, review workflow, release control, and audit. It is not a
hospital ERP and must not become one.

### 1.4 The separation, stated as a rule

GEPER knows nothing about patients. It receives a VCF and returns an
interpretation. The Clinical Platform knows about patients and never performs
interpretation.

Any feature that would require GEPER to know a patient identity, or the Platform
to evaluate a variant, is on the wrong side of the boundary.

---

## 2. The two constraints that precede everything

Both were identified by audit and neither is a code defect. They are structural
properties of the current system, and the Platform exists partly to solve them.

### 2.1 There is no identity

**ABSENT.** Every "who" in the current codebase is a value someone typed.

- Authentication is a shared API key. It says a caller is authorised; it says
  nothing about who they are. A capability, not an identity.
- Sign-off stores `clinician_name`, `reg_number`, `hospital` as free strings.
  Nothing verifies them.
- There are no users, no roles, no sessions.

**Consequence for ISO 15189 7.4.1.6 j) `[UNVERIFIED-AGAINST-ISO-TEXT]`
(corrected 2026-09-08, was 7.4.1.5 c) — see note at top of file):** the
standard requires that the identity of the reviewer be retrievable. A
sign-off record holding an unauthenticated string does not satisfy this,
even though the record exists.

**Consequence for build order:** audit logging cannot be built before identity.
There is nothing to attribute entries to. Identity is the first component.

### 2.2 There is no lineage

**ABSENT.** A FASTQ run, a VCF, and an interpretation are three artefacts with no
thread between them.

- `kim_pipeline`'s `run_id` (uuid4, its API's primary key) never appears in
  `geper_results.json`.
- GEPER's `output_dir` never appears in `kim_pipeline`'s `runs` table.
- The only value crossing the boundary is `sample_id` — a caller-typed string,
  defaulting to the literal `"SAMPLE"`, with no uniqueness enforcement and no
  verification anywhere.

`sample_id` currently carries three unrelated jobs at once: work-directory scope,
cross-tree join key, and the DELETE guard's collision check. Every identity and
lineage problem in the system traces to that one overloaded field.

**Consequence:** even holding all three artefacts, nothing proves they belong to
the same sample. Impact analysis under ISO 15189 7.5 d) `[UNVERIFIED-AGAINST-ISO-TEXT]` — identifying which
released reports are affected by a discovered nonconformance — is impossible
without lineage.

---

## 3. Users, roles and RBAC

**ABSENT in full.**

### 3.1 Roles

Six roles. Each is a set of permissions, not a job title — one person may hold
several.

| Role | Purpose |
|---|---|
| **Orderer** | Places genetic test orders. Clinician or genetic counsellor. |
| **Lab technician** | Receives and tracks samples, records QC, operates sequencing. |
| **Interpreter** | Reads GEPER drafts, adds interpretation, prepares for sign-off. |
| **Approver** | Signs off reports. Must be qualified; the qualification is recorded. |
| **Administrator** | Manages users, roles, organisation settings. |
| **Auditor** | Read-only access to audit trail and lineage. Cannot alter records. |

**DECISION:** whether Orderer and Interpreter are distinct in your intended
deployment, or whether a clinician placing an order also interprets. This changes
whether the platform needs a handoff step between order and interpretation.

### 3.2 Permission model

Role-based, evaluated per action, scoped by organisation. Every permission check
is on `(actor, action, resource, organisation)`.

**The Approver role is special and must be treated as such.** It is the only role
whose action releases a report for clinical use, and ISO 15189 7.4.1.6 j)
`[UNVERIFIED-AGAINST-ISO-TEXT]` (corrected 2026-09-08, was 7.4.1.5 c) — see
note at top of file) requires the approver's identity be retrievable. An
Approver assignment must record who
granted it, when, and on what basis of qualification.

**EXPERT:** what qualification is required to hold the Approver role. This is a
clinical and regulatory question, not a technical one — it determines who may sign
a genomic report in an Indian clinical laboratory.

### 3.3 Separation of duties

**DECISION:** may the same person interpret and approve the same report?

Two options:
- **Permitted** — simpler, and realistic for a small laboratory where one
  qualified person does both.
- **Prohibited** — stronger control, requires at least two qualified people per
  report, and blocks a single-person deployment.

**Recommendation: permitted but recorded.** Prohibiting it makes small
deployments impossible. Recording it — the report shows the same person did both —
makes the fact visible to an assessor without preventing the workflow.

---

## 4. Organisations and tenancy

**ABSENT.**

### 4.1 Model

Single-instance, multi-organisation. Every record belongs to exactly one
organisation. There is no cross-organisation record.

An organisation is a hospital, a diagnostic laboratory, or a research group. It
owns its patients, orders, samples, runs and reports.

### 4.2 Isolation

**Enforced at the data layer, not the application layer.** Every query is scoped
by organisation before any other filter. A missing organisation scope is a bug
class, not a permission failure, and must be structurally impossible rather than
checked.

**Implementation:** organisation id is a required column on every table, and data
access goes through a layer that will not construct a query without it. Not a
`WHERE` clause a developer remembers to add.

### 4.3 Cross-organisation referral

**DECISION:** can Organisation A send a sample to Organisation B for sequencing or
interpretation, and see the result?

ISO 15189 6.8.2 `[UNVERIFIED-AGAINST-ISO-TEXT]` covers referral laboratories, so this is a real clinical pattern.
But it is the single largest source of complexity in a tenancy model, and it
changes the isolation rule from "never crosses" to "crosses under these
conditions."

**Recommendation: out of scope for v1.** State it as explicitly not supported. Add
it when a real referral relationship exists to design against.

---

## 5. Patient identity and records

**ABSENT.**

### 5.1 Patient record

The minimum a genomic report requires, and nothing more. Minimum-necessary is a
design rule here, not a preference — this is a system holding genomic data.

| Field | Purpose |
|---|---|
| Internal patient id | System-generated, opaque, never derived from identifying data |
| Name | Required on the report |
| Date of birth | Identity disambiguation, age-dependent interpretation |
| Sex | Required for X/Y interpretation. **See PCPNDT constraint below.** |
| External identifier(s) | Hospital MRN or equivalent, per organisation |
| Consent records | See §6 |

**Not held:** address, contact details, insurance, employment, next of kin. If a
future feature needs them, that is a decision to revisit — not a default.

### 5.2 Patient identity resolution

Two patients with the same name and date of birth are two patients until a human
says otherwise. The system never merges automatically.

**DECISION:** does the platform own patient identity, or does it consume identity
from a hospital system? If the latter, the external identifier becomes the primary
key and this section changes substantially.

### 5.3 PCPNDT constraint

**EXPERT / legal.** The Pre-Conception and Pre-Natal Diagnostic Techniques Act
prohibits sex determination of a foetus in India. A genomic pipeline handles sex
chromosomes.

**Requirement:** before any prenatal sample can be accepted, establish whether the
platform must refuse prenatal orders entirely, or suppress sex-chromosome
reporting on them, or both. This is a legal constraint with criminal penalties and
it is not a technical decision.

**Until ruled: the platform must not accept an order marked prenatal.** A refusal
is recoverable; a violation is not.

---

## 6. Consent

**PARTIAL.** GEPER already records consent metadata in every run document
(`patient_consent`). Nothing checks it.

### 6.1 Model

Consent is a record attached to a patient, with a scope, a date, and the identity
of who recorded it. A consent record is never edited — a change is a new record
superseding the old, and both are retained.

**Scopes, at minimum:**
- Consent to genomic testing for the clinical indication
- Consent to secondary findings being reported (separate, and separately
  withdrawable)
- Consent to data retention beyond the clinical episode
- Consent to research use, if offered

**EXPERT:** the actual scope set, and what each must say. Consent language for
genomic testing is a clinical and legal artefact.

### 6.2 Enforcement

**Consent is a precondition on submission, not a caveat on output.** An order
cannot proceed to sequencing without valid consent for its scope. This is one of
the platform-side preconditions gating automatic submission (§10).

### 6.3 Withdrawal

Consent can be withdrawn. Withdrawal does not delete existing reports — those were
lawfully produced — but it stops further processing and flags the patient record.

**DECISION:** what withdrawal means for data already held. DPDP Act 2023 gives a
right to erasure; a clinical record may have retention obligations that conflict.
This needs counsel.

---

## 7. Genetic test ordering

**ABSENT.**

### 7.1 The order

An order is the clinical intent: this patient, this test, this indication, this
orderer.

| Field | Notes |
|---|---|
| Order id | System-generated |
| Patient | Reference |
| Test / panel | See §7.2 |
| Clinical indication | Free text plus optional HPO terms |
| Ordering clinician | Identity, not a string |
| Priority | Routine or urgent. **DECISION:** does urgent mean anything operationally, or is it a label? |
| Consent reference | Which consent record authorises this order |

### 7.2 Test catalogue

**DECISION, and it is the one that sets your regulatory class.**

The set of genes and conditions a test covers determines whether Bij AI is CDSCO
Class A, B or C. Critical conditions → Class C. Serious → Class B. Non-serious →
Class A.

The catalogue is therefore not a configuration table — it is the artefact that
defines the validated scope, and adding to it is a regulatory action rather than a
data entry.

**EXPERT:** where the critical/serious boundary falls for genomic conditions.
Specifically for the genes in any first panel.

**Requirement:** the platform must make scope expansion a gated action, not a form
submission. Adding a gene to a catalogue changes what has been validated.

### 7.3 Order states

`draft → placed → sample_awaited → in_progress → reported → closed`

Plus `cancelled` at any point before `reported`.

---

## 8. Sample lifecycle

**ABSENT.**

### 8.1 Sample record

| Field | Notes |
|---|---|
| Sample id | System-generated, globally unique. **Never a caller-supplied string.** |
| Order | Reference |
| Type | Blood, saliva, tissue, extracted DNA |
| Collected at | Timestamp and collector identity |
| Received at | Timestamp and receiver identity |
| Condition on receipt | ISO 15189 6.6 `[UNVERIFIED-AGAINST-ISO-TEXT]` requires this for reagents; the same applies to samples |
| QC status | See §8.3 |

### 8.2 The sample_id problem, solved

The Platform generates sample identifiers. They are opaque, unique, and never
defaulted.

When submitting to `kim_pipeline`, the platform passes its own sample identifier
as `sample_id`. This eliminates the `"SAMPLE"` default collision at the source —
`kim_pipeline` never sees a caller-typed value again.

**This does not fix `kim_pipeline`'s internal overloading** (§2.2). The work
directory is still `sample_id`-scoped. But with globally unique identifiers, two
runs can no longer collide on it.

### 8.3 QC and rejection

A sample can fail QC. A failed sample does not proceed and the order returns to
`sample_awaited`.

**QC criteria are EXPERT.** What constitutes an acceptable sample for genomic
sequencing is a laboratory decision, and the thresholds are the laboratory's.

---

## 9. Sequencing and the lab boundary

### 9.1 What the platform owns

Sample receipt, tracking, QC recording, and the trigger to begin sequencing.

### 9.2 What kim_pipeline owns

**IMPLEMENTED.** FASTQ → QC → alignment → variant calling → VCF. Exposed as an
HTTP API with `POST /api/v1/pipeline/start`, status polling, and DELETE.

**Recent fixes (2026-09-03):** startup reconciliation of crashed runs, failure-path
persistence, progress isolation, unbounded DELETE guard scan, and a durable
default for `GEPER_DB_PATH`.

### 9.3 The boundary

The platform submits to `kim_pipeline` and polls for completion. It does not reach
into the work directory, read checkpoints, or interpret intermediate state.

**PARTIAL:** `bridge/combined_pipeline.py` currently drives `kim_pipeline` as a
subprocess and joins on filesystem paths, bypassing the run_id-keyed API entirely.
The platform must use the API, not the bridge. The bridge remains for CLI use.

---

## 10. VCF ingestion and automatic submission to GEPER

**RULED: automatic.** Once a VCF is produced and all platform-side preconditions
are satisfied, the platform submits it to GEPER without manual trigger.

### 10.1 Preconditions — all must hold

1. **Valid consent** for the order's scope, not withdrawn.
2. **Patient and sample identity resolved** — both reference real records, not
   strings.
3. **Required order data present** — test/panel, indication, ordering clinician.
4. **Sample QC passed.**
5. **VCF validates** — see §10.2.

If any precondition fails, submission does not occur and the order enters the
exception workflow (§10.6). **A failed precondition is never a warning.**

### 10.2 VCF validation

Before submission, the platform validates:

- File exists, is readable, is VCF or bgzipped VCF
- Header present and parseable
- Reference build declared in the header, and matches the order's expected build
- At least one sample column
- Variant count is non-zero

**Build mismatch is a hard stop.** GEPER has no build validation of its own —
nothing checks that the FASTA, GFF3 and VCF share a genome build, and
`fasta_path` is caller-configurable. A GRCh37 VCF against a GRCh38 reference
produces coordinates that are silently wrong.

### 10.3 The submission contract

**GEPER's actual interface today is a CLI.** There is no importable API, no HTTP
endpoint returning a classification, no queue, no webhook. The integration
surfaces are: subprocess invocation, reading `geper_results.json` from a known
path, the structures endpoint, and gated LIMS export.

**Requirement:** wrap GEPER in a service interface. The platform must not shell
out and watch a directory.

**Minimum service contract:**

```
POST /interpretations
  {
    "submission_key": "<idempotency key, see 10.4>",
    "vcf_path": "<path>",
    "assembly": "GRCh38",
    "sample_ref": "<platform sample id>",
    "hpo_terms": ["HP:0000001", ...],       optional
    "qc_metrics": { ... },                   optional
    "consent_ref": "<platform consent id>"
  }
  → 202 { "interpretation_id": "...", "status": "queued" }
  → 200 { "interpretation_id": "...", "status": "..." }   if key already seen

GET /interpretations/{id}
  → { "status": "queued|running|complete|failed",
      "run_document_ref": "...",             when complete
      "error": "..." }                       when failed
```

**The platform passes no patient identity.** `sample_ref` is an opaque platform
identifier. GEPER never learns who the patient is, which preserves the boundary
in §1.4 and limits what a GEPER compromise exposes.

### 10.4 Idempotency

**The submission key is `hash(vcf_content) + platform_sample_id`.**

Not `sample_id` alone — that is the field this whole system's identity problems
trace to. Not a uuid generated at submission — that is not idempotent, since a
retry generates a new one.

Content hash plus sample means: the same VCF for the same sample submitted twice
returns the first interpretation. A re-sequenced sample produces a different VCF,
a different hash, and a new interpretation — which is correct, because it is new
evidence.

### 10.5 Authentication and audit of an automatic action

An automatic submission has no human actor. It is nevertheless an action on a
patient's data and must be attributable.

**Requirement:** the platform has a named system principal — not an empty actor
field, not the last human who touched the order. The audit entry records the
system principal, the order, the sample, the submission key, and the precondition
check results that authorised it.

**The precondition results are part of the audit record.** "Submitted because
consent C-123 was valid, identity resolved, QC passed" is the auditable claim.

### 10.6 Failure handling

Failures route to an operational exception workflow. They are never logged and
dropped.

**Exception categories:**

| Category | Handling |
|---|---|
| Precondition failure | Order enters `blocked` with the failing precondition named. Visible to the orderer and the lab. Not retried automatically. |
| Validation failure | Same. The VCF is wrong and retrying will not fix it. |
| Transient submission failure | Retried with backoff. Idempotency key ensures no duplicate on success. |
| Interpretation failure | Order enters `blocked`. GEPER's error recorded verbatim. Retryable by a human. |

**Retry safety:** GEPER writes files before it finishes. A failed submission can
leave a partially written output directory. A retry must not read a partial
directory as a completed run — the run document's `run_complete` marker is the
check, not the presence of files.

**An exception has an owner and a state.** It is a work item someone sees, not a
row in a log.

---

## 11. Identifiers and lineage

**ABSENT.** This section defines what the platform must create.

### 11.1 Identifier set

| Identifier | Generated by | Scope |
|---|---|---|
| `patient_id` | Platform | Organisation |
| `order_id` | Platform | Organisation |
| `sample_id` | Platform | Global, unique |
| `sequencing_run_id` | Platform, mapped to kim's `run_id` | Global |
| `interpretation_id` | Platform, mapped to a GEPER run document | Global |
| `report_id` | Platform | Global |

### 11.2 The lineage chain

```
patient → order → sample → sequencing_run → vcf → interpretation → report
```

**Every link is a stored reference, not an inference.** The platform can answer,
for any report, which interpretation produced it, which VCF that interpreted,
which sequencing run produced the VCF, which sample, which order, which patient.
And in reverse.

**This is what makes ISO 15189 7.5 d) `[UNVERIFIED-AGAINST-ISO-TEXT]` achievable.** Given a discovered
nonconformance — a wrong model version, a failed data source — the platform can
identify every affected report.

### 11.3 What must be recorded from GEPER

The run document already carries what is needed: `code_version`,
`model_checkpoints` with resolved versions and calibration status, `provenance`
with database versions and retrieval modes, `service_health` recording source
failures, and `run_complete`.

**Requirement:** the platform stores these, indexed. Not a copy of the document in
a blob column — the fields that impact analysis queries against.

**PARTIAL, and it is the discovery gap:** GEPER run documents are currently
unfindable by construction. No id, no recorded location, no index. The platform
supplies that layer, and it is a prerequisite for everything in §11.2.

---

## 12. Result ingestion

When an interpretation completes, the platform retrieves the run document and
stores:

- The complete document, immutably, as the record of what GEPER produced
- Indexed fields for query: model versions, database versions, source health,
  criteria evaluated, per-variant classifications
- The lineage links (§11.2)

**The stored document is never edited.** Interpretation additions, corrections and
sign-off are separate records referencing it. What GEPER produced and what a human
concluded are two different claims and must remain distinguishable.

---

## 13. Interpretation, review, approval and release

### 13.1 States

```
draft → under_review → approved → released
                    ↘ returned (back to under_review)
```

**IMPLEMENTED in part:** GEPER has `review_status` with `draft` / `reviewed` /
`overridden`, `reviewed_by`, `reviewed_at`, and `require_reviewed()` gating LIMS
export.

**PARTIAL:** the sign-off identity is an unverified string (§2.1), and the gate
covers export only.

### 13.2 The review

An Interpreter reads the draft, adds clinical interpretation, and may:
- Accept a variant classification
- Disagree with it, recording the disagreement and the reasoning
- Add variants GEPER did not surface
- Mark variants as not clinically relevant to the indication

**GEPER's classification is never silently overwritten.** A disagreement is a
second claim recorded alongside the first, not a correction of it.

**EXPERT:** whether the reviewer re-derives the classification independently or
ratifies GEPER's draft. These are different workflows with different evidentiary
weight, and the answer changes what the review screen must show.

### 13.3 Approval

Only an Approver approves. The approval records identity, timestamp, and the exact
content approved — a content hash of the report at approval time.

**Approval is what makes a report releasable.** Nothing else does.

### 13.4 Release

**RULED (§ automatic submission decision): no report leaves the platform without
approval.**

The boundary, restated: producing a draft is not delivering it. A draft in the
system is the pipeline working. A report reaching a clinician, a patient, a LIMS,
or any downstream consumer requires approval.

**Every delivery path checks approval.** This is stated in GEPER's `signoff.py`
docstring as a rule for future paths. The platform enforces it structurally: a
delivery method that does not check is a bug, and there is one function every
delivery path calls.

---

## 14. Draft and final states

Every artefact carries its state visibly, on every surface, and the marking
survives rendering.

**IMPLEMENTED in GEPER:** draft markings on JSON, Markdown, full PDF and short
PDF, verified by tests that assert on rendered output rather than source
constants.

**Requirement for the platform:** the same discipline. A draft displayed in a UI,
exported, printed or emailed says draft. The state is not a badge that can be
styled away.

---

## 15. Locking, amendment and re-analysis

### 15.1 Locking

An approved report is immutable, and the immutability is enforced by the
database rather than by application code. A BEFORE UPDATE trigger on
`reports` refuses any change to an approved or released row except the two
writes the workflow requires: the approved → released transition, and the
retention tombstone. Its content hash is recorded at approval and verifiable
afterwards (`verify_report_integrity`), and because neither the hashed
content nor the stored hash can be rewritten after approval, verification is
PREVENTION rather than DETECTION.

The same trigger extends to `interpretations` and `vcfs`. WHAT IS NOT
COVERED, stated so the claim is not read wider than it is: the trigger does
not make every column of these tables immutable, and on `interpretations`
and `vcfs` it does not stand alone. On `reports`, `state` remains mutable by
design — the trigger enforces content-immutability, not workflow legality;
which state transitions are legal is an application precondition, not a
database constraint — and the tombstone columns (`tombstoned_at`,
`tombstoned_by`) are mutable for exactly one principal, gated by
column-level UPDATE privilege rather than by role name. On
`interpretations` and `vcfs`, `clinical_app`'s inability to UPDATE at all is
enforced by a separate mechanism — a `REVOKE UPDATE` from `clinical_app` —
not by this trigger; the trigger's own job on those two tables is narrower
and different: it binds the database superuser, which no `REVOKE` can
reach, since a grant (or its absence) never constrains a superuser. A
migration requiring an exemption from any of this must disable the trigger
explicitly.

Why the guarantee is stated this way rather than as a blanket "immutable":
unqualified, nothing at the database layer used to deliver it — the
guarantee was application convention on every table involved, and
verification could not bear the weight the word implied. Verification
(`verify_report_integrity`) is a comparison of recomputed content against a
stored hash; if the same principal could rewrite both the content and the
stored hash, tampered content verified clean. Per table: `reports.content_hash`
is protected against the same principal that could alter content, so
tampering there is DETECTION — a mismatch at verification. `interpretations.run_document`,
being the hashed content itself, is now PREVENTION — protecting it stops the
tampering rather than reporting it. `vcfs` is PREVENTION for lineage, not
covered by the report hash at all. Before this trigger, neither
`run_document` nor `vcfs` content was protected, and tampering there was
undetectable: alter the content, rewrite the stored hash, verification
passes.

DETECTION REMAINS IMPLEMENTED AND REMAINS UNTESTED on the paths where
prevention makes the attack unconstructable. This is a statement about what
the system can demonstrate, not a claim that both guarantees are exercised:
retiring the regression test that simulated a post-approval tamper of
`interpretations.run_document` (prevention now makes that tamper
impossible to construct, not merely harder) removed the executable evidence
that detection works on that specific path. Detection against
`reviewer_claims` and `evidence_json` — neither covered by this trigger — is
unaffected, still implemented, and still tested.

### 15.2 Amendment

**ISO 15189 7.4.1.8 `[UNVERIFIED-AGAINST-ISO-TEXT]` requires:** the reason for change recorded and included in the
revised report; revised results delivered as an additional document clearly
identified as revised, with the original's date and patient identity indicated;
the user made aware of the revision; a completely new report uniquely identified
with traceability to the one it replaces.

**Requirement:** amendment creates a new report referencing the original. The
original is never modified and never withdrawn from the record.

### 15.3 Re-analysis

A sample's VCF can be re-interpreted later — new evidence, updated databases,
changed criteria.

**Re-analysis produces a new interpretation, a new report, and a new lineage
branch from the same VCF.** It does not update the old one.

**DECISION:** what triggers re-analysis. Options: manual only; automatic on a
schedule; automatic when a variant's ClinVar classification changes. The third is
the most clinically valuable and the most complex — it requires watching external
databases and knowing which patients are affected, which is exactly what §11.2's
lineage enables.

**Recommendation: manual for v1**, with the lineage built so automatic re-analysis
is possible later without redesign.

---

## 16. Patient access

**DECISION, and it is a significant one.**

Options:
- **No patient access.** Reports reach the ordering clinician, who discusses them
  with the patient. Simplest, and matches most clinical genetics practice.
- **Patient portal.** Patients see their own approved reports.

**Recommendation: no patient access in v1.** A genomic report is not
self-explanatory, and ISO 15189 7.4.1.4 d) `[UNVERIFIED-AGAINST-ISO-TEXT]` requires that genetic results needing
counselling not reach a patient without the opportunity for adequate counselling.
A portal that hands over a variant classification without a counsellor is a
clinical safety problem, not a feature.

**EXPERT** if you want it: what counselling provision must accompany patient
access.

---

## 17. Audit trail

**ABSENT.** Depends on identity (§2.1) and cannot be built first.

### 17.1 What is audited

Every action that creates, modifies, releases or accesses clinical data:

- Patient record creation and modification
- Consent recorded, modified, withdrawn
- Order placed, modified, cancelled
- Sample received, QC recorded, rejected
- Submission to GEPER, with precondition results
- Interpretation viewed, modified
- Approval, release, amendment
- Any access to a patient record, including read

### 17.2 Entry shape

```
{ timestamp, actor, actor_role, organisation, action,
  resource_type, resource_id, outcome, detail }
```

**Actor is an identity, never a string.** For automatic actions, the named system
principal (§10.5).

### 17.3 Properties

- **Append-only.** No update, no delete, at the storage layer.
- **Retained** per §22.
- **Queryable** by actor, resource, time, action.
- **Never contains clinical content** — it records that a report was viewed, not
  what it said.

---

## 18. Notifications

**Scope, deliberately narrow.**

**In scope:**
- Order state changes to the orderer
- Sample QC failure to the lab
- Interpretation ready for review, to the interpreter pool
- Report approved, to the orderer
- Exception raised, to whoever owns it

**Out of scope:** patient notifications, appointment reminders, marketing, general
messaging.

**Requirement:** a notification never contains clinical content. It says a report
is ready, not what the report says. **DECISION:** delivery channel — in-app only,
or email? Email carrying a link is fine; email carrying results is not.

---

## 19. Scheduling

**Explicitly out of scope.**

The platform does not manage appointments, clinician calendars, room bookings or
patient visits. It tracks sample and order states, which have timestamps but are
not a schedule.

**In scope:** turnaround time tracking — how long an order has been open, whether
it has exceeded an expected duration. That is order state, not scheduling.

---

## 20. Billing

**Explicitly out of scope.**

The platform does not price tests, generate invoices, process payments, or
integrate with insurance.

**In scope:** recording that a test was performed, which is what a billing system
would consume. **DECISION:** whether the platform exposes a read-only feed for an
external billing system. Recommendation: yes, eventually; not v1.

---

## 21. Files and documents

### 21.1 What is stored

- Uploaded FASTQ and reference files
- Generated VCFs
- GEPER run documents and rendered reports
- Any documents attached to an order or patient record

### 21.2 Requirements

- **Content-addressed where possible.** A VCF's hash is its identity (§10.4).
- **Never served by user-supplied path.** Every file access goes through a
  reference the platform resolves and authorises.
- **Filesystem paths never appear in API responses.** They reveal deployment
  structure — the same leak class fixed in `kim_pipeline`'s config endpoint and
  500 handler.

**PARTIAL:** `kim_pipeline` currently accepts and returns paths. The platform must
not expose them onward.

---

## 22. Retention and lifecycle

**ABSENT.** `kim_pipeline` has no TTL, no scheduled retention, and files persist
indefinitely until DELETE is called.

### 22.1 What must be decided

**DECISION / EXPERT / legal.** Retention for clinical genomic data is governed by
NABL 112A (VCF and FASTQ ≥ 5 years per §7.8.5(b)), the DPDP Act's erasure rights,
and any hospital policy.

Required decisions:
- Retention period per artefact class — raw reads, VCFs, run documents, reports,
  audit entries
- What consent withdrawal means for retained data (§6.3)
- Whether deletion is real deletion or tombstoning

**Requirement regardless:** retention is a policy the platform enforces, not a
manual process. Nothing is deleted by someone remembering to.

---

## 23. Security, authentication and access control

**ABSENT beyond a shared API key.**

### 23.1 Authentication

- Per-user credentials. No shared accounts.
- **MFA required for Approver and Administrator.** These roles release clinical
  reports and manage access; a password alone is not sufficient.
- **DECISION:** MFA for all roles, or only privileged ones? Recommendation: all
  roles, given the data class.

### 23.2 Sessions

- Time-limited, idle timeout, explicit logout
- Session identity, not just a token — an audit entry attributes to a session and
  a user
- Concurrent session policy: **DECISION**

### 23.3 Service authentication

The platform authenticates to `kim_pipeline` and GEPER with service credentials,
not user credentials. Both currently use `GEPER_API_KEYS` with a fail-to-start
control (`GEPER_DEV_INSECURE=1` as the explicit dev opt-in). **IMPLEMENTED** on
both trees as of 2026-09-03.

### 23.4 Transport

TLS everywhere, including between platform and engines when they are separate
processes.

---

## 24. Privacy and minimum-necessary access

- A user sees only records their role and organisation permit.
- A lab technician sees samples and QC, not clinical interpretations.
- An auditor sees the audit trail, not clinical content.
- **Every read of a patient record is audited** (§17.1). Access without a reason
  is visible after the fact even where it is not prevented.

**DPDP Act 2023:** genomic data is personal data. Consent, purpose limitation, and
erasure rights apply. Full compliance obligations bite May 2027. **Counsel
required** on the interaction between DPDP erasure and clinical retention.

---

## 25. APIs and frontend/backend responsibilities

### 25.1 Backend

Owns all state, all permission checks, all clinical logic. The API is the only way
to reach data.

**No business rule lives in the frontend.** A precondition check, a permission
check, or a state transition enforced only in the UI is not enforced.

### 25.2 Frontend

Renders state and collects input. Screens required:

| Screen | Purpose |
|---|---|
| Login / MFA | Authentication |
| Worklist | Orders and their states, filtered by role |
| Patient record | Identity, consents, order history |
| Order entry | Place an order |
| Sample receipt | Log receipt, record QC |
| Interpretation | GEPER draft, evidence per variant, interpreter's additions |
| Approval | Review the assembled report, approve or return |
| Report view | Rendered report with state marking |
| Exceptions | Failed preconditions, failed submissions, blocked orders |
| Audit | Query the trail |
| Administration | Users, roles, organisation settings |

**The interpretation screen is the product's centre.** It must show, per variant:
the classification, the criteria triggered and not triggered, the evidence behind
each with its source and version, what was not evaluated and why, and what failed
to be retrieved.

**The four-status distinction must survive to the UI.** `not_run`, `error`,
`not_found` and `found` are four different claims. Rendering them identically as a
blank cell is the defect GEPER spent weeks removing from its reports and must not
be reintroduced at the last inch.

---

## 26. Persistent storage

**ABSENT for the platform.** `kim_pipeline` has SQLite for run state; GEPER writes
files only.

### 26.1 Requirement

A relational database. The platform's data is highly relational — patients to
orders to samples to runs to interpretations to reports — and lineage queries are
joins.

**Recommendation: PostgreSQL.** Mature, well-understood, good JSON support for
storing run documents alongside indexed columns.

### 26.2 Core tables

```
organisations
users, roles, user_roles
patients
consents
orders
samples
sequencing_runs
vcfs
interpretations
reports
report_approvals
exceptions
audit_entries
```

Every clinical table carries `organisation_id`.

### 26.3 What is stored versus transient

**Persisted:** everything in §26.2, all run documents, all rendered reports, all
audit entries.

**Transient:** in-flight job state, cached lookups, UI session state, anything
reconstructible from persisted data.

**The test:** if losing it after a restart would leave a question unanswerable,
persist it. `kim_pipeline`'s `/tmp` database default failed exactly this test —
and it broke the DELETE collision guard and the startup reconciliation that depend
on it.

---

## 27. Failure states, retries and QC rejection

Covered in §10.6 for submission. Generally:

- **A failure is a state, not a log line.** It has an owner and a resolution.
- **Transient failures retry with backoff; permanent failures do not.** The
  distinction is made explicitly, never by retry count alone.
- **A partial result is never presented as complete.** GEPER's `run_complete`
  marker is the check.
- **Degradation is reported.** A run where an external source failed and was
  retried carries that in its output. **IMPLEMENTED** in GEPER as of
  `service_health` reaching all four surfaces.

---

## 28. Versioning and reproducibility

**IMPLEMENTED in GEPER, and this is a genuine strength.**

Every run document records the code version, resolved model versions (not source
pointers — corrected 2026-09-02), database versions with an explicit
identifiability status, retrieval mode distinguishing live query from cache
replay, and per-model calibration status.

**Requirement for the platform:** store these indexed, so the impact-analysis
query in §11.2 is a query and not a filesystem walk.

**ISO 15189 7.3.3 and 7.6.3 a) `[UNVERIFIED-AGAINST-ISO-TEXT]` both require change control** — a change to a
validated method must be reviewed and a decision recorded before implementation.
The platform must be able to state which version of everything produced a given
report.

---

## 29. FHIR, LIS and EMR integration

**Out of scope for v1, with the boundary stated.**

**IMPLEMENTED:** GEPER has a LIMS export (JSON and CSV), gated on sign-off.
`kim_pipeline` has no LIMS integration at all.

**Boundary:** the platform exposes approved reports through an export interface.
It does not implement FHIR resources, HL7 messaging, or EMR-specific integrations
in v1.

**DECISION when a real integration target exists:** which standard, and whether
the platform pushes or is polled.

---

## 30. Deployment

### 30.1 Model

**DECISION:** hosted by Geper, on-premise at the customer, or both?

This has consequences throughout:
- **Hosted:** Geper holds patient data, becomes a data fiduciary under DPDP, needs
  the full obligation set.
- **On-premise:** the customer holds the data, Geper ships software. Simpler
  privacy position, harder support and update story, and model weights and
  database caches must be distributable.

**Recommendation: on-premise for v1.** Genomic data leaving a hospital is a
significant barrier to adoption in India, and the DPDP position is much simpler
when Geper never holds patient data.

### 30.2 If on-premise

- Must run without internet for the clinical workflow. External lookups
  (ClinVar, gnomAD) need a local cache and an update mechanism.
- Model weights ship with the deployment or are fetched once at install.
- **VENDOR:** which reference databases can be redistributed in an on-premise
  package. ClinVar and gnomAD are permissive; PharmVar is not; HGMD requires a
  commercial licence. This is a licensing question per source and it is already
  partly answered by the licence audit.

### 30.3 Commercial status

**ISO 15189 7.3.2 versus 7.3.3 `[UNVERIFIED-AGAINST-ISO-TEXT]`:** verification checks that a laboratory can
achieve performance the manufacturer specified; validation is required for
laboratory-developed or modified methods.

**If Bij AI ships with stated performance specifications, each adopting laboratory
performs verification rather than full validation.** That is a material reduction
in adoption cost per customer, and it makes performance specifications a
commercial deliverable, not only a regulatory one.

**Blocked on validation** (§32).

---

## 31. Clinical safety and human oversight

### 31.1 The standing position

Bij AI prioritises variants and surfaces them with evidence to a qualified human
interpreter who performs the classification. It produces a draft requiring
qualified human review.

**The platform must never weaken this**, in workflow, in wording, or in default
behaviour.

### 31.2 Concrete requirements

- No report reaches a clinician without an identified Approver's sign-off
- The draft state is visible on every surface, always
- GEPER's uncertainty is preserved, not resolved: not-evaluated criteria,
  unavailable sources and failed lookups are shown as such, never as absence of
  finding
- A disagreement between the interpreter and GEPER is recorded, not overwritten
- **ISO 15189 7.4.1.6 i) `[UNVERIFIED-AGAINST-ISO-TEXT]`:** the report identifies that it derives from a research
  or development programme for which no specific performance claims are available.
  That is Bij AI's exact current status.

### 31.3 Rapid suspension

**ISO 15189 7.4.1.5 d) `[UNVERIFIED-AGAINST-ISO-TEXT]`** requires that automated selection, review, release and
reporting can be rapidly suspended.

**Requirement:** an administrator can suspend automatic submission platform-wide,
immediately, without a deployment. In-flight interpretations complete; no new ones
start; orders queue in a visible state.

---

## 32. What blocks what

### 32.1 Build order

Each depends on the one before.

**Phase 1 — Identity.** Users, roles, organisations, authentication, MFA,
sessions, permission enforcement. Nothing else can be built correctly first,
because nothing else can be attributed.

*Acceptance:* a user authenticates, holds roles scoped to an organisation, and no
query returns data from another organisation.

**Phase 2 — Audit.** Append-only trail attributing every action to an identity.

*Acceptance:* every action in Phase 1 produces an entry; entries cannot be
modified or deleted; the trail is queryable by actor, resource and time.

**Phase 3 — Clinical records.** Patients, consents, orders, samples. The domain
model.

*Acceptance:* an order cannot be placed without valid consent; a sample cannot be
received without an order; PCPNDT constraint enforced (§5.3).

**Phase 4 — Lineage.** The identifier scheme and the chain from patient to report,
including the discovery layer for run documents.

*Acceptance:* given any report, every upstream artefact is retrievable; given a
model version, every affected report is retrievable.

**Phase 5 — Engine integration.** The `kim_pipeline` API client, the GEPER
service wrapper, the automatic submission path with preconditions, idempotency,
retry and the exception workflow.

*Acceptance:* a VCF submitted twice produces one interpretation; a failed
precondition blocks submission and raises a visible exception; a transient failure
retries without duplicating.

**Phase 6 — Review and release.** Interpretation screen, approval, release
control, report rendering with state markings.

*Acceptance:* no report reaches any delivery path without an identified Approver's
sign-off; every delivery path calls one gate function.

**Phase 7 — Amendment, re-analysis and retention.**

*Acceptance:* an amended report references its original and both are retained; a
retention policy executes without manual action.

### 32.2 Blocked on decisions

Phases 3, 6 and 7 cannot complete without:
- The test catalogue and its regulatory scope (§7.2) — **EXPERT**
- The critical/serious boundary setting the CDSCO class — **EXPERT**
- PCPNDT handling for prenatal orders — **EXPERT / legal**
- Retention periods and DPDP erasure interaction — **counsel**
- Consent scopes and language — **EXPERT**
- Approver qualification requirements — **EXPERT**
- Re-derive versus ratify in review (§13.2) — **EXPERT**

### 32.3 Blocked on validation

Nothing in this specification claims GEPER's outputs are correct. **ISO 15189
7.3.3 `[UNVERIFIED-AGAINST-ISO-TEXT]` requires validation**, and no measurement runs have been performed. The
validation study design exists with 25 expert-judgement decisions unfilled.

**The platform can be built before validation completes.** It cannot be used
clinically before it does.

---

## 33. Decisions required from Kislay Prasad

Ordered by what they block.

| # | Decision | Blocks | Recommendation |
|---|---|---|---|
| 1 | Deployment: hosted, on-premise, or both | Everything downstream | On-premise for v1 |
| 2 | Does the platform own patient identity, or consume it from a hospital system | §5, Phase 3 | Platform owns it |
| 3 | Are Orderer and Interpreter distinct roles | §3.1, Phase 3 | Distinct, one person may hold both |
| 4 | May the same person interpret and approve | §3.3 | Permitted but recorded |
| 5 | Cross-organisation referral in v1 | §4.3 | Out of scope for v1 |
| 6 | Patient portal access | §16 | No patient access in v1 |
| 7 | MFA for all roles or privileged only | §23.1 | All roles |
| 8 | Notification channel: in-app only, or email | §18 | In-app for v1 |
| 9 | Re-analysis trigger: manual, scheduled, or evidence-driven | §15.3 | Manual for v1, lineage built for later |
| 10 | Read-only feed for external billing | §20 | Not v1 |
| 11 | Does urgent priority mean anything operationally | §7.1 | — |
| 12 | Concurrent session policy | §23.2 | — |

---

## 34. What this document does not do

It does not specify the test catalogue — that is a regulatory artefact requiring
expert input.

It does not set any clinical threshold.

It does not claim Bij AI is validated, certified, production-ready, or safe for
clinical use. It is none of those things today.

It does not replace the validation study design, which remains the document that
would establish whether GEPER's outputs are correct.

---

**End of specification.**

*Corrections to this document should be made against the codebase, not against
recollection. Where a claim here conflicts with the code, the code is right and
this document is wrong.*
