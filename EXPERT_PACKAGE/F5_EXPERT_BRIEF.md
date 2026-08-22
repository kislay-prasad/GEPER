# F5 Expert Brief — Ranking Integrity and Arm A Safeguard Asymmetry

**Context:** §5.9 study design established F5 (rank-ordering integrity across pathogenic/benign boundary) as a validation anchor for S1. This brief documents two findings that shape how F5 is weighted and sufficient.

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

---

## Finding 1b: Independent code corroboration (strongest evidence available)

The codebase found this exact hole independently, by a route none of us took, in the same terms.

**`prioritization_engine.py:184-193` (verbatim):**

> "review priority was previously driven purely by this engine's own evidence-completeness factors, which are near-orthogonal to whether GEPER's classification disagrees with an expert-panel/practice-guideline ClinVar record (a synonymous or otherwise evidence-sparse variant scores low on Protein Impact/Conserved Domain/Splicing regardless of how urgent the classification disagreement itself is) -- so a Critical clinical conflict could rank **below** findings with no conflict at all."

**This closes the subsumption question outright.** Angela's concern is not speculative. 1a does not cover ranking. F5 fills a real structural gap, validated by two independent discoveries.

**Governance value:** This is the strongest validation signature available on this board — a finding that had to pass a test that could have failed, backed by independent code documentation of the same defect.

---

## Finding 2: Arm A rank-safety mechanism cannot fire

**The stated property:** A rank-level protection mechanism exists that floors review category at Critical when `critical_conflict` is set (prioritization_engine.py:194-199). This protection requires agreement-conflict with an expert-panel/practice-guideline ClinVar record.

**The structural gap:** Arm A is defined as the **ABSENCE** of exactly that record (§1.3, per EJ-01).

**What this means:** The one rank-safety mechanism that exists cannot fire in the primary arm by construction.

---

## The asymmetry

F2 forbids Arm B rescuing Arm A (re-scoping closure). So:

| Arm | ClinVar record | Rank-safety mechanism | Classification-level protection |
|---|---|---|---|
| **B** (with expert-panel/guideline conflict markers) | Present | Fires ✓ | F1 catches inversions |
| **A** (no expert-panel/guideline markers) | Absent | **Disabled by construction** | F1 only (structurally blind to ranking inversions) |

**This property belongs in the expert package** — alongside §5.9, the trilemma, and F5 itself — because it is a stated asymmetry between the two arms of the study. The validation design must be transparent about what differs.

---

## What this means for EJ-25

EJ-25 (recall criterion for ranked output: the review-queue depth K and minimum fraction of truth-P/LP variants that must appear within top K) depends on what F5 is sufficient to protect.

- **If F5 is sufficient for both arms:** EJ-25 applies uniformly to both.
- **If F5 is insufficient for Arm A (one fewer safeguard):** Either (1) Arm A requires additional rank-safety mechanism (engineering + validation), or (2) this trade-off is documented and accepted at expert level.

**Remedy question:** Open. Stays with you.

---

## References

- §5.9 (Study-level failure declaration): lines 1876–2278 in VALIDATION_STUDY_DESIGN.md @ 2bc81b6409cc88563a9c91c20f315abd3fd64a56 (verified 2026-08-22 13:40)
- §5.9.8 (F5 detail): lines 2016–2162
- prioritization_engine.py:184-199 (code corroboration): in-repo verification
- 1a falsifier: VALIDATION_READINESS_RUBRIC.md @ 89bf125 (verified)
