# GEPER Validation Study Design

**Status:** DEFINITION ONLY. No measurement runs performed, no data collected, no
acceptance thresholds set. This document defines how GEPER's accuracy would be measured.
It contains no measurements.
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

**Where the failure conditions are.** §5.9 is the study-level failure declaration — what
result would mean *this validation failed*, as distinct from *this criterion failed*. It is
the one part of this document that determines whether the studies can produce a negative
result at all, and it should be read before the study designs rather than after.

**The frame every section below depends on:** prior work established that GEPER no longer
**misstates what it observed**. It did **not** establish that what it observes is
**correct**. That gap is not partial — it is absent, and closing it is what this document
plans.[^baseline]

Until one of these studies is executed, **nothing in this document is evidence of GEPER's
accuracy.** It is a plan to obtain that evidence.

**Every acceptance threshold in this document is deliberately blank.** They are blank
because setting them is a clinical judgement, not an engineering one — and they are the
reason this document is in front of you.

[^baseline]: Provenance, kept out of the frame: tag `validation-baseline-2026-08-21`
    (`470a993`), whose own message reads *"NOT ESTABLISHED: that what it observes is
    CORRECT... Not partial — absent."*

### 0.1 Index of unfilled expert-judgement decisions — **25**

Every one of the 25 is a decision a qualified human must make; none may be inferred from
this document. Verify the count has not drifted:

```
# (a) THE COUNT -- this one is legitimate, because the count IS the claim.
grep -cE '^<<<EXPERT-JUDGEMENT-REQUIRED: EJ-[0-9]+[a-z]?$' VALIDATION_STUDY_DESIGN.md  # expect 25

# (b) WELL-FORMEDNESS -- prints the id of any token that does not open and close
#     exactly once. EXPECT NO OUTPUT. Do not substitute `grep -c '^>>>$'`: see below.
awk '/^<<<EXPERT-JUDGEMENT-REQUIRED:/ { if (o) print "UNCLOSED: " id; o=1; id=$2; next }
     /^>>>$/ { if (!o) print "ORPHAN CLOSER: line " NR; else o=0; next }
     END { if (o) print "UNCLOSED AT EOF: " id }' VALIDATION_STUDY_DESIGN.md

# (c) UNIQUENESS -- prints any id used twice. EXPECT NO OUTPUT.
grep -oE '^<<<EXPERT-JUDGEMENT-REQUIRED: EJ-[0-9]+[a-z]?$' VALIDATION_STUDY_DESIGN.md \
  | sort | uniq -d

# (d) INDEX AGREEMENT -- prints any id in one place and not the other, in EITHER
#     direction. EXPECT NO OUTPUT.
diff <(grep -oE '^<<<EXPERT-JUDGEMENT-REQUIRED: EJ-[0-9]+[a-z]?$' VALIDATION_STUDY_DESIGN.md \
        | grep -oE 'EJ-[0-9]+[a-z]?' | sort) \
     <(sed -n '/^| ID | § | The decision |$/,/^$/p' VALIDATION_STUDY_DESIGN.md \
        | grep -oE '^\| EJ-[0-9]+[a-z]?' | grep -oE 'EJ-[0-9]+[a-z]?' | sort)
```

> **⚠ WHY (b) REPLACED `grep -c '^>>>$' # expect 25`, 2026-08-22 — the old check was
> agreeing with a number.** A closer count of 25 against 25 openers is satisfied by a document
> in which **one token carries two closers and another carries none**: the gap and the extra
> cancel, and the total is silent about it. The property being claimed is *"every token opens
> and closes exactly once"*, which is a **per-item** property, and no total can confirm one.
> **(b) names the exception instead** — it prints the offending id, and **prints nothing when
> the document is sound.** *An empty exception list is evidence; a matching total is not.*
> Replaced under the floor-wide rule: **a total may confirm a per-item property only if the
> check names which items it examined and which it excluded.**
>
> **This is the third repair to this document's own self-check**, after the grep that matched
> its own prose and the pattern that assumed token ids were digits. All three were checks that
> passed while the property they described went unverified — which is the failure mode this
> index exists to prevent, arriving each time from inside the instrument rather than the text.

A count other than 25 means tokens were added, or silently filled in — either way this index
is stale and the document must not be used as though it were complete.

**⚠ THE FIRST GREP'S PATTERN CHANGED on 2026-08-22** — `EJ-[0-9]+` became
`EJ-[0-9]+[a-z]?` — because splitting EJ-03 introduced the suffixed ids `EJ-03a` and
`EJ-03b`. **Against the current count of 25, the old pattern returns 23**, silently dropping
both suffixed tokens: it under-reports by exactly the two ids it cannot match, and does so as
a plausible-looking number rather than an error. *(At counts of 24 and 23 the same pattern
returned 22 and 21. The discrepancy has been RE-DERIVED against the new count rather than carried
forward — it is a function of the count, and a stale-pattern warning that quotes a stale
count would be the defect it exists to describe, twice over.)* Flagged because a self-check
whose own pattern goes stale is the failure mode this index exists to prevent, and it is the
second time this document's self-check has needed repair rather than the document (the first
was the §0.1 grep matching its own prose).

**Count history — recorded so a drift is distinguishable from a deliberate addition.**
19 at first approval (`a505ea0`). **EJ-20, EJ-21 and EJ-22 added 2026-08-22** with §5.9, the
study-level failure declaration — parameters of a rule that did not previously exist.
**EJ-23 added 2026-08-22** with §5.1.1, the two provisional intended-use statements. In every
case no existing token was split, merged, renumbered or filled.

**EJ-03 was SPLIT into EJ-03a and EJ-03b on 2026-08-22** (§5.5), taking the count 23 → 24.
**This is one decision separated into two, not a new decision** — the definitional half was
always inside EJ-03, doing work that its single threshold-shaped question concealed. Recorded
so the +1 is not read as new scope.

**EJ-23 was REMOVED on 2026-08-22, taking the count 24 → 23. THE −1 IS A DECISION ANSWERED,
NOT A DECISION DROPPED**, and the distinction is the reason this line exists. EJ-23 asked
which of §5.1.1's two provisional intended-use shapes to adopt. **The human answered it** by
ratifying the prioritisation shape (§5.1.2), so the token had nothing left to hold open. It
was kept standing only while that ratification was under review, and §5.1.2 recorded at the
time that *"the count of 23 overstates the number of genuinely open decisions by one"* —
**this removal is that note being discharged.** No other token absorbed the question, no
token was merged or renumbered, and the shape EJ-23 selected is now stated outright in
§5.1.2 rather than deferred to a token.

**EJ-24 and EJ-25 were ADDED on 2026-08-22 with §5.9.8, taking the count 23 → 25.** Both are
parameters of **F5**, a failure route that did not previously exist — new scope, and labelled
as such rather than as elaboration of something already present. **The id 23 is deliberately
NOT reused**: numbering resumes at 24 so that `EJ-23` continues to refer to the removed
intended-use selection wherever it appears in this document's history, and a reader who
notices the gap in the sequence is looking at a record rather than at an error.

**One token has been PARTLY RULED rather than filled: EJ-19.** Its lockability half is closed
(2026-08-22); its value half remains open, so it still counts as unfilled. Recorded here
because a token that is half-answered is exactly the kind of state a bare count cannot show.

| ID | § | The decision |
|---|---|---|
| EJ-01 | 5.1 | The intended-use statement — what clinical claim this validation is meant to support |
| EJ-02 | 5.4 | The binarisation rule: how VUS is treated when computing sensitivity/specificity |
| EJ-03a | 5.5 | **DEFINITION:** which event E2 counts (mis-drafted / not-surfaced / union / end-to-end) |
| EJ-03b | 5.5 | **THRESHOLD:** acceptable E2 rate in Arm A — **blocked on EJ-03a** |
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
| EJ-20 | 5.9.7 | The pattern of per-criterion outcomes that constitutes FAILURE of S1 as a whole (rule at 5.9.2) |
| EJ-21 | 5.9.7 | What a FAIL obligates — claim narrowed, intended use restricted, or release blocked (rule at 5.9.5) |
| EJ-22 | 5.9.7 | The trigger values that make S1 INVALID (could not measure) rather than FAILED (rule at 5.9.3) |
| EJ-24 | 5.9.8 | Whether a score TIE across the ratified boundary counts as an F5 rank inversion |
| EJ-25 | 5.9.8 | The recall criterion for the ranked output: queue depth K, and truth-P/LP fraction within it — **blocked on operator scope (review capacity)** |

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

### 0.2a What would make a `[VERIFIED-IN-REPO]` badge self-invalidating — proposed 2026-09-10, options 1 and 2 BUILT the same day (god's question, conv-validation-badges; header corrected 2026-09-10 after the "not built" framing below itself went stale -- see the note after the recommendation)

**The structural defect, stated plainly:** a `[VERIFIED-IN-REPO]` badge as used throughout
this document is a claim about a moment — this session, this commit — written in a form
(bold text, present tense, "checked directly in this repo") that reads as a claim about
whatever the reader's present is. Nothing re-checks it. §0.4/§0.4a is this document catching
its own citations drifting **twice** on exactly this failure mode, both times by accident,
both times because someone happened to re-run the underlying commands rather than because
anything forced the check. A badge with no sha attached cannot be re-verified without redoing
the original work by hand — which is precisely why it doesn't get redone, and why the PP5
row that started this audit survived with a quote that no longer existed, at a line that had
moved, describing something logically impossible, sitting in a table of otherwise-correct
rows with the identical badge on every row.

**Three options, in order of how much they'd actually fix versus how much they'd cost:**

1. **Pin the sha, unconditionally, on every badge.** `[VERIFIED-IN-REPO @ abaa7c96]` instead
   of `[VERIFIED-IN-REPO]`. Cheapest change — a formatting convention, no tooling — and it
   converts an unanswerable question ("is this still true?") into an answerable one ("what
   changed between that sha and now, in the cited file?"), which is exactly the `git diff`
   invocation this audit ran by hand 19 times. Does not catch drift automatically; it only
   makes catching it a five-second `git diff --stat` instead of a manual re-derivation of
   what the citation was even supposed to prove. This is the weakest fix that is still a real
   fix, and is the one §0.4 already does informally (naming `9112411`) without extending the
   convention to the other 18 badges — which is exactly why only §0.4 caught its own second
   drift and the PP5 row did not catch its first. **BUILT, same day, after this section was
   written**: every badge in this document now carries a pinned sha (confirmed by grep,
   2026-09-10).
2. **A quoted string a test greps for.** For badges that cite an exact string (PP5's reason
   text, `_BP6_DEPRECATION_CAVEAT`'s content, `STAR_PRACTICE_GUIDELINE = 4`), a small CI check
   could grep the cited file for the quoted substring and fail the build if it's gone, the same
   shape as `tests/test_round16_not_evaluated_categories.py`'s existing drift-protection test
   for `_NEVER_INTEGRATED_ACMG_CODES`. This catches the PP5 defect's first half (the quote no
   longer existing) automatically and continuously, not just at audit time — but it cannot
   catch a moved-but-still-correct line number (most of this audit's findings), and it cannot
   catch a characterisation that is wrong while the quote is still accurate word-for-word,
   which is PP5's more serious half. A passing grep is weaker evidence than it looks: it would
   have stayed green through everything except the exact string going missing. **BUILT, same
   day**: `geper/tests/test_validation_study_design_badge_pins.py` (three tests: PP5's reason
   string, `_BP6_DEPRECATION_CAVEAT`'s fragment, `STAR_PRACTICE_GUIDELINE == 4`; each imports
   or calls the real production object rather than re-implementing the value under test) --
   catches exactly what this paragraph says it can, and nothing more. The paragraph's own
   limits above are unchanged by the test existing; they are the reason a green run of it must
   not be over-trusted, and the test's own docstring says so.
3. **Drop the badge, cite the git blame instead, or don't cite a line at all.** The dispatch's
   own framing — an unverifiable verification claim is worse than no claim — taken to its
   conclusion: state the fact and let a reader run `git log -S"<distinctive phrase>"` or
   `grep -rn` themselves rather than asserting a location that will go stale. Loses the
   "reviewer can re-check it in seconds" property this document's own legend (§0.2) claims for
   the badge, which is a real cost, not a free option. Not taken.

**Recommendation, not a decision when this was written:** option 1 (pin the sha on every badge)
for all of them, plus option 2 for the handful whose entire evidentiary weight rests on one
exact string (PP5, BP6's caveat, the star-rating default) rather than a broader "this function
does X" claim that a line number alone can't fully capture anyway. **Both were built the same
day this section was written, and this paragraph originally said "not built in this pass" --
which was accurate for a few hours and then became the exact kind of stale, unrechecked claim
this whole section exists to describe. Corrected 2026-09-10 (`conv-andy-pp5-clo`) after god
caught a document whose own "we have not built this" had quietly gone false and drawn no
scrutiny for it, precisely because a pessimistic claim about your own state reads as honest
modesty rather than something to verify.**

### 0.3 Citation verification status — every external citation was attempted, 2026-08-22

**The first draft of this document cited every external source from training knowledge,
marked `[unfetched]`, and made primary-source verification a hard gate before use. That
gate has now been run.** Every external citation below was attempted against its primary
source. Each now carries one of five verdicts:

| Marker | Meaning |
|---|---|
| **[VERIFIED]** | Fetched. The cited document exists with the stated identifiers, and the thing attributed to it was located. |
| **[VERIFIED (secondary)]** | The attributed content was confirmed, but via an accreditation body or published summary rather than the standard itself — see the ISO note below. |
| **[UNFETCHED-PAYWALLED]** | Attempted, blocked by a paywall or purchase requirement. The blocker is named. |
| **[UNFETCHED-NOT-LOCATED]** | Attempted, could not be confirmed. Where the search went is named. |
| **[INVENTED]** | Fetched, and **the citation was wrong.** Retained as INVENTED rather than silently corrected — see below. |

**⚠ Four citations failed verification and are now marked [INVENTED]** (§4.3, EJ-05,
EJ-12, EJ-15/EJ-16). They are **not** quietly corrected to whatever the source actually
says, because *a corrected citation looks identical to one that was right all along.* Each
INVENTED marker states what was asserted, what is actually true, and where the truth was
found — so the record distinguishes a claim that was checked and held from one that was
checked and failed. In three of the four the *substance* of the argument survived and only
the clause number was wrong; that distinction is stated at each site, and it does not
soften the marker.

**⚠ ISO 15189:2022 is paywalled and was NOT read.** `iso.org` returned HTTP 403; the
standard is sold, not published. Every ISO clause verdict below therefore rests on
**secondary sources** — accreditation-body documents (SADCAS, NATA), published
clause-by-clause comparisons, and the standard's indexed table of contents — never on the
standard's own text. That is weaker than the CLIA and journal verifications, which were
read directly, and it is marked **[VERIFIED (secondary)]** rather than [VERIFIED] wherever
it applies. **An assessor holding the actual standard can overturn any ISO verdict in this
document, and should be invited to.** (One narrower worry this paywall raised is now closed:
NABL 112A §7.8.5, the NGS clause EJ-18 (§5.7/5.8) cites, turned out to be free — confirmed
2026-08-31, see EJ-18's guidance — so ISO 15189:2022 remaining unpurchased is irrelevant to
that particular citation, though the paywall stands for every ISO clause verdict elsewhere.)

This remains a weaker standard than the one `geper/GROUND_TRUTH_DATASET_AUDIT.md` met for
datasets, which fetched primary sources directly throughout. Citations corroborated by an
in-repo fetched source are additionally marked `[corroborated in-repo]`.

### 0.4 What is pinned, and the check that it is — **re-run 2026-08-22, and it moved**

**[VERIFIED-IN-REPO]** Every code fact in §1 was originally read at `71d0e37` and confirmed
byte-identical to the baseline tag. **That check was re-run on 2026-08-22 and no longer
passes as originally written.** What follows is the corrected version, not the original
claim — recorded this way because a pinning claim that has silently stopped being true is
worse than no pinning claim at all, and §5.6 asks an assessor to trust exactly this kind of
ordering evidence.

**The three files carrying every §1 architectural finding are still byte-identical to the
baseline tag:**

```
git diff --stat validation-baseline-2026-08-21 HEAD -- \
    geper/pipeline/acmg_rules.py geper/pipeline/ps1_pm5/ \
    geper/database/clinvar_client.py                        # empty
```

**`geper/config.py` is NOT, and the original command wrongly included it as though it were.**
Three commits have touched it since the tag (`57e561c`, `560d2df`, `9112411`), all of them
ClinGen dosage-score work by another lane, unrelated to this design:

```
git diff --stat validation-baseline-2026-08-21 HEAD -- geper/config.py   # 39 insertions, 6 deletions
```

**What that does and does not affect, checked rather than assumed:**

- **The `ps1_pm5` configuration block is untouched.** `git diff validation-baseline-2026-08-21
  HEAD -- geper/config.py | grep -E 'ps1_pm5|MIN_STAR_RATING'` returns nothing. The value
  §1.2 and §2.4 depend on — `MIN_STAR_RATING` defaulting to `2`, overridable via
  `GEPER_PS1_PM5_MIN_STAR_RATING` — is unchanged in substance.
- **The line number moved: 1677 → 1710.** Every citation in this document has been updated,
  and now cites the line **as of `9112411`** rather than as of the tag, with that stated at
  each site.

**This is a live illustration of §5.9.3's I5 condition rather than a hypothetical one.** The
code pin drifted underneath a document that asserted it had not, within a day of the document
being written, through entirely legitimate work by another lane. **Any measurement run must
re-verify both pins immediately before executing, not rely on this section** — which is what
I5 already requires, and is now the second time this document has caught one of its own
self-checks failing (the first was the §0.1 token grep matching its own prose).

**The organisation's knowledge graph was queried for NABL/ICMR context and is empty**
(`kg.cjs list` → "The knowledge base is empty"). No internal policy, house standard, or
prior regulatory correspondence informed this design, because none was available. Stated so
its absence is not mistaken for its having been consulted and found silent.

### 0.4a Re-verified 2026-09-10, during a stale-`[VERIFIED-IN-REPO]`-badge audit (conv-validation-badges) — §0.4's own warning came true a second time

**§0.4 predicted this exact failure mode and it happened anyway, unnoticed until this audit.**
Every `[VERIFIED-IN-REPO]` badge in this file (19 sites, this document's own PP5 row among
them — see §2.1's channel table) was checked against the current tree at
`abaa7c96bbd8e6a3efdee7d160a332f0d70922d5`, not re-derived from what this section already
claimed:

```
git diff --stat validation-baseline-2026-08-21 abaa7c96 -- \
    geper/pipeline/acmg_rules.py geper/pipeline/ps1_pm5/ \
    geper/database/clinvar_client.py
#  geper/database/clinvar_client.py |   8 +
#  geper/pipeline/acmg_rules.py     | 311 ++++++++++++++++++++++++++++++++-------
#  geper/pipeline/ps1_pm5/utils.py  |   2 +-
#  3 files changed, 266 insertions(+), 55 deletions(-)

git diff --stat validation-baseline-2026-08-21 abaa7c96 -- geper/config.py
#  geper/config.py | 165 ++++++++++++++++++++++++++++++++++++++++++++++++--------
#  1 file changed, 142 insertions(+), 23 deletions(-)
```

**§0.4's own "still byte-identical to the baseline tag" claim is false again, right now, for
`acmg_rules.py` and `ps1_pm5/utils.py` — it was true only for the commit it was checked at
(`9112411`) and has since drifted a second time through further legitimate work.**
`config.py`'s drift is larger too: 142 insertions now, not the 39 §0.4 recorded.
`MIN_STAR_RATING` moved again, `1710` → `1768`; every citation to it in this document has
been updated in this pass, dated, with the prior value kept alongside the new one rather than
silently overwritten (§2.1, §2.2, §2.4, §2.4's footnote). Several other line citations
throughout this document — none of them self-labelled as pins the way `config.py:1710` was —
had drifted the same way and are fixed at their sites, each marked "re-verified 2026-09-10."

**Restated, because it is the actual finding:** a `[VERIFIED-IN-REPO]` badge with no sha
attached is a claim about the moment it was written, presented in a form that reads as a claim
about the present, and nothing re-checks it automatically. This section is proof the pattern
recurs on a timescale of weeks even when a prior instance is documented in the same file the
next drift happens to — see §0.2a's proposal for what would make a badge catch its own
staleness instead of relying on someone re-running this exact audit again.

### 0.4b The pattern recurred a THIRD time, inside this same pinning pass, before a single sha was written down

**god's implementation dispatch (conv-validation-badges, 2026-09-10) named the merge sha of
§0.4a's audit, `36234d38`, as the sha to pin every badge to (or, its own text, "current
master if you re-verify at the newer one — say which you chose").** The dispatch's own stated
base was `c2258df10ccee86a92639ac47e7ca2f26a262b75` — a later commit. Checked before pinning
anything, per this dispatch's own "re-verify every premise" instruction:

```
git diff --stat 36234d38 c2258df1 -- geper/pipeline/acmg_rules.py geper/pipeline/ps1_pm5/ \
    geper/database/clinvar_client.py geper/config.py geper/pipeline/prioritization_engine.py
#  geper/pipeline/acmg_rules.py | 21 +++++++++++++++++++++
#  1 file changed, 21 insertions(+)
```

**`acmg_rules.py` had drifted AGAIN, +21 lines, in the single interval between the audit
merging and this implementation dispatch landing — unrelated calibration-disclosure work, not
a mistake by anyone.** Every citation at or after line 2141 in this document (BP6's point-tally
skip and its comment, `_BP6_DEPRECATION_CAVEAT`, `_clinvar_crossref`'s definition and its own
comment) had moved a further ~21 lines and was re-verified and re-fixed against `c2258df1`
directly rather than assumed from arithmetic on the diff. Citations before line 2141 (PP5,
PS1, PM5, the confidence field, the star-rating gate, `CombineResult.classification`) were
unaffected and re-confirmed exact at `c2258df1` regardless.

**Chose to pin every badge in this document to `c2258df1` (this dispatch's base), not
`36234d38` (the audit's own merge sha) — stated explicitly, since the dispatch allowed
either.** `c2258df1` is the newer, and now the correct, verified-against sha; pinning to the
older sha would have shipped a badge that was already one drift behind its own pin on the day
it was written.

**This is the third distinct instance of the exact failure this whole card exists to fix,
inside the time it took to fix it once:** §0.4 (2026-08-22, first drift), §0.4a (2026-09-10,
§0.4's own correction gone stale a second time), and this section (2026-09-10, the same day,
inside the implementation of the fix for the first two). No claim in this document about "the
current state of `acmg_rules.py`" survives more than a few hours without being re-checked. A
sha pin does not stop this from happening — nothing can, short-of freezing the file — it only
keeps the cost of noticing at a five-second `git diff --stat` instead of a manual re-derivation,
which is the entire argument for §0.2a's recommendation and is now three-for-three in the wild.

---

#### Cross-tree claims — which state of `kim_pipeline/` they were read from

**Added 2026-08-22, on a floor notice that the shared tree carries held, uncommitted changes.**
Everything above pins the `geper/` claims against the baseline tag. **It said nothing about the
claims this document makes about the OTHER tree**, and those were read from the **shared
working tree** — not from the tag, and not from `HEAD`.

**That distinction is not academic today.** A held change sets `_MIN_CONCORDANT_PREDICTORS = 2`
in `kim_pipeline/pipeline/acmg/classifier.py`, replacing the shipped majority-of-present-voters
rule for PP3/BP4; **the constant does not exist in `HEAD` at all**, so anything run against that
tree today exercises a **proposed** classifier rather than the shipped one. Ten-plus files under
`kim_pipeline/` are modified.

**The two cross-tree claims, and whether they survive it — checked, not asserted:**

| Claim | Made at | Verdict |
|---|---|---|
| `kim_pipeline/` has **no star-rating threshold** of any kind | §2.4 | **Holds for the working tree AND `HEAD`** |
| `kim_pipeline/` has **no variant-prioritisation stage and no ranked output** | §5.9.8 | **Holds for both** |

Both are **absence** claims established by identifier grep, and
`git diff HEAD -- kim_pipeline/` touches **none** of `star_rating`, `priorit`, `rank_batch` or
`PriorityResult`, **in either direction**. Reading a dirty tree would have mattered in two ways:
a held change that **added** one of those identifiers would have shown me something that does
not ship, and one that **removed** one would have hidden something that does. **Neither
happened**, so absence in the working tree implies absence in `HEAD` for these two claims
specifically — a checked result, not a general property of dirty trees.

**What this document does NOT claim about `kim_pipeline/`: any measurement whatsoever.** §3's S2
is a **design** for a GIAB variant-calling study that has never been executed, so **no number in
this document could have come from the proposed classifier.** Recorded explicitly because the
floor notice asks everyone to say which system their numbers came from, and *"there are no
numbers"* is the answer here — **had there been one, it would have needed relabelling rather
than this footnote.**

**One structural fact about that tree, recorded because §3 targets it — relayed from the floor
notice, then VERIFIED rather than repeated.** SpliceAI is **permanently `None`** in
`kim_pipeline` real runs. [VERIFIED-IN-REPO @ c2258df1] `kim_pipeline/pipeline/vep/stage.py:13-24`: the
plugin was removed 2026-08-22 because its pretrained models are CC BY-NC 4.0 and its code
GPL-3.0. **Two details the notice did not carry, and both matter here:**

1. SpliceAI **fed live ACMG evidence in that tree** — PP3/BP4, and BP7 via
   `orchestration/shared.py::synonymous_or_intronic` — so its removal **changes
   `kim_pipeline`'s evidence stack**, rather than merely dropping an annotation.
   **Sharpened 2026-08-22, and the sharper version is not this document's finding:** PP3
   (`classifier.py:863-864`) and BP4 (`:1224-1225`) collect the SpliceAI vote only
   `if e.spliceai_score is not None`, so **the voter never joins the votes list at all and the
   live pool has permanently shrunk by one.** That is material to `kim_pipeline`'s held
   `_MIN_CONCORDANT_PREDICTORS = 2`, which is now evaluated against a smaller pool than it was
   costed against — **an open diagnosis in another lane, recorded here only because it
   supersedes the weaker wording above**, which said the stack "changed" without saying that a
   threshold's denominator moved underneath it.
2. The `spliceai_score` fields **remain on the dataclasses and are always `None`.** The
   evidence is therefore **structurally absent while still present as a readable field** —
   precisely the shape that reads as *"unavailable for this variant"* to anything inspecting it,
   which is a different claim entirely.

**Neither `geper/` nor S2 is affected, and both were checked rather than assumed.**
`LICENSE_AUDIT.md`'s *"never integrated into GEPER"* verdict for SpliceAI was scoped to
`geper/` only, so the PP3/BP4 stack Arm A measures never contained it; and S2 measures variant
*calling* against GIAB, which carries no splice-prediction component.

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
| **PP5** | **No — never fires** | — | **No** | **[VERIFIED-IN-REPO @ c2258df1]** `pipeline/acmg_rules.py:957-962` — `PP5` is set to `_not_evaluated` **unconditionally**, reason `"recommended against by ClinGen's SVI Working Group (Biesecker & Harrison 2018) as circular with respect to an independent ACMG/AMP evaluation; not applied."` A CI grep test (`tests/test_validation_study_design_badge_pins.py`) keeps this exact reason string honest going forward. **PP5 is NOT "deprecated" — that word, and the quote and line range this row previously carried, do not exist in the file and never described anything that could be true: PP5 was *introduced by* the 2015 ACMG/AMP guideline, so it cannot have been deprecated *in* that same document.** The real reason is a **deliberate policy exclusion**, on ClinGen SVI's later (2018) recommendation — a decision GEPER made and must defend, not a fact about the 2015 guideline itself. (Separately, and left untouched per instruction: PP5's `_not_evaluated` category is `NOT_INTEGRATED`, the same bucket used for genuine data-integration gaps elsewhere in this table — arguably a mis-categorisation, since this is a policy choice with the data available, not a missing integration. Noted here, not opened, per god's explicit instruction not to open this card in this pass — tracked separately on the floor.) |
| **BP6** | Yes | The variant's **own** record | **No** | **[VERIFIED-IN-REPO @ c2258df1]** `acmg_rules.py:3734-3746` (moved again from `:3713-3725`, unrelated work between the two same-day re-verifications — see §0.4b) — `_combine` skips `BP6` in the point tally with a comment naming this exact circularity; `_BP6_DEPRECATION_CAVEAT` (`:3113-3122`, was `:3092-3101`) states it is "reported at capped Low confidence and is excluded from this engine's own point-based combining rules... so it cannot silently move the final classification" — a CI grep test keeps this exact string honest going forward. It **does** reach the human-readable evidence text. |
| `_clinvar_crossref` | Yes | Own record | **No** | **[VERIFIED-IN-REPO @ c2258df1]** `acmg_rules.py:3644` (definition, was `:3623`; called from `:983`, unchanged), and the comment at `:3735-3736` (was `:3714-3715`) — descriptive cross-reference, "kept out of these combining rules entirely". |
| **PS1** (Strong, path.) | **Yes** | **Other** variants at the same codon, identical resulting AA change | **YES** | **[VERIFIED-IN-REPO @ c2258df1]** `acmg_rules.py:1209-1234`; anchors filtered by `CONFIG.ps1_pm5.MIN_STAR_RATING` (**default 2**, `config.py:1768`
as of `c2258df1` — the line has moved AGAIN since §0.4's own 2026-08-22 correction pinned it at `1710`; the value is still unchanged, only its position, see §0.4a); the variant's own record is excluded (`pipeline/ps1_pm5/decision.py:137`, *"excluding this variant's own record"*). |
| **PM5** (Moderate, path.) | **Yes** | **Other** variants at the same codon, *different* AA change | **YES** | **[VERIFIED-IN-REPO @ c2258df1]** `acmg_rules.py:1237-1263`; same threshold, same self-exclusion. |

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

- **[VERIFIED-IN-REPO @ c2258df1]** PS1/PM5 admit anchors at ≥2★ (`config.py:1768`, see §0.4a — moved again since §0.4's 2026-08-22 pin), so 3★ and 4★
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

**GEPER already captures exactly that.** **[VERIFIED-IN-REPO @ c2258df1]** `clinvar_client.py:355-372`
(`_fetch_submitters`, batched VCV XML, re-verified 2026-09-10 — range still exact) and `pipeline/ps1_pm5/utils.py:155` (was `:161`, minor drift, re-verified 2026-09-10) capture the
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
(`clinvar_client.py`, `ps1_pm5/lookup.py`) — **[VERIFIED-IN-REPO @ c2258df1]**. Withholding variants
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

**Availability: not gated.** **[VERIFIED-IN-REPO @ c2258df1]** This is a `review_status` filter on data
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

**Dependency this creates, and whose it is.** **[VERIFIED-IN-REPO @ c2258df1]** GEPER queries the NCBI
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

**Sub-flag on Arm A: `bp6_fired`.** **[VERIFIED-IN-REPO + INVENTED @ c2258df1]** BP6 cannot move the
score (`acmg_rules.py:3734-3746`, was `:3509-3522` then `:3713-3725`, re-verified 2026-09-10 against `c2258df1` after a further +21-line shift from unrelated work between the two re-verifications the same day) but does reach the rendered evidence text. Arm A must
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

**[VERIFIED-IN-REPO @ c2258df1]** `CONFIG.ps1_pm5.MIN_STAR_RATING` is read from the environment
(`config.py:1768`, `GEPER_PS1_PM5_MIN_STAR_RATING`, default `2` — moved again since §0.4's
2026-08-22 pin, see §0.4a) and is the sole star-rating
gate applied to PS1/PM5 anchors (`acmg_rules.py:1230`, `:1259`, was `:1137`/`:1166`, re-verified 2026-09-10). ClinVar's maximum star
rating is 4 (`pipeline/ps1_pm5/models.py`, `STAR_PRACTICE_GUIDELINE = 4` — a CI grep test in `tests/test_validation_study_design_badge_pins.py` keeps this value honest going forward).

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

**The same knob is both the ablation lever and the thing that must not move — and those are
compatible.** §2.4 argues that `MIN_STAR_RATING` must be *changeable* so Arm C can exist;
EJ-19 records a ruling that it must be *locked against runtime override* in a validated
deployment. Read together without this paragraph they look contradictory, so it is stated
explicitly: **an ablation is a deliberate, recorded study run under a declared configuration;
a deployment is not.** The lock governs deployments carrying a validation certificate. Arm C
is a study run whose whole purpose is to record that it used a non-certified setting, and it
is pre-registered as such under §5.6. A configuration change that is declared in advance and
reported with the result is the opposite of the failure mode the lock exists to prevent —
which is a deployment silently differing from the certified one.

**The enforcement mechanism is not specified here and is not this document's to specify.**
How a lock is implemented, and how a deployment *proves* it is locked, is an engineering
question. **It is currently unowned** — stated as an open ownership gap rather than assigned
or designed, because a design document naming a mechanism nobody has agreed to build would be
asserting an implementation that does not exist.

#### The ruling's premise, tested against code rather than accepted — it holds, but not as worded

The ruling rests on the claim that a diverging configuration *"invalidates the certificate
without any signal"*. **Read literally that is false, and the true version is stronger.** The
premise was checked rather than carried, because a ruling resting on an over-broad premise can
be dismissed by refuting the premise while the conclusion is still right.

**[VERIFIED-IN-REPO @ c2258df1]**, `geper/pipeline/ps1_pm5/decision.py`:

- **The effective value IS emitted — at exactly one place.** `:326`, inside the
  **not-applies** rationale: *"no ClinVar record at codon N sharing ‹relation› met the
  confidence bar (>= `{min_star_rating}`-star, not conflicting, Pathogenic/Likely
  pathogenic)"*. A reader of that rationale learns the threshold that was in force.
- **It is emitted on that branch and no other.** The module reads `min_star_rating` at four
  places — `:47` (a second default), `:240` (a docstring), `:283` (the filter itself) and
  `:326` (this rationale). **No applies-path output states the threshold at all.**
- **No run-level manifest carries it either.** `MIN_STAR_RATING` appears nowhere under
  `geper/report/` or `geper/utils/` — the per-variant not-applies rationale is the only
  surface that ever names it.

> **⚠ SO THE SIGNAL'S COVERAGE IS ANTI-CORRELATED WITH THE RISK, WHICH IS WORSE THAN NO
> SIGNAL AND HARDER TO NOTICE.** **Raise** the bar and anchors are suppressed, PS1/PM5 does
> not apply, and the rationale dutifully prints the setting that suppressed it — full
> disclosure of a change that made GEPER *more* conservative. **Lower** the bar and weaker
> anchors qualify, PS1/PM5 fires *more* often, `qualifying` is non-empty, the not-applies
> branch is never reached — and **the output says nothing about the threshold whatsoever**.
> **The setting is disclosed exactly when it cost nothing, and withheld exactly when it moved
> the answer toward a stronger pathogenic call.**
>
> **This strengthens the ruling by removing its only attackable clause.** *"No signal"* is
> refutable by pointing at `:326`. *"A signal present only on the branch where the setting
> made no difference"* is not refutable, and it is what the code actually does.

**Scope of what was verified, stated because it is narrower than it looks.** This establishes
what the **pipeline produces**, not what a **report renders**. Whether that rationale string
reaches any artifact a reader or assessor receives is a separate question, and it is one this
document has repeatedly been unable to answer because **no rendered report artifact exists
anywhere in the repository to inspect**. If the rationale is dropped downstream, the single
emission above is not a signal at all and the literal reading of the human's premise becomes
true after all. **Either way the ruling stands** — which is the point of testing a premise
that only ever argued *for* the conclusion.

**One observation recorded without a defect claim attached.** `decision.py:47` carries a
**second, independent default** (`min_star_rating: int = 2`) duplicating `config.py:1768`'s (line moved again since §0.4's 2026-08-22 correction, re-verified 2026-09-10, see §0.4a).
Both are `2` today, so nothing is wrong now. Were they ever to diverge, a caller constructing
thresholds without going through `CONFIG` would silently get a different evidence bar with no
error. **Not asserted as a live defect** — every construction site was not traced, and saying
"two defaults exist" is the checked part while "they can diverge in practice" is not.

**Cross-tree, since a configuration-lock finding reads as product-wide:** `kim_pipeline/` was
checked directly and has **no star-rating threshold of any kind** — zero matches for
`star_rating`/`STAR_RATING` outside tests. **This finding is `geper/`-only as a checked fact**,
not by assumption from where it was found.

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-19
  DECIDE   — **the certified VALUE only. The lockability half of this token is RULED and
             CLOSED** (2026-08-22): `MIN_STAR_RATING` must be locked against runtime
             override as part of any validation certificate, on the human's ruling that
             *"a configuration that can silently differ from the validated one invalidates
             the certificate without any signal"* — the same class as the ClinGen knob, but
             what it corrupts is the accreditation claim rather than one evidence weight.
             **What remains open:** which value the validation certifies (default is 2 =
             "criteria provided, multiple submitters, no conflicts"). A validation performed
             at one evidence-admission setting does not validate any other setting, so the
             certified value bounds the certificate.
  GUIDANCE — ISO 15189:2022, clause 7.3.3 (validation of examination methods) — validation
             applies to the method as specified, and a change to the specified method
             requires re-validation [VERIFIED (secondary) — 7.3.3 is "Validation of
             examination methods", sitting between 7.3.2 Verification and 7.3.4 Measurement
             uncertainty; confirmed via published clause-by-clause comparisons of
             ISO 15189:2012 vs :2022].
             Richards et al., ACMG/AMP 2015, Genet Med 17(5):405-424, PS1/PM5 definitions
             and the evidentiary weight assigned to a prior classification [VERIFIED —
             PMC4544753; doi 10.1038/gim.2015.30; PS1 = "Same amino acid change as a
             previously established pathogenic variant regardless of nucleotide change",
             PM5 = "Novel missense change at an amino acid residue where a different
             missense change determined to be pathogenic has been seen before"].
  DECIDED-BY — the laboratory director or clinical-genetics lead accountable for the
             report, on the basis of what strength of prior evidence they are willing to
             have contribute at Strong (PS1) weight to a released classification. Not an
             infrastructure decision and not this document's.
>>>

### 2.5 Curator-provenance sub-strata within Arm B

**[INVENTED — the stratum; VERIFIED-IN-REPO @ c2258df1 — the provenance data it uses]**

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
             [VERIFIED — Biesecker LG, Harrison SM, ClinGen Sequence Variant
             Interpretation Working Group; Genet Med 2018;20:1687-1688; and corroborated
             in-repo, already relied on at `acmg_rules.py:2943-2951`, which makes this the
             strongest-sourced citation in the document]. Richards et al., ACMG/AMP 2015,
             PS1/PM5 [VERIFIED — PMC4544753, definitions quoted at EJ-19].
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
             of specimens/variants required for validation [VERIFIED — PMID 28341590,
             doi 10.1016/j.jmoldx.2017.01.011; the guideline's own scope includes
             "requirements for minimal depth of coverage and minimum number of samples that
             should be used to establish test performance characteristics"]. CLIA, 42 CFR
             §493.1253(b)(2), establishment and verification of performance specifications
             [VERIFIED — heading is "Standard: Establishment and verification of
             performance specifications"; (b)(2) exists and requires accuracy, precision,
             analytical sensitivity, analytical specificity, reportable range and reference
             intervals]. ISO 15189:2022 clause 7.3.3 [VERIFIED (secondary) — see EJ-19].
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
             requirements for targeted vs. genome-wide assays [VERIFIED as to citation —
             PMID 23887774, authors and 15(9):733-747 exact; the targeted-vs-genome-wide
             attribution specifically was NOT located at section level, full text not
             reached]. ISO 15189:2022 clause 7.3.1, scope of examination procedures
             [UNFETCHED-NOT-LOCATED — 7.3.2/7.3.3/7.3.4 titles were all confirmed but no
             free source gave 7.3.1's own title; iso.org 403, NATA and qualimetric PDFs
             unparseable. Do not rely on this clause number].
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
  GUIDANCE — Biesecker & Harrison, ClinGen SVI, Genet Med 2018;20:1687-1688 [VERIFIED —
             see EJ-09; corroborated in-repo at `acmg_rules.py:2943-2951`]. ISO 15189:2022
             clause 7.4.1 on reporting of results [VERIFIED (secondary) as to the clause —
             7.4.1 is "Reporting of results", with subclauses 7.4.1.1 General through
             7.4.1.8 Changes to reported results], **but the specific requirement attributed
             to it here — that a report must not create a misleading impression of the
             method's scope — is [UNFETCHED-NOT-LOCATED]**; no subclause confirming that
             wording was found. The decision stands on its own reasoning, not on this
             clause.
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
             confident-region conventions [VERIFIED — PMID 30858580; the paper's own scope
             is a GA4GH benchmarking framework giving "guidance on how to match variant
             calls with different representations, define standard performance metrics, and
             stratify performance by variant type", which is exactly what is cited here].
             Jennings et al., AMP/CAP, J Mol Diagn 2017;19(3):341-365, analytical validation
             sections [VERIFIED as to citation — PMID 28341590; section-level attribution
             not reached, jmdjournal.org returned 403].
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
  GUIDANCE — Krusche et al., Nat Biotechnol 2019;37(5):555-560 [VERIFIED — see EJ-14].
             Jennings et al., AMP/CAP, J Mol Diagn 2017;19(3):341-365, analytical
             sensitivity/specificity sections [VERIFIED as to citation — note the
             guideline's own abstract frames this as "positive percentage agreement and
             positive predictive value for each variant type" rather than
             sensitivity/specificity; the terminology here is looser than the source's].
             CLIA 42 CFR §493.1253 [VERIFIED — see EJ-08].
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

**[GUIDANCE]** The requirement driving all of this: ISO 15189:2022 requires participation in
external quality assessment where available, and where an EQA programme is **not** available
or is **not suitable**, requires the laboratory to use an alternative approach and to justify
the rationale for it with evidence of its effectiveness.

> **⚠ [INVENTED] — the clause number originally cited here was wrong, and is retained as
> INVENTED rather than silently corrected.** This document originally attributed the above to
> **clause 7.3.7.2**. Verification (2026-08-22) establishes that **7.3.7.2 is "Internal quality
> control (IQC)"** — a different subject entirely — and that the EQA requirement, including
> the alternative-approach limb, is **clause 7.3.7.3 "External quality assessment (EQA)",
> specifically 7.3.7.3 f)**. Parent clause 7.3.7 is "Ensuring validity of examination
> results"; 7.3.7.1 is "General". Confirmed via SADCAS F134 (an accreditation body's own
> published requirements document, titled for ISO 15189:2022 clause 7.3.7.3) and corroborating
> published summaries — **not** from the standard, which is paywalled (§0.3).
>
> **The substance of the requirement verified as real, and §4's argument is unaffected** —
> the alternative-approach limb genuinely exists and genuinely does what §4.3 relies on it to
> do. Only the clause number was invented. That is stated rather than smoothed because the
> two failure modes are indistinguishable once a citation is corrected in place.

**NABL accredits medical laboratories to ISO 15189**, which is why this clause rather than a
CAP checklist item is the governing one for the stated assessor. **If enrolment is genuinely unavailable to a software vendor, the
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
  GUIDANCE — ISO 15189:2022 **clause 7.3.7.3 f)**, external quality assessment and the
             alternative approach where an EQA programme is unavailable or unsuitable
             [**[INVENTED]** as originally cited — this token originally said clause
             7.3.7.2, which is in fact "Internal quality control (IQC)". Retained as
             INVENTED, not silently corrected; the corrected location and the evidence for
             it are given in full at §4.3. The requirement itself is real]. CAP All Common
             Checklist, proficiency testing and alternative performance assessment
             requirements [UNFETCHED-PAYWALLED — attempted at cap.org/laboratory-improvement/
             accreditation/accreditation-checklists; CAP checklists are not freely readable
             and must be bought ("Purchase a Checklist"). No item number was ever asserted
             and none can be until a copy is obtained]. ICMR Guidelines for Good
             Clinical Laboratory Practices (GCLP), 2021 [VERIFIED — title and 2021 revision
             confirmed via ICMR's own site (icmr.gov.in), a 2021 revision of the original
             2008 guidelines; note ICMR's own hosted filename reads `GCLP_Guidelines_2020_
             Final.pdf` while the document is titled 2021, so cite the title, not the file].
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
  GUIDANCE — ISO 15189:2022 clause 7.3.7.3, "External quality assessment (EQA)"
             [**[INVENTED]** as originally described — the clause NUMBER is right, but this
             token originally described 7.3.7.3 as "evaluation of performance in
             interlaboratory comparison", treating 7.3.7.2 and 7.3.7.3 as two stages of one
             EQA requirement. They are not: 7.3.7.2 is Internal quality control and 7.3.7.3
             is External quality assessment. Retained as INVENTED because the description,
             not merely the number, was wrong. See §4.3]. CLIA 42 CFR §493.801-493.865,
             proficiency testing programme requirements and unsuccessful-participation
             consequences [VERIFIED — Subpart H, "Participation in Proficiency Testing for
             Laboratories Performing Nonwaived Testing"; §493.801 is "Condition: Enrollment
             and testing of samples" ("Each laboratory must enroll in a proficiency testing
             (PT) program that meets the criteria in subpart I of this part and is approved
             by HHS") and §493.803 is "Condition: Successful participation". §§493.801 and
             .803 were read directly; the sections up to .865 were not enumerated].
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
  GUIDANCE — ISO 15189:2022 clause 7.3.3, validation of examination methods against
             intended use [VERIFIED (secondary) — see EJ-19; note 7.3.3 b) requires that the
             extent of validation be "sufficient to ensure the validity of results pertinent
             to clinical decision making", which is the clearest external support in this
             document for making intended use prior to every threshold]. ISO 15189:2022
             clause 7.3.1 [UNFETCHED-NOT-LOCATED — see EJ-11; do not rely on it]. CLIA 42
             CFR §493.1253 [VERIFIED — see EJ-08]. Rehm et al., ACMG, Genet Med
             2013;15(9):733-747 [VERIFIED as to citation — PMID 23887774].
  DECIDED-BY — the laboratory director and the accountable clinical lead, with regulatory
             sign-off. The single decision in this document least substitutable by anyone
             else, and the one that blocks the rest.
>>>

### 5.1.1 Two provisional intended-use statements — a comparison, not a recommendation

**EJ-01 is not answered here and is not answerable here.** The human has **declined to answer
it**, and asked that the declining be recorded **as a decision rather than a delay**: it is
the statement the whole plan rests on, it is theirs alone to write, and writing it badly would
propagate through every dependent decision below. EJ-01 remains open and remains the top item
in §8.

What follows is what they asked for instead: **two provisional statements set side by side, so
that the decision is read against a comparison rather than a blank page.**

> **⚠ THIS SUBSECTION CONTAINS NO RECOMMENDATION, AND ITS SYMMETRY IS PART OF THE
> DELIVERABLE.** Both statements are drafted at equal depth, against an identical set of
> consequence rows, in the same order. **Nothing here should be read as an argument for
> either.** If one option looks better to a reader, that is the reader's judgement forming,
> which is the intent — it is not a conclusion this document reached and then presented.
> Writing one option more persuasively than the other would convert a decision into a
> ratification, and a statement that is merely ratified carries the ratifier's authority with
> the drafter's judgement inside it.

**Both statements are PROVISIONAL.** Neither is adopted. Neither has been checked against the
population, sample types, or reporting workflow that EJ-01 also requires — selecting a shape
**constrains** EJ-01, it does not **discharge** it.

#### Statement A — PROVISIONAL — **(SUPERSEDED 2026-08-22 — retained as the revalidation roadmap's source, see §5.1.3)**

> *GEPER interprets sequence variants and produces an ACMG/AMP classification that is used
> for clinical reporting.*

- **What GEPER's output is:** a classification (P / LP / VUS / LB / B) with its evidence
  trail, which reaches a clinical report.
- **What the human's role is:** review and sign-off on GEPER's classification. The human is
  an approver of a proposed answer.
- **What the study is measuring:** whether the classification is correct.

#### Statement B — PROVISIONAL — **(AMENDED 2026-08-22 — this is the text as originally drafted; the ratified statement, as amended, is in §5.1.2)**

> *GEPER prioritises sequence variants and surfaces them, with supporting evidence, to a
> human interpreter who performs the classification.*

- **What GEPER's output is:** an ordering or flagging of variants with its evidence trail,
  which reaches an interpreter's work queue.
- **What the human's role is:** perform the classification. The human is the author of the
  answer.
- **What the study is measuring:** whether the right variants reach the interpreter.

#### How each of the dependent decisions resolves

Derived by reading each EJ block in this document, not by reasoning about what such a choice
usually implies. **[ANALOGY]** throughout unless a row says otherwise: these are readings of
this document's own tokens against two hypothetical uses, and no external source was consulted
for any of them.

| EJ | Under Statement A | Under Statement B | Discriminates? |
|---|---|---|---|
| **EJ-02** VUS binarisation | VUS is a terminal reportable category; the rule determines what a released sensitivity figure means | VUS is not terminal; the operative split is surfaced vs. suppressed, not P/LP vs. VUS | **Yes — different metric** |
| **EJ-03** E2 missed pathogenic | A truth-P/LP called VUS reaches a report as VUS; the signing reviewer is the only backstop, and they are reviewing a proposed answer rather than forming one | A truth-P/LP called VUS is still surfaced if VUS enters the queue; the safety event is *not surfaced*, not *called VUS* | **Yes — different event** |
| **EJ-04** E3 over-call | False positive reaches a report → possible unnecessary intervention, surveillance or cascade testing of relatives | False positive enters a review queue and is filtered by the interpreter → workload cost | **Yes — severity class changes** |
| **EJ-05** E4 VUS inflation | Report burden and patient-facing uncertainty, with re-contact obligations attaching to each VUS released; a secondary metric | Queue burden — arguably the *primary* metric, since reducing it is what triage is for | **Yes — rank changes** |
| **EJ-06** E5 within-tier drift | Matters wherever P and LP drive different management, surveillance or cascade-testing pathways | Both surface at high priority; drift within tier is largely inert | **Yes — tolerance differs** |
| **EJ-07** E6 no-call | A no-call is an absent result; a reportable-range question | A no-call must be *routed to review*, never dropped | **Yes — different obligation** |
| **EJ-08** sample size / clustering | Needed, at a precision set by diagnostic consequence | Needed, at a precision set by queue-performance consequence | No — same structure |
| **EJ-09** Arm B−C delta | If classification leans on ClinVar, a claim of independent interpretation is compromised | A triage signal may legitimately use other labs' classifications, since the human interprets | **Yes — changes character** |
| **EJ-10** may Arm B headline | Arm A must headline and Arm B cannot substitute, because the claim being made is one of independent interpretation | Arm B may be the operative number; in-practice performance with all signals is what ships | **Yes** |
| **EJ-11** gene scope | The reportable gene list | The triage coverage list | No — same structure |
| **EJ-12** Indian-population gate | Population-matched performance is a direct clinical-validity question about a released result, with no downstream filter between it and the patient | Still real — ranking could systematically deprioritise under-represented variation — with the interpreter as partial backstop | **Yes — weight differs, existence does not** |
| **EJ-13 / EJ-14** GIAB | Upstream of the classification | Upstream of the ranking | No — **identical**; a wrong genotype breaks both equally |
| **EJ-15** EQA scheme / route | An interpretation claim points toward enrolled participation | A triage tool points toward shadow assessment being defensible | **Yes** |
| **EJ-16** EQA pass criterion | Same structure; the standing the evidence must carry differs | Same structure; the standing the evidence must carry differs | Partly — standing only |
| **EJ-17** re-validation trigger | Any change to rules, config, or models | Any change to rules, config, or models | No — **identical** |
| **EJ-18** who signs | Points toward accredited laboratory-director standing, since a released classification carries that authority | Points toward internal QA sign-off being sufficient | **Yes** |
| **EJ-19** certified config value | Needed; lock ruled and closed for both | Needed; lock ruled and closed for both | No — **identical** |
| **EJ-20** FAIL pattern weighting | Weighted toward E2/E3, the patient-facing classes | Weighted toward surfacing failures and queue metrics | **Yes — weighting only** |
| **EJ-21** what a FAIL obligates | Release block or scope restriction are live options | Shipping with a documented limitation is a live option | **Yes** |
| **EJ-22** INVALID triggers | Measurement-validity conditions, use-independent | Measurement-validity conditions, use-independent | No — **identical** |

**Count: 14 of 21 dependent decisions resolve differently; 7 resolve identically or differ
only in value.** The finding god asked me to watch for — that EJ-01 might not be the
dependency it is believed to be — **does not hold. It is.** Reported with the count rather
than only the discriminating rows, because reporting only the discriminators would have made
the dependency look stronger than the evidence supports.

#### What each statement costs, stated at equal weight

**Statement A — the cost is regulatory load.** EJ-12, EJ-15, EJ-16, EJ-18 and EJ-21 all
resolve toward the more demanding end: population-matched evidence closer to a hard gate,
enrolled rather than shadow EQA, accredited laboratory-director signing standing, and
release-blocking as a live consequence of failure. Each is an external dependency on a body
outside this organisation — an EQA provider, an assessor, a qualified signatory — so the
schedule is not wholly within the project's control. In exchange, the study instrument in
this document works as written: E1–E6, the Arm A/B/C structure and F1's threshold-independent
anchor all apply unchanged.

**Statement B — the cost is study-instrument rework.** §5.5's error taxonomy (E1–E6) and much
of S1's arm structure are built on **classification agreement**. Under B the operative failure
is **ranking and surfacing**, so E1–E6 would have to be rebuilt on a different axis, and F1 —
the threshold-independent anchor in §5.9.2, inherited from the rubric's ratified 1a falsifier —
**has no defined equivalent for a ranking output**. That rework is internal and within the
project's control, but it is not small: it reopens the acceptance-criteria structure this
document exists to fix, and a replacement anchor would have to come from Angela's rubric
rather than from here. The regulatory load resolves toward the lighter end.

> **⚠ SUPERSEDED IN PART, 2026-08-22 — ANNOTATION ONLY. The paragraph above is left exactly
> as drafted**, because §5.1.1 is the historical record of the comparison and rewriting it
> would erase what the comparison actually said at the time the choice was made. Two of its
> claims are no longer true:
>
> 1. **"F1 ... has no defined equivalent for a ranking output" — SUPERSEDED BY F5 (§5.9.8).**
>    An equivalent now exists. F5 forbids a rank inversion across the **same** P/LP↔B/LB
>    boundary at the **same** ratified zero tolerance, inheriting its threshold-independence
>    from rubric 1a exactly as F1 does.
> 2. **"a replacement anchor would have to come from Angela's rubric rather than from here"
>    — also superseded.** F5 was designed here and is ratified there. That boundary was about
>    where the anchor **lives**, not who designs it, and it was ruled accordingly.
>
> **⚠ AND THE CONSEQUENCE RUNS IN THE DIRECTION NOBODY CHECKS: THIS MAKES THE ADOPTED OPTION
> CHEAPER THAN THIS DOCUMENT COSTED IT.** Statement B is the shape that was ratified
> (§5.1.2). Its cost was stated above as study-instrument rework whose hardest component was
> that the anchor had **no** ranking equivalent and would have to be sourced from outside this
> document. **That component is now discharged** — not reduced, removed. **Stated explicitly
> because a cost that RISES gets re-checked and a cost that FALLS does not.** An unannounced
> reduction leaves a ratified decision resting on a worse case than the one now true, which is
> a quieter error than an overstatement and survives far longer for exactly that reason.
>
> **What did NOT change, so the reduction is not read as larger than it is:** E1–E6 still
> require rebuilding on a ranking axis, that remains the bulk of the rework, F5's own
> parameters (**EJ-24**, **EJ-25**) are unfilled, and **EJ-25 blocks on operator scope**. One
> named component of the cost went to zero. The cost as a whole did not.

> **⚠ THE ASSUMPTION A READER IS MOST LIKELY TO IMPORT, AND IT IS NOT VERIFIED.** Every row
> above that resolves toward *lighter regulatory obligation under B* rests on the premise that
> a triage tool falls outside the scope that applies to a diagnostic interpretation. **That is
> a regulatory determination. It was not checked, it is not within this document's competence,
> and no source was consulted for it — [ANALOGY], and the single most consequential untagged
> assumption available to a reader of this subsection.** If it is wrong, Statement B's cost
> profile is wrong with it and much of the right-hand column collapses toward the left. It is
> named here rather than left to be inferred, and it belongs to whoever answers EJ-01 with
> regulatory counsel — not to this floor.

#### The selection

Adopting either shape is the decision recorded at **EJ-23** below. **Neither statement
discharges EJ-01**, which still requires the population, the sample types, the reporting
destination, and the human-review step. A third option — neither shape, or a different one
entirely — is available and is not disfavoured by having gone undrafted here; only two were
requested.

> **⚠ EJ-23 STOOD HERE AND WAS REMOVED ON 2026-08-22 — THE SELECTION IT HELD OPEN WAS MADE,
> NOT ABANDONED.** It asked which of the two shapes above becomes the basis for EJ-01. The
> human ratified the prioritisation shape, later amended to name the draft classification
> explicitly (§5.1.2), which is the answer EJ-23 was waiting for. The count moved **24 → 23**
> and the reason is recorded in §0.1's count history, so a reader comparing counts across
> versions cannot read the −1 as scope quietly shrinking.
>
> **The prose immediately above still reads "the decision recorded at EJ-23 below", and is
> left exactly as written.** A superseded identifier inside a historical record is correct
> rather than stale — the same rule that keeps the bare `EJ-03` in §5.1.1's discrimination
> table after the split. Rewriting it would erase the fact that the selection was once open,
> which is the one thing this paragraph is for.
>
> **What did NOT move with it:** EJ-01 is still open on population, specimen, operator and
> regulatory scope, and the regulatory-scope premise flagged above EJ-23 — that a triage tool
> attracts lighter obligation — remains **unverified and owed to counsel**. It was carried in
> EJ-23's DECIDED-BY line; with the token gone it is carried in §5.1.2's sequencing ruling
> instead, and is not discharged by this removal.

### 5.1.2 EJ-01's clinical claim is ANSWERED — triage of what that moves, and the revalidation roadmap

**Added 2026-08-22, after §5.1.1. Re-keyed the same day to the AMENDED statement** — a
triage keyed to superseded wording is the stale-citation class, which is the defect this
document polices everywhere else.

**The ratified provisional statement, as amended:**

> *"GEPER is a variant prioritisation system that assists qualified clinicians and
> pathologists. **It produces a draft classification** requiring qualified human review and
> final sign-off before any clinical use. It does not independently provide final clinical
> interpretation."*

<details><summary>The superseded original, retained as history</summary>

> *"GEPER is a variant prioritisation system that assists qualified clinicians and
> pathologists in interpreting genomic results. Mandatory human review and final sign-off by
> a qualified professional. GEPER does not independently provide final clinical
> interpretation, and no output may be used clinically without a qualified human
> interpreter's review."*

</details>

**What actually changed, judged row by row below against this and not against the mere fact
of an amendment:** the statement **adds an explicit "produces a draft classification"**. It
**keeps** "variant prioritisation system", **keeps** "does not independently provide final
clinical interpretation", and **keeps** mandatory human review and sign-off before clinical
use. Nothing was weakened.

**Why it changed, recorded because it is a rare direction:** five lanes independently found
the product is classification-shaped, and the human accepted that as **accurate rather than
as a defect** — moving the statement to match reality instead of moving reality to match the
statement.

> **⚠ THIS AMENDMENT IS NOT PRECEDENT FOR AMENDING TOWARD A STRONGER CLAIM.** The human
> ruled explicitly that it does **not** cross the "only via revalidation, never by amending
> this statement" rule, because it **claims no independent final interpretation — it makes an
> existing limit explicit.** Recorded here so that a later reader cannot cite this amendment
> as a precedent for widening the claim by rewording. Widening still requires revalidation
> against §5.1.3's entry criteria, without exception.

**PROVISIONAL, pending clinical-expert review.** It answers the **clinical-claim part only**.
**Population, specimen, operator and regulatory scope remain unfilled**, and EJ-01 therefore
stays open for those components.

> **⚠ WHAT THIS RATIFICATION RESTS ON THAT HAS NOT BEEN CHECKED — surfaced here, beside the
> statement, added 2026-08-22 on the human's ruling that a reader should meet it WITHOUT having
> to read §5.1.1, and that anyone citing this decision should meet it where the decision is
> recorded.**
>
> **The premise:** that a **triage tool falls outside the regulatory scope that applies to a
> diagnostic interpretation**.
>
> **Its status: UNVERIFIED.** **[ANALOGY]** — **no source was consulted for it**; it is a
> **regulatory determination, outside this document's competence**; and §5.1.1 names it as the
> single most consequential untagged assumption available to a reader of that comparison. It is
> cross-referenced rather than restated here: §5.1.1 carries the full text and the comparison
> it underwrites.
>
> **What rests on it — the human's own sharpening, which is stronger than the flag §5.1.1
> carries:** *"My ratification of Statement B rests on an unverified premise for **its entire
> cost advantage, not part of it**. Every lighter-obligation row depends on triage falling
> outside diagnostic-interpretation scope."* **Every** such row in §5.1.1's comparison rests on
> it, not merely several — so **if the premise is wrong, Statement B's cost profile is wrong
> with it**, and much of that comparison's right-hand column collapses toward the left.
>
> **⚠ WHAT IS NOT IN DOUBT, STATED AS PLAINLY AS THE RISK — because overstating this in the
> other direction would be the same defect reversed.** **The ratified statement stands on its
> merits, independently of this premise.** In the human's words: *"the clinical claim stands on
> its merits regardless — but the reason B looked cheaper is conditional."* **What is
> conditional is the COST COMPARISON, not the ratification.** Nothing here reopens the shape
> that was chosen; it bounds what may be claimed about what that choice **costs**.
>
> **Ownership: regulatory counsel, not this floor — and it is NOT discharged by the
> ratification having happened.** A decision taken against a conditional cost comparison leaves
> the condition outstanding rather than settling it. **The sequencing ruling attached to this
> premise — counsel's determination BEFORE the amended statement is relied upon for any scope
> decision, governing EJ-11, EJ-15, EJ-16 and EJ-18 — is recorded further down this subsection
> and is unchanged by this addition.**

**§5.1.1's selection is superseded.** The ratified shape is the prioritisation shape, so
there is no longer a choice to make between the two provisional statements. **EJ-23 HAS NOW
BEEN REMOVED (2026-08-22), taking the count 24 → 23** — the pending-removal note that stood
here is discharged, and the index no longer overstates the number of genuinely open
decisions. **Statement A is relabelled SUPERSEDED in §5.1.1 rather than rewritten**, and its
text is retained intact because §5.1.3 derives the revalidation roadmap from it: a branch
that is no longer an option is still the record of what that option would have cost.

**The stronger interpretation claim is a FUTURE GOAL, reachable only after concordance
studies, validation and expert sign-off, and ONLY VIA REVALIDATION — never by amending this
statement.** That constraint is what §5.1.3 exists to serve.

> **⚠ THE AMENDMENT MAKES THE UNVERIFIED REGULATORY PREMISE MORE FRAGILE, NOT LESS — and a
> sequencing ruling now attaches to it.** §5.1.1 flagged, and deliberately did not assert, the
> premise that a **triage tool** falls outside the regulatory scope applying to a diagnostic
> interpretation. The amended wording moves **away** from pure triage framing and **toward**
> "produces a draft classification" — i.e. closer to the category the premise assumes the
> product sits outside of. **This does not make the premise wrong. It raises the stakes on
> getting it answered.**
>
> **RULED:** counsel's determination is to be **sequenced BEFORE the amended statement is
> relied upon for any scope decision** — not after. That governs the four scope-gated tokens
> in particular: **EJ-11, EJ-15, EJ-16 and EJ-18**, each of which is blocked on the
> regulatory frame in the table below, and it is the reason none of them may be answered by
> reading the amended statement alone.

#### Triage of all 24 dependent decisions against the ratified claim

**⚠ The range this triage was first given was itself too narrow, and correcting it is part
of the finding.** The task as dispatched asked for "EJ-02 through EJ-19" — 18 tokens. **The
actual dependent set is 21 decisions**: at the time of writing, every token except EJ-01
itself and EJ-23 (then superseded, and since removed — §0.1).
EJ-20, EJ-21 and EJ-22 were omitted from the first pass because they fell outside the range
handed to me, and accepting that range **reproduced, inside this very section, the same
understatement the count test below exists to catch.** The table was re-keyed to all 21.

**⚠ IT IS NOW 24, AND THE SECOND MOVE WAS NOT A RECOUNT.** 21 → 24 because **EJ-03 was split
into EJ-03a/EJ-03b** (§5.5, +1) and **EJ-24/EJ-25 were created with F5** (§5.9.8, +2). The
first correction found decisions that were always there and had been missed; **this one
absorbs decisions that did not exist when the triage was written.** Distinguished because a
denominator that moves for two different reasons, reported as one number, is exactly the kind
of arithmetic this section keeps catching.

**[ANALOGY]** throughout — these are readings of this document's own tokens against the
ratified statement; no external source bears on any of them.

**(A) ANSWERABLE NOW — the clinical claim was the only blocker. 2 of 24.**

| EJ | What the answer now is |
|---|---|
| **EJ-09** Arm B−C delta | **Informational, not disqualifying.** The token's own DECIDED-BY made this turn on "what the product is represented as doing", which is now settled. A prioritisation system may legitimately use other laboratories' classifications as signal, because it makes no claim of independent interpretation. The delta must still be **reported** — it cannot fail the study on independence grounds. |
| **EJ-10** may Arm B headline | **Yes.** Arm B may be the operative figure, because in-practice prioritisation with all signals available is what ships. Arm A is retained, not discarded: it remains the measure of how much of the result is GEPER's own, and is the number §5.1.3 makes load-bearing again. |

**(C) PARTIALLY UNBLOCKED — the clinical claim settles the SHAPE, a remaining scope decision
sets the VALUE. 10 of 24. NINE of the ten are blocked on OPERATOR scope** (who reviews, at
what capacity), which makes it by a wide margin the most load-bearing of the four remaining
components — and the margin **widened** with the re-key rather than narrowing, because both
tokens F5 created land here and both are operator-gated.

| EJ | What the clinical claim narrowed | What still blocks the value |
|---|---|---|
| **EJ-02** VUS binarisation | **RE-KEYED.** The pure-prioritisation reading gave "surfaced vs. suppressed, not P/LP vs. VUS". The amendment restores the five tiers as an actual output, so **binarisation over P/LP/VUS/LB/B is meaningful again** and the earlier narrowing is withdrawn | Operator scope — whether the reviewer re-derives the tier or ratifies the draft |
| **EJ-03a** E2's DEFINITION (§5.5) | **RE-KEYED, and this is the largest movement in the table.** The pure-prioritisation reading made the safety event *not surfaced* rather than *called VUS*. With a **draft classification** as the output, a truth-P/LP drafted as VUS **is a wrong drafted answer**, not merely a queue position — so the safety event is now **both**: mis-drafted **and/or** not surfaced. The reviewer, not the queue, is the backstop. **This is the half the amendment actually moved** | Operator scope — whether the reviewer independently re-derives. Answered in the ruled order: guidance-frame, then D4-or-not, then ratify-vs-re-derive (§5.5) |
| **EJ-04** E3 over-call | Still reviewer-workload rather than direct patient harm — the interpreter filters and sign-off precedes clinical use — but the error is now **a wrong asserted draft** rather than a mis-ranking, which is a different thing to put in front of a reviewer | Operator scope — review capacity |
| **EJ-05** E4 VUS inflation | Still promoted toward a primary metric, but on **two** grounds now rather than one: queue burden, **and** the rate at which GEPER drafts an uncertain answer that a reviewer must resolve unaided | Operator scope — review capacity |
| **EJ-06** E5 within-tier drift | **RE-KEYED — the narrowing is WITHDRAWN.** The pure-prioritisation reading called this "largely inert" because P and LP both surface at high priority. With a **draft classification**, P vs. LP **is the drafted answer a reviewer signs off on**, so drift is no longer inert. Whether it matters now turns entirely on the review model | Operator scope — whether the reviewer re-derives the tier or ratifies the draft |
| **EJ-07** E6 no-call | **Obligation settled**: a no-call must be **routed to review**, never dropped | Operator scope — the routing destination's capacity |
| **EJ-20** S1 FAIL pattern | **RE-KEYED — the narrowing is WITHDRAWN.** The pure-prioritisation reading shrank the safety-relevant class to **surfacing failures** alone. With a draft classification as the output, **mis-classification returns to the safety-relevant class** (mediated by review, not eliminated by it), so F3's weighting question is materially less settled than the first pass claimed | Operator scope — review capacity, plus the review model |
| **EJ-21** what a FAIL obligates | **Shipping with a documented limitation becomes a live option**, because mandatory human review stands between any output and clinical use, so a FAIL does not reach a patient unmediated | **Regulatory frame** — the only row in (C) not gated on operator scope; whether release may be blocked is a regulatory question |
| **EJ-24** F5 tie rule (**NEW**, §5.9.8) | **The shape is settled by the claim**: a bounded human reads a queue, so the ORDER within it is a real property rather than a presentation detail. What a tie *means* is not settled | Operator scope — and specifically the review **workflow**: if the reviewer reads every surfaced variant, ordering is cosmetic and a tie is inert; if they read to a depth, the order is the whole of it |
| **EJ-25** F5 recall@K (**NEW**, §5.9.8) | **The claim is what makes this metric exist at all.** A bounded reviewer implies a queue depth; under an independent-interpretation claim there would be no K to have a recall against. The shape is settled; K is not | Operator scope — **review capacity**, explicitly and solely. K is not a property of GEPER |

**(B) STILL BLOCKED — with the specific blocker named. 9 of 24.** Naming the blocker is not
optional: *"still blocked" without naming what blocks it recreates EJ-01's own failure one
level down.*

> **⚠ ONE ROW HERE BREAKS THIS CATEGORY'S UNSTATED ASSUMPTION, AND IT IS FLAGGED RATHER THAN
> RECATEGORISED.** Every other blocker in this table is an **unfilled component of EJ-01**
> — population, specimen, operator, regulatory frame. **EJ-03b's blocker is another token in
> this document.** The four-category scheme was built assuming blockers are EJ-01 components,
> and it has no slot for that. A fifth category was considered and rejected: EJ-03b **is**
> still blocked, which is what (B) means, and its dependency on EJ-01 is real but
> **transitive**, reaching it through EJ-03a. Marking the row is honest; moving it would
> imply a different relationship to EJ-01 than it has.

| EJ | Blocked on |
|---|---|
| **EJ-03b** E2's THRESHOLD (§5.5) | **EJ-03a — the only intra-document blocker in this table**, and it is a hard block rather than a sequencing preference: the same numeric rate means materially different things under D1, D2, D3 and D4. Operator scope reaches it only *through* EJ-03a |
| **EJ-08** sample size / precision | **Population + specimen** (they set the truth-set composition), and downstream of the EJ-03a/EJ-03b–EJ-07 values |
| **EJ-11** gene / condition scope | **Specimen + regulatory frame** — what is in reportable scope |
| **EJ-12** Indian-population gate | **Population** — explicitly and solely |
| **EJ-13** GIAB thresholds | **Specimen** — which sample types S2 covers |
| **EJ-14** GIAB genomes / strata | **Population + specimen** |
| **EJ-15** EQA scheme / route | **Regulatory frame** |
| **EJ-16** EQA pass criterion | **Regulatory frame** |
| **EJ-18** who signs | **Regulatory frame + operator scope** |

**(D) NEVER BLOCKED BY EJ-01 AT ALL — a fourth category the three-way triage does not
capture, and reporting these as "unblocked now" would be false. 3 of 24.**

| EJ | Why |
|---|---|
| **EJ-17** re-validation trigger | §5.1.1's comparison found it resolves **identically** under both shapes. Any change to rules, config or models triggers re-validation regardless of what GEPER claims to be |
| **EJ-19** certified config value | Also **identical** under both. It turns on evidence-strength judgement, not on the clinical claim. Its lockability half is separately ruled (§2.4) |
| **EJ-22** INVALID trigger values | Also **identical** under both — **study-validity is use-independent**. Whether a study could measure GEPER does not depend on what GEPER claims to be. Its values are blocked on EJ-08's precision target, i.e. on population and specimen, never on the clinical claim |

#### ⚠ What the re-key actually moved — and its direction is the surprise

**No row changed category. The counts are unchanged: 2 answerable / 8 partial / 8 blocked /
3 never-dependent.** Reported plainly because a re-key that finds nothing reclassified is a
real result, and because "the statement changed" is not evidence that any particular row did.

**But four rows changed CONTENT materially — EJ-02, EJ-03, EJ-06 and EJ-20 — and two more
sharpened (EJ-04, EJ-05). The direction is the finding:**

> **THE AMENDMENT UNBLOCKS NOTHING. IT PARTIALLY *RE-BLOCKS* FOUR ROWS, BY RESTORING
> CLASSIFICATION SEMANTICS THAT THE PURE-PRIORITISATION READING HAD SUPPRESSED.**

Every one of the four moved the same way: the first pass claimed a **narrowing** that
depended on GEPER's output being a **ranking**, and the amendment's explicit "produces a
draft classification" **withdraws that narrowing**. EJ-06 is the clearest — it went from
"largely inert" to "no longer inert, and its weight turns on the review model". EJ-03 is the
most consequential: the safety event is no longer *not surfaced* alone, but **mis-drafted
and/or not surfaced**.

**Every one of the four now turns on the same unasked question: does the reviewer
independently RE-DERIVE the classification, or RATIFY the draft?** That is an operator-scope
question, and it is now the single highest-value unknown in this document — it did not exist
as a question at all under the pure-prioritisation reading, because there was no draft to
ratify.

**A re-key that makes the picture LESS resolved is the honest outcome here**, and it is the
opposite of what a re-key is usually expected to produce. Recorded in that direction rather
than presented as refinement.

#### ⚠ The count, and the test of the "18" claim — which fails in BOTH directions

**24 dependent DECISIONS. Fully answerable now: 2. Partially unblocked: 10. Still blocked: 9.
Never blocked by EJ-01: 3.** (2 + 10 + 9 + 3 = 24.)

> **✓ THE ROW/TOKEN MISMATCH RECORDED HERE IS DISCHARGED, 2026-08-22 — by doing the work the
> number pointed at, not by adjusting the number.** The earlier note read *"21 decisions is
> now 22 tokens"* and left the reconciliation owed. **The document holds 25 tokens; 24 are
> dependent (all but EJ-01); the table now has 24 rows. They agree.**
>
> **The re-key needed MORE than the split it was raised for, and that is the part worth
> keeping.** Splitting the shared `EJ-03` row alone would have produced 22 rows against 24
> dependent tokens — **still wrong, and wrong in a way that would have looked fixed**, because
> the diagnosis named EJ-03a/EJ-03b as the whole discrepancy. It was not: **EJ-24 and EJ-25
> had been created by F5 (§5.9.8) after the diagnosis was written and were never triaged at
> all.** A stale diagnosis is the same failure class as a stale citation or a stale grep
> pattern, and it was caught only by re-deriving the arithmetic from the current token list
> rather than trusting the note that described it.
>
> **What the split itself revealed:** the two halves do **not** land in the same category.
> EJ-03a is partially unblocked and gated on operator scope; **EJ-03b is blocked on EJ-03a**,
> making it the only row in the table whose blocker is not a component of EJ-01. That is a
> genuine difference the combined row was concealing — the row was not merely under-counted,
> it was **averaging two different states into one**.

*"18 decisions depend on EJ-01"* was repeated throughout the work that produced this
document. **Tested, it is wrong twice over, in opposite directions:**

- **Understated on dependents.** The real figure was **21**, not 18 — the range everyone
  used excluded EJ-20, EJ-21 and EJ-22, which are as dependent as the rest. **It is now 24**,
  after the EJ-03 split and F5's two new tokens (above); the 18 was wrong when made and has
  since been overtaken twice.
- **Overstated on power.** Only **2 of 24 become answerable** from the ratified clinical
  claim, because the statement deliberately settled one of EJ-01's five components.
  Separately, **7 of the 21 rows in §5.1.1's own comparison table do not discriminate between
  intended-use shapes at all** — a wrong genotype breaks either use equally, and
  study-validity conditions are use-independent. **That 7-of-21 denominator is §5.1.1's, not
  this table's**: it counts a frozen pre-split comparison and is deliberately not restated
  against 24, because re-basing a frozen table's arithmetic onto a later token list would
  invent a result §5.1.1 never reached.

**Neither error would have surfaced without running the count.** The corrected picture:
**21 of 24** depend on EJ-01 *in its full form*, and **the four unsettled components —
population, specimen, operator, regulatory frame — are now the real blockers. OPERATOR SCOPE
alone gates ten of them** (nine in (C), plus EJ-18 in (B)), far more than any other, which
makes it the highest-value of the four to settle next — and its lead **grew** with the
re-key, since both of F5's new tokens are operator-gated.

Reported with the full breakdown rather than the headline, because *"EJ-01 is answered"*
would otherwise be read as *"the dependent decisions are unblocked"*, and 2 of 24 is not
that.

### 5.1.3 The revalidation roadmap — entry criteria for a future interpretation claim

§5.1.1's Statement A is **moot as an option** and is retained for a different purpose: its
consequence analysis documents **what would have to be true to claim independent
interpretation later**. Per the ratified statement, that claim is reachable **only via
revalidation, never by amending the statement**, so these are **entry criteria for a future
revalidation** — not a roadmap for widening the current claim.

**[ANALOGY]** throughout, inherited from §5.1.1's analysis.

| # | Entry criterion | What changes from the current position |
|---|---|---|
| **R1** | **S1 executed and PASSED on ARM A**, not Arm B | EJ-10's current answer **flips**. Arm B may headline a prioritisation claim; an interpretation claim is a claim of independence, so the ClinVar-independent arm becomes load-bearing |
| **R2** | **The Arm B−C delta becomes DISQUALIFYING**, not informational | EJ-09's current answer **flips**. A large delta would mean the classification is substantially ClinVar's rather than GEPER's — tolerable for a prioritisation signal, fatal to an independence claim |
| **R3** | **E1–E6 thresholds reset at diagnostic stringency** | EJ-03b and EJ-04 revert from reviewer-workload to **patient-safety** consequence; EJ-06's tolerance tightens where P vs. LP drives different management |
| **R4** | **The population gate resolved** | EJ-12 moves from a weighted consideration to a probable hard gate — and §6.1's finding stands: no population-matched ground truth is obtainable today |
| **R5** | **Enrolled EQA, not shadow assessment** | EJ-15/EJ-16 resolve toward participation with external standing rather than an internal benchmark |
| **R6** | **Accredited signing standing** | EJ-18 resolves toward laboratory-director standing |
| **R7** | **Release-blocking as a live failure consequence** | EJ-21 resolves toward blocking rather than shipping with a documented limitation |
| **R8** | **The regulatory-scope determination resolved by counsel** | The premise flagged in §5.1.1 — that a prioritisation tool attracts lighter obligation than a diagnostic interpretation — is **unverified**. It underwrites the current position's lighter load, so it must be settled before any claim moves |

**R1 and R2 are the two that invert rather than tighten**, and they are the reason this is a
revalidation rather than an extension: the current position's answers to EJ-09 and EJ-10 are
not weaker versions of the interpretation claim's answers, they are the **opposite** ones. A
study designed and passed under the current claim does not partially support the stronger
one — **it measured a different thing**, and reusing its number would be the pooling defect
§2.3 already forbids, one level up.

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
             [VERIFIED — PMC4544753; the guideline explicitly recommends the five-tier
             terminology "pathogenic, likely pathogenic, uncertain significance, likely
             benign, and benign", which is what makes a binarisation rule necessary here at
             all]. **Its handling of Uncertain Significance in reporting is thinner than
             this token originally implied**: what was located is a "Variant re-analysis"
             recommendation that laboratories "suggest periodic inquiry by healthcare
             providers to determine if knowledge has changed on any VUSs" — which does not
             answer the binarisation question. Jennings et al., AMP/CAP, J Mol Diagn
             2017;19(3):341-365 [VERIFIED as to citation — see EJ-13, including the
             terminology caveat].
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

#### ⚠ EJ-03 is SPLIT: a definition question first, a threshold question second

**E2's threshold cannot be set, because E2's *event* has not stopped moving.** Under three
successive intended-use wordings the thing E2 counts has been redefined twice:

| Wording | What E2 counted |
|---|---|
| Original (classification-agreement framing) | truth P/LP **drafted as VUS** |
| Ratified, pure-prioritisation reading | the variant was **not surfaced** to the reviewer |
| Amended, "produces a draft classification" | **mis-drafted AND/OR not surfaced**, with the **reviewer** rather than the queue as backstop |

**A number attached to an event whose meaning is still moving is a threshold looking for an
event** — the same defect the human named at the start of this phase ("a concordance number
produced before we've agreed what would count as passing is a number looking for a
justification"), one level down. **A token that keeps moving under rewording is not ready to
be answered.**

So EJ-03 becomes **EJ-03a (definition)** and **EJ-03b (threshold)**, in that order and not
interchangeably. **EJ-03b is unanswerable until EJ-03a is fixed**, and the ordering is
encoded in the identifiers so it survives a reader who meets only one of them.

**Whether any other token has the same disease — checked, not assumed.** Of the rows that
moved during the re-key: **EJ-02 needs no split** (it is *already* a definition question, not
a threshold, so it has nothing to separate). **EJ-06 and EJ-04/EJ-05 moved in weight, not in
event** — what "within-tier drift" or "over-call" counts never changed, only how much it
matters. **EJ-20 did move definitionally, but its instability is INHERITED from EJ-03's, not
independent**: what moved was the membership of the safety-relevant class, which is precisely
what EJ-03a fixes. **Fixing EJ-03a stabilises EJ-20 as a side effect**, so EJ-20 is not split
here. EJ-03 is the only token whose own event definition moved.

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-03a
  DECIDE   — **DEFINITION, and it must be settled before EJ-03b exists as a question.** What
             event does E2 count? Four candidates, and they are not variants of one another:
             **D1 — the drafting error alone:** GEPER drafted a non-P/LP classification for a
             truth-P/LP variant, regardless of whether it was surfaced.
             **D2 — the surfacing error alone:** the variant never reached the reviewer,
             regardless of what was drafted.
             **D3 — the union of D1 and D2**: either failure counts.
             **D4 — the end-to-end error:** the variant reached the reviewer AND the reviewer
             signed off on the wrong draft.
             **⚠ D4 changes what S1 measures.** It requires modelling or observing the
             reviewer, which converts S1 from *"measure GEPER"* into *"measure GEPER plus its
             reviewer"* — the same distinction §4.2 already draws for external QA Route (i),
             arriving here independently.
             ———
             **⚠ THE ORDER IN WHICH THIS TOKEN IS ANSWERED IS RULED (human, 2026-08-22), AND IT
             IS NOT THE ORDER A READER WOULD GUESS. Three questions, in this sequence:**
             **(0) FIRST — THE GUIDANCE FRAME. Read it before either question below.** No
             published guidance defines analytical sensitivity for a **draft-awaiting-review**
             output (see this token's GUIDANCE block). **The expert is therefore DECIDING, not
             APPLYING.** This is placed first on the human's explicit ruling, and their reason
             is the whole of its value: *it changes what KIND of answer the other two can
             have.* **An expert who believes they are applying a standard will go looking for
             one — and will find THREE that appear to fit**: Richards 2015, Jennings 2017 and
             CLIA §493.1253(b)(2), each of them real, each independently verified, and none of
             them on target. A near-miss citation is more dangerous here than no citation,
             because it terminates the search.
             **(1) SECOND — D4 OR NOT, AND THIS IS A SCOPE DECISION RATHER THAN A DEFINITIONAL
             ONE.** Ruled so by the human: choosing D4 makes S1's subject **the workflow**
             rather than **the software**. It is answerable on its own terms and must be taken
             on its own terms — it does **not** wait on the question below.
             **(2) THIRD, AND ONLY IF D4 IS REJECTED — does the reviewer independently
             RE-DERIVE the classification, or RATIFY the draft?** That question selects among
             **D1, D2 and D3 only**; it has no bearing on D4. If the reviewer re-derives, a
             wrong draft is caught and the safety-relevant event is **D2**. If the reviewer
             ratifies, a wrong draft propagates to sign-off and the event is **D1 or D3**.
             **Naming that dependency is this token's deliverable; resolving it is not.**
             **⚠ THIS ORDERING SUPERSEDES AN EARLIER FRAMING IN THIS TOKEN** which made all
             four of D1–D4 turn on ratify-vs-re-derive. They do not: **D4 is settled before
             that question is ever reached**, and collapsing the two would hand a scope
             decision to a workflow answer.
  GUIDANCE — **None of the external sources bears on this.** Richards et al. 2015, Jennings
             et al. 2017 and CLIA §493.1253(b)(2) [all VERIFIED — see EJ-19, EJ-13, EJ-08]
             define analytical sensitivity for an assay whose output IS the answer. **They do
             not address an output that is a draft awaiting review, which is what the amended
             intended-use statement describes.** [ANALOGY] — the D1–D4 taxonomy is this
             document's own construction and nothing external backs it.
  DECIDED-BY — **the clinical expert, at the expert gate, alongside EJ-01's remaining scope
             components.** The ratify-vs-re-derive question it depends on has been ruled a
             clinical-workflow decision rather than a product decision, and is carded at the
             TOP of the expert-gate list rather than inside operator scope. Not the
             laboratory director alone, and emphatically not this floor.
>>>

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-03b
  DECIDE   — **THRESHOLD, and it is BLOCKED ON EJ-03a — not merely sequenced after it.** The
             maximum acceptable E2 rate in Arm A, for whichever event EJ-03a fixes. This
             remains the single most safety-relevant number in the design, which is exactly
             why it must not be set against a moving definition: **the same numeric rate
             means materially different things under D1, D2, D3 and D4**, so a number agreed
             now would not survive EJ-03a being answered, and would be quoted afterwards as
             though it had.
  GUIDANCE — Richards et al., ACMG/AMP 2015, Genet Med 17(5):405-424 [VERIFIED — see
             EJ-19]. Jennings et al., AMP/CAP, J Mol Diagn 2017;19(3):341-365, analytical
             sensitivity [VERIFIED as to citation — see EJ-13]. CLIA 42 CFR
             §493.1253(b)(2) [VERIFIED — see EJ-08; analytical sensitivity is named
             explicitly in (b)(2)]. **These support a threshold once the event is fixed; they
             cannot help fix it — see EJ-03a's GUIDANCE.**
  DECIDED-BY — the laboratory director / accountable clinical geneticist, on the basis of the
             intended use and the clinical consequence of a missed pathogenic call in that
             specific use — including whether a human reviewer sees the evidence trail and
             could catch it. **A rate acceptable behind mandatory expert review is not
             acceptable without it**, and which of those two regimes applies is EJ-03a's
             answer, not this token's assumption.
>>>

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-04
  DECIDE   — the maximum acceptable E3 rate (truth Benign/Likely Benign or VUS classified by
             GEPER as Pathogenic/Likely Pathogenic) in Arm A: the over-call rate. Its
             consequence is different in kind from E2 — unnecessary intervention, surveillance
             or cascade testing rather than a missed finding — so it needs its own number and
             its own reasoning, not a symmetric one derived from EJ-03b.
  GUIDANCE — Richards et al., ACMG/AMP 2015 [VERIFIED — see EJ-19]. Jennings et al.,
             AMP/CAP, J Mol Diagn 2017;19(3):341-365, analytical specificity /
             false-positive rate [VERIFIED as to citation — see EJ-13; note the source's own
             framing is positive predictive value per variant type, which is closer to what
             this token needs than the "analytical specificity" wording used here].
  DECIDED-BY — the laboratory director / accountable clinical geneticist, on the basis of
             what clinical action a P/LP call triggers in the intended-use workflow.
>>>

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-05
  DECIDE   — the maximum acceptable E4 rate (truth Benign/Likely Benign classified as VUS):
             the VUS-inflation rate. Not a safety failure, but a high rate makes the tool
             clinically unusable by generating review burden that swamps its benefit, and a
             validation that only measures safety-relevant errors will not surface it.
  GUIDANCE — **[INVENTED]** — this token originally cited Richards et al., ACMG/AMP 2015,
             Genet Med 17(5):405-424 for "guidance on minimising unnecessary VUS
             assignment". **Verification (2026-08-22, PMC4544753) establishes that the
             guideline does not explicitly address minimising unnecessary VUS assignment.**
             What it does contain on VUS is a "Variant re-analysis" recommendation about
             periodic re-inquiry, which is a different subject. Retained as INVENTED rather
             than swapped for a better citation, because no better one was found and a
             substituted citation would conceal that the original attribution was wrong.
             The document identity itself is [VERIFIED]; only this attribution failed.
             ACMG/AMP interpretation literature on VUS burden in clinical reporting
             [UNFETCHED-NOT-LOCATED — no single document was named originally and none was
             located; searched the ACMG/AMP guideline and its ClinGen SVI specifications].
             **Consequence: EJ-05 currently has NO verified external guidance behind it.**
             The decision is still real — VUS inflation is a real burden — but whoever
             fills it is reasoning from clinical judgement and local review capacity, not
             from a published threshold. That is a materially weaker footing than EJ-03b or
             EJ-04 and must not be presented as equivalent.
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
  GUIDANCE — Richards et al., ACMG/AMP 2015 [VERIFIED — see EJ-19; the five-tier system it
             establishes is what makes P↔LP and B↔LB drift a distinct error class].
             `VALIDATION_READINESS_RUBRIC.md`
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
             reportable-range requirements [VERIFIED as to citation — see EJ-13; the
             no-call-rate attribution specifically was not located at section level]. CLIA
             42 CFR §493.1253(b)(2), reportable range [VERIFIED — see EJ-08; "reportable
             range of test results" is named explicitly in (b)(2), so this is the
             best-supported half of this token]. ISO 15189:2022 clause 7.3.3
             [VERIFIED (secondary) — see EJ-19].
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
             procedure [VERIFIED (secondary) — see EJ-19; 7.3.3 d) requires a decision on
             whether to implement a modified method and 7.3.3 e) requires validation
             records, which is the specific support this token needs]. CAP Molecular
             Pathology Checklist, revalidation requirements following assay modification
             [UNFETCHED-PAYWALLED — see EJ-15; CAP checklists must be purchased]. Jennings
             et al., AMP/CAP, J Mol Diagn 2017;19(3):341-365, sections on assay modification
             [VERIFIED as to citation — see EJ-13].
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
             authorisation of procedures [UNFETCHED-NOT-LOCATED — no free source gave
             titles for 5.2 or 5.3; iso.org 403 and the accreditation-body PDFs that
             confirmed the 7.3.x clauses did not enumerate clause 5. Do not rely on these
             numbers]. NABL 112, Specific Criteria for Accreditation of Medical Laboratories
             [VERIFIED — **this was flagged as the document's lowest-confidence citation and
             it checks out**: NABL 112 is "Specific Criteria for Accreditation of Medical
             Laboratories", published by the National Accreditation Board for Testing and
             Calibration Laboratories and hosted at nabl-india.org. A **NABL 112A** also exists;
             **confirmed (2026-08-31) to be the applicable document**: "Specific Criteria
             for Accreditation of Medical Laboratories", Issue No. 01, Issue Date
             18-Dec-2024, freely downloadable from nabl-india.org — no purchase, no
             paywall (SHA256
             dafc1404d019b8258b434a8baf45f26c5392933a73a597b6230e880704212e13, 110 pp.).
             Clause 7.8.5 "Next Generation Sequencing" (pp.83–88) is NABL's own bespoke
             NGS content, not a renumbered ISO clause: siblings 7.8.1–7.8.4 and 7.9 each
             tag their lettered items with an explicit ISO cross-reference (e.g. "Cl. 6.3
             of ISO 15189:2022"); none of 7.8.5's four items carry any such tag. **This is
             a confirmation, not certainty**: confidence that this is the document the
             original "NABL 7.8.5(b)" citation meant is high but not absolute — it rests
             on content match (the four sub-item headings map 1:1 onto the four topics)
             and notation match, not on chain of custody, since whoever wrote the original
             citation could not be asked. The same principle §0.3 states for a corrected
             citation applies to a confirmed one: the basis is given explicitly rather than
             left to be inferred, because a confirmation that hides its basis is
             indistinguishable from an assertion]. CLIA 42 CFR §493.1445, laboratory director
             responsibilities [VERIFIED — heading is "Standard; Laboratory director
             responsibilities"; opens "The laboratory director is responsible for the
             overall operation and administration of the laboratory..."].
  DECIDED-BY — the organisation's regulatory/accreditation lead. Not a technical decision and
             not one this floor can make.
>>>

---

### 5.9 Study-level failure declaration — what result would mean THIS VALIDATION FAILED

Everything above defines when an individual **criterion** fails. **None of it defines when
the study fails.** Those are different questions, and only the first was answered.

A study whose only defined outcomes are per-criterion can always be written up as *"these
criteria passed, these are pending, these need re-scoping"* — a conclusion that no result
can prevent. That is the cannot-fail defect at plan level, and its location is not any
single criterion: it is **the absence of an aggregation rule**. This subsection is that
rule.

**[INVENTED] — all of §5.9.** No external source supplies a study-level failure declaration
for a variant-interpretation validation; this is the same principle the rest of the document
applies to individual tests, raised one level. Per §0.2, marking it INVENTED does not make it
optional — the tiering records provenance, not force. It tells a reader they cannot check
this against an authority and must judge the reasoning instead. The reasoning is the
paragraph above.

#### 5.9.1 S1 terminates in one of THREE states, not two

| Outcome | Meaning | What it is a finding about |
|---|---|---|
| **PASS** | Every acceptance criterion met, at its declared stratum, at thresholds fixed before the run | GEPER |
| **FAIL** | GEPER's accuracy is insufficient for the intended use | **GEPER** |
| **INVALID** | The study could not measure GEPER | **the study** |

**A two-outcome design silently converts INVALID into whichever of PASS or FAIL is more
convenient at the time.** The third state exists so that *"we could not measure this"* is a
reportable result rather than a gap someone fills with a narrative. **A study that cannot
distinguish "GEPER failed" from "this study could not measure GEPER" will report the first
when the second is true** — and an assessor reading a FAIL has no way to tell which they
are looking at unless the design separated them in advance.

#### 5.9.2 FAIL — the pattern, stated structurally

S1 FAILS if **any** of the following holds. The shape is fixed here; only the numbers are
open.

- **F1 — any E1 occurrence.** Any clinically-opposed call (truth P/LP vs. GEPER B/LB, either
  direction). **Threshold-independent and already ratified** — the rubric's 1a falsifier
  fixes it at "no tolerance", which is why E1 carries no token (§5.5). **This is the anchor
  of the entire declaration: one failure route that exists regardless of where every EJ in
  this document lands, and that no threshold-setting decision can tune away.**
- **F2 — Arm A misses any of its acceptance criteria, regardless of Arm B or Arm C.**
  **Arm B cannot rescue Arm A.** Stated explicitly because Arm B will almost certainly
  produce the better number and is the one that resembles a conventionally-reported
  concordance rate. Without F2, a failing primary arm could be reported alongside a passing
  assisted arm and the reader left to average them.
- **F3 — more than a declared number of acceptance criteria unmet** across all strata, even
  where no single criterion is individually disqualifying → **EJ-20**.
- **F4 — any criterion that could not be evaluated because its stratum was not populated**
  counts as **unmet** for F3. Never as "pending".
- **F5 — any ratified-boundary RANK INVERSION.** Within one ranked run, a **truth-P/LP**
  variant ranked below a variant that is **both truth-B/LB and GEPER-classified B/LB**.
  **Threshold-independent, on the same inherited authority as F1** — it applies the rubric's
  already-ratified P/LP↔B/LB boundary at zero tolerance to GEPER's *other* output. **F1 is
  structurally blind to this failure**, and the reason is verifiable in code rather than
  arguable from the design — §5.9.8, which also records why an anchor was needed here at all.

**The "pending" route is closed.** No acceptance criterion may terminate in any state other
than **met**, **unmet**, or **study-invalid**. "Pending", "deferred", "inconclusive" and "to
be re-scoped" are not outcomes of this study, and a report using them for a criterion has
departed from this design.

#### 5.9.3 INVALID — the case that matters most, and its escape-hatch risk

S1 reports **INVALID** rather than FAIL when the study could not measure what it set out to
measure. The conditions, all declared in advance:

- **I1 — Arm A too small after stratification.** The Arm A:B split is unknown until measured
  (§2.6); if Arm A lands below the precision target set at EJ-08, or below the minimum
  distinct-gene count, S1 cannot support a claim in either direction → **EJ-22**.
- **I2 — the exclusion table is too large for the denominator to be credible.** §5.3 requires
  every exclusion be named and counted; beyond a declared fraction, what remains is not a
  sample of the truth set → **EJ-22**.
- **I3 — Arm A is unrepresentative, evidenced by the B−C delta.** If ablating PS1/PM5 moves
  Arm B's result by more than a declared amount, the ClinVar-independent and
  ClinVar-assisted populations are not answering the same question, and an Arm A number
  cannot be generalised → **EJ-22**. (Distinct from EJ-09, which asks when the delta is a
  *finding about GEPER*; this asks when it invalidates *the study*.)
- **I4 — Arm C could not be produced.** If the §2.4 pre-flight fails and no ablation is
  available, I3 cannot be evaluated at all, so its own precondition is missing.
- **I5 — the two pins broke.** Code not at `470a993`, or truth labels and consumed evidence
  drawn from different ClinVar releases (§2.2). Observed discordance is then partly
  reclassification drift and cannot be attributed.
- **I6 — Arm A collapsed onto too few genes**, so that per-variant results are effectively
  a handful of independent observations (§2.6's clustering problem) → **EJ-22**.

**Precedence: validity is adjudicated BEFORE outcome.** An invalid study yields no accuracy
finding.

**⚠ The mirror-image risk, named because it is the obvious way to abuse this subsection:
INVALID must not become the hatch that a disappointing result is routed into.** Two controls,
both structural:

1. **Every invalidity condition and its trigger value is pre-registered under §5.6**, on
   exactly the same footing as a threshold and in the same commit. An invalidity criterion
   invented after the results are visible is not an invalidity criterion; it is a
   description of the results.
2. **An INVALID study may not be reported as a pass, at any scope, in any form.** It
   licenses no accuracy claim in either direction. **If the sentence that naturally follows
   "the study was invalid" is "but the results looked good", that sentence is the defect
   this control exists to prevent.**

#### 5.9.4 The re-scoping escape, closed explicitly

The claim, the strata and the gene scope fixed under **EJ-01** and **EJ-11** **may not be
narrowed after results are seen.**

If they are narrowed, S1 is reported as **FAILED, and then re-scoped** — never as *passed at
the narrower scope*. Both facts travel together in every subsequent citation of the result.

This is the least enforceable rule in the document and the most necessary. A study that can
redraw its own boundary after seeing the data has no defined failure state whatever else it
declares, because any result becomes a pass for some scope. The mechanism is the same cheap
one §5.6 uses: the scope is committed before the run, and `git log` on this file carries the
ordering where anyone — including an assessor — can see it.

#### 5.9.5 Consequence — a failure with no defined consequence is not a failure

A declared FAIL that obligates nothing is indistinguishable, in every downstream artifact,
from a PASS. The consequence therefore has to be fixed at the same time as the failure
condition and in the same commit — not decided once a failure is in front of someone with an
interest in its meaning. **Shipping unchanged is a legitimate consequence and must be
nameable as one**; what is not legitimate is leaving the question open. The decision is
**EJ-21** (§5.9.7).

#### 5.9.6 S2 and S3

**S2** takes this same three-state structure against its own acceptance criteria (EJ-13,
EJ-14), with I5's pin requirement and §5.3's denominator rule applying unchanged. Its F1
equivalent is not defined here: no ratified zero-tolerance falsifier exists for
variant-calling the way the rubric supplies one for classification, and **inventing one
would be setting an acceptance threshold**, which is the boundary this document does not
cross. Named as a gap in this subsection's coverage rather than left to be discovered.

**S3** is different in kind: its outcome is the external scheme's to declare, not this
design's, and EJ-16 already carries both the passing criterion and the consequence of a
fail.

#### 5.9.8 F5 — the threshold-independent anchor for the RANKING output

**Why this subsection exists, and why its first job was to try to make itself unnecessary.**
Three routes reached the same suspicion independently: §5.9.6's note that S2 has no
F1-equivalent; §5.1.1's Statement-B cost analysis, which found that F1 *"has no defined
equivalent for a ranking output"*; and Angela's precision-vs-recall question against rubric
1a. **Angela's own position was that 1a's ratified falsifier MAY ALREADY SUBSUME the
concern**, held explicitly without confidence. **So the first question is whether a gap
exists at all** — *"the existing falsifier subsumes it, here is why"* would have closed this
as a real result, and an anchor manufactured to justify the question would be worse than no
anchor.

**It was tested. The gap is real, and it is demonstrable in code rather than arguable from
the design.**

##### The subsumption test, stated precisely enough that it could have passed

Rubric 1a's falsifier (`VALIDATION_READINESS_RUBRIC.md` @ `89bf125`, verbatim):

> *"Any variant in the truth set where GEPER's proposed classification and the truth set's
> expected classification disagree across a Pathogenic/Likely-Pathogenic vs Benign/Likely-
> Benign boundary is an automatic fail, no tolerance."*

**It quantifies over CLASSIFICATIONS.** The quantity it inspects is
`combine_result.classification` (`geper/pipeline/acmg_rules.py:684` @ `c2258df1`, the `CombineResult.classification` field definition — was `:957`, now-wrong-location: that line now falls inside the PP5 `_not_evaluated` block after intervening insertions). It never reads a score,
a category, or a rank. **Subsumption would therefore require that rank be a function of
classification** — if it were, a buried pathogenic variant would have to have been
mis-classified first, and F1 would catch it on the way past.

**It is not.** [VERIFIED-IN-REPO @ c2258df1], `geper/pipeline/prioritization_engine.py`:

- **`:2-11`** — the engine's own docstring says it *"answers a different question"* than ACMG
  classification, that it *"computes its own score with its own weights"*, and that it
  **uses** the classification as **one of its weighted inputs** rather than deriving from it.
- **`:153-163`** — the score is a weighted sum of **eleven** factors. ACMG classification is
  one. The others are confidence, clinical evidence, population rarity, AI agreement, protein
  impact, conserved domain, structural, splicing, sequence context and BLAST.
- **`:170-174`** — a **multiplicative conflict penalty** is then applied:
  `final_score = max(0.0, raw_score * (1.0 - penalty))`, which can move a variant's position
  without its classification changing at all.
- **`:583-587`** (`rank_batch`) — rank is assigned by sorting those final scores.

**So the classification→rank map is many-to-many, and the failure that matters is invisible to
F1:** *a variant GEPER classified **correctly** as Pathogenic, ranked below the review cutoff,
never reaching a human.* That produces **zero** F1 events — not few, zero — because the one
quantity F1 inspects is **right** in exactly this failure. **F1 is not weak here. It is
structurally blind.**

##### A fourth route, which is the strongest evidence available: the defect was already observed

The three routes above are design arguments. **The code contains a fourth, and it is not an
argument — it is a record of this exact failure having happened and been patched once**
(`prioritization_engine.py:184-193`, verbatim):

> *"review priority was previously driven purely by this engine's own evidence-completeness
> factors, which are near-orthogonal to whether GEPER's classification disagrees with an
> expert-panel/practice-guideline ClinVar record (a synonymous or otherwise evidence-sparse
> variant scores low on Protein Impact/Conserved Domain/Splicing regardless of how urgent the
> classification disagreement itself is) -- so a Critical clinical conflict could rank
> **below** findings with no conflict at all."*

**That closes the subsumption question outright.** Angela's concern is not speculative and
1a does not cover it: the codebase found the same hole from the inside, by a route none of us
took, and describes it in the same terms.

> **⚠ AND THE SCOPE OF THAT PATCH IS THE FINDING UNDERNEATH THE FINDING.** The remedy
> (`:194-199`) floors the review category at **Critical** — but **only when
> `critical_conflict` is set, which requires disagreement with an expert-panel/
> practice-guideline ClinVar record.** **Arm A is defined by the absence of exactly that
> record.** So the one rank-safety mechanism that exists today **cannot fire in the primary
> arm**, and F2 forbids Arm B rescuing Arm A. The protection is real, and it is present in
> precisely the arm that is not load-bearing.

##### Why F5 needs no number, and why that is inherited rather than claimed

**F5 does not choose a tolerance.** It applies the **already-ratified** P/LP↔B/LB boundary, at
the **already-ratified** zero tolerance, to GEPER's other output. Same truth-set pairs, same
boundary, same ratified zero — the authority is the rubric's, not this document's. **Had the
anchor required a number I selected, it would be an EJ token and not an anchor**, and E-4
would have been answered with the defect it was raised to prevent.

**F5 also never mentions the review cutoff K.** It is a **pairwise ordering** property: given
any two variants in one ranked run, the ordering between them must not invert across the
ratified boundary. That is deliberate, because **K is where operator scope lives** — an anchor
that needed K would inherit EJ-01's unfilled operator component and stop being an anchor.

##### The strongest objection to F5, recorded because an unattacked anchor is not yet an anchor

The engine ranks **review urgency**, not pathogenicity (`:3-7`: *"not 'is this variant
pathogenic'... but 'how urgently does this variant deserve human review'"*). A confidently
benign variant needs no review. An **uncertain** one may legitimately outrank an unambiguous
pathogenic call that needs only a signature. **Under that reading a P-below-B inversion is not
a defect at all — it is the engine working as specified**, and a naive F5 would fail a
correct system.

**That objection is exactly why F5 carries its second clause.** The higher-ranked variant must
be **GEPER-classified B/LB** as well as truth-B/LB. That removes every available uncertainty
story: **GEPER's own classification says the variant it placed first is benign, and it placed
it first anyway.** No review-urgency reading survives that. **The objection does not defeat
F5; it locates it** — and the located version is narrower, and correspondingly harder to
dismiss.

##### Unranked variants — ruled structurally, because the alternative is a number

`rank_batch` assigns `None` rank to any variant whose priority scoring failed or was skipped
(`:574-575`, `:583`) — *"`None` scores get `None` rank rather than being fabricated a
position"*, which is the right behaviour for the engine and a hole for the study. **Against a
top-K queue, an unranked truth-P/LP variant is not "no result". It is ABSENT** — the same
outcome as being ranked last, reached by a different route.

**RULED: an unranked variant counts as ranked LAST for F5's purposes.** [INVENTED], by direct
analogy to **F4**, which already refuses "pending" as a terminal state for an unevaluated
criterion. **This introduces no number** and is a shape decision of the same kind F4 is; it is
tagged rather than presented as derived because no external source supplies it.

##### What F5 does NOT close — three things, named rather than left to be discovered

1. **It does not close S2's gap, and the two must not be merged.** §5.9.6's missing
   F1-equivalent is for **variant calling** (is the variant present at all); F5 is for
   **ranking** (does a correctly-called, correctly-classified variant reach a human). They
   resemble each other closely enough to be pooled by a hurried reader, and **pooling them
   would be the error §2.3 forbids one level up**. **S2's gap stands open and unchanged.**
2. **F5 is not a recall criterion.** It forbids an inversion; it says nothing about queue
   depth or what fraction of truth-P/LP must appear above the cutoff. That is a **threshold**,
   it is **EJ-25**, and **it is the half that BLOCKS ON OPERATOR SCOPE — specifically the
   REVIEW-CAPACITY sub-component of EJ-01**, because review capacity is what sets K. Named
   precisely, because *"blocked on scope"* without naming which component recreates EJ-01's
   own failure one level down.
3. **The tie case is not ruled here** — **EJ-24**.

##### A supersession F5 creates inside a frozen subsection — flagged, not applied

§5.1.1's Statement-B cost paragraph states that F1 *"has no defined equivalent for a ranking
output"*. **F5 supplies one, so that sentence is now false.** §5.1.1 is under the human's
change freeze; C-10 unfroze it **for one label only**, and this is not that label. **The
sentence is therefore left standing and the conflict is recorded here for ruling** rather
than corrected in place — the same discipline applied to every other frozen-text conflict on
the consolidated list.

##### Cross-tree check, stated because the finding is product-shaped

**`kim_pipeline/` was checked directly, not assumed.** It has **no variant-prioritisation
stage and no ranked output**: its only `priorit`/`rank` matches are
`pipeline/annotation/gff_index.py:269-321` (deterministic **transcript** selection order) and
`pipeline/pgx/stage.py:296` (deterministic **allele** specificity order), neither of which is
a review queue. **F5 is therefore scoped to `geper/` only**, and that is a checked fact rather
than a scope assumed from where the finding was made.

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-24
  DECIDE   — whether a **score TIE across the ratified boundary** counts as an F5 inversion:
             a truth-P/LP variant and a GEPER-B/LB-and-truth-B/LB variant receiving the
             **same** final priority score. **This is not a tolerance and cannot be answered
             by choosing one.** The reason it needs deciding is mechanical: `rank_batch`
             breaks ties by **processing order** (`prioritization_engine.py:574`, *"ties keep
             the order they appeared in"*), so a tie never surfaces as an equal rank — it
             surfaces as a **definite ordering produced by input order**, which carries no
             clinical meaning. A tie is therefore either (i) not an inversion, because the
             engine expressed no preference, or (ii) worse than an inversion, because the
             ordering a reviewer sees was decided by file order. **Both readings are
             defensible and this document does not choose between them.**
  GUIDANCE — **None. No external source addresses tie-breaking in a clinical review queue**,
             and none of the sources cited elsewhere in this document reaches it — checked,
             not assumed. [INVENTED] — the question is this document's own construction,
             arising from a verified implementation detail rather than from any standard.
  DECIDED-BY — the accountable clinical lead, with whoever owns the review workflow. It is
             adjacent to but **not** the same as EJ-25's capacity question: EJ-24 is about
             what the ordering MEANS, EJ-25 about how deep it is read.
>>>

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-25
  DECIDE   — the **recall criterion for the ranked output**: the review-queue depth **K**
             that the study measures against, and the minimum fraction of truth-P/LP variants
             that must appear within the top **K**. **This is a THRESHOLD and is deliberately
             separate from F5**, which is the anchor. F5 fires on an inversion at any depth
             and needs no K; this token is the coverage question F5 does not answer, and the
             two must not be conflated — satisfying F5 says nothing about how many pathogenic
             variants a reviewer actually reaches.
             **⚠ BLOCKED ON OPERATOR SCOPE — specifically the REVIEW-CAPACITY sub-component of
             EJ-01.** K is not a property of GEPER; it is how many variants the reviewing
             service can actually read per case. A K chosen without that is a number chosen
             for the convenience of the metric.
  GUIDANCE — **No published guidance sets a recall-at-K for a clinical variant review queue**
             — the same finding recorded at EJ-03a, arriving independently. Richards et al.
             2015 and CLIA §493.1253(b)(2) [both VERIFIED — see EJ-19, EJ-08] define
             analytical sensitivity for an assay whose output IS the answer; **a ranked queue
             read to depth K is not that**, and citing them here would be the
             verified-but-off-target error EJ-03a names. [ANALOGY] — recall@K is borrowed
             from information retrieval, and the borrowing is the tag.
  DECIDED-BY — the accountable clinical lead together with whoever owns review capacity, and
             **only after EJ-01's operator component is filled**. Not the laboratory director
             alone; emphatically not this floor.
>>>

#### 5.9.7 The decisions §5.9 creates

**Five, not the three this subsection originally created:** EJ-20, EJ-21 and EJ-22 with the
three-state declaration, plus **EJ-24 and EJ-25 with F5** (§5.9.8). All five must be filled
and committed **before the first measurement run**, under §5.6, on the same footing as any
threshold. **EJ-22 in particular is not a threshold on GEPER — it is
a threshold on whether the study means anything**, and deferring it until after the results
are visible would defeat the whole of §5.9.3.

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-20
  DECIDE   — the F3 threshold: how many acceptance criteria may be unmet across all strata
             before S1 as a whole is declared FAILED, even where no single criterion is
             individually disqualifying. Note this is not one number but a policy: whether
             unmet criteria are counted equally, or weighted by the safety-relevance of the
             error class they govern (E2 and E3 are safety-relevant; E4 and E5 are not).
  GUIDANCE — ISO 15189:2022 clause 7.3.3 b), which requires the extent of validation to be
             "sufficient to ensure the validity of results pertinent to clinical decision
             making" [VERIFIED (secondary) — see EJ-19; this is the closest external
             anchor for an aggregate rather than per-criterion judgement, and it is an
             anchor rather than an answer]. `VALIDATION_READINESS_RUBRIC.md` D1/1a
             falsifier @ `89bf125` [in-repo, verified — supplies F1 outright, and nothing
             beyond it]. **No external source defines an aggregate failure rule for a
             variant-interpretation validation; F3's shape is [INVENTED] (§5.9) and only its
             value is being decided here.**
  DECIDED-BY — the laboratory director with the accountable clinical lead, on the basis of
             the intended use fixed in EJ-01. This is the decision that determines whether
             the study can return a negative result at all, so it must be made before the
             run and not by whoever assembles the final report.
>>>

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-21
  DECIDE   — what a FAIL obligates. At minimum, which of: the claim is narrowed to a
             stated scope; the intended use is restricted; release is blocked pending
             remediation; or the result is reported with the product shipped unchanged.
             The last option is legitimate and must be nameable — but it has to be chosen
             in advance and recorded, because a failure whose only consequence is decided
             after the fact is not a failure. Must also state who is notified and within
             what period.
  GUIDANCE — ISO 15189:2022 clause 7.3.3 d), requiring a decision on whether to implement
             a modified method, and 7.3.3 e), validation records [VERIFIED (secondary) —
             see EJ-19 and EJ-17]. CLIA 42 CFR §493.1253 [VERIFIED — see EJ-08].
             **Both establish that a validation outcome must drive an implementation
             decision; neither specifies what the decision must be for a software
             interpretation tool that is not itself a regulated device. That part is
             [INVENTED] and rests on §5.9's reasoning.**
  DECIDED-BY — the laboratory director with the organisation's regulatory lead, and with
             whoever holds release authority — the three cannot be the same person for this
             decision without defeating its purpose.
>>>

<<<EXPERT-JUDGEMENT-REQUIRED: EJ-22
  DECIDE   — the trigger values that make S1 INVALID rather than FAILED: (a) the minimum
             Arm A size and minimum distinct-gene count below which no claim is supportable
             (I1, I6); (b) the maximum total exclusion fraction beyond which the denominator
             is not credible (I2); (c) the maximum Arm B−C delta beyond which Arm A cannot
             be generalised (I3). All three must be fixed and committed BEFORE the run under
             §5.6, on the same footing as a threshold — an invalidity trigger chosen after
             results are visible is a description of the results.
  GUIDANCE — Jennings et al., AMP/CAP, J Mol Diagn 2017;19(3):341-365, on the minimum
             number of samples required to establish test performance characteristics
             [VERIFIED — see EJ-08; informs (a) only]. **Nothing external was found for (b)
             or (c): no source defines an exclusion-fraction ceiling or a stratum-divergence
             ceiling for this kind of study. Both are [INVENTED], and (c) in particular
             depends on the Arm A/B/C structure, which is itself this document's invention
             (§1.3) — so no external authority for it can exist.**
  DECIDED-BY — a clinical-genetics lead with a statistician, jointly, as for EJ-08. The
             statistician's half is load-bearing here: (a) and (c) are questions about
             whether an estimate means anything, not about whether GEPER is good.
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
             Human Participants, 2017 [VERIFIED — exact title and year confirmed; issued by
             the Indian Council of Medical Research, released 12 October 2017]. ISO
             15189:2022, biological reference intervals and clinical decision limits and
             their applicability to the served population [**[INVENTED]** — this token
             originally cited **clause 7.3.4**. Verification establishes that **7.3.4 is
             "Evaluation of measurement uncertainty (MU)"**, not reference intervals.
             Biological reference intervals and clinical decision limits ARE a subject of
             clause 7.3, but at a different subclause — most likely 7.3.5, which could NOT
             be confirmed to this document's standard and is therefore not asserted.
             Retained as INVENTED rather than corrected to a guessed number, which would
             repeat the original error. **EJ-12 currently has no verified ISO clause behind
             it**; the ICMR citation and the in-repo audit below carry it].
             `geper/GROUND_TRUTH_DATASET_AUDIT.md`
             @ `6f27e7b`, "The ICMR/India gap" [in-repo, verified].
  DECIDED-BY — the human, with regulatory counsel. This escalates above the floor: it is a
             product and market-access decision with a budget consequence, not a study-design
             decision, and Meredith's own audit says it "should reach whoever owns the
             ICMR/NABL readiness question directly."
>>>

### 6.2 Dataset dependencies, and their gating status

| Study | Dataset | Gated? | Status |
|---|---|---|---|
| S1 | ClinVar 3★/4★ subset | **No** | Public domain; already flowing through the existing client; no new integration **[VERIFIED-IN-REPO @ c2258df1]** |
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

**[VERIFIED-IN-REPO @ c2258df1]** `acmg_rules.py:624` (was `:601`; the old line now falls inside an unrelated `DATA_UNAVAILABLE`-default comment) — the per-criterion `confidence` field is
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

1. ~~Every [GUIDANCE] citation must be verified against its primary source.~~ **DONE
   2026-08-22 — the gate was run; see §0.3 for the verdict scheme and §9 for the tier
   counts.** 13 citations verified, 2 blocked by paywall (with the blocker named), 4
   not located, **4 re-marked [INVENTED]** (§4.3 / EJ-15 / EJ-16 ISO 7.3.7.2→7.3.7.3,
   EJ-05 Richards-on-VUS, EJ-12 ISO 7.3.4). The prediction in the original version of this
   item was half right and half wrong, which is worth recording: **NABL 112 was flagged as
   the lowest-confidence citation in the document and it verified clean**, while the ISO
   clause numbers — flagged only as a group — are where all three clause-level failures
   actually were. What remains open from this item:
   - **ISO 15189:2022 itself has still never been read** (paywalled, §0.3). Every ISO
     verdict rests on secondary sources and is marked [VERIFIED (secondary)]. Buying a copy
     would close this properly, and is the single highest-value citation purchase here.
   - **ISO clauses 7.3.1, 5.2 and 5.3, and the true clause for biological reference
     intervals** remain [UNFETCHED-NOT-LOCATED]. Three EJ tokens (EJ-01, EJ-11, EJ-12,
     EJ-18) lean partly on clause numbers that are not confirmed.
   - **CAP checklists remain [UNFETCHED-PAYWALLED]** — they must be purchased, so the
     existence and current scope of a CAP in-silico interpretation survey is still open.
   - **Whether any Indian genomic variant-interpretation EQAS exists** is still open;
     §4.3 already carries it as an investigation item rather than a candidate.
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

**Study-level failure declaration: §5.9.** Added 2026-08-22, after the design was approved
as a plan, to close a gap the human found: the document defined when a *criterion* failed but
had no rule for when the *study* failed, and a study whose only outcomes are per-criterion
can always be reported as "passed, pending, needs re-scoping". §5.9 supplies three terminal
states (PASS / FAIL / **INVALID**), a threshold-independent failure anchor (F1, any E1
occurrence — the one route no EJ decision can tune away), an explicit rule that **Arm B
cannot rescue Arm A**, closure of the "pending" and "re-scoping" escapes, and the separation
of *GEPER failed* from *this study could not measure GEPER*. All of §5.9 is **[INVENTED]**.
**Known gap in it, named rather than left to be found: S2 has no F1-equivalent** — no
ratified zero-tolerance falsifier exists for variant-calling, and inventing one would be
setting an acceptance threshold. **That gap is still open.**

**The RANKING anchor, added 2026-08-22: §5.9.8 / F5.** Asked first whether rubric 1a already
subsumed the concern; **it does not, and the code says so rather than the design** — rank is
an eleven-factor weighted score with a multiplicative penalty in which classification is one
input (`prioritization_engine.py:153-174`), so a **correctly**-classified pathogenic variant
can be buried with **zero** F1 events. `:184-193` records the same defect having been found
and patched once from inside the codebase, and **the patch cannot fire in Arm A** because it
requires an expert-panel ClinVar record. **F5 sets no number** — it applies the rubric's
already-ratified P/LP↔B/LB boundary at its already-ratified zero tolerance to the ranked
output, and is independent of queue depth by construction. Its two parameters are EJ-24 and
EJ-25; **EJ-25 blocks on the review-capacity sub-component of operator scope.** **⚠ F5
supersedes §5.1.1's statement that F1 has no ranking equivalent** — that subsection is frozen
and was NOT edited; the conflict is flagged for ruling (§5.9.8).

**Two provisional intended-use statements: §5.1.1.** Added 2026-08-22 at the human's request
after they **declined to answer EJ-01**, recorded as a decision rather than a delay. Two
shapes — interpret-for-reporting, and prioritise-for-a-human-interpreter — drafted at equal
depth with **no recommendation**, each dependent decision traced by EJ number. **14 of 21
dependent decisions resolve differently between them, 7 identically**, so EJ-01 is confirmed
as the dependency it was believed to be. The costs are asymmetric in kind rather than in size:
Statement A carries heavier regulatory load; Statement B requires rebuilding §5.5's error
taxonomy on a ranking axis and leaves F1 without an equivalent. **The premise that a triage
tool attracts lighter regulatory obligation is [ANALOGY] and explicitly unverified** — it is
the assumption most likely to be imported unexamined, and it is flagged in place. **Statement
A has since been SUPERSEDED** — the human ratified the prioritisation shape and then amended
it to name the draft classification explicitly (§5.1.2) — and Statement A's branch is
retained, unrewritten, as the source of §5.1.3's revalidation roadmap.

**Unfilled expert-judgement decisions: 25** (19 at first approval; EJ-20/21/22 added with
§5.9; EJ-23 added with §5.1.1 and **since REMOVED as ANSWERED**, 24 → 23; **EJ-03 split into
EJ-03a/EJ-03b** — one decision separated into two, not new scope; **EJ-24/EJ-25 added with
F5**, §5.9.8, 23 → 25, which IS new scope)**.** Indexed at §0.1, greppable via
the §0.1 grep. Each names the guidance that would inform it and
who decides it. **None may be filled by anyone on this floor.**

**Citation verification (run 2026-08-22, after the design was approved as a plan) — tier
counts, including the zeros, because "nothing failed" is a result and is not the same as not
having looked:**

| Verdict | Count | Notes |
|---|---|---|
| **VERIFIED** (read directly) | **8** | CLIA §493.1253, §493.1445, §493.801/.803; Richards 2015; Rehm 2013; Jennings 2017; Biesecker & Harrison 2018; Krusche 2019; plus ICMR 2017 and ICMR GCLP 2021 and NABL 112 confirmed as documents — 13 citation-instances in all |
| **VERIFIED (secondary)** | **1 clause, cited at 6 tokens** | ISO 15189:2022 clause 7.3.3, and clause 7.4.1's title — confirmed only via accreditation bodies, never the standard |
| **UNFETCHED-PAYWALLED** | **2** | ISO 15189:2022 itself (iso.org HTTP 403 — sold, not published); CAP checklists (cap.org: "Purchase a Checklist") |
| **UNFETCHED-NOT-LOCATED** | **4** | ISO 7.3.1; ISO 5.2/5.3; ISO 7.4.1's "misleading impression of scope" attribution; the ACMG/AMP VUS-burden literature |
| **RE-MARKED [INVENTED]** | **4** | §4.3 + EJ-15 (ISO 7.3.7.2 is IQC, not EQA); EJ-16 (7.3.7.2/7.3.7.3 described as two stages of one requirement; they are IQC and EQA); EJ-05 (Richards 2015 does not address minimising VUS assignment); EJ-12 (ISO 7.3.4 is measurement uncertainty, not reference intervals) |

**No design element changed as a result.** In three of the four INVENTED cases the substance
survived and only the citation was wrong — most importantly §4.3's EQA argument, whose
alternative-approach limb verified as real at clause 7.3.7.3 f). The fourth (EJ-05) leaves
that token with **no verified external guidance behind it**, which is stated at the token
rather than absorbed.

**Two tokens are now on a materially weaker footing than their neighbours, and must not be
presented as equivalent:** **EJ-05** (no verified guidance at all) and **EJ-12** (no verified
ISO clause; carried by the ICMR citation and the in-repo audit).

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
