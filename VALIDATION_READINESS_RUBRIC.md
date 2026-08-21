# GEPER Clinical-Validation-Readiness Rubric

**Version:** 1.0.0-rc1
**Status:** PROPOSED -- awaiting human sign-off. Not yet used to score anything.
**Owner:** Angela (clinical-validation-readiness).
**Baseline tag:** `validation-baseline-2026-08-21` @ `470a993`. Any future score
must state which commit/tag of GEPER it was measured against, in addition to
which rubric version produced it.
**Supersedes:** the 2026-08-08(ish) forensic audit that produced "28% readiness"
-- that rubric is unrecoverable (searched exhaustively: every `.md` in this
repo, the hive's board/tasks, `git log --all`, the shared knowledge graph; one
mention of the number exists anywhere, with zero criterion-level breakdown
behind it). A score under this rubric is not a successor number to 28% and
must never be cited as one.

---

## 0. What this document is for

A readiness score is a measurement. A measurement is only a measurement if a
different auditor, reading the same repo, reaches the same number. That
property is what the original rubric lacked, and it is the entire reason this
document exists.

This rubric will be used, once signed off, to score GEPER's readiness for
clinical validation. It does **not** itself constitute a validation study. Two
days of work preceding this rubric established that the pipeline no longer
**misstates** what it observed (review status, consent, QC, evidence
provenance, error classification). That work has **not** established that
what it observes is **correct**. Domain 1 exists specifically to keep that
line visible instead of letting the rest of the rubric blur it.

---

## 1. Evidence tiers

Four tiers. Three carry credit; the fourth exists so an assertion is never
mistaken for one of the three.

| Tier | Name | Definition |
|---|---|---|
| **A** | Evidenced | A persisted, automated check exists that would go RED if the property broke, re-runnable by someone other than the person who built the fix, from the repo alone. |
| **B** | Verified-not-persisted | A real check was performed once and is described precisely enough to redo, but nothing in the repo re-checks it on the next commit. |
| **C** | Documented | A decision or boundary is written down with rationale. Correct and **sufficient** for a criterion that is asking "was a decision made and recorded" (a license citation, a design-boundary ruling). **Insufficient alone** for a criterion that is asking "does this behave correctly" -- a paragraph is not a substitute for an assertion when the thing being claimed is behavioral, not editorial. |
| **D** | Asserted | Claimed in a report, a card, or a docstring, backed by **neither** a document **nor** a test. Always scores **zero**. |

**On the human's three vs. my four:** the human named three tiers --
evidenced-by-test, evidenced-by-manual-verification, documented-only. Read
together with their own ruling on this rubric's purpose (a rubric that cannot
name an assertion will round it up to documentation), I read their three as
*the three tiers that can carry credit*, not as an instruction to delete Tier
D. Tier D is not a fourth grade of credit -- it is the null case, and it has
to be nameable or every unearned claim in a future audit quietly becomes a
Tier-C "documented" one by default, which is the exact failure mode this
rubric exists to prevent. Kept explicitly; flagged here rather than silently
resolved so the human can override it directly if my reading is wrong.

Each sub-criterion below states its **expected tier** in advance -- decided
now, not chosen after looking at what currently exists -- and a **satisfier**
(what evidence at that tier looks like) and a **falsifier** (the specific
observation that would fail it). The falsifier is the harder half and the one
newly required: "a persisted test exists" is satisfiable by a test that
cannot fail, which is the exact defect class the last two days were spent
removing. A criterion without a falsifier is not yet a criterion, it's a
wish.

---

## 2. Scoring mechanics -- tiers are never summed without the breakdown

**A score is not a single percentage.** Any presentation of a readiness score
under this rubric must show the weighted-point split across all four tiers,
not just their sum. This is the artifact that makes citing a score correctly
the path of least resistance, not a rule that depends on someone remembering
it.

**Correct citation format:**
> Readiness under rubric v1.0.0 @ `<commit-or-tag>`: **A=38.5 / B=9.0 / C=12.0
> / unmet(D+unscored)=40.5** (of 100 weighted points). Domain breakdown:
> [table or link].

**Incorrect, and not to be produced by any tool or report this rubric feeds:**
> "GEPER is 62% ready."

A single blended percentage may appear **only** alongside its tier split in
the same breath, never standing alone in a header, a Slack message, a card
title, or a report footer. If a future dashboard or script renders this
rubric's output, the tier split is not an optional footnote -- it is the
return value; a bare sum is a bug in that tool, not a valid alternate
presentation.

Weighted points: each sub-criterion below carries `domain_weight x
sub-criterion_weight_within_domain` points out of 100 total. A sub-criterion's
points are awarded entirely to the tier its current evidence actually
reaches (not split or interpolated): Tier A reached -> full points to the A
column; Tier B reached -> full points to the B column; Tier C reached (and C
is that criterion's own sufficient tier) -> full points to the C column; C
reached but A/B was required -> **zero**, that criterion is unmet, because
documentation was offered where behavior was asked for; Tier D or nothing ->
zero, unmet.

**On sub-criterion weights:** held back in my first proposal for a separate
sign-off pass. I now think they belong in this pass instead, and am including
them below. Reasoning: writing a falsifier for each sub-criterion forced the
same precision a weight decision needs anyway, so deferring weights no longer
saves a genuinely separate round of judgment -- it just re-opens the document
later for something I already had to think through to write the falsifiers.
The domain weights (unchanged from my first proposal) are the bigger
judgment calls; the within-domain splits below are comparatively mechanical
once the falsifiers exist. Flagging the ones I'm least confident about inline.

---

## 3. The eight domains

### D1. Analytical & Clinical Validation -- weight 30

The axis that was unscored before this rubric existed. Currently sits at
**zero across all three sub-criteria** -- stated as unmet, not omitted, and
deliberately the largest number in the whole rubric so nothing below it can
inflate the headline.

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 1a | Concordance against a known-truth variant set | 15 | A | A persisted, re-runnable comparison of GEPER's ACMG classifications against a named truth set (e.g. a GIAB-derived panel, or an internal curated ground-truth set with documented provenance), with stated sensitivity/specificity/PPV and a pass threshold agreed in advance. | Any variant in the truth set where GEPER's classification and the truth set's expected classification disagree across a Pathogenic/Likely-Pathogenic vs Benign/Likely-Benign boundary is an automatic fail, no tolerance. VUS-boundary drift beyond a pre-agreed count also fails. **No truth-set comparison exists at all is itself a falsifying observation** -- current state. |
| 1b | Inter-run reproducibility | 10 | A | The same VCF input, run repeatedly (or run through both `geper/` and `kim_pipeline`'s independent stacks for a variant both can classify), produces an identical classification, an identical `criteria_met`/`criteria_unknown` set, and confidence scores within a stated numeric tolerance -- persisted as a test. | Any two runs of byte-identical input produce a different classification, a different criteria set, or a confidence delta outside the stated tolerance. |
| 1c | External QA / proficiency-testing panel | 5 | A | Documented participation in, or scored comparison against, an external PT panel (e.g. CAP, GenQA, or an equivalent scheme) with a result meeting the panel's own passing threshold. | No external panel result exists, **or** one exists but concordance with panel consensus falls below the panel's own threshold. Flagging for the human directly: this sub-criterion may need a "not yet applicable, plan on file" state distinct from "failed" if no such relationship currently exists to even attempt -- that's a scoring-semantics call for them, not one I should make unilaterally. |

### D2. ACMG Evidence Integrity -- weight 15

Does the interpretation engine ever let a failure, an uncalibrated model, or
a lookup crash masquerade as real evidence.

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 2a | Uncalibrated-model disclosure reaches the evidence text | 4 | A | Model name / calibration status / clinical-validation status is present adjacent to any criterion evidence derived from an uncalibrated model, verified by a test that calls the real production evidence-builder (not a hand-authored fixture). Current: MET at A (`test_bp7_spliceformer_disclosure.py`, calls the real `ACMGRuleEngine._bp7`). | Any criterion output derived from an uncalibrated model (SpliceFormer, SpliceBERT, or any future AI-model plugin) appears in a rendered report without the disclosure substrings, or with the disclosure present but not colocated with the evidence it describes. |
| 2b | Lookup-failure sentinel integrity | 4 | A | For every external lookup feeding an ACMG criterion, an exception or unavailable path returns a state distinguishable from, and never defaulting to, a confirmed positive or negative -- backed by a test asserting on the sentinel itself, not a downstream consumer whose own guard might mask the bug. Current: MET at A for gnomAD (`test_gnomad_lookup_failure_not_absent.py`). Not yet independently verified for every other lookup (ClinVar, constraint/hotspot, PS1/PM5) -- those currently use the correct `None` pattern per my own prior read of `orchestration/shared.py`, but none of the three has its own dedicated regression test the way gnomAD now does. Scoring this as MET at A only for the specific lookups with a persisted test; the domain total below reflects a partial score, not a full one, until the others are covered the same way. | Any lookup's exception or unavailable path produces a criterion status indistinguishable from (or silently defaulting to) a confirmed answer -- the exact shape of the original gnomAD defect, in any lookup, old or new. |
| 2c | Error-classification honesty in model loading | 4 | A | Model-load failures propagate with their real exception class and message when the cause is local; only genuine network/HTTP failures are generalized to a sanitized "model unavailable," and this distinction is backed by a persisted test per plugin (not manual verification at review time). Current: tier B -- the fix (41935a2, committed) is real and I verified it directly, but no dedicated regression test exists yet; Pam is writing one. Scored at B, not A, until that test lands. | A plugin's exception handler catches bare `OSError` (or an equivalent overbroad catch) under a network-shaped label, **or** a local resource-exhaustion error is relabeled "model unavailable" with no trace of the real cause surviving at a visible (non-debug) log level. |
| 2d | This domain's own supporting tests are not cannot-fail | 3 | A | A persisted, periodically re-run sweep confirms every test backing 2a-2c is not proxy-pinned, logic-duplicating, non-discriminating, or fixture-masked, re-run whenever a supporting test file changes. Current: tier B -- I ran this sweep once, by hand, and found the four previously-known instances genuinely fixed plus one new live finding (`test_gap_fixes.py`'s suite-wide collection-abort risk) -- real, but not itself a repo artifact that reruns automatically. | Any test backing 2a-2c is found to pass identically whether or not the defect it claims to guard is present, **or** the sweep has not been re-run since the last change to a file it covers. |

### D3. Governance & Chain of Custody -- weight 12

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 3a | Review-status single source of truth | 4 | A | Every rendered output (PDF, Markdown, JSON, LIMS export) derives "reviewed" state from the same document field; no renderer accepts an unrelated free-text field as a proxy. Current: MET at A (0fdf874 + 3 test files). | Any two output surfaces for the same document disagree on reviewed / draft / overridden status, or any renderer's review-state claim can be changed by an input other than the real sign-off record. |
| 3b | Consent single source of truth | 4 | A | Same shape as 3a for `patient_consent`. Current: MET at A (2cc57a6 + `test_consent_metadata.py`). | Any two output surfaces disagree on consent status for the same document. |
| 3c | Run-level caveat parity | 4 | A | A degraded-run caveat (e.g. an unreachable data source) appears in every shipping output format when triggered. Current: MET at A (`test_export_lims_caveats.py` + JSON/Markdown pins). | The caveat appears in fewer than all shipping output formats for the same triggering run condition. |

### D4. QC & Data Traceability -- weight 10

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 4a | QC durability through storage and re-render | 6 | A | QC metrics persist on the document and survive a sign-off re-render (approve/override) into every renderer. Current: MET at A (`test_qc_metrics_durability.py`, exercises both branches through a real `approve()`). | A re-render of a document that had real QC metrics ever produces the "no QC observed" text instead of the real values, in any renderer. |
| 4b | QC accuracy against the source instrument/tool output | 4 | A | The QC values a report shows match the actual upstream tool's own output (e.g. samtools/fastqc) exactly or within a stated, pre-agreed rounding tolerance, backed by a persisted comparison test. **Currently UNSCORED, not zero-by-default** -- I have verified 4a (durability/parity) directly but have not personally checked whether the numbers themselves are ever compared back against the tool that produced them. Flagging this as a gap in my own audit coverage rather than assuming a tier because a neighboring sub-criterion is strong. | Any rendered QC number differs from the source tool's own reported value beyond the stated tolerance. |

### D5. Documentation & Provenance -- weight 10

Tier C is the **correct and sufficient** expected tier for every sub-criterion
in this domain -- deliberately different treatment from D2-D4. A license
citation cannot become "more evidenced" by adding a test; there is nothing to
test. This is also the domain where the human's finding from my last audit
("one criterion was documentation wearing an evidence label") lives -- 5c's
falsifier exists specifically because of that finding.

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 5a | AI-model license audit currency | 4 | C | `LICENSE_AUDIT.md` accurately reflects every currently-integrated model's license, each re-verified against a primary source. | Any currently-integrated model is absent from `LICENSE_AUDIT.md`, or a documented license claim contradicts its primary source when re-checked. |
| 5b | Data-source license audit currency | 3 | C | `DATA_SOURCE_LICENSE_AUDIT.md` accurately reflects every currently-integrated external data source's license. | Any currently-integrated data source is absent from the audit doc, or a documented claim contradicts its primary source when re-checked. |
| 5c | Design-boundary decisions are recorded, not merely claimed | 3 | C | When a human rules an omission or a behavior intentional, that ruling exists in writing with rationale (e.g. the LIMS export boundaries, 13c458e), reachable from the component it governs. | A component's absence or behavior is described anywhere (a report, a card, a docstring) as "intentional" or "by design" with **no** corresponding written decision record to point to -- an assertion wearing documentation's clothes, which is Tier D regardless of how confident the surrounding prose sounds. |

### D6. Security & Supply-Chain Integrity -- weight 10

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 6a | RCE-shaped surfaces are pinned | 4 | A | Every `trust_remote_code=True` (or equivalent) load resolves to a specific, reviewed commit SHA, not a floating/default branch ref, with an in-code review note. Current: MET at A (DNABERT-2, committed). | Any `trust_remote_code=True` load point resolves to a floating ref rather than a pinned, reviewed commit. |
| 6b | No unresolved commercial-license conflict | 3 | A (closure) + C (record) | No active integration has an unresolved non-commercial or copyleft-conflict license; any past conflict has both the offending code removed/gated **and** a decision record. Current: MET (OMIM retired, 51558c1/bd86103, with a decision record matching the IndiGenomes precedent's rigor). | An integration is found whose upstream license forbids commercial use or redistribution and no decision record or code change addresses it. |
| 6c | Auto-install/pip safety against the shared interpreter | 3 | A | Package auto-install is either sandboxed (isolated venv / separate site-packages per the pattern Andy already uses) or made safe against interruption (no live `pip install` shelled against a shared interpreter mid-test-run; any install step is atomic against a kill). **Currently UNMET at any tier** -- newly surfaced by today's floor-wide pip advisory; nothing in the repo addresses this yet. | Any test run or pipeline run can be observed shelling real `pip install`/`uninstall` against the shared global interpreter. **This falsifier is already met as of today** -- confirming the unmet score, not a hypothetical. |

### D7. Operational Resilience -- weight 8

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 7a | Retry before latch | 4 | A | A single failed health probe does not latch a service offline; only repeated failures within a stated window do. Current: MET at A (5094620, `test_service_health.py`). | A single probe failure latches a service offline for the remainder of a run. |
| 7b | Bounded reprobe / recovery | 4 | A | A latched-offline service that genuinely recovers is un-latched within a bounded number of attempts or elapsed time, without every remaining call paying a full timeout, backed by a passing test. **Currently FAILS its own falsifier** -- `test_service_health_bounded_reprobe_pending.py`'s three tests are deliberately RED (the feature does not exist yet; 6a52384 only instruments latency/latch state to size the feature later). Scored unmet, not partial -- instrumentation towards a fix is not the fix. | A genuinely-recovered service remains latched offline for the rest of a run. Currently true; the falsifier is presently observed. |

### D8. Test-Suite Integrity -- weight 5

Small weight, load-bearing role: every Tier-A claim elsewhere in this rubric
is only as trustworthy as this domain being sound.

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 8a | Full-suite cannot-fail sweep, repeatable | 5 | A | A persisted, periodically re-run sweep across the full test suite (both `geper/` and `kim_pipeline/`) for proxy-pinned, logic-duplicating, non-discriminating, and fixture-masked tests, with every flagged instance either fixed or explicitly accepted with recorded rationale. Current: tier B -- my sweep was real, manual, and covered the highest-risk files, but is a one-time pass, not a re-runnable artifact, and explicitly did not cover all ~2900 tests across both suites. | A test is found, anywhere in the suite, where no change to the production path it claims to guard could make it fail, **and** it is not already flagged/tracked. (`test_gap_fixes.py`'s suite-wide collection-abort risk is exactly this domain's own open item as of today.) |

---

## 4. Weighted points and unscored state, current snapshot (informational, not a score)

This section is **not a score** -- boundaries for this document are proposal
only, nothing is being measured yet. It exists purely to show the mechanics
in section 2 are usable in practice: of the 47 sub-criterion points assigned
above outside D1 (D2-D8 sum to 70; 30 is D1), several are already known
Tier-A, several Tier-B, several Tier-C-sufficient, several explicitly unmet or
unscored-pending-audit (4b). D1 alone accounts for 30 of the 100 points and
is entirely unmet. A real scoring pass, once this rubric is signed off, would
walk every row above and produce the A/B/C/unmet point table section 2
requires -- I have not done that pass here, by design, per the phase rule.

---

## 5. Open items for the human, named rather than smoothed

1. **D1's 30-point weight is a judgment call, not a derivation** -- carried
   forward unchanged from the first proposal, still true.
2. **1c (external QA panel)** may need a "not yet applicable, plan on file"
   state distinct from "failed" -- a scoring-semantics decision, not mine to
   make unilaterally.
3. **D8 as its own domain vs. a cross-cutting multiplier** on every other
   domain's Tier-A credit -- I chose legibility over rigor; a multiplier is
   more theoretically correct but harder to audit by eye.
4. **5c's tier-C-sufficient treatment** means two sub-criteria can both read
   "fully met" while one has a CI-enforced test and the other has a
   well-written paragraph -- correct by this rubric's own design, but a
   reader skimming only a weighted total (never the tier split, per section
   2) could still miss which is which without opening the domain table.
5. **The three-vs-four tier question**, addressed in section 1 -- keeping D
   unless told otherwise.
6. **Sub-criterion weights are now included in this pass** rather than held
   for a separate round -- said in section 2 with reasoning; reversible if
   the human would rather decouple them.
