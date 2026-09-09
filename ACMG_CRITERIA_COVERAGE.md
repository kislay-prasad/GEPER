# ACMG/AMP Criteria: the nine GEPER never evaluates

GEPER's ACMG rule engine (`geper/pipeline/acmg_rules.py`) accounts for all 28
ACMG/AMP criteria explicitly in its output — including the ones it has no
basis to evaluate, so a reader sees an accounted-for gap rather than a
silent omission. Nine of the 28 always return `"not_evaluated"`, for every
variant, regardless of what evidence GEPER does hold for that variant.
`_NEVER_INTEGRATED_ACMG_CODES` (`acmg_rules.py:291`) names exactly these
nine and is cross-checked against a real rule-engine run by
`tests/test_round16_not_evaluated_categories.py::test_never_integrated_constant_matches_the_real_rule_set`,
so this list cannot silently drift from the code that implements it.

**This document exists to answer one question for a reader who does not
want to open the source: for each of the nine, why not, and what would it
take to close the gap.** The reason text below is quoted, not paraphrased,
from each criterion's own `reason` argument at the cited line —
`CriterionResult.reason` is not source-only prose; it is the same string
GEPER's own report renderers surface to a clinical reader
(`report/clinical_report_builder.py`), so this table's "reason" column and
the pipeline's own output are read from a single source, not two
descriptions that could drift apart.

*Location note: this repository has no root `README.md`, so an earlier
brief's reference to "README §9" does not resolve to anything. This is a
new standalone file rather than an addition to an existing document — none
of the existing root `.md` files (`DATA_PROVENANCE.md`, an append-only audit
log; `VALIDATION_STUDY_DESIGN.md`, a study-design document that already
touches PP5 for an unrelated reason, see the note below) is a reference
document this table belongs inside. Naming this decision explicitly since
the brief did not settle it.*

## The table

| Criterion | Strength / direction | Reason (quoted from `acmg_rules.py`) | Data type needed | Source |
|---|---|---|---|---|
| **PS2** | Strong, pathogenic | "requires confirmed de novo trio (parental) sequencing data; not integrated." | Trio (parent-child) sequencing data confirming de novo origin | `acmg_rules.py:933-937` |
| **PS4** | Strong, pathogenic | "PS4 requires comparing this variant's prevalence in affected (case) individuals against its prevalence in unaffected/control populations. GEPER integrates only the control side (gnomAD); no case-frequency source ... is integrated, so the case-control comparison PS4 requires cannot be computed, and this criterion can never be triggered by this pipeline as currently built." | A disease-specific case cohort/registry, or a published case-control study, giving case-side prevalence | `acmg_rules.py:1481-1570` (method `_ps4`, called at `:875`) |
| **PM3** | Moderate, pathogenic | "requires trans-phase data for a recessive disorder; not integrated." | Phasing data establishing a second pathogenic variant is in *trans* (recessive disorders) | `acmg_rules.py:938-942` |
| **PM6** | Moderate, pathogenic | "requires confirmed (non-parentally-tested) de novo status; not integrated." | De novo status asserted without parental confirmatory sequencing | `acmg_rules.py:943-947` |
| **PP2** | Supporting, pathogenic | "requires a gene-level missense-constraint metric (e.g. gnomAD missense Z-score); not integrated." | A gene-level missense-constraint metric (e.g. gnomAD's missense Z-score) | `acmg_rules.py:948-952` |
| **PP5** | Supporting, pathogenic | "recommended against by ClinGen's SVI Working Group (Biesecker & Harrison 2018) as circular with respect to an independent ACMG/AMP evaluation; not applied." | **N/A — see note below** | `acmg_rules.py:953-957` |
| **BS2** | Strong, benign | "requires observation in unaffected individuals at the expected penetrance age; not integrated." | Case-level observation of the variant in individuals unaffected at the disorder's expected penetrance age | `acmg_rules.py:959-963` |
| **BP2** | Supporting, benign | "requires trans/cis phase data; not integrated." | Phasing data (trans with a known pathogenic variant, or cis in a dominant disorder) | `acmg_rules.py:964-966` |
| **BP5** | Supporting, benign | "requires case-level data on an alternate molecular cause; not integrated." | Case-level data establishing an alternate molecular basis for the same phenotype | `acmg_rules.py:967-971` |

## PP5 is not shaped like the other eight, and the table format says so

Eight of the nine are `NOT_INTEGRATED` in the honest sense that category
name intends (`NotEvaluatedReason.NOT_INTEGRATED`'s own docstring,
`acmg_rules.py:532-534`): a real data source GEPER could in principle
integrate, and has not. Any of the eight becomes evaluable the day that
source is added.

**PP5 is different in kind, not just in citation.** It is coded with the
same `NotEvaluatedReason.NOT_INTEGRATED` category, but its reason text
says something structurally unlike the other eight: PP5 is withheld by
*deliberate policy* — ClinGen's SVI Working Group (Biesecker & Harrison
2018, *Genet Med* 20:1687–1688) recommended against using PP5 (and its
benign counterpart BP6) at all, because a "reputable source reports
pathogenic" criterion is circular with an independent ACMG/AMP evaluation
when that reputable source is itself built from ACMG/AMP-based
classifications. **No future data integration would ever make PP5 fire in
this engine** — that is why its "data type needed" cell above reads N/A
rather than naming a missing resource. The existing `NOT_INTEGRATED`
category value is not technically wrong (PP5 is, in fact, not integrated)
but it invites the same reading as the other eight — "add the data source
and this closes" — which is false for PP5 specifically. Flagging this
distinction here rather than changing the category enum: the five-category
`NotEvaluatedReason` design (`acmg_rules.py:514-573`) was a deliberate,
tested piece of work in its own right (round 16), and adding a sixth
category is a larger decision than this documentation task, not something
to fold in silently.

**Prior art, checked before writing PP5's row above, per the dispatch's
explicit instruction:** `pp5-not-evaluated-comment-misattributes-its-own-deprecation-source`
and `check-kim-pipeline-for-pp5-false-claim` (`hive/tasks.json`, both
`done`) record that GEPER's PP5 comment previously cited "deprecated in
the 2015 ACMG/AMP guideline update" — an impossible claim, since PP5 was
*created* by that same 2015 guideline, not deprecated by it. Kelly
corrected the citation to Biesecker & Harrison 2018 in commit `e037e9f`;
the current source text quoted in the table above already reflects that
fix, verified by reading `acmg_rules.py:953-957` directly at this
worktree's HEAD (`c548f807af03d9c4b7a6f786354f1c6f79b66711`) rather than
trusting the card. `kim_pipeline`'s own PP5/BP6 handling was checked
separately and found already correct (states both the 2015 origin and the
2018 recommendation against) — no change needed there, and none made here.

**One related, stale citation found and NOT fixed here (out of this
task's scope, flagged for whoever owns that document):**
`VALIDATION_STUDY_DESIGN.md:367` still quotes PP5's *old*, incorrect reason
text ("deprecated in the 2015 ACMG/AMP guideline update; not applied.") and
cites it at a stale path/line (`pipeline/acmg_rules.py:923-926` — no
`geper/` prefix, and PP5 has since moved to `:953-957`). That document is a
separate validation-study design artifact, not the coverage table this
task asked for, and this task's boundaries are read-only implementation of
*this* deliverable — noting it rather than editing an unrelated document
without authorization.

## Why no code change was needed here

All nine `_not_evaluated` call sites already carry a specific, current
reason as the actual `reason` argument passed to `CriterionResult` — not a
bare source comment sitting beside the code, but a live string that flows
into the engine's own output and into what a clinical reader sees. Adding
a separate `#` comment duplicating that same text next to each call site
would create a second copy of the same claim with no mechanism keeping the
two in sync — exactly the kind of divergence risk this floor has flagged
repeatedly elsewhere tonight. This document instead quotes the `reason`
strings directly, so there is one source of truth (the code) and one
reader-facing description of it (this table), not three.
