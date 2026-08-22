# REVIEW_MANIFEST — 72aec5a landing verification

**Written 2026-08-22. Corrected 2026-08-23.**

---

## CORRECTION 2026-08-23 — the original list was FALSE WHEN WRITTEN. Read this before the list.

This manifest's job is to establish what shipped. It did the opposite: **7 of the 10 files it
listed as "landed in 72aec5a" were never in that commit.** Found by Angela during a Phase 2
artifact audit; each entry re-verified against `git show --name-only 72aec5a` before this
correction was written.

The original text is preserved below rather than deleted. A manifest silently corrected is a
manifest nobody can trust the history of.

**The entry that mattered most:** `kim_pipeline/pipeline/acmg/classifier.py` was listed as having
landed in 72aec5a. **It did not.** That file carries the unratified `_MIN_CONCORDANT_PREDICTORS = 2`
change, which is **deliberately held uncommitted in the working tree** pending a clinical expert's
ruling. Its last commit is `c7b9859`, the *revert* of d1128ee.

A reader trusting this manifest would have concluded the ≥2 rule was on master. That is precisely
the false belief `d1128ee` created and that a full night was spent reverting — reproduced here, in
the artifact whose purpose is recording what shipped.

**Two further facts the original got wrong:**

- It claimed a 10-file commit. `72aec5a` touched **73 files**. The list was never a complete
  account of the commit; it was a partial list, 7/10 of it wrong.
- Its title was dated **2026-08-24** — two days *after* the commit it describes
  (`72aec5a`, 2026-08-22 15:36:37 +0530). A future date on a landing record.

### Corrected status of each originally-listed file

Verified against `git show --name-only 72aec5a`:

| file | claim | actual |
|---|---|---|
| `geper/DATA_SOURCE_LICENSE_AUDIT.md` | in 72aec5a | **TRUE** |
| `geper/pipeline/confidence_engine.py` | in 72aec5a | **TRUE** |
| `geper/report/report_generator.py` | in 72aec5a | **TRUE** |
| `geper/pipeline/acmg_rules.py` | in 72aec5a | **FALSE** — last landed `089b5ab`; modified in working tree |
| `geper/pipeline/interpretation.py` | in 72aec5a | **FALSE** — last landed `9e2a3ee` |
| `geper/pipeline/orchestrator.py` | in 72aec5a | **FALSE** — last landed `089b5ab` |
| `geper/pipeline/prioritization_engine.py` | in 72aec5a | **FALSE** — last landed `089b5ab` |
| `geper/pipeline/pvs1/utils.py` | in 72aec5a | **FALSE** — last landed `9e2a3ee` |
| `geper/verify_environment.py` | in 72aec5a | **FALSE** — last landed `089b5ab` |
| `kim_pipeline/pipeline/acmg/classifier.py` | in 72aec5a | **FALSE** — last landed `c7b9859` (the revert); **held uncommitted, expert-gated** |

The first entry also carried a typo in the original: `eper/DATA_SOURCE_LICENSE_AUDIT.md`, missing
its leading `g`.

### Status of the two untracked files

Both flags were **correct when written**. Current state:

- `geper/tests/test_gnomad_lookup_failure_not_absent.py` — **no longer untracked**, landed in `9e2a3ee`.
- `kim_pipeline/tests/test_pp3_bp4_spliceai_removal_invariance.py` — **still untracked, deliberately**,
  held with the classifier.py change pending the same expert ruling.

---

## ORIGINAL TEXT (2026-08-22) — PRESERVED, AND FALSE AS NOTED ABOVE

> # REVIEW_MANIFEST — 72aec5a landing verification (2026-08-24)
>
> Tracked changes landed in 72aec5a:
> - eper/DATA_SOURCE_LICENSE_AUDIT.md
> - geper/pipeline/acmg_rules.py
> - geper/pipeline/confidence_engine.py
> - geper/pipeline/interpretation.py
> - geper/pipeline/orchestrator.py
> - geper/pipeline/prioritization_engine.py
> - geper/pipeline/pvs1/utils.py
> - geper/report/report_generator.py
> - geper/verify_environment.py
> - kim_pipeline/pipeline/acmg/classifier.py
>
> Untracked (new files, not in index):
> - geper/tests/test_gnomad_lookup_failure_not_absent.py [FLAG: untracked, won't appear in git diff]
> - kim_pipeline/tests/test_pp3_bp4_spliceai_removal_invariance.py [FLAG: untracked, won't appear in git diff]
>
> Note: geper/tests/test_gnomad_lookup_failure_not_absent.py is NEW, untracked, and critical for the
> gnomAD sentinel defect fix. Reviewers working from git diff will not see this test, only the 10
> production guard-site changes. The test must be reviewed separately to understand what the guard
> changes defend against.
