# Cross-tree divergences

`geper/` and `kim_pipeline/` are separate implementations living in one
repository. Each is internally coherent and each documents its own
choices in its own tree. This file records the places where they
**deliberately disagree**, because a choice that is well documented
inside one tree is invisible from the other.

**This is metadata, not a defect list.** Nothing here is a bug report and
nothing here is a proposal. Every entry describes behaviour that is
intentional on both sides.

**The risk these entries describe is not realised while the trees are
used separately.** It is realised when they are **compared,
benchmarked, or described to an outside party as one product** — at
which point a divergence that neither tree considers remarkable can move
a classification.

---

## 1. PP5 / BP6 — ClinVar's own classification as ACMG evidence

ClinGen's SVI Working Group (Biesecker & Harrison 2018) recommended
against PP5/BP6, on the grounds that letting a source's own
classification feed an independent ACMG/AMP evaluation is circular. The
two trees responded differently, and **`geper/` responded differently to
the two criteria**, which is the part most easily missed.

| | PP5 (pathogenic-supporting) | BP6 (benign-supporting) |
|---|---|---|
| **`geper/`** | **Never evaluated.** `_not_evaluated`, with the ClinGen SVI recommendation as its stated reason. There is no code path in which PP5 contributes anything. | **Evaluated, and it can trigger.** It appears in `all_criteria` / `triggered_criteria` and in the rendered trace — but it is **skipped in the point-combining loop**, so it contributes **zero** to `pathogenic_points` / `benign_points`. |
| **`kim_pipeline/`** | **Evaluated and scored, ON by default.** `disable_pp5_bp6` defaults to `False`; labs wishing to follow the SVI recommendation opt out via `acmg_thresholds.disable_pp5_bp6: true`. | **Evaluated and scored, ON by default.** Same switch, same default. |

Sources: `geper/pipeline/acmg_rules.py` (`_not_evaluated` for PP5; `_bp6`
plus the `if code == "BP6": … continue` branch in the point-combining
loop); `kim_pipeline/pipeline/acmg/classifier.py` (`_disable_pp5_bp6`,
defaulting `False`, documented there as preserving existing behaviour and
test expectations).

### Consequence

**The same variant can pick up PP5 pathogenic-supporting evidence in
`kim_pipeline/` and not in `geper/`.** Both trees are behaving as
designed and neither is wrong on its own terms.

### The part that specifically bites a benchmark

**`geper/` reports BP6 as triggered while scoring it as zero.** A
comparison that joins the two trees on their *triggered criteria* — the
obvious key, and the one a reviewer would reach for first — will show
BP6 present in both trees and conclude they agree, when in fact it moves
the classification in only one of them.

So the two natural comparisons disagree with each other:

- **compare triggered criteria** → BP6 looks like agreement
- **compare contributions to the classification** → BP6 is a divergence

Anyone benchmarking these trees should compare **what reached the point
totals**, not what appears in the criteria list. `geper/` states the
exclusion in its own trace text (*"BP6 triggered, but excluded from point
totals"*), so the information is present in a single tree's output — it
is only the cross-tree reading that loses it.

---

## 2. PP3 / BP4 — how conflicting computational predictors resolve

Recorded here as a pointer, because it is under active decision and the
detail lives with that decision rather than here.

- **`geper/`** — evaluates PP3 and BP4 together from one shared scan and
  applies *"any dissent blocks the claim"*: if both a damaging and a
  benign signal are present, **both** criteria report `not_triggered`.
  Its docstring explicitly **considered and rejected** majority voting,
  citing ACMG/AMP 2015, Pejaver et al. 2022, and consistency with `_bp7`.
- **`kim_pipeline/`** — majority vote among predictors that had data.

Both trees satisfy "one predictor may suffice" and "PP3 and BP4 never
both fire"; **neither is invariant to a predictor being removed.** This
one is **not** settled: it has been referred to a clinical expert,
precisely because overriding a written, cited rationale is a
clinical-genetics judgement rather than an implementation choice.

---

## Scope of this file

Divergences in **ACMG criterion behaviour** — cases where the same
variant can end up with different evidence or a different classification
depending on the tree. It is not a general feature-comparison of the two
pipelines, and it is not a list of things to reconcile.

**Adding an entry does not propose fixing it.** Several of these
divergences are the correct outcome of two teams reasoning carefully
about the same question and reaching different defensible answers. The
purpose of writing them down is that **nobody discovers them during a
comparison, a benchmark, or an audit.**
