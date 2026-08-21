# GEPER Validation Study Design

**Status:** DEFINITION ONLY. **UNCOMMITTED.** No measurement runs performed, no data
downloaded, no code written or changed. Nothing in this document commits without human
review.
**Author:** Vic (validation strategy & study design lane), dispatched by god, 2026-08-21.
**Scope:** the two studies that would fill rubric domain **D1** — concordance (1a) and
external QA panel (1c) — plus the **structure** of the acceptance criteria all of D1 is
scored against. **Inter-run reproducibility (1b) is NOT in this document**; it is
`geper/REPRODUCIBILITY_PROTOCOL.md` @ `71d0e37` (Andy), and is referenced here only where a
dependency crosses.
**Baseline:** every study below measures against tag `validation-baseline-2026-08-21`
(`470a993`), never against a moving `master`.
**Graded against:** `VALIDATION_READINESS_RUBRIC.md` (Angela). This document proposes **no
change to that rubric.** Where reading it against real code surfaced a problem in its text,
that is listed in §7 for Angela to rule on, not edited here.

---

## 0. How to read this document, and what it deliberately does not contain

**This document contains no acceptance thresholds. Not one.** It defines the *shape* of two
studies and the *structure* the acceptance criteria must take. Every place a number would be
needed to say "this passed," there is a named, structurally-unmistakable placeholder
instead, and a statement of what kind of expert judgement has to fill it and on whose
authority.

That split is not caution. A number invented here would be an assertion wearing an evidence
label, introduced at the level of the validation plan — the one place where it would be
hardest to detect later and most likely to be quoted without its caveat. The rubric this
design is graded against calls that failure mode Tier D (`VALIDATION_READINESS_RUBRIC.md`
§1) and scores it zero. It would be incoherent for the study design to commit the defect the
instrument grading it exists to catch.

**The standing frame, restated because every section below depends on it:** the work
preceding this design established that GEPER no longer **misstates what it observed**. It
did **not** establish that what it observes is **correct** (tag
`validation-baseline-2026-08-21`, its own message: *"NOT ESTABLISHED: that what it observes
is CORRECT... Not partial — absent."*). These studies are the plan for attacking that
second, separate question. Until one of them is executed, **nothing in this document is
evidence of anything about GEPER's accuracy** — it is a plan to obtain such evidence.

### 0.1 Index of unfilled expert-judgement decisions — **19**

Every one of the 19 is a decision a qualified human must make; none may be inferred from
this document. Verify the count has not drifted:

```
grep -cE '^<<<EXPERT-JUDGEMENT-REQUIRED: EJ-[0-9]+$' VALIDATION_STUDY_DESIGN.md   # expect 19
grep -c  '^>>>$'                                       VALIDATION_STUDY_DESIGN.md   # expect 19
```

A count other than 19 means tokens were added, or silently filled in — either way this index
is stale and the document must not be used as though it were complete.

| ID | § | The decision |
|---|---|---|
| EJ-01 | 5.1 | The intended-use statement — what clinical claim this validation is meant to support |
| EJ-02 | 5.4 | The binarisation rule: how VUS is treated when computing sensitivity/specificity |
| EJ-03 | 5.5 | E2 (truth P/LP → GEPER VUS; missed pathogenic) acceptable rate, Arm A |
| EJ-04 | 5.5 | E3 (truth B/LB or VUS → GEPER P/LP; over-call) acceptable rate, Arm A |
| EJ-05 | 5.5 | E4 (truth B/LB → GEPER VUS; VUS inflation) acceptable rate |
| EJ-06 | 5.5 | E5 within-tier drift (P↔LP, B↔LB) tolerance |
| EJ-07 | 5.5 | E6 no-call / not-evaluated ceiling |
| EJ-08 | 2.6 | Sample size: the precision target, and the clustering unit it is computed on |
| EJ-09 | 2.5 | Arm B − Arm C delta: how much ClinVar-dependence is itself a finding |
| EJ-10 | 2.7 | Whether Arm B may ever be reported as a headline number, or Arm A only |
| EJ-11 | 2.6 | Gene/condition scope the concordance claim is permitted to cover |
| EJ-12 | 6.1 | Whether Indian-population validation is a release gate (go/no-go, not a threshold) |
| EJ-13 | 3.4 | GIAB precision/recall/F1 thresholds, stated separately per variant class |
| EJ-14 | 3.3 | Which GIAB genomes and which confident-region strata are in scope |
| EJ-15 | 4.4 | Which external QA scheme, and whether shadow assessment substitutes for enrolment |
| EJ-16 | 4.5 | The external-QA passing criterion, and the consequence of a fail |
| EJ-17 | 5.7 | Re-validation trigger and cadence |
| EJ-18 | 5.8 | Who signs the validation report, and with what accreditation standing |
| EJ-19 | 2.4 | Which GEPER *configuration* the validation certifies (the star-rating floor especially) |

### 0.2 Tiering legend — applied to every substantive element below

Per the dispatch, every design element states which of three it is. A fourth marker is used
for facts checked directly in this repository this session; it is not one of the three tiers
because it is stronger than all of them — it is a demonstrated fact about the code, not an
inference.

- **[VERIFIED-IN-REPO]** — checked directly in this repo this session, with file and line. A
  reviewer can re-check it in seconds. Not a tier; a demonstrated fact.
- **[GUIDANCE]** — drawn from published guidance, with the document **and section** named.
  **Read the citation caveat in §0.3 before relying on any of these.**
- **[ANALOGY]** — inferred by analogy, from adjacent practice or from GEPER's own
  architecture, with no specific external source behind it. **Per the dispatch's own
  framing: this is where a plausible-looking wrong answer hides.** Read these more
  sceptically than [GUIDANCE] ones, and treat "it looks obviously right" as a reason for
  more scrutiny, not less.
- **[INVENTED]** — reasoned out here, nothing backs it. Flagged so a reviewer knows exactly
  where to push back hardest.

### 0.3 ⚠ Citation caveat — read before treating any [GUIDANCE] item as verified

**No external standards document was fetched or read during the preparation of this
design.** This session had no primary-source access to CAP, CLIA, ACMG/AMP, ISO, NABL or
ICMR material. Every [GUIDANCE] citation below is therefore **named from training
knowledge**, and is marked inline `[unfetched]`.

That is a weaker standard than the one `geper/GROUND_TRUTH_DATASET_AUDIT.md` met for
datasets (which fetched primary sources directly and said which), and it is stated here
rather than left for an assessor to discover. A named-but-unfetched citation is a pointer to
where an answer lives, **not evidence that the cited section says what is claimed**.
Document numbers, clause numbers and years are the parts most likely to be wrong.

**Consequence, stated as a hard rule rather than a recommendation:** §8.1 requires every
[GUIDANCE] citation to be verified against its primary source before this design is used to
run anything. A citation that survives that check is [GUIDANCE]; one that does not is
[INVENTED] and must be re-marked as such. Citations corroborated by an in-repo fetched
source are marked `[corroborated in-repo]` and are exempt.

### 0.4 What is pinned, and the one-command check that it is

**[VERIFIED-IN-REPO]** Every code fact in §1 was read at working-tree HEAD (`71d0e37`) and
confirmed byte-identical to the baseline tag:

```
git diff --stat validation-baseline-2026-08-21 -- \
    geper/pipeline/acmg_rules.py geper/pipeline/ps1_pm5/ \
    geper/database/clinvar_client.py geper/config.py        # empty
```

All four commits between `470a993` and `71d0e37` are documentation-only. The architectural
findings in §1 therefore describe the baseline, not a moved tree, and an assessor can
confirm that in one command.

**The organisation's knowledge graph was queried for NABL/ICMR context and is empty**
(`kg.cjs list` → "The knowledge base is empty"). No internal policy, house standard, or
prior regulatory correspondence informed this design, because none was available. Stated so
its absence is not mistaken for its having been consulted and found silent.

---

## 1. The circularity problem — verified channel map, and the resolution this design takes

The dispatch elevated this from a note to a design constraint: any ClinVar-based concordance
study must address the echo problem, and this document must state **which approach it takes
and why**. It does, in §1.3. First, what is actually there — because the answer changed once
the code was read.

### 1.1 The problem in one sentence

GEPER consumes ClinVar as live evidence. If the truth set is also ClinVar, then for any
variant where that evidence fired, comparing GEPER's output back against ClinVar partly
measures whether GEPER echoed what it was told — a measurement that cannot fail, reported as
though it could.

### 1.2 The verified channel map — narrower, and differently shaped, than assumed

`geper/GROUND_TRUTH_DATASET_AUDIT.md` @ `6f27e7b` flags the circularity via
`clinvar_client.py:344-345`, and the dispatch describes the channels as "PP5/BP6
direct-match, PS1/PM5 same-residue". Reading the engine directly gives a materially
different picture, and that difference decides the study design:

| Channel | Consumes ClinVar? | Whose record? | Reaches the **classification**? | Evidence |
|---|---|---|---|---|
| **PP5** | **No — never fires** | — | **No** | **[VERIFIED-IN-REPO]** `pipeline/acmg_rules.py:923-926` — `PP5` is set to `_not_evaluated` **unconditionally**, reason `"deprecated in the 2015 ACMG/AMP guideline update; not applied."` |
| **BP6** | Yes | The variant's **own** record | **No** | **[VERIFIED-IN-REPO]** `acmg_rules.py:3509-3522` — `_combine` skips `BP6` in the point tally with a comment naming this exact circularity; `_BP6_DEPRECATION_CAVEAT` (`:2943-2951`) states it is "reported at capped Low confidence and is excluded from this engine's own point-based combining rules... so it cannot silently move the final classification". It **does** reach the human-readable evidence text. |
| `_clinvar_crossref` | Yes | Own record | **No** | **[VERIFIED-IN-REPO]** `acmg_rules.py:3420`, and the comment at `:3511` — descriptive cross-reference, "kept out of these combining rules entirely". |
| **PS1** (Strong, path.) | **Yes** | **Other** variants at the same codon, identical resulting AA change | **YES** | **[VERIFIED-IN-REPO]** `acmg_rules.py:1116-1140`; anchors filtered by `CONFIG.ps1_pm5.MIN_STAR_RATING` (**default 2**, `config.py:1677`); the variant's own record is excluded (`pipeline/ps1_pm5/decision.py:137`, *"excluding this variant's own record"*). |
| **PM5** (Moderate, path.) | **Yes** | **Other** variants at the same codon, *different* AA change | **YES** | **[VERIFIED-IN-REPO]** `acmg_rules.py:1144-1170`; same threshold, same self-exclusion. |

**Three consequences, each of which changes the design:**

1. **Record-level circularity is already architecturally closed.** A variant's own ClinVar
   classification **cannot** move GEPER's classification of that variant. This is not
   incidental — the code says so in its own comments, citing the same ClinGen SVI
   deprecation this design would otherwise have had to import from outside. It is the
   strongest single fact in this section and an assessor should be pointed at it directly.

2. **"Disable PP5/BP6 for validation runs" — one of the routes the dispatch listed — is very
   nearly a no-op here.** PP5 never fires; BP6 already cannot affect the score. Taking that
   route would produce a validation run essentially identical to a normal one while
   *appearing* to have controlled for circularity. **That is a worse outcome than not
   controlling for it**, because it would look like a control and function as none. Rejected
   on that ground, not on preference.

3. **The live channel is PS1/PM5, and it is a different shape of circularity than the one
   named.** It is not "GEPER read this variant's label"; it is "GEPER read a *neighbouring*
   variant's label from the same database." Weaker, still real, and — critically — **not
   caught by any control aimed at the variant's own record.** A design controlling only for
   direct match would leave the entire real channel open.

### 1.2b The second-order channel not yet named anywhere: **curator circularity**

**[ANALOGY — reasoned from the two facts below; no external source, and the inference is the
load-bearing part, so read it sceptically]**

The proposed truth set is ClinVar's 3★ expert-panel + 4★ practice-guideline subset. Two
verified facts collide inside it:

- **[VERIFIED-IN-REPO]** PS1/PM5 admit anchors at ≥2★ (`config.py:1677`), so 3★ and 4★
  records are themselves *admissible anchors* — the truth tier and the anchor tier overlap.
- **[GUIDANCE, corroborated in-repo]** ClinVar's expert panels curate **whole genes or gene
  sets**, not scattered variants: ENIGMA for *BRCA1/2*, InSiGHT for Lynch-syndrome genes
  (`geper/GROUND_TRUTH_DATASET_AUDIT.md`, ClinVar row — sourced there from
  `ncbi.nlm.nih.gov/clinvar/docs/review_status/`, fetched by Meredith).

So for a truth variant in *BRCA1*, the PS1 anchor at the same codon is plausibly curated by
**the same expert panel that produced the truth label**. GEPER's PS1 then re-imports that
panel's judgement, and the comparison scores GEPER against a body of opinion that partly fed
it. This is not record-level echo, and stratifying on "did ClinVar evidence fire" does not
isolate it — you need the **anchor's submitting organisation**.

**GEPER already captures exactly that.** **[VERIFIED-IN-REPO]** `clinvar_client.py:355-372`
(`_fetch_submitters`, batched VCV XML) and `pipeline/ps1_pm5/utils.py:161` capture the
submitting organisation per record. The provenance needed to measure curator circularity is
already flowing through the pipeline; §2.5 uses it. **No new integration is required** —
which is the reason this stratum is proposed at all, rather than named as an unaddressable
limitation.

### 1.3 THE APPROACH THIS DESIGN TAKES, and why

> **Chosen: stratification on the verified classification-affecting channel (PS1/PM5), with
> a paired ablation arm and a curator-provenance sub-stratum. Arm A — the ClinVar-independent
> arm — is the primary, and the only arm eligible to carry a headline number.**

Set out in full in §2.3–2.5. The reasoning, on the merits:

**Why not "an independent source" (the dispatch's route 2).** Because Meredith's audit
establishes there is not one. HGMD is commercially blocked without a QIAGEN purchase;
PharmVar is CC-BY-NC-ND; DECIPHER is research-only; GenomeIndia and GenomeAsia100K are
DAC-gated with no established commercial pathway; GIAB answers a different question
entirely. That audit's own covering note calls the ClinVar expert-panel subset *"the single
dataset whose loss would break the validation plan."* Choosing "use an independent source"
would be choosing a dataset that does not exist. Rejected on availability, evidenced by a
peer's fetched-primary-source audit — not on preference. **[GUIDANCE, corroborated in-repo:
`6f27e7b`]**

**Why not "disable PP5/BP6" (route 3).** §1.2, consequence 2: nearly a no-op, and it would
function as a decorative control. Rejected on verified code, not judgement.

**Why not "restrict the set to variants where those codes did not fire" alone (route 4).**
This is Arm A, and Arm A **is** the primary — but *alone* it discards the information an
assessor most wants. A restricted-set-only design can state that GEPER agrees with expert
panels where ClinVar did not help; it cannot state **how much of GEPER's apparent
performance is ClinVar's**. The ablation arm (C) produces that quantity directly, at the cost
of one extra run over an already-selected variant set. Restriction is kept as the primary and
extended, not replaced.

**Why not hold-out (route 5).** Hold-out controls for a model *trained* on the data. GEPER's
ACMG engine is a rule engine that **queries ClinVar live at classification time**
(`clinvar_client.py`, `ps1_pm5/lookup.py`) — **[VERIFIED-IN-REPO]**. Withholding variants
from a training set that does not exist changes nothing: the engine would query the live API
for a held-out variant's codon neighbours exactly as before. Rejected as a category error
about this system's architecture, and named as such because it is precisely the kind of
control that sounds rigorous and would do nothing here.

**A third route this design adds, beyond the two the dispatch named:** the **paired ablation
arm (C)** and the **curator-provenance sub-stratum (B1/B2)**. Arm C is what turns "we
excluded the contaminated variants" into "here is the size of the contamination." Both are
**[INVENTED]** as applied here — no external source proposes either for this setting; they
are reasoned from the verified channel map above. Flagged as this design's most
pushable-back element.

### 1.4 What is measured and what is not — stated first, so an assessor does not find it

Required verbatim in any report these studies produce, per the dispatch's instruction that
the write-up state this plainly:

- **Arm A measures** whether GEPER's non-ClinVar evidence stack (PVS1, PM1, PM2, PP3/BP4,
  PS3/BS3, BA1/BS1 and the rest) independently reaches the classification an expert panel
  reached.
- **[ANALOGY]** Arm A is expected to be **systematically harder** than the full truth set:
  variants where ClinVar evidence does not fire are, on average, those with fewer
  well-characterised codon neighbours. **An Arm A number is therefore not comparable to
  another laboratory's published overall concordance figure** and must never be presented as
  if it were.
- **Arm A does not measure** performance on the variants where ClinVar evidence is most
  informative — i.e. much of the well-studied, medically-actionable territory where GEPER
  would most often be used in practice.
- **Arm B measures** in-practice agreement **against a partially non-independent truth
  label**. It is an upper bound. It is not a concordance claim, and EJ-10 exists because
  whether it may appear as a headline at all is not this document's call.
- **Neither arm measures** whether GEPER is correct where ClinVar is *wrong*. The truth set
  is expert opinion, not biological ground truth, and 3★ records are reclassified over time.
  **[ANALOGY]** No design in this document closes that, and none can with available data.

---

## 2. Study S1 — ACMG classification concordance (rubric D1 / 1a)

### 2.1 The claim this study can support, in its narrowest honest form

Modelled on `geper/REPRODUCIBILITY_PROTOCOL.md` §0's discipline of stating the *claimable*
form rather than the desirable one, and on the rubric's own cross-domain "Scope constraints"
subsection which requires exactly that treatment for any claim broader than its possible
evidence.

> **Not claimable:** "GEPER classifies variants correctly."
>
> **Claimable:** "For single-nucleotide and small-indel variants in the genes listed in
> `<EJ-11>`, run at configuration `<EJ-19>` against GEPER `470a993` and ClinVar release
> `<pinned date, §2.2>`, GEPER's ACMG classification agreed with the ClinVar
> expert-panel/practice-guideline classification at the rates reported per arm below, where
> Arm A excludes every variant for which PS1 or PM5 contributed to GEPER's own
> classification."

The bounding conditions in that sentence are load-bearing and must be **repeated wherever
the claim appears**, not stated once and carried by reference — the same requirement the
rubric imposes on 1b's (a)/(b) conditions, applied here because the failure mode is
identical.

### 2.2 Truth set, and the two pins it requires

**Source.** ClinVar's 3★ `reviewed_by_expert_panel` + 4★ `practice_guideline` subset —
22,402 + 663 = **23,065 records** as counted by Meredith @ `6f27e7b` from
`ncbi.nlm.nih.gov/clinvar/docs/statistics/` (page-dated 2026-08-16). **[GUIDANCE,
corroborated in-repo]**

**Availability: not gated.** **[VERIFIED-IN-REPO]** This is a `review_status` filter on data
already flowing through `geper/database/clinvar_client.py`; no new credential, endpoint or
integration. Public domain per NCBI site policy, re-confirmed by Meredith for this specific
use (validation ground truth rather than runtime evidence). **This is the only dataset in
this design with no acquisition dependency** — §6 lists the ones that do.

**Pin 1 — code.** Tag `validation-baseline-2026-08-21` (`470a993`).

**Pin 2 — data. A single dated ClinVar release must supply BOTH the truth labels AND the
evidence GEPER consumes during the run.** **[ANALOGY]** ClinVar is versioned and reclassifies
continuously; if the truth labels are drawn on one date and GEPER queries the live API on
another, some fraction of any observed discordance is reclassification drift between the two
dates rather than GEPER error — a discordance that cannot be attributed and would be reported
as if it could. The release date must be recorded alongside the code tag in every output.

**Dependency this creates, and whose it is.** **[VERIFIED-IN-REPO]** GEPER queries the NCBI
E-utilities API live (`clinvar_client.py`, `ps1_pm5/lookup.py`; a cache exists at
`pipeline/ps1_pm5/cache.py`). Whether a frozen, dated snapshot can be substituted for the
live API without changing the code path under test is an **infrastructure question, and this
design does not answer it** — it belongs to Andy's lane. Named as a cross-lane dependency in
§6.3 rather than assumed solvable.

### 2.3 Arm structure

Every truth variant is assigned to exactly one arm by a rule evaluated **from GEPER's own
output**, not from any property of the variant chosen in advance.

| Arm | Membership rule | Role |
|---|---|---|
| **A — ClinVar-independent** | Neither PS1 nor PM5 triggered in GEPER's output for this variant | **PRIMARY.** The only arm eligible for a headline number (subject to EJ-10) |
| **B — ClinVar-assisted** | PS1 **or** PM5 triggered | Reported separately. **Never pooled with A.** |
| **C — ablation of B** | The same variants as Arm B, re-run with PS1/PM5 forced not to trigger | Yields the **magnitude** of ClinVar's contribution (B − C) |

**Sub-flag on Arm A: `bp6_fired`.** **[VERIFIED-IN-REPO + INVENTED]** BP6 cannot move the
score (`acmg_rules.py:3509-3522`) but does reach the rendered evidence text. Arm A must
therefore be reported **twice** — with and without `bp6_fired` variants. The reason is not
statistical: a clinician reading GEPER's evidence narrative is a consumer of that text, and
an assessor will ask whether ClinVar's own verdict was visible to a human reader even where
it could not move the score. Reporting only the pooled figure would answer that question
implicitly and wrongly. The double-reporting requirement is **[INVENTED]** — no source
requires it.

**Never pooled into one number.** **[ANALOGY, from Angela's rubric §2]** The rubric already
forbids presenting a blended readiness score without its tier split, on the ground that a
blended number makes correct citation harder than incorrect citation. A pooled A+B
concordance figure is the same defect one level down: it would be the number everyone quotes,
and it is the one number in this study that means nothing, because its value depends entirely
on the A:B ratio in whatever variant set happened to be selected.

### 2.4 The ablation lever for Arm C

**[ANALOGY — derived from verified code reading; the *behaviour* is NOT demonstrated and
must be verified before use]**

**[VERIFIED-IN-REPO]** `CONFIG.ps1_pm5.MIN_STAR_RATING` is read from the environment
(`config.py:1677`, `GEPER_PS1_PM5_MIN_STAR_RATING`, default `2`) and is the sole star-rating
gate applied to PS1/PM5 anchors (`acmg_rules.py:1137`, `:1166`). ClinVar's maximum star
rating is 4 (`pipeline/ps1_pm5/models.py`, `STAR_PRACTICE_GUIDELINE = 4`).

**Inferred:** setting `GEPER_PS1_PM5_MIN_STAR_RATING=5` should admit no anchor, so PS1 and
PM5 never trigger — **an ablation lever that already exists and needs no code change.**

**This inference must not be relied on until verified by execution.** It is exactly the
[ANALOGY]-tier reasoning the dispatch warns about: architecturally clean, obviously right on
inspection, and unverified. Required pre-flight (out of this phase's boundaries — definition
only, no runs):

1. Run a variant known to trigger PS1 at default config; confirm it triggers.
2. Re-run at `MIN_STAR_RATING=5`; confirm PS1 and PM5 both report not-triggered rather than
   erroring, defaulting, or silently admitting the anchor by another path.
3. Confirm **no other criterion's behaviour changes** between the two runs — an ablation
   that also perturbs an unrelated criterion is not an ablation.
4. If any step fails, Arm C requires a code change and becomes a scoped engineering
   dependency, not a config flip. Say so rather than working around it.

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-19
  DECIDE   — which GEPER configuration the validation certifies, `MIN_STAR_RATING` in
             particular (default 2 = "criteria provided, multiple submitters, no
             conflicts"). A validation performed at one evidence-admission setting does not
             validate any other setting, and this value is environment-overridable at
             runtime, so a deployment can silently differ from the validated configuration.
             The decision must cover both the certified value AND whether that value is to
             be locked against environment override in a validated deployment.
  GUIDANCE — ISO 15189:2022, clause 7.3.3 (validation of examination methods) — validation
             applies to the method as specified, and a change to the specified method
             requires re-validation [unfetched, clause number uncertain — verify].
             Richards et al., ACMG/AMP 2015, Genet Med 17(5):405-424, PS1/PM5 definitions
             and the evidentiary weight assigned to a prior classification [unfetched].
  DECIDED-BY — the laboratory director or clinical-genetics lead accountable for the
             report, on the basis of what strength of prior evidence they are willing to
             have contribute at Strong (PS1) weight to a released classification. Not an
             infrastructure decision and not this document's.
>>>

### 2.5 Curator-provenance sub-strata within Arm B

**[INVENTED — the stratum; VERIFIED-IN-REPO — the provenance data it uses]**

Arm B splits on whether the PS1/PM5 anchor's submitting organisation overlaps the submitter
set of the truth variant's own record:

- **B1 — disjoint curators.** The anchor came from a different organisation than the one
  that produced the truth label. Weaker echo.
- **B2 — overlapping curators.** Same expert panel on both sides. **Strongest echo in the
  study, and the stratum an assessor is most likely to probe.**

Both are computable from data GEPER already fetches (`_fetch_submitters`,
`clinvar_client.py:355-372`). If the pre-count in §2.6 shows B2 is a large fraction of B, that
is itself a finding about the truth set and must be reported as one, whatever the concordance
rate inside it turns out to be.

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-09
  DECIDE   — how large a gap between Arm B and Arm C (the same variants with PS1/PM5
             ablated) constitutes a finding in its own right, and what that finding
             obligates. A large B−C gap means GEPER's apparent performance on
             well-characterised variants rests substantially on ClinVar rather than on its
             own evidence stack. That may be entirely acceptable clinical practice — or it
             may mean the product should not claim independent interpretation. This is a
             judgement about what the product claims to be, not a statistic.
  GUIDANCE — Biesecker & Harrison (ClinGen SVI), "The ACMG/AMP reputable source criteria for
             the interpretation of sequence variants," Genet Med 2018;20(12):1687-1688 — the
             rationale for deprecating deference to another laboratory's classification
             [unfetched, but corroborated in-repo: this citation is already relied on in
             `acmg_rules.py:2943-2951`, which is the strongest support any citation in this
             document has]. Richards et al., ACMG/AMP 2015, PS1/PM5 [unfetched].
  DECIDED-BY — the clinical-genetics lead together with whoever owns the product's
             regulatory claim, on the basis of what the product is represented as doing.
>>>

### 2.6 Sampling, feasibility pre-count, and the clustering problem

**A feasibility pre-count must run before any arm size is committed.** **[INVENTED]** The
Arm A:B ratio is unknown and unknowable without measurement — it depends on how often codon
neighbours exist at ≥2★ across the truth set. A count-only pass (assign each of the 23,065
records to an arm; compute no concordance) is cheap, answers it, and prevents committing to a
sample size that Arm A cannot supply. If Arm A turns out to be small, that constrains the
whole study and it is far better to know before designing around a number that does not
exist.

**The truth set is clustered, and per-variant confidence intervals will overstate
precision.** **[ANALOGY — standard clustered-sampling practice; no genomics-specific source]**
Expert-panel records concentrate in a small number of well-studied genes (*BRCA1/2*,
mismatch-repair genes, and similar — Meredith @ `6f27e7b`: "concentrated in well-studied,
medically actionable genes, not a genome-wide representative sample"). Variants within one
gene share a transcript model, a constraint profile, one panel's curation conventions and
often one domain annotation. They are **not independent observations.** A CI computed as if
they were will be too narrow, and the error runs in the optimistic direction.

Structural requirements that follow (all **[ANALOGY]**):

- Report **per-gene** results alongside per-variant results, always both.
- Treat **gene as the clustering unit** in any interval estimate.
- Report the **gene count and the per-gene variant distribution** in every output. "n=4,000
  variants" across 11 genes is a materially weaker result than the same n across 300, and a
  reader given only the first number cannot tell which they have.

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-08
  DECIDE   — the sample-size / precision target: the acceptable confidence-interval
             half-width on the primary Arm A rate, computed on the clustering unit named
             here (gene), and the minimum number of distinct genes the claim requires. Both
             halves are needed — a variant count alone does not determine precision on
             clustered data.
  GUIDANCE — Jennings, Arcila, Corless et al., "Guidelines for Validation of
             Next-Generation Sequencing-Based Oncology Panels: A Joint Consensus
             Recommendation of the Association for Molecular Pathology and College of
             American Pathologists," J Mol Diagn 2017;19(3):341-365 — sections on the number
             of specimens/variants required for validation [unfetched]. CLIA, 42 CFR
             §493.1253(b)(2), establishment and verification of performance specifications
             [unfetched]. ISO 15189:2022 clause 7.3.3 [unfetched].
  DECIDED-BY — a clinical-genetics lead with a statistician, jointly. Neither alone is
             sufficient: the statistical half depends on a clinically-set target precision,
             and the clinical half depends on understanding that clustering makes the naive
             n misleading.
>>>

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-11
  DECIDE   — the gene and condition scope the concordance claim is permitted to cover. The
             truth set is not genome-wide; it concentrates in well-studied medically
             actionable genes. A claim stated over "the genome" would be broader than the
             evidence for it, regardless of how favourable the numbers are — the failure
             mode the rubric's own "Scope constraints" subsection exists to prevent. The
             decision is which gene list the claim names, and what is said about every gene
             outside it (silence is not an option; "not validated" is a finding to state).
  GUIDANCE — Rehm, Bale, Bayrak-Toydemir et al., "ACMG clinical laboratory standards for
             next-generation sequencing," Genet Med 2013;15(9):733-747 — scope-of-validation
             requirements for targeted vs. genome-wide assays [unfetched]. ISO 15189:2022
             clause 7.3.1, scope of examination procedures [unfetched, clause uncertain].
  DECIDED-BY — the laboratory director, on the basis of the intended-use statement fixed in
             EJ-01. Downstream of EJ-01 and must not be decided before it.
>>>

### 2.7 Reporting rules for S1

- Arm A, Arm B, Arm C, B1, B2 and Arm-A-excluding-`bp6_fired` are **six** separately reported
  figures. **No pooled figure is produced at all**, so there is none for a reader to quote.
- Every figure carries its own denominator and its own gene count inline, not in a footnote.
- Every figure repeats the §2.1 bounding conditions.

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-10
  DECIDE   — whether Arm B may ever be presented as a headline concordance figure, or
             whether only Arm A may, with Arm B confined to a supporting section. Arm B will
             almost certainly produce the higher number and is the one that most resembles a
             conventionally-reported concordance rate — which is exactly why the decision
             must be made in advance and in writing, rather than at the moment someone is
             choosing what to put in a summary. Pre-committing is the whole point.
  GUIDANCE — Biesecker & Harrison, ClinGen SVI, Genet Med 2018;20(12):1687-1688 [unfetched,
             corroborated in-repo at `acmg_rules.py:2943-2951`]. ISO 15189:2022 clause 7.4.1
             on reporting of results, in particular that a report must not create a
             misleading impression of the method's scope [unfetched, clause uncertain].
  DECIDED-BY — the laboratory director jointly with whoever signs off external
             communications, since the risk being managed is a number quoted without its
             stratum. Not a technical decision.
>>>

---

## 3. Study S2 — variant-calling accuracy against GIAB (supports D1, distinct question)

### 3.1 Why this is a second study and not part of S1

**[GUIDANCE, corroborated in-repo]** GIAB provides high-confidence genotype truth sets with
confident-region BEDs; it carries **no ACMG classifications** (Meredith @ `6f27e7b`: "This
validates variant-*calling* accuracy... a different question from ClinVar's classification
ground truth"). S1 and S2 answer different questions and neither substitutes for the other.
They are reported separately and their numbers are never combined.

**Target.** kim_pipeline's FASTQ→VCF stages, not geper's ACMG interpretation layer. A
classification cannot be right if the variant call feeding it is wrong, so S2 is a
precondition for S1's result meaning anything in an end-to-end deployment — but it is not
part of S1's measurement.

### 3.2 Availability

**[GUIDANCE, corroborated in-repo]** Seven NIST reference genomes (HG001-HG007), public
domain under NIST policy plus the program's explicit commercial-redistribution consent
language. **Flagged weaker than the ClinVar row by Meredith's own audit** — no single
GIAB-specific licence document was locatable, and her outstanding item 4 says so. This design
inherits that caveat rather than resolving it: **S2's licensing is verified-but-not-closed**,
and §8 carries it forward.

### 3.3 Stratification

**[GUIDANCE]** Benchmarking against GIAB confident regions using `hap.py`-style comparison is
the field's standard practice and is what GIAB's own truth-set documentation points to
(Meredith @ `6f27e7b`, GIAB row) — this design proposes nothing novel here and should not.

**[ANALOGY]** Results must be stratified at minimum by variant class (SNV vs. indel; indels
are systematically harder and a pooled figure is dominated by the SNV count) and by region
difficulty (low-complexity / homopolymer regions vs. the rest).

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-14
  DECIDE   — which of the seven GIAB genomes are in scope, and which confident-region strata
             the claim covers. Using one genome and using all seven support materially
             different claims; excluding low-complexity regions produces materially better
             numbers for a materially narrower claim. Both choices must be made in advance
             and stated in the claim itself, not selected after seeing results.
  GUIDANCE — Krusche, Trigg, Boutros et al., "Best practices for benchmarking germline
             small-variant calls in human genomes" (Global Alliance for Genomics and Health
             benchmarking team), Nat Biotechnol 2019;37(5):555-560 — stratification and
             confident-region conventions [unfetched]. Jennings et al., AMP/CAP, J Mol Diagn
             2017;19(3):341-365, analytical validation sections [unfetched].
  DECIDED-BY — the laboratory director with the bioinformatics lead, on the basis of the
             sample types and regions the assay is intended to report on.
>>>

### 3.4 The ancestry limitation, stated here rather than in a limitations paragraph

**[GUIDANCE, corroborated in-repo]** GIAB's seven genomes comprise one HapMap individual, an
Ashkenazi Jewish trio and a Han Chinese trio. **No South Asian ancestry is represented**
(Meredith @ `6f27e7b`, GIAB row, explicitly). For a NABL/ICMR assessor this is likely to be
the first question asked of S2, and it must be answered before it is asked. It is a fact
about the only usable dataset, not a shortcoming of the design — and there is no substitute
available (§6.1).

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-13
  DECIDE   — the minimum acceptable precision, recall and F1 for S2, stated separately per
             variant class and per region stratum from EJ-14. A single pooled number across
             SNVs and indels is not an acceptable form for this criterion regardless of its
             value, because it is dominated by whichever class is more numerous.
  GUIDANCE — Krusche et al., Nat Biotechnol 2019;37(5):555-560 [unfetched]. Jennings et al.,
             AMP/CAP, J Mol Diagn 2017;19(3):341-365, analytical sensitivity/specificity
             sections [unfetched]. CLIA 42 CFR §493.1253 [unfetched].
  DECIDED-BY — the laboratory director with the bioinformatics lead, on the basis of the
             clinical consequence of a missed or spurious call in the intended use (EJ-01) —
             not on what the pipeline currently achieves. A threshold set after seeing the
             result is not a threshold.
>>>

---

## 4. Study S3 — external QA / proficiency-testing panel (rubric D1 / 1c)

### 4.1 The structural obstacle, named first

**[ANALOGY — reasoned from how PT schemes are structured; no scheme's enrolment terms were
fetched, and this is the element most likely to be wrong]**

External QA and proficiency-testing schemes generally enrol **laboratories**, and commonly
require the participant to be an operating and/or accredited clinical laboratory. **GEPER is
software that produces an interpretation; it is not a laboratory.** Enrolment as GEPER may
therefore be structurally unavailable — not difficult, unavailable.

This is stated first because it determines whether 1c is a study to design or a decision to
escalate, and getting that wrong would waste the entire section.

### 4.2 Two routes, which assess different things

**Route (i) — enrol via a partner accredited laboratory** that deploys GEPER, submitting
GEPER's output as part of that laboratory's response. **[ANALOGY]** Realistic, and the only
route that yields genuine enrolled participation. **It changes what is being assessed**: the
result characterises the laboratory-plus-GEPER system, including that laboratory's review
step, not GEPER alone. If GEPER's output is reviewed and corrected by a human before
submission, the PT result measures the reviewed output. That must be stated in any claim
arising from this route, because it is the difference between "GEPER passed" and "a
laboratory using GEPER passed."

**Route (ii) — retrospective / shadow assessment.** Obtain past scheme materials and
consensus answers where the scheme's terms permit, run GEPER, compare. **[ANALOGY]**
Cheaper, no partner needed. **It is not participation** and must never be reported as such:
no blinding by the scheme, no scheme-side scoring, no external witness to the run, and the
consensus answer may be discoverable to the software at run time. It produces an internal
benchmark against an external reference — which is genuinely useful and genuinely not the
same thing.

### 4.3 Candidate schemes

**[GUIDANCE — every entry unfetched; treat as a list of leads to verify, not a shortlist to
choose from]**

| Scheme | Relevance | What must be verified before it can be shortlisted |
|---|---|---|
| **EMQN** (European Molecular Genetics Quality Network) | Runs variant-interpretation EQA schemes; accepts international participants | Whether a non-laboratory software vendor may enrol; whether a current scheme covers ACMG classification rather than genotyping alone |
| **GenQA / UK NEQAS** | Genomic EQA including interpretation schemes | Same two questions; plus whether a non-UK participant may enrol |
| **CAP** proficiency testing | Named directly in the rubric's own 1c satisfier; includes in-silico/genomic surveys | Whether an in-silico interpretation survey currently exists and is internationally subscribable; CAP PT is oriented to CAP-accredited laboratories |
| **An Indian EQAS provider** | Likely the most relevant for a NABL assessor, since NABL expects PT from an approved provider where one exists | **Whether a genomic variant-interpretation scheme exists in India at all is an open question this session could not answer.** Named as an investigation item, not a candidate — asserting one exists would be inventing a fact about a regulator's ecosystem |

**[GUIDANCE]** The requirement driving all of this: ISO 15189:2022 clause 7.3.7.2 requires
participation in interlaboratory comparison (EQA/PT) where available, and where it is **not**
available, requires an alternative approach with documented justification of its adequacy
[unfetched, clause number uncertain — verify]. **NABL accredits medical laboratories to ISO
15189**, which is why this clause rather than a CAP checklist item is the governing one for
the stated assessor. **If enrolment is genuinely unavailable to a software vendor, the
"alternative approach with documented justification" limb is the applicable path — and it is
a documented, defensible path, not a failure.** That matters for Angela's open item on 1c's
scoring semantics (§7.3).

### 4.4 and 4.5 — the two decisions

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-15
  DECIDE   — which external QA scheme is pursued, via which route (§4.2 (i) enrolled via a
             partner laboratory, or (ii) retrospective shadow assessment), and whether route
             (ii) is acceptable as a substitute for enrolment for the purpose being claimed.
             Route (ii) is not participation; if it is chosen, the decision record must state
             that it was chosen knowingly and why enrolment was not pursued or not available.
  GUIDANCE — ISO 15189:2022 clause 7.3.7.2, interlaboratory comparison and the alternative
             approach where EQA is unavailable [unfetched, clause uncertain]. CAP All Common
             Checklist, proficiency testing and alternative performance assessment
             requirements [unfetched, item number not asserted]. ICMR Guidelines for Good
             Clinical Laboratory Practices, 2021 [unfetched, title and year uncertain].
  DECIDED-BY — the quality manager or accreditation lead, with the laboratory director. This
             is a regulatory-strategy decision about what standing the evidence needs to
             have, not a technical one about which scheme is most convenient.
>>>

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-16
  DECIDE   — the passing criterion for S3 and the consequence of failing it. Two parts,
             both required: (a) whether the scheme's own published passing threshold governs
             or an internal, stricter one does — and if the scheme's does, what is claimed
             when a scheme grades on consensus among participants rather than against a
             fixed standard, since a consensus-graded pass means "agreed with peers," not
             "was correct"; (b) what a failure obligates — investigation, a hold on release,
             a scope restriction — decided before a failure occurs rather than after.
  GUIDANCE — ISO 15189:2022 clause 7.3.7.3, evaluation of performance in interlaboratory
             comparison, including the requirement to act on unacceptable results
             [unfetched, clause uncertain]. CLIA 42 CFR §493.801-493.865, proficiency
             testing programme requirements and unsuccessful-participation consequences
             [unfetched].
  DECIDED-BY — the quality manager and laboratory director jointly. Part (b) in particular
             must be recorded before the first submission; a consequence decided after a
             failure is not a control.
>>>

---

## 5. Acceptance-criteria STRUCTURE (values are not in this document)

This section defines the **form** every acceptance criterion in D1 must take. It contains no
values. Its purpose is that a criterion written in this form **cannot be satisfied by a
measurement that could not have failed** — the defect class this whole phase exists to
remove, appearing here at the level of the criterion rather than the test.

### 5.1 The intended-use statement is prior to everything

No threshold is meaningful without it. A missed-pathogenic rate acceptable for a research
triage tool is not acceptable for a diagnostic report, and the same number would be correct
in one setting and indefensible in the other. **Every other decision in this document is
downstream of EJ-01 and must not be made before it.**

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-01
  DECIDE   — the intended-use statement: what clinical claim this validation supports, for
             what population, for what sample types, reported to whom, and with what human
             review step between GEPER's output and any clinical action. Every threshold
             below is a function of this statement and none can be set before it exists in
             writing.
  GUIDANCE — ISO 15189:2022 clause 7.3.1 and 7.3.3, examination procedures and validation
             against intended use [unfetched, clause numbers uncertain]. CLIA 42 CFR
             §493.1253 [unfetched]. Rehm et al., ACMG, Genet Med 2013;15(9):733-747
             [unfetched].
  DECIDED-BY — the laboratory director and the accountable clinical lead, with regulatory
             sign-off. The single decision in this document least substitutable by anyone
             else, and the one that blocks the rest.
>>>

### 5.2 The six-slot form — every acceptance criterion, no exceptions

**[ANALOGY — slots 1, 5 and 6 are lifted from Angela's rubric §3 satisfier/falsifier
discipline and Andy's §0 asymmetry rule; slots 2-4 are [INVENTED] additions this design
needs]**

Every criterion states, in this order:

1. **The claim, in its narrowest honestly-claimable form.** Not the desirable form. Per the
   rubric's cross-domain "Scope constraints" subsection: a claim broader than its possible
   evidence must be narrowed in the criterion itself, not merely weakly evidenced — otherwise
   the criterion cannot fail for the wrong reason.
2. **The measured quantity**, with its exact computation **and its denominator**.
3. **The stratum it applies to.** Never "overall" where arms exist.
4. **The threshold** — an `<<<EXPERT-JUDGEMENT-REQUIRED>>>` token until a human fills it.
5. **The falsifier** — the specific observation that fails it. Per the rubric: *"A criterion
   without a falsifier is not yet a criterion, it's a wish."*
6. **The bounding preconditions**, restated in full at every point the criterion appears —
   never once and carried by reference. This is Andy's rule from
   `REPRODUCIBILITY_PROTOCOL.md` §0, adopted here because the failure mode is identical: a
   bounded claim quoted without its bounds is an unbounded claim.

### 5.3 The denominator rule — the single strongest anti-"cannot-fail" control here

**[INVENTED]**

**One denominator, declared in writing before the run, and every excluded variant counted
into a named exclusion bucket that is reported alongside the result.**

A concordance rate can be made arbitrarily favourable by quietly dropping variants from the
denominator — no-calls, parse failures, transcript-mismatch cases, lookup timeouts. Each
individual exclusion is usually defensible; the aggregate is where a result stops being a
measurement. The control is not "do not exclude" — some exclusions are correct. The control
is that **the exclusion buckets are named in advance, counted, and published with the
result**, so the reader can compute what the rate would have been under a different exclusion
policy.

A result reported without its exclusion table is not a result under this design, whatever its
value.

### 5.4 Binarisation — a definition with the force of a threshold

**[ANALOGY]** Sensitivity, specificity and PPV — which the rubric's 1a satisfier names — are
defined for a binary outcome. GEPER produces a five-class ACMG output (P / LP / VUS / LB / B).
They are therefore **undefined** until a binarisation rule is fixed, and different defensible
rules produce materially different numbers from identical data. Treating VUS as negative,
excluding VUS entirely, or handling it as a third outcome are all defensible, and they are not
close to each other numerically.

This is nominally a definition rather than a threshold. It is an
`<<<EXPERT-JUDGEMENT-REQUIRED>>>` anyway, because it determines what the resulting sensitivity
figure *means clinically* — and it is exactly the kind of choice that gets made silently, in
an analysis script, by whoever writes it first.

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-02
  DECIDE   — the binarisation rule for computing sensitivity/specificity/PPV from GEPER's
             five-class output, specifically how VUS is treated: as negative, as excluded
             (which changes the denominator and interacts with EJ-07 and §5.3), or as a
             third class with a separate metric. The choice must be recorded before the
             first run, with its clinical rationale, because it is not recoverable from the
             results afterwards.
  GUIDANCE — Richards et al., ACMG/AMP 2015, Genet Med 17(5):405-424 — the five-tier system
             and the intended handling of Uncertain Significance in reporting [unfetched].
             Jennings et al., AMP/CAP, J Mol Diagn 2017;19(3):341-365 — analytical
             sensitivity/specificity definitions for panel validation [unfetched].
  DECIDED-BY — the clinical-genetics lead, on the basis of how a VUS is actually handled in
             the reporting workflow defined by EJ-01. A VUS that stops the workflow and a
             VUS that is reported as a negative are different clinical objects and cannot
             share a binarisation rule.
>>>

### 5.5 The error taxonomy — structure, and one place where a threshold already exists

**[INVENTED — the taxonomy]** Discordance is not one thing, and a single "concordance rate"
suppresses the distinction that matters most clinically: the direction of the error.

| Class | Definition | Clinical character |
|---|---|---|
| **E1** | **Clinically opposed:** truth P/LP ↔ GEPER B/LB, either direction | The most severe class |
| **E2** | Truth P/LP → GEPER VUS | **Missed finding.** Safety-relevant |
| **E3** | Truth B/LB or VUS → GEPER P/LP | **Over-call.** Safety-relevant — risk of unnecessary intervention |
| **E4** | Truth B/LB → GEPER VUS | VUS inflation. Burden, not danger |
| **E5** | Within-tier drift: P↔LP, B↔LB | Sub-clinical under most reporting conventions |
| **E6** | GEPER produced no classification | **Not a concordance failure and not a silent exclusion** — see §5.3 |

**E1 carries no token, deliberately.** Angela's rubric has already decided it: 1a's falsifier
states that any P/LP vs. B/LB disagreement is *"an automatic fail, no tolerance."* That
decision exists, was made by the rubric's owner, and this document applies it rather than
re-opening it. **Noted explicitly because the absence of a token here is a judgement being
respected, not an omission** — and because a reader auditing the token count should be able to
see why E1 is not among them.

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-03
  DECIDE   — the maximum acceptable E2 rate (truth Pathogenic/Likely Pathogenic classified
             by GEPER as VUS) in Arm A. This is the missed-pathogenic rate and is the single
             most safety-relevant number in the entire design.
  GUIDANCE — Richards et al., ACMG/AMP 2015, Genet Med 17(5):405-424 [unfetched]. Jennings
             et al., AMP/CAP, J Mol Diagn 2017;19(3):341-365, analytical sensitivity
             [unfetched]. CLIA 42 CFR §493.1253(b)(2) [unfetched].
  DECIDED-BY — the laboratory director / accountable clinical geneticist, on the basis of
             the intended use fixed in EJ-01 and the clinical consequence of a missed
             pathogenic call in that specific use — including whether a human reviewer sees
             the evidence trail and could catch it. A rate acceptable behind mandatory
             expert review is not acceptable without it.
>>>

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-04
  DECIDE   — the maximum acceptable E3 rate (truth Benign/Likely Benign or VUS classified by
             GEPER as Pathogenic/Likely Pathogenic) in Arm A: the over-call rate. Its
             consequence is different in kind from E2 — unnecessary intervention, surveillance
             or cascade testing rather than a missed finding — so it needs its own number and
             its own reasoning, not a symmetric one derived from EJ-03.
  GUIDANCE — Richards et al., ACMG/AMP 2015 [unfetched]. Jennings et al., AMP/CAP, J Mol
             Diagn 2017;19(3):341-365, analytical specificity / false-positive rate
             [unfetched].
  DECIDED-BY — the laboratory director / accountable clinical geneticist, on the basis of
             what clinical action a P/LP call triggers in the intended-use workflow.
>>>

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-05
  DECIDE   — the maximum acceptable E4 rate (truth Benign/Likely Benign classified as VUS):
             the VUS-inflation rate. Not a safety failure, but a high rate makes the tool
             clinically unusable by generating review burden that swamps its benefit, and a
             validation that only measures safety-relevant errors will not surface it.
  GUIDANCE — Richards et al., ACMG/AMP 2015, Genet Med 17(5):405-424 — guidance on
             minimising unnecessary VUS assignment [unfetched]. ACMG/AMP interpretation
             literature on VUS burden in clinical reporting [unfetched, no single document
             named — this citation is weaker than the others and should be treated as a
             pointer to a literature rather than to a section].
  DECIDED-BY — the clinical-genetics lead with whoever owns the downstream review workload,
             on the basis of review capacity in the intended-use workflow.
>>>

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-06
  DECIDE   — the tolerance for E5 within-tier drift (P↔LP and B↔LB). The rubric's 1a
             falsifier already anticipates this decision, in its words: "VUS-boundary drift
             beyond a pre-agreed count also fails." The count is not pre-agreed and this
             token is where it is agreed. Note the decision has two halves: the tolerance
             itself, and whether P↔LP and B↔LB share one, since their clinical consequences
             are not symmetric.
  GUIDANCE — Richards et al., ACMG/AMP 2015 [unfetched]. `VALIDATION_READINESS_RUBRIC.md`
             D1/1a falsifier @ `89bf125` [in-repo, verified — this is the strongest-sourced
             blank in the document: the requirement for this decision is already ratified,
             only its value is open].
  DECIDED-BY — the clinical-genetics lead, on the basis of whether P and LP (and B and LB)
             produce different clinical actions in the intended-use workflow. If they do not,
             the tolerance can be wide; if they do, it cannot.
>>>

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-07
  DECIDE   — the maximum acceptable E6 no-call / not-evaluated rate, and — separately —
             whether no-calls sit inside or outside the concordance denominator. The two
             halves interact with EJ-02 and §5.3 and must be decided together: a generous
             no-call allowance combined with no-calls excluded from the denominator is the
             configuration in which a concordance figure stops being able to fail.
  GUIDANCE — Jennings et al., AMP/CAP, J Mol Diagn 2017;19(3):341-365 — no-call rate and
             reportable-range requirements [unfetched]. CLIA 42 CFR §493.1253(b)(2),
             reportable range [unfetched]. ISO 15189:2022 clause 7.3.3 [unfetched, clause
             uncertain].
  DECIDED-BY — the laboratory director. The denominator half of this decision in particular
             must not be delegated to whoever writes the analysis script, which is where it
             will otherwise be made.
>>>

### 5.6 Pre-registration — thresholds are fixed before the first run

**[ANALOGY, from an in-repo source]** The rubric's own 1a satisfier requires "a pass
threshold agreed **in advance**." This design makes that operational: **every EJ token must
be filled, and the filled document committed, before the first measurement run.** A threshold
set after the results are visible is not a threshold; it is a description of the results.

The mechanism is deliberately the cheapest one that works and needs no tooling: fill the
tokens, commit, run, and let `git log` on this file carry the date ordering. If any token is
filled after the run's own commit, the ordering is visible to anyone who looks — including an
assessor.

### 5.7 and 5.8 — governance decisions

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-17
  DECIDE   — what triggers re-validation, and on what cadence. At minimum the trigger list
             must address: a change to any ACMG rule implementation; a change to any
             configuration value the validation certified (EJ-19); a new or upgraded model
             plugin entering an evidence path; a ClinVar release advance beyond a stated
             interval; and elapsed time with no change at all. A validation with no
             expiry silently becomes a claim about code that no longer exists.
  GUIDANCE — ISO 15189:2022 clause 7.3.3, re-validation on change to the examination
             procedure [unfetched, clause uncertain]. CAP Molecular Pathology Checklist,
             revalidation requirements following assay modification [unfetched, item number
             not asserted]. Jennings et al., AMP/CAP, J Mol Diagn 2017;19(3):341-365,
             sections on assay modification [unfetched].
  DECIDED-BY — the quality manager with the laboratory director. The change-triggered half
             needs the engineering lane's input on what constitutes a material change, but
             the decision is not theirs.
>>>

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-18
  DECIDE   — who signs the validation report, and what accreditation standing that signature
             must carry for a NABL/ICMR assessor to accept it. An unsigned validation report,
             or one signed by whoever performed the work, is a document rather than a
             validation. This also determines who must be in place before the studies are
             run, which makes it a scheduling dependency and not only a formality.
  GUIDANCE — ISO 15189:2022 clause 5.2 / 5.3, laboratory director responsibilities and
             authorisation of procedures [unfetched, clause numbers uncertain]. NABL 112,
             Specific Criteria for Accreditation of Medical Laboratories [unfetched, document
             number named from training knowledge and NOT confident — verify before citing
             this to anyone external]. CLIA 42 CFR §493.1445, laboratory director
             responsibilities [unfetched].
  DECIDED-BY — the organisation's regulatory/accreditation lead. Not a technical decision and
             not one this floor can make.
>>>

---

## 6. Dependencies, blockers, and what this design cannot close

### 6.1 The ICMR/India population gap — a blocking dependency with no workaround

**[GUIDANCE, corroborated in-repo — Meredith @ `6f27e7b`, "The ICMR/India gap"]**

There is **no population-matched ground-truth dataset for the Indian population obtainable
for a commercial product today.** Three candidates checked, zero usable: IndiGen is
research-only and was already retired from GEPER's own evidence pipeline for exactly this
conflict; GenomeIndia is DAC-gated and its own sourcing frames commercial use as an
*unsolved policy gap* — **there is nothing to apply for yet**; GenomeAsia100K's Data Access
Agreement text was not reachable.

**Consequences for this design, stated rather than worked around:**

- S1's truth set (ClinVar expert panels) and S2's truth set (GIAB) are both
  ancestry-skewed away from South Asian populations. **[VERIFIED via `6f27e7b`]** GIAB has no
  South Asian ancestry among its seven genomes at all.
- **No substitution is made.** Per the dispatch's explicit instruction, a non-Indian cohort is
  not quietly used as a stand-in. Both studies' claims are bounded by the ancestry composition
  of their truth sets, and that composition is reported in the claim itself.
- Every report from S1 and S2 must carry an explicit ancestry-representativeness statement.
  This is likely the first question a NABL/ICMR assessor asks, and it must be answered before
  it is asked.

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-12
  DECIDE   — whether population-matched (Indian) validation is a release gate. This is a
             go/no-go, not a threshold. If yes, the release is blocked on a dataset that does
             not currently exist and the decision is really about which acquisition path to
             open — a GenomeAsia100K DAC application, a GenomeIndia licensing approach with
             no established process, or a prospectively-collected internal cohort. If no,
             that decision must be recorded with its rationale, because an assessor will ask
             and "we could not obtain data" is only an answer if it was a decision rather
             than an omission.
  GUIDANCE — ICMR National Ethical Guidelines for Biomedical and Health Research Involving
             Human Participants, 2017 [unfetched, year uncertain]. ISO 15189:2022 clause
             7.3.4, biological reference intervals and their applicability to the served
             population [unfetched, clause uncertain]. `geper/GROUND_TRUTH_DATASET_AUDIT.md`
             @ `6f27e7b`, "The ICMR/India gap" [in-repo, verified].
  DECIDED-BY — the human, with regulatory counsel. This escalates above the floor: it is a
             product and market-access decision with a budget consequence, not a study-design
             decision, and Meredith's own audit says it "should reach whoever owns the
             ICMR/NABL readiness question directly."
>>>

### 6.2 Dataset dependencies, and their gating status

| Study | Dataset | Gated? | Status |
|---|---|---|---|
| S1 | ClinVar 3★/4★ subset | **No** | Public domain; already flowing through the existing client; no new integration **[VERIFIED-IN-REPO]** |
| S2 | GIAB HG001-007 | **No, but licence citation is not closed** | Meredith's outstanding item 4: no GIAB-specific licence document was locatable. Inherited, not resolved |
| S3 | An external QA scheme's materials | **Yes — unresolved** | Enrolment eligibility for a non-laboratory is unknown (§4.1). Blocks S3's design past its current state |
| — | kim_pipeline PGx star-allele truth | **Yes — blocked** | PharmVar is CC-BY-NC-ND. **No usable ground truth exists for the PGx stage**, so no PGx validation is designed here. Named as an uncovered area rather than omitted |

### 6.3 Cross-lane dependencies

- **Frozen ClinVar snapshot vs. the live API (§2.2).** Infrastructure question, Andy's lane.
  This design states the requirement and does not answer how it is met.
- **The Arm C ablation lever (§2.4).** Needs an execution check before it can be relied on.
  Definition-only phase; not run here.
- **`871f23d`'s untested ClinGen Dosage Sensitivity module.**
  **[VERIFIED via `VALIDATION_BASELINE_2026-08-21.md`, Findings item 1]** A 324-line
  production module was wired into a real ACMG evidence path (`is_lof_intolerant`,
  PVS1-adjacent) with **zero test coverage**, and the baseline record names it as "the one
  production code path in the baseline whose correctness rests entirely on code review."
  **It sits inside the evidence stack Arm A measures.** This does not block S1 — Arm A
  measures the stack as it is, defects included, which is the point of a concordance study
  — but if Arm A underperforms, this module is the first place to look, and that should be
  written down now rather than rediscovered later.

### 6.4 What no study in this document covers

Stated so the boundary is visible rather than inferred from silence: CNV and structural
variants (no usable truth set — DECIPHER is research-only); pharmacogenomic star-alleles
(§6.2); mitochondrial variant classification; Indian-population performance (§6.1); and
inter-run reproducibility (Andy's, by design).

---

## 7. Flags to god — issues found, not edited

Each of these touches a document owned by someone else. None is edited here.

### 7.1 Rubric 1a's satisfier names GIAB as an example ACMG-classification truth set

`VALIDATION_READINESS_RUBRIC.md` D1/1a satisfier: *"a named truth set (e.g. a GIAB-derived
panel, or an internal curated ground-truth set...)"*. **[VERIFIED via `6f27e7b`]** GIAB
carries no ACMG classifications — it is a genotype truth set. As written, the satisfier could
be read as permitting a GIAB-only study to satisfy a classification-concordance criterion,
which it cannot. **Angela's text, Angela's call.** Flagged, not amended.

### 7.2 Rubric 1b's "confidence scores within a stated numeric tolerance"

**[VERIFIED-IN-REPO]** `acmg_rules.py:601` — the per-criterion `confidence` field is
categorical (`"High" | "Moderate" | "Low"`), not numeric. A separate `significance_score`
exists. Whichever is meant, "numeric tolerance" does not apply to the categorical field.
Touches 1b, which is Andy's and Angela's, not mine. Flagged only.

### 7.3 Rubric 1c's open item — this design supports adding the state she asked about

Angela's open item 2 asks whether 1c needs a "not yet applicable, plan on file" state distinct
from "failed." §4.1 and §4.3 give the input that decision needs: PT enrolment may be
**structurally unavailable** to a non-laboratory, and ISO 15189's own EQA clause provides for a
documented alternative approach where EQA is unavailable. That makes "not applicable, with a
documented alternative" a recognised path rather than a euphemism for a gap. **Still her
call.**

### 7.4 ⚠ A correction to a peer's committed document — Meredith's circularity flag

`geper/GROUND_TRUTH_DATASET_AUDIT.md` @ `6f27e7b` flags the circularity at
`clinvar_client.py:344-345` and names "PP5/BP6" as a channel. The flag is **directionally
right and was the right thing to raise** — but §1.2 above verifies that **PP5 never fires**
(`acmg_rules.py:923-926`) and **BP6 is excluded from the point tally** (`:3509-3522`), so
neither can move a classification. The live channel is **PS1/PM5 only**, and it is a
different shape (a *neighbouring* variant's label, not the variant's own).

**Why this needs routing rather than a footnote:** the dispatch's own remedy list included
"disable PP5/BP6 for validation runs." Acting on that as stated would produce a control that
does nothing while looking like a control — a strictly worse outcome than no control at all.
The audit's row is worth an addendum. **Hers to make, not mine.**

---

## 8. Outstanding items

1. **Every [GUIDANCE] citation marked `[unfetched]` must be verified against its primary
   source before this design is used to run anything.** Per §0.3 this is a hard gate, not a
   recommendation. The highest-risk items, in order: **NABL 112's document number**
   (named from training knowledge with low confidence — do not cite externally until
   checked); **every ISO 15189:2022 clause number**; the existence and current scope of a
   CAP in-silico interpretation survey; whether any Indian genomic variant-interpretation
   EQAS exists at all. A citation that fails verification must be **re-marked [INVENTED]**,
   not quietly dropped.
2. **Whether PT/EQA enrolment is available to a non-laboratory software vendor** (§4.1). The
   single unknown that most changes S3's shape.
3. **The Arm C ablation lever needs its execution check** (§2.4, four steps). If it fails,
   Arm C is an engineering dependency rather than a config flip.
4. **The S1 feasibility pre-count** (§2.6) should run before any sample size is committed.
   Cheap, count-only, and it determines whether Arm A is large enough to carry the study at
   all.
5. **GIAB's licence citation remains open**, inherited from Meredith's outstanding item 4.
6. **Independent legal review**, the same standing recommendation both prior audits carry.

---

## 9. Covering note

**What this document is:** the shape of two validation studies (S1 classification
concordance, S2 variant-calling accuracy), the structure of a third that may not be a study
at all (S3 external QA, pending §4.1), and the six-slot form every acceptance criterion in
rubric domain D1 must take.

**What it is not:** a validation. Nothing has been measured. D1 still scores zero and this
document does not change that — it describes how it would stop being zero.

**Unfilled expert-judgement decisions: 19.** Indexed at §0.1, greppable via
the §0.1 grep. Each names the guidance that would inform it and
who decides it. **None may be filled by anyone on this floor.**

**Marked [INVENTED] — where to push back hardest:**

- The **Arm C paired ablation** and the **B1/B2 curator-provenance sub-strata** (§1.3, §2.5).
  The design's most substantive original contribution and its least supported. No external
  source proposes either for this setting.
- The **E1-E6 error taxonomy** (§5.5). Reasoned from clinical consequence, not taken from a
  published error classification.
- The **denominator rule with named exclusion buckets** (§5.3).
- The **feasibility pre-count** as a required first step (§2.6).
- The **double-reporting of Arm A with and without `bp6_fired`** (§2.3).
- Slots 2-4 of the six-slot acceptance-criterion form (§5.2); slots 1, 5 and 6 come from
  Angela's and Andy's documents.

**The heaviest [ANALOGY] items — where a plausible-looking wrong answer would hide:**

- **§1.2b curator circularity.** The stratum rests on an inference about how expert panels
  distribute across genes. The facts under it are verified; the inference joining them is not.
- **§2.4 the ablation lever.** Architecturally clean, obviously right on inspection,
  **unverified by execution.** §2.4 carries a four-step check for exactly this reason.
- **§4.1 PT enrolment eligibility.** No scheme's actual enrolment terms were read. If this is
  wrong, §4 is wrong.
- **§2.6 clustering.** Standard practice, no genomics-specific source.

**Two things an assessor should be pointed at directly, because they are stronger than
expected:** GEPER's engine **already** closes record-level ClinVar circularity in its own
code, citing ClinGen SVI's deprecation in its own comments (`acmg_rules.py:2943-2951`,
`:3509-3522`) — that is a design decision made before any validation pressure existed. And
the code underlying every architectural claim here is **byte-identical to the baseline tag**,
confirmable in one `git diff` (§0.4).

**One thing the design cannot close, and does not pretend to:** the ICMR/India population gap
(§6.1). It is escalated as EJ-12, a go/no-go for the human with regulatory counsel, not a
study-design decision.
