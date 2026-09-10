# F5 Expert Brief — Ranking Integrity and Arm A Safeguard Asymmetry

**Context:** §5.9 study design established F5 (rank-ordering integrity across pathogenic/benign boundary) as a validation anchor for S1. This brief documents two findings that shape how F5 is weighted and sufficient.

**Anchors added 2026-09-10, in response to a finding that the previous version of this brief carried zero regulatory/standard citations across its seven sections.** Every section below now states, explicitly, what standard or clause its question arises from — **or that none exists**, when that is the honest answer. Where a citation would need the licensed ISO 15189:2022 text and that text has not been purchased (still outstanding, per `VALIDATION_STUDY_DESIGN.md` §0.3), that is stated rather than either omitted or asserted as checked. **No wording below is offered as ISO's own text; where ISO is discussed, only the fact that a clause has or has not been verified is stated, never a paraphrase presented as a quotation.** None of the four questions this brief raises is answered here — this pass adds only anchors and one-sentence asks.

---

## Finding 1: F5's subsumption test was run and found a real gap

**The test:** Could 1a's falsifier already subsume F5's concern? Does classification→rank map decompose such that F1 catches rank inversions?

**Three independent routes to suspicion:**
1. §5.9.6: S2 has no F1-equivalent (gap exists at rank level, not classification level)
2. §5.1.1 Statement B: F1 "has no defined equivalent for a ranking output"
3. Angela's precision-vs-recall question against 1a

**Test run:** Does rank = f(classification)?

**Verified result:** No. Rank is a weighted sum of 11 factors. ACMG classification is one. Multiplicative penalty applied after. Many-to-many map.

**Consequence:** Classification→rank is not decomposable. A variant GEPER classified **correctly** as Pathogenic, ranked below review cutoff, never reaching a human, produces **zero F1 events** (not few; zero) because the one quantity F1 inspects is **right** in exactly that failure. **F1 is structurally blind to ranking inversions.**

> **Anchor: there is no regulatory or standards citation for this finding, and there should not be one manufactured.** F1 and F5 are both defined in `VALIDATION_STUDY_DESIGN.md` §5.9, and that section opens with an explicit, self-applied marker: *"[INVENTED] — all of §5.9. No external source supplies a study-level failure declaration for a variant-interpretation validation; this is the same principle the rest of the document applies to individual tests, raised one level."* (§5.9, verified against the current file this session.) This finding is a test of our own internal validation-design machinery against our own internal rubric (`VALIDATION_READINESS_RUBRIC.md`'s 1a falsifier, cited by commit `89bf125`) — not a test against any external clause. **What is actually being asked of the expert here:** nothing yet — this section is background for the question in "What this means for EJ-25" below; read it as evidence, not as a request.

---

## Finding 1b: Independent code corroboration (strongest evidence available)

The codebase found this exact hole independently, by a route none of us took, in the same terms.

**`prioritization_engine.py:184-193` (verbatim):**

> "review priority was previously driven purely by this engine's own evidence-completeness factors, which are near-orthogonal to whether GEPER's classification disagrees with an expert-panel/practice-guideline ClinVar record (a synonymous or otherwise evidence-sparse variant scores low on Protein Impact/Conserved Domain/Splicing regardless of how urgent the classification disagreement itself is) -- so a Critical clinical conflict could rank **below** findings with no conflict at all."

**This closes the subsumption question outright.** Angela's concern is not speculative. 1a does not cover ranking. F5 fills a real structural gap, validated by two independent discoveries.

**Governance value:** This is the strongest validation signature available on this board — a finding that had to pass a test that could have failed, backed by independent code documentation of the same defect.

> **Anchor: code citation only, not a regulatory citation, and none is owed here.** This finding's evidence is a direct quotation from `geper/pipeline/prioritization_engine.py` (not from any audit document — audit documents have been wrong about code three times today), cited to the file and line range above. It corroborates Finding 1; it does not carry an independent regulatory question. **What is actually being asked of the expert:** nothing yet — same status as Finding 1.

---

## Finding 2: Arm A rank-safety mechanism cannot fire

**The stated property:** A rank-level protection mechanism exists that floors review category at Critical when `critical_conflict` is set (prioritization_engine.py:194-199). This protection requires agreement-conflict with an expert-panel/practice-guideline ClinVar record.

**The structural gap:** Arm A is defined as the **ABSENCE** of exactly that record (§1.3, per EJ-01).

**What this means:** The one rank-safety mechanism that exists cannot fire in the primary arm by construction.

> **Anchor: same as Finding 1 — none exists, and Arm A/B's own definition is tagged the same way.** `VALIDATION_STUDY_DESIGN.md` §1.3 defines Arm A/B and marks its own reasoning `[GUIDANCE, corroborated in-repo]` for what it rejected and `[INVENTED]` for what it added (the paired ablation arm C and the curator-provenance sub-stratum) — no external standard specifies how to stratify a ClinVar-independence arm for this kind of validation. The safety-mechanism code itself (`prioritization_engine.py:194-199`) is a verified fact about our own software, not a regulatory requirement. **What is actually being asked of the expert:** nothing yet — this section states a structural fact the next two sections draw a consequence from.

---

## The asymmetry

F2 forbids Arm B rescuing Arm A (re-scoping closure). So:

| Arm | ClinVar record | Rank-safety mechanism | Classification-level protection |
|---|---|---|---|
| **B** (with expert-panel/guideline conflict markers) | Present | Fires ✓ | F1 catches inversions |
| **A** (no expert-panel/guideline markers) | Absent | **Disabled by construction** | F1 only (structurally blind to ranking inversions) |

**This property belongs in the expert package** — alongside §5.9, the trilemma, and F5 itself — because it is a stated asymmetry between the two arms of the study. The validation design must be transparent about what differs.

> **Anchor: F2 (Arm B may not rescue Arm A) is this document's own study-failure rule, §5.9.2, and carries the same `[INVENTED]` provenance as the rest of §5.9** — stated there as a design choice to prevent a failing primary arm being reported alongside a passing assisted arm, not derived from any external requirement. **What is actually being asked of the expert:** nothing directly in this table — it sets up the "What this means for EJ-25" question below.

---

## What this means for EJ-25

EJ-25 (recall criterion for ranked output: the review-queue depth K and minimum fraction of truth-P/LP variants that must appear within top K) depends on what F5 is sufficient to protect.

- **If F5 is sufficient for both arms:** EJ-25 applies uniformly to both.
- **If F5 is insufficient for Arm A (one fewer safeguard):** Either (1) Arm A requires additional rank-safety mechanism (engineering + validation), or (2) this trade-off is documented and accepted at expert level.

**Remedy question: Open. Stays with you.**

> **Anchor, stated precisely because this is the one place adjacent regulatory guidance exists and was deliberately not used — not an oversight, a documented decision.** `VALIDATION_STUDY_DESIGN.md`'s own EJ-25 guidance block (§5.9.8) reads, verbatim: *"No published guidance sets a recall-at-K for a clinical variant review queue... Richards et al. 2015 and CLIA §493.1253(b)(2) [both VERIFIED — see EJ-19, EJ-08] define analytical sensitivity for an assay whose output IS the answer; a ranked queue read to depth K is not that, and citing them here would be the verified-but-off-target error EJ-03a names. [ANALOGY] — recall@K is borrowed from information retrieval, and the borrowing is the tag."* **In plain terms: Richards et al. 2015 (the ACMG/AMP classification guideline) and CLIA §493.1253(b)(2) are real, fetched, verified sources — but this document's own author concluded, and states outright, that citing either one as the standard governing a ranked-review-queue's recall obligation would misapply a real citation to a case it was not written for.** This is a different and more specific finding than "no citation was found" — a citation was found, considered, and ruled inapplicable, and the reasoning for that ruling is available in full at the location cited. **Separately, no ISO 15189:2022 clause was checked against this question at all** — the standard remains unpurchased and paywalled (`VALIDATION_STUDY_DESIGN.md` §0.3), so its silence here is an absence of an attempt, not a checked absence. **What is actually being asked of the expert, in one sentence: given that no published guidance defines a recall threshold for a ranked (rather than binary) clinical variant output, what review-queue depth K and what minimum fraction of truth-positive variants within it should this validation require, and does that requirement need to differ between Arm A and Arm B given the safeguard asymmetry above?**

---

## References

- §5.9 (Study-level failure declaration): lines 1876–2278 in VALIDATION_STUDY_DESIGN.md @ 2bc81b6409cc88563a9c91c20f315abd3fd64a56 (verified 2026-08-22 13:40)
- §5.9.8 (F5 detail): lines 2016–2162
- prioritization_engine.py:184-199 (code corroboration): in-repo verification
- 1a falsifier: VALIDATION_READINESS_RUBRIC.md @ 89bf125 (verified)

**Anchor verification note, added 2026-09-10:** the line ranges above were not re-verified against the current tree in this pass — this pass added regulatory/provenance anchors to the existing findings and did not re-audit the pre-existing References section's own line numbers, which is a separate check (the same kind of drift the zygosity card's citation suffered once already, per `board.md`). If this brief is read alongside a `VALIDATION_STUDY_DESIGN.md` at a commit other than `2bc81b6`, the section numbers (§5.9, §5.9.8) are the more durable reference; the line numbers may have moved.

**What no section of this brief cites, and why that is stated rather than left silent:** no clause of ISO 15189:2022, no NABL clause, and no CDSCO/MDR provision appears anywhere above. That is not an omission this pass corrected — it is this pass's finding. Every question in this brief (Findings 1/1b/2, the asymmetry, and EJ-25) traces back to `VALIDATION_STUDY_DESIGN.md`'s own internally-reasoned validation-design machinery (§5.9, §1.3), which that document itself marks `[INVENTED]`/`[GUIDANCE]`/`[ANALOGY]` rather than attributing to any external standard — because, on the document's own admission, no external standard addresses study-level failure declarations, ranking-output safety mechanisms, or ranked-queue recall thresholds for a variant-interpretation validation. An expert reading this brief should treat F5, F2, and EJ-25 as **this floor's own methodological judgement calls**, open to being overturned on their merits, not as applications of an external requirement they could instead go verify. The one place an external, verified-but-inapplicable citation exists (EJ-25, above) is called out precisely so the expert does not independently rediscover Richards 2015 / CLIA §493.1253(b)(2), assume they were missed, and apply them where this document already reasoned they do not fit.
