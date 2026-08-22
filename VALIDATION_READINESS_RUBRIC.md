# GEPER Clinical-Validation-Readiness Rubric

**Version:** 1.12.0
**Status:** RATIFIED. v1.0.0 (as v1.0.0-rc1) was approved by the human as-is
and committed at `89bf125`; this document is that baseline plus the v1.1.0
through v1.12.0 amendments below. **v1.10.0 is the human's own ruling on
two carded questions (D1 dual-engine; 2d->8a) and is cleared to commit once
both land -- everything else remains proposed, uncommitted, awaiting first
read.** See Amendment history for why each version is its own version
rather than folded into a neighbor.
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

## Amendment history

- **v1.0.0** (ratified, committed `89bf125`): initial 8-domain rubric,
  falsifiers on every sub-criterion, tier-split-never-summed scoring mechanic,
  Tier D kept.
- **v1.1.0** (**APPROVED BY THE HUMAN 2026-08-22, as written** -- reviewed in
  full, not in summary): amends sub-criterion **1b (Inter-run
  reproducibility)** and adds a new **"Scope constraints on specific claim
  types" subsection** under section 1, per `geper/REPRODUCIBILITY_PROTOCOL.md`
  @ `71d0e37` section 0. **Also folds in three flags Vic raised against the
  committed v1.0.0 text, all D1, all corrections to what a satisfier admits
  as valid evidence rather than new scope**: (1) **1a's satisfier wrongly
  named GIAB as an example ACMG-classification truth set** -- GIAB carries no
  pathogenicity classifications at all (it is a genotype/variant-calling
  truth set); corrected to cite the ClinVar expert-panel/practice-guideline
  subset instead, per `geper/GROUND_TRUTH_DATASET_AUDIT.md` @ `6f27e7b`. (2)
  **1b's satisfier asked for "confidence scores within a numeric tolerance,"
  but `confidence` (`acmg_rules.py:601`) is categorical** (High/Moderate/Low)
  -- split into an exact-match requirement on `confidence` and a stated
  numeric tolerance on the actually-numeric `significance_score` field,
  aligned with `REPRODUCIBILITY_PROTOCOL.md` section 1d's own
  jitter-vs-flip distinction. (3) **1c now has an explicit "structurally
  unavailable, documented alternative" path**, resolving this document's own
  open item 2 (section 5) -- PT/EQA schemes enrol laboratories, not
  software vendors, and ISO 15189:2022 is understood to recognise a
  documented-alternative limb for exactly that case (clause number
  unverified, flagged in 1c's own row).
  **Also corrects a WRONG claim in the COMMITTED v1.0.0 text (`89bf125`),
  stated visibly here rather than silently rewritten, per the same standard
  given to Meredith's audit addendum.** Sub-criterion **2c**'s v1.0.0 text
  read, verbatim: *"Current: tier B -- the fix (41935a2, committed) is real
  and I verified it directly, but no dedicated regression test exists yet;
  Pam is writing one. Scored at B, not A, until that test lands."* This was
  false at the moment `89bf125` was committed (2026-08-21 17:25:16):
  `geper/tests/test_plugin_network_error_handling.py`, covering exactly this
  property, had already landed in `470a993` -- **27 minutes earlier**
  (16:57:59) -- and `470a993` is this very rubric's own cited baseline tag,
  named in this document's own header. The promise ("Pam is writing one")
  had already been kept before the sentence reporting it as still-pending
  was committed. Found during a self-audit sweep of this floor's own
  committed artifacts, confirmed by running the test directly (12 passed)
  and comparing commit timestamps, not assumed. 2c is corrected to Tier A in
  its own row above. **This defect class is named explicitly because
  nothing in this rubric's own falsifier machinery would have caught it on
  its own**: a falsifier tests the codebase, not whether an annotation
  describing the codebase is still current. Also folds in **2d**'s "Current"
  annotation update (a second cannot-fail instance found by Andy in the
  infra lane, `test_gap7_pytest_anyio_mark_recognised` -- independently
  verified before folding in) and a new, repo-wide, informational note on
  Tier A added to section 1: **no CI exists anywhere in this repo** (no
  `.github/workflows`, confirmed by Andy), so every current Tier-A claim in
  this document rests on someone choosing to re-run the test by hand, not on
  continuous enforcement -- stated once, centrally, since it is uniform
  across every Tier-A row rather than a per-criterion fact. No domain,
  weight, or tier-count change from any of the six amendments folded into
  this version. See the covering messages for the full reasoning and why
  this is still one MINOR bump, not a patch, a re-baseline, or six separate
  revisions.
- **v1.2.0** (**APPROVED BY THE HUMAN 2026-08-22, as written**, sequenced after
  v1.1.0 rather than folded into it -- and the sequencing is what let it be
  approved on its own terms, exactly as the reasoning below predicted): adds a new
  **"Configuration-knob legitimacy: threshold the sanitised derivative,
  never the raw code" subsection**, sibling to "Scope constraints" under
  section 1, per a human ruling on Meredith's config-knob sweep. States the
  rule and its worked pair (ClinGen Dosage Sensitivity's raw, non-ordinal
  "Score" column, wrongly thresholded until `c3e23ab`; ClinVar
  `MIN_STAR_RATING`, a legitimate threshold on `star_rating()`'s sanitised
  derivative, not the raw `review_status` string; AlphaFold pLDDT as the
  inverse error, categorical-sounding names over a genuinely continuous
  scale). No domain, weight, tier-count, or existing-row change -- a new
  general methodology, not new scored scope, same shape as "Scope
  constraints" itself.
  **Why v1.2.0 and not a seventh fold into v1.1.0, my own call, reasoning
  stated rather than defaulted**: every change folded into v1.1.0 above is
  a CORRECTION to something already in v1.0.0 or already drafted into
  v1.1.0 -- fixing what's already there. This is different in kind: wholly
  new content (a stated rule plus a worked pair), not a fix to an existing
  row or subsection. Bundling new material into a version whose own story
  is "here is what v1.0.0 got wrong, now fixed" would blur what a reader
  is actually being asked to review in each pass. Keeping it a separate,
  self-contained version also means it can be accepted or pushed back on
  independently of v1.1.0's six corrections, which a fold-in would have
  prevented. The human's instruction to add this now overrides the
  earlier "stop growing the pending amendment" guidance on WHETHER to do
  the work now rather than waiting for the paused sweep to resume -- it
  does not, on its own, decide WHICH version number the result carries;
  that mechanic stays mine to own, per the standing rubric-ownership
  ruling.
- **v1.3.0** (this revision, proposed, uncommitted): C-8 and C-9, the two
  WORDING-only fixes from my own EJ-01 survey
  (`ej01-rubric-vs-intended-use-survey.md`, sections 1.1-1.2) against the
  newly-ratified, provisional intended-use statement. **C-8**: a new
  clarifying sentence at D1's head, plus "classification" prefixed with
  "proposed" at the 4 places in 1a/1b/1c where the sentence names GEPER's
  *own* output specifically -- not the 6 remaining occurrences in the same
  rows, which name the truth set's own classification, a domain descriptor,
  or another document's own term-of-art (`REPRODUCIBILITY_PROTOCOL.md`
  section 1d's "classification-flip"), none of which the ratified statement
  makes "proposed": only GEPER's own verdict is pending human review, not
  the ground truth it is compared against. Narrower than the "~8
  occurrences" estimate in the survey, which was written before a
  by-occurrence count; the correct number is what the ratified statement's
  own reasoning actually implies, not the earlier estimate. **C-9**: the D2
  intro reword, as drafted. **No falsifier changed in either fix** -- this
  is the reason both were approvable as wording rather than as a premise
  change; what D1 and D2 measure is unchanged, only how GEPER's own output
  is named while describing it. Sequenced as its own version rather than
  folded into v1.1.0 or v1.2.0 because neither of those was open for edits
  by the time this was authorised, and because this is a correction to
  wording that has been present, unchanged, since the committed v1.0.0
  baseline (`89bf125`) -- not a correction to either pending amendment.
- **v1.4.0** (this revision, proposed, uncommitted): adds new sub-criterion
  **3d (Mandatory-review enforcement, not just label consistency)** to D3,
  approved verbatim (satisfier and falsifier unchanged from the survey
  draft), adopted **knowing it scores RED on arrival** against the state
  Meredith's sign-off-scope work established -- the red-on-arrival state is
  evidence the criterion discriminates, not a reason to withhold it; a
  criterion that could only be added once it already passed would be exactly
  the cannot-fail shape this rubric exists to prevent, one level up. Carries
  a required "RED BY DESIGN ON ARRIVAL, 2026-08-21" annotation and an
  explicit guardrail (in the row itself, where a remediator will meet it)
  against closing it by making draft and reviewed outputs superficially
  distinguishable rather than through Meredith's actual sign-off-scope
  decision -- a cannot-fail FIX for a cannot-fail CHECK, the same defect
  shape one layer further downstream. **Also reweights D3's four
  sub-criteria to 3/3/3/3** (from 3a/3b/3c at 4/4/4) to admit 3d without
  changing D3's own domain weight (12) or this rubric's 100-point total --
  my own call, reasoning stated in D3's own header note rather than only
  here, since a reader of that table needs it without cross-referencing this
  history. No domain-weight, tier-count, or other row's satisfier/falsifier
  changed. Sequenced as its own version, after v1.3.0, because it is wholly
  new scored scope (a new falsifiable criterion) rather than a correction to
  existing text -- same "correction vs. wholly-new-content" distinction that
  separated v1.2.0 from v1.1.0, applied consistently.
- **v1.5.0** (this revision, proposed, uncommitted): corrects a defect in
  this document's own v1.2.0 text, found by me during that amendment's own
  cross-document sweep and reported to god at the time as unresolved,
  pending unblock -- now fixed. v1.2.0's ClinGen worked example conflated
  GEPER's two independent, deliberately-decoupled ClinGen dosage
  integrations: it attributed `CONFIG.clingen.DOSAGE_SUFFICIENT_EVIDENCE_SCORES`
  (a `geper/config.py` dataclass field) and the "made non-configurable"
  README narrative to `kim_pipeline`'s `c3e23ab` fix, when `c3e23ab` touches
  only `kim_pipeline/pipeline/clingen/lookup.py` and never used `CONFIG` at
  all -- that file deliberately avoids importing it (`lookup.py:14`'s own
  comment). The `CONFIG`-based fix and its README narrative belong to
  `geper`'s own, separate integration (`57e561c` + `560d2df`), which never
  touches `kim_pipeline`. Split into two worked examples per god's input
  (my own call to accept it): two independent, cross-referencing fixes of
  the same defect shape in GEPER's two decoupled subprojects is a stronger
  illustration than one conflated example, and is also simply what actually
  happened -- verified directly against `c3e23ab`, `57e561c`, and
  `560d2df`'s own commit messages and the current source of both files
  (`kim_pipeline/pipeline/clingen/lookup.py:108`, `geper/config.py:982`)
  before rewriting, not reconstructed from the earlier (wrong) text. No
  falsifier, weight, tier, or domain affected -- this subsection is
  methodology, not scored scope, same as v1.2.0 itself. Sequenced as its
  own version rather than silently rewritten into v1.2.0's own diff,
  per the same "corrections stay visible, not silently folded backward
  into an already-described version" standard applied to 2c in v1.1.0.
- **v1.6.0** (this revision, proposed, uncommitted): the human accepted the
  v1.4.0 D3 3/3/3/3 reallocation as-is (no reversal) and separately ruled
  that its cross-version scoring effect needed surfacing directly on the
  rubric, not only in this history -- reasoning: **the person who needs a
  comparability caveat is someone glancing at two domain numbers and
  concluding something moved, not someone already reading a changelog.**
  Adds: (1) a bolded "CROSS-VERSION COMPARABILITY CAVEAT" in D3's own
  section, immediately before its table -- where a D3 number is actually
  read -- stating plainly that a pre-v1.4.0 D3 score (three rows, 4/4/4) is
  not directly comparable to a v1.4.0+ one (four rows, 3/3/3/3), and stating
  the **specific direction** this instance moves rather than leaving it as
  a generic disclaimer: a post-v1.4.0 D3 score will tend to look *worse*
  for the same, unregressed pipeline state, because 3a/3b/3c's ceiling
  dropped from 4 to 3 each while 3d's 3 points start unreachable (unmet
  until Meredith's A-2 sign-off-scope work closes it -- see 3d's own row);
  9/12 is achievable today with 3a/3b/3c fully MET and 3d untouched, where
  12/12 was achievable pre-v1.4.0 for the identical state of those three
  criteria. (2) A shorter, general-purpose note in section 2's own citation
  guidance, next to the correct-citation-format example, stating that a
  domain's sub-criterion count or within-domain weights can move between
  rubric versions even when the domain's total weight does not, and that
  citing two scores under the same domain name and the same weight total
  is not, by itself, license to compare them -- each affected domain's own
  amendment history has to be checked. No falsifier, weight, tier, or
  domain-total change anywhere in this amendment -- purely a
  legibility/placement fix for a scoring consequence that already existed
  as of v1.4.0's own reasoning, not a new decision.
- **v1.7.0** (this revision, proposed, uncommitted): two items from the same
  dispatch, both verified against primary source before writing (source
  pointers given by god after an earlier, correctly-ambiguous "B1" reference
  I flagged rather than guessed at -- see memory for that exchange), bundled
  into one version rather than split, because neither is a correction to
  the other or to prior text, both are additions from the same verification
  pass, and neither changes what any other row means: **(1) adds
  sub-criterion 1e**, ratifying Vic's F5 anchor
  (`VALIDATION_STUDY_DESIGN.md` section 5.9.8) -- full reasoning, the
  subsumption test against 1a that failed, and the weight-reallocation
  logic (1a 15->10, 1e new at 5, D1 total unchanged at 30) are stated in
  D1's own header rather than only here. This resolves my own EJ-01 survey's
  open item 2.1, which I explicitly declined to resolve myself at the time.
  **(2) Updates 3d's guardrail** with what Meredith's `A2-signoff-scope-
  options-with-costs.md` establishes: the human's adopted options (2 and 3)
  do not close 3d by Meredith's own summary matrix, because 3d's falsifier
  is really about her finding **B1** (draft/reviewed content-identical
  except one banner string -- disambiguated explicitly from
  `ROUND_CANDIDATES.md`'s unrelated round-14 "B1", the exact collision that
  made me stop and ask for sources rather than guess), which **no costed
  option touches**, and which Meredith's own report states **may not be
  closeable by any `signoff.py` change at all** given GEPER's lack of an
  access-mediation layer. The human's ruling, carried into the row verbatim
  in substance: this is a true fact about the product, and the criterion
  should keep saying so rather than being re-aimed to something achievable.
  **3d's satisfier, falsifier, weight, and tier are all unchanged** -- this
  is new information about why it stays red, not a redefinition of what
  "red" means here. No domain, weight, or tier-count change anywhere in
  this amendment beyond D1's stated 1a/1e reallocation.
- **v1.8.0** (this revision, proposed, uncommitted): makes D2's scope
  explicit -- **"scoped to `geper/` only"**, the same treatment 3d and 1e
  already have -- discovered while resuming the baseline-table audit and
  ruled by the human via god: take the narrow (precedent-consistent) option
  rather than widen D2, since making the existing, already-`geper/`-only
  scoring say so is a legibility fix with zero effect on any current
  tier/weight/falsifier outcome, while widening D2 to cover `kim_pipeline`
  would be a real scope decision left on the table for the human. Records a
  named open item outside D2's new explicit scope: `kim_pipeline`'s
  `GnomadConstraintLookup.is_lof_intolerant()` collapses a ClinGen-fallback
  exception into bare `False`, the exact shape 2b's own falsifier names --
  **not a new defect** (carded as
  `clingen-lof-fallback-collapses-lookup-failure-into-confirmed-tolerant`,
  human-ruled HOLD as a Phase-3 priority, independently rediscovered by me
  via the baseline-table audit and confirmed already known/carded rather
  than reported a third time). What v1.8.0 actually adds is narrower than
  the defect itself: **the fact that this rubric's own D2 domain heading
  read as product-wide while only ever scoring `geper/`**, which could have
  let a reader mistake 2b's "MET at A for gnomAD" for coverage this exact
  failure shape already has elsewhere. It does not, and now says so.
  **Also adds a dated pointer (not an edit) to `VALIDATION_BASELINE_2026-08-21.md`'s
  Finding #1**, under the same standing rule: the baseline record is
  correctly frozen to its tag and Finding #1 (`871f23d` had zero
  verification in-window) is still an accurate statement about that window
  -- but the gap it names has since closed on the moving branch
  (`3ee154f`, ~2h after the tag, verified directly: 45 passed, genuinely
  discriminating coverage, not assumed from the commit title). The frozen
  finding text is untouched; a dated note was added beneath it so a reader
  is not left to independently discover, or wrongly assume still-open, what
  the moving branch has since done. No falsifier, weight, tier, or domain
  change anywhere in this amendment beyond D2's stated scope note.
- **v1.9.0** (this revision, proposed, uncommitted): the rubric-wide sweep
  god ordered ahead of the 48-commit remainder, once D2's fix (v1.8.0)
  turned out to be the second instance of "criterion scope narrower than
  its title implies" (after 3d/1e's B1 finding) rather than a one-off.
  **His reasoning for sequencing this first, carried here since it explains
  why this version exists at all**: the two prior instances came from
  targeted checks, not an exhaustive one, implying more rather than
  confirming a complete list; the rubric is an instrument every future
  score inherits, so wrong scope here misleads every time it's used, unlike
  a wrong entry in a historical record; and the pass itself is bounded (one
  property, every row) where the 48-commit remainder is open-ended --
  finishing the bounded thing first unblocks reading the instrument
  correctly sooner.
  **Scope of the pass, exactly as instructed**: every row, every domain;
  for each, which tree(s) it actually scores and whether the row says so;
  legibility only, no tier/weight/satisfier/falsifier change anywhere;
  check each neighbor rather than assuming a shared defect; distinguish the
  two disclosure directions rather than using one blanket tag.
  **Found a third, genuine instance while sweeping D1: `kim_pipeline` runs
  its own independent, live ACMG/AMP 2015 classifier**
  (`kim_pipeline/pipeline/acmg/classifier.py`, its own aggregator naming its
  output "the clinically-meaningful result") **that 1a/1c have never scored
  in either direction** -- not a scope-label fix, a real widening question,
  carded rather than resolved (D1's own header states this explicitly, not
  smoothed into a one-line tag).
  **Found the disclosure direction is not uniform even within one
  domain**: D4's 4a is `geper/`-only (no counterpart exists), while 4b is
  inherently cross-tree by what it measures -- the QC tool output it checks
  against (samtools/fastqc) is generated in `kim_pipeline`, not `geper/`.
  **Found the mirror-image failure god predicted after D2's fix taught a
  convention**: D6's 6a and 6b cited `kim_pipeline`-sourced evidence
  (DNABERT-2's pin, OMIM's retirement) without naming the tree -- silently
  *borrowing* the other tree's evidence rather than silently *excluding*
  it, the opposite direction from 5a. 6c also gained a coverage note: its
  own falsifier already reads product-wide, and it is -- `kim_pipeline` has
  its own separate pip-install call sites, independently unverified against
  this falsifier, named rather than silently folded into the existing
  (unchanged) UNMET score.
  **Confirmed clean, not just left alone**: 1b, 5b, 5c, 8a were each
  checked and found to already state their cross-tree/tree-agnostic
  standing correctly -- recorded as checked-and-clean in the relevant
  domain sections so a future reader does not have to re-derive that they
  were considered, per this document's own standing rule that an absent
  caveat and a checked-and-clean caveat must never look identical on the
  page.

  **AMENDED MID-PASS: the human approved this sweep and added two
  requirements, applied retroactively to everything drafted above before
  this line was written.** His own reasoning for the approval: "the
  instrument's defect propagates into every score read off it, and partial
  disclosure making 6a worse than before is the argument against doing this
  incrementally." Two consequences, both retrofitted into this same
  version rather than layered as a separate one, since nothing above had
  been reviewed yet:

  1. **Every row states its tree, including the already-correct ones.**
     God's own scoping had marked only the rows that were wrong; the human
     widened it, naming the same defect one level up: "or we've just
     recreated the convention problem with a different default" -- marking
     only exceptions teaches a reader that unmarked means one thing, then
     marking only the *other* kind of exception would teach them unmarked
     means something else, and a reader still can never tell a checked row
     from an unexamined one. **After this version, no row is silent about
     scope** -- all 23 sub-criterion rows now open their Satisfier cell
     with an explicit **Tree:** statement, including 1b, 5b, 5c, and 8a,
     whose correct, already-product-wide (or tree-agnostic) standing is now
     a stated positive finding, not an unmarked default.
     **This exact sentence was false for one row when first drafted, found
     by god, and is worth recording precisely because of what it
     demonstrates rather than smoothing over now that it's fixed:** 3d
     carried its correct `geper/`-only scope mid-cell, not as an opening
     tag, and my own verification of "23 rows, 23 tags" was a count that
     could not detect the difference between 22 real tags plus a missing
     one, and 22 real tags plus this very changelog sentence's own prose
     mention of "**Tree:**" supplying the 23rd match. The fix for "the
     instrument makes claims its rows don't support" briefly introduced
     that exact defect into itself. Corrected by adding 3d's own opening
     tag (its existing mid-cell note is unchanged) and re-verified properly
     this time -- by listing which rows lack an opening tag, not by
     counting -- confirming all 23, including 3d, now comply.
  2. **The two rows that asserted something false get a dated correction,
     not a silent restatement.** 6a and 6b did not merely omit their
     scope -- 6a's pre-existing text, read the way this sweep's own new
     convention teaches a reader to read it, asserted a `geper/`-side
     DNABERT-2 pin that does not exist. The human's requirement, verbatim:
     "the record should show it was found rather than always having been
     right." Both rows now carry, in place, a dated ("2026-08-22") banner
     quoting their own original `v1.0.0`-through-`v1.8.0` text verbatim
     before explaining the correction -- the same "quote the original wrong
     text rather than silently rewrite it" standard 2c already set in
     v1.1.0, applied here to a false attribution rather than a stale
     status claim. **Neither row's MET status or tier was ever wrong** --
     only which tree the evidence came from was misstated (6a) or left
     needing a reader to guess wrong (6b).

  This distinguishes three remedies, not two, where the original pass drew
  only one line: **understated scope** (1a, 1c, 1e, 3a-3d, 4a, 5a, 7a, 7b,
  D2's 2a-2c -- the row was silent, not wrong, so it gets *stated*);
  **false assertion** (6a, 6b -- the row said something untrue, so it gets
  *corrected, dated*); and **genuinely product-wide or tree-agnostic**
  (1b, 4b, 5b, 5c, 6c, 8a -- a positive finding, so it gets *stated as
  one*, not left unmarked as if unexamined).

  **No tier, weight, satisfier, or falsifier changed anywhere in this
  amendment, including the retrofit** -- every current score, met or
  unmet, reads identically before and after; only what a reader now knows
  about what was actually being measured, and how confident to be that
  every row was actually checked, has changed.
- **v1.10.0** (this revision, RULED BY THE HUMAN, cleared to commit once
  both parts land): the two questions carded out of the v1.9.0 sweep
  (D1 dual-engine; 2d's two findings) came back with rulings and specific
  analyses requested first -- delivered as two separate analysis-only
  messages, then ruled. **This version is a CONTENT change, the opposite
  boundary from v1.9.0's legibility-only sweep.**
  **(1) D1 becomes dual-engine.** 1a and 1c now require evidence from
  `geper/`'s ACMG engine AND `kim_pipeline`'s independent ACMG engine
  (`pipeline/acmg/classifier.py`) separately -- neither substitutes for the
  other. The human's own reasoning, why this tipped from "carded" to
  "ruled": *"Ruling it out-of-scope would leave a clinician-facing ACMG
  classification produced by an engine no readiness criterion measures --
  and D1 exists precisely to keep that line visible."* **The resulting
  score drop is recorded as a finding, not smoothed into the ruling's
  prose**, per the human's own words, carried verbatim: *"the previous
  score measured less than it claimed, and a number that drops because the
  instrument got honest is the right kind of drop."* Guardrail carried into
  both rows, same one already applied to 3d: if an engine cannot satisfy a
  criterion on available data, that is a stated gap, not a lowered bar --
  do not re-aim either row so a tree that lacks evidence can pass on easier
  terms. Explicitly scoped to `kim_pipeline`'s ACMG classifier only, not
  its separate PGx stage (no P/LP/VUS/LB/B boundary, no usable ground truth
  at all per the human's own example) -- a harder, adjacent case this
  widening does not fold in. D1's weight is unchanged (30, per v1.7.0's
  1a/1e split); the evidence bar under 1a/1c doubled instead, and a new
  cross-version comparability sentence for this specific change was added
  next to the v1.7.0 one already there.
  **(2) 2d's two `kim_pipeline` findings move to 8a**, dated, same defect
  shape as 6a/6b (evidence attributed to the wrong criterion, not a wrong
  finding). Ruled organisational, as my own independently-derived analysis
  found before this ruling landed: *"the tier B rests on hand-run, not a
  rerunning artifact, which is true either way, so the score does not
  move."* 2d keeps a dated note explaining the two findings were surfaced
  by its own sweep but back 8a's full-suite (both-tree) scope, not 2a-2c's
  `geper/`-only tests; 8a now hosts both findings with their own dated
  receipt note. Neither row's tier changed.
  **The human's own instruction once both land: commit the rubric.** god
  commits and pushes; this agent still commits nothing itself, per the
  standing rule.
- **v1.11.0** (this revision, dated 2026-08-23): corrects sub-criterion
  **6c**'s falsifier, which made a factual claim about "today" that had
  gone stale -- found and dispatched by god, ruled here. `3b0ede6` (Andy)
  added `_auto_install_disabled_for_tests()` to
  `geper/utils/auto_install.py`: `ensure_pip_package_available` now
  returns `False` under pytest instead of shelling a real `pip install`,
  closing the **test-run half** of 6c's falsifier in `geper/` (measured:
  CI run `32602931028` @ `3b0ede6` completed with zero live pip
  installs). **Not a tier move, for three reasons, all now stated on the
  row itself:** the **pipeline-run half** is untouched -- a real,
  non-test run still auto-installs -- so the falsifier remains met and
  the UNMET score is unchanged; 6c's satisfier asks for sandboxing OR
  atomicity-against-interruption, and `3b0ede6` delivers neither (it
  makes the unsafe path unreachable under exactly one condition, running
  under pytest, which is a third, narrower thing than either disjunct);
  and `kim_pipeline`'s six call sites remain unverified either way,
  unchanged from the v1.9.0 scope note.
  **Chose text correction over the other two options the dispatch
  offered (tier change; splitting 6c into test-run/pipeline-run
  halves).** A tier change is ruled out directly by the three reasons
  above -- the satisfier is still unmet, full stop. A split was
  considered and declined: sandboxing-or-atomicity is one property of
  the auto-install mechanism, not two independently-scorable ones: which
  run-type currently happens to supply the falsifying evidence is a fact
  about the evidence available today, not a second axis this row's own
  satisfier asks about separately, and it can move again in either
  direction without the underlying property changing. Splitting would
  produce two rows that would both still read UNMET, diluting weight for
  no scoring benefit. Same remedy category as 6a/6b (v1.9.0): a false
  assertion gets corrected in place, dated, with the original quoted
  verbatim rather than silently rewritten -- applied here to a claim
  that was true when written and expired without anyone updating it,
  the same "claim with a shelf life" shape 2c's v1.1.0 correction and
  6a/6b both already established, now confirmed as a recurring failure
  mode of this rubric's own "as of today" phrasing rather than a one-off.
  **No weight, tier, or satisfier changed; only the falsifier cell's
  stale half and a clarifying sentence in the satisfier cell.**
  Committed, not pushed, per this dispatch's explicit authorization for
  this one edit -- the standing rule (god commits and pushes) resumes
  after.
- **v1.12.0** (this revision, dated 2026-08-23): two methodology
  statements, both the human's own ruling that reasoning from the v1.9.0
  and v1.11.0 amendments generalises past the row it was written for.
  Neither corrects an existing score; both are new subsections under
  section 1, siblings to "Scope constraints on specific claim types" and
  "Configuration-knob legitimacy."
  **(1) Scoring-axis stability.** 6c's split-declined reasoning --
  splitting a row on which fact currently happens to be observable, not
  on a structural difference in what is being measured, would make the
  row's own shape track the observer rather than the property -- is
  ruled load-bearing beyond 6c, stated as: *"A row whose scoring axis
  moves when the evidence moves would be measuring the observer."* 6c
  stays the worked example; D1's 1a/1e split and 3d's split are named as
  the contrast case (legitimate splits, because each names a structurally
  distinct question, not an evidence-source snapshot).
  **(2) Unanchored temporal claims, prohibited as a form.** v1.11.0's own
  text called the "as of today" failure "a recurring failure mode... not
  a one-off"; this decides what follows from that. **Chose prohibition
  over continued detection.** The evidence: this class was diagnosed by
  name in v1.1.0 (2c) and the document produced two further instances
  after the diagnosis (6a/6b in v1.9.0, 6c in v1.11.0) -- naming the
  class did not defend against it, which is the same conclusion reached
  independently the same night on two other parts of this floor
  (`VALIDATION_STUDY_DESIGN.md`'s EJ-13 pooled-metric form-prohibition;
  the board's own "naming a class does not defend against it" finding,
  which cites this rubric's v1.1.0 diagnosis as one of its two proof
  cases). **The cost, stated rather than hidden:** an anchored claim
  reads harder at a glance and does not stop failing -- it fails as a
  stale *pointer* instead of a stale *claim*, which is accepted as
  strictly better because a stale pointer is self-diagnosing (a reader
  can check the cited commit) where a stale unanchored claim gives no
  signal at all. **Why this document specifically has no legitimate
  claims the rule would wrongly burden:** every Satisfier/Falsifier here
  is already a claim about current codebase state by Tier A/B/C's own
  definitions -- there is no timeless-sentence class here for the rule to
  tax for nothing. Practical form: any temporal qualifier about codebase
  state needs a commit/tag/CI-run/date anchor in the same or adjacent
  sentence, or the claim reads as Tier D regardless of phrasing.
  **Not applied retroactively as a sweep** -- existing rows are not
  combed for unanchored language under this version; the rule binds any
  row touched from here forward. A retroactive sweep, if wanted, is
  separate work.
  Committed, not pushed, same one-off authorization as v1.11.0; the
  standing rule resumes after.

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

**"Automated" means the check's own mechanism, not that it is wired to run
automatically -- stated once here, centrally, because it is uniform across
every current Tier-A claim in this rubric, not a per-criterion judgment
call.** Andy confirmed, 2026-08-21: **no `.github/workflows` directory
exists anywhere in this repo, for either `geper/` or `kim_pipeline/` --
nothing automated ever runs `pytest` for either tree.**

> *** DO NOT DISTRIBUTE THIS NOTE INTO THE ROWS IT APPLIES TO, AND DO NOT DROP
> IT WHEN SECTIONS MOVE. *** It is stated once, centrally, precisely BECAUSE it
> is uniform across every Tier-A row. That uniformity is what makes it look
> like boilerplate to a future editor restructuring this document -- and a fact
> that qualifies every row of a table is the kind that survives least well when
> the table is reorganised, because no single row owns it.
>
> **What breaks if it is lost:** every Tier-A claim in this rubric silently
> reverts to reading as "continuously enforced", which is what Tier A means
> everywhere else that phrase is used. The rubric would then overstate its own
> evidence grade across every domain at once, with no individual row having
> changed. **If this document is ever restructured, this note moves WITH the
> tier definitions or the restructuring is not finished.** Tier A's definition
above ("a persisted, automated check exists that would go RED... re-runnable
by someone other than the person who built the fix") is satisfied by the
check requiring no human judgment to interpret once run -- it does **not**
claim the check is actually re-run on every commit, because right now,
nothing is. Every sub-criterion below currently marked "MET at A" rests on
someone choosing to run that test by hand; this is honestly still Tier A by
this rubric's own definition (the check exists, is persisted, and would go
red), but a real score under this rubric should carry this as a single,
repo-wide fact rather than something a reader has to reconstruct from
silence, and an assessor should not read "Tier A" here as "CI-enforced."
Flagged as informational rather than as a new tier or a downgrade of any
existing MET-at-A row -- introducing CI enforcement, if the human wants it,
is implementation work outside this document's own definition-only scope.

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

### Scope constraints on specific claim types (cross-domain -- not domain-numbered, to avoid colliding with D1's own sub-criterion "1a")

Some claims this rubric could score are broader than any evidence that can
exist for them. When that is true, the claim itself must be **narrowed in the
rubric**, not just weakly evidenced -- otherwise the criterion cannot fail for
the wrong reason: not because the evidence is thin, but because the thing
being scored is unfalsifiable by construction, regardless of how strong the
evidence gets. This is the same disease D8 names for an individual test
(a check that cannot fail), one level up, in a **criterion's scope** rather
than its evidence.

**Reproducibility (first instance of this pattern, established by
`geper/REPRODUCIBILITY_PROTOCOL.md` @ `71d0e37`, section 0):** "GEPER is
reproducible" is not a claim this rubric can score -- it is not a claim the
underlying protocol can support at all. GEPER's default deployment mode
(start with whatever is installed, let auto-install fill gaps on demand) is,
by design, allowed to resolve differently run to run, and no evidence exists
or can exist for a general reproducibility claim about that mode. The only
claimable, scoreable form is: **"GEPER is reproducible when run
pre-provisioned and isolated."** Any reproducibility criterion in this rubric
(currently sub-criterion 1b) is scored **against that bounded claim only**.
Evidence from a default-mode or otherwise unbounded run does not satisfy it,
regardless of how favorable the result looks.

The bounding conditions carry an asymmetry that must be **stated wherever
this claim appears**, not once here and assumed carried forward by reference:

- **(a) Pre-provisioned** -- every package the run could need is already
  installed before it starts, so the auto-install code path is never entered.
  **Independently verifiable after the fact**, from a before/after
  resolved-package freeze: a mismatch puts that run out of scope for the
  claim by construction -- not noisier, not counted with a caveat, excluded.
- **(b) Isolated** -- the interpreter is not shared with any other process
  capable of installing or removing packages during the measurement window.
  **Not verifiable from a run's own output at all** -- procedural, must be
  true by arrangement, and the protocol cannot detect after the fact that it
  wasn't.

Any future criterion of this same shape -- a claim broader than any possible
evidence -- gets the same treatment: state the narrowed, actually-claimable
form explicitly, cite the source that established the narrowing, and repeat
the load-bearing asymmetry or precondition at every point the claim is
scored, not just once in this section.

### Configuration-knob legitimacy: threshold the sanitised derivative, never the raw code

**The rule (Meredith's, from the config-knob sweep):** a deployer-tunable
threshold on an evidence-weight configuration knob is legitimate only when
it thresholds a value that has already been mapped, deliberately, into a
genuinely ordinal or continuous scale -- never a raw, unsanitised coded
field, even one that superficially resembles a scale. Thresholding the
sanitised derivative is a real choice a deployer can correctly make.
Thresholding the raw code is not, because the raw field's own definition
does work a threshold cannot: some of its values are not ordinal at all,
just numbered.

**Stated here as a rule, not left in a sweep report, because the finding
collapses to the wrong policy without its own carve-out attached.** Read
without the exception below, "ClinVar has a coded review-status field, so
no threshold on anything derived from it is ever legitimate" is the naive
generalization a future reader would apply -- and it is false, as
`MIN_STAR_RATING` demonstrates two paragraphs down. A rule that produces
that generalization when applied without its exception is a worse
instrument than no rule at all: it would break a legitimate deployer-facing
config in the name of a defect that config does not have.

**Worked pair -- resemblance is the failure mode in BOTH directions, and
neither is detectable by looking at what the value resembles; only reading
the value's own definition settles it:**

- **A raw code that was thresholded and shouldn't have been** (cleared, for
  a time, by resembling its continuous *neighbours* -- inference from
  context) -- **split into two worked examples, not one, correcting this
  document's own earlier text (see Amendment history, v1.5.0): GEPER has
  two independent, deliberately-decoupled ClinGen dosage integrations
  (`kim_pipeline` and `geper` each has its own), and the same defect shape
  was found and fixed separately in both, which is a stronger illustration
  than either alone.** Both share the same root fact: ClinGen's Dosage
  Sensitivity "Score" column is not one ordinal range -- `0`-`3` are graded
  evidence levels, but `30` ("gene associated with autosomal recessive
  phenotype") and `40` ("dosage sensitivity unlikely") are special codes
  that argue *against* haploinsufficiency, not stronger versions of it.
  - **`kim_pipeline/pipeline/clingen/lookup.py`**: `is_lof_sufficient()`
    tested `score >= 3`, so `40 >= 3` read a gene ClinGen had affirmatively
    curated as dosage-*insensitive* as LoF-intolerant -- a false positive on
    pathogenicity. Fixed (`c3e23ab`, "ClinGen score 40 was read as strong
    evidence of the opposite of what it means") as an explicit whitelist,
    `DOSAGE_SUFFICIENT_EVIDENCE_SCORES: FrozenSet[int] = frozenset({3})`
    (verified at `lookup.py:108`, current source) -- a **bare module
    constant, never configurable at all**, not a `CONFIG` field: this file
    deliberately avoids importing geper's `CONFIG` (`lookup.py:14`'s own
    comment names this explicitly), so there was never a deployer-facing
    knob to remove here in the first place. The commit's own reasoning for
    a whitelist over a blacklist of `{30, 40}`: "a blacklist passes every
    test we could write today and breaks the first time ClinGen adds a
    code, preserving the shape of the bug while hiding its instance."
  - **`geper/config.py` + `geper/pipeline/interpretation.py`**: a
    *separate* integration, its own defect, same shape and same
    false-positive direction, fixed in two steps. `57e561c` ("ClinGen score
    30 scored +1.5 toward pathogenic for genes curated as recessive")
    replaced a `hi_score >= 3 and hi_score != 40` gate (score `30` fell
    through the `!= 40` exclusion) with explicit set membership -- but,
    unlike `kim_pipeline`, **kept the value deployer-configurable** via
    `GEPER_CLINGEN_DOSAGE_SUFFICIENT_SCORE(S)`, per that commit's own
    "CONFIGURABILITY, FLAGGED RATHER THAN REMOVED" note, reasoning (at the
    time) that removing the knob would contradict a stated
    all-thresholds-configurable requirement. `560d2df` ("The dosage-score
    threshold was never a threshold, so stop letting deployers set it") is
    the follow-up that removed that configurability entirely, correcting
    `geper/README.md`'s own justification for the knob directly: the README
    had argued the override was legitimate because it "match[ed] every
    other ACMG threshold in this codebase," an analogy the commit message
    calls out as false precisely because CADD/REVEL/pLI/LOEUF/oe_mis are
    continuous scores and this one is not -- "the override was not
    arbitrary; it was reasoned from an analogy that fails exactly where the
    defect lived." `CONFIG.clingen.DOSAGE_SUFFICIENT_EVIDENCE_SCORES` is
    verified at `geper/config.py:982`, current source, a plain
    non-overridable constant post-`560d2df`.
  - Both commit messages cross-reference each other directly (`57e561c`:
    "the same direction as c3e23ab in kim_pipeline, which is why it is
    covered by the same exception"; `560d2df`: "this also stops geper and
    kim_pipeline asserting opposite things about the same constant") --
    two independently-fixed instances of the same defect shape, not two
    unrelated fixes that happen to resemble each other; the fixes
    themselves record the parallel.
- **A legitimate ordinal derivative that could be mistaken for the same
  defect on sight:** ClinVar's `review_status` field is itself a coded,
  non-ordinal text field -- but `star_rating()`
  (`geper/pipeline/ps1_pm5/models.py`) maps it *first* into a genuinely
  ordinal 0-4 value, deliberately placing "criteria provided, conflicting
  classifications" at `0` (verified directly against `_REVIEW_STATUS_STARS`
  in the current source) rather than letting a naive ordering place it
  above an unreviewed submission. `MIN_STAR_RATING` thresholds this
  sanitised derivative, not the raw `review_status` string -- a deployer
  choosing a 2-star vs. 3-star confidence bar over it is making a real
  choice this rule protects, not the defect this rule exists to catch.
- **The inverse error, as its own worked example** (also Meredith's, same
  sweep -- categorical-*sounding* names over a genuinely continuous scale):
  AlphaFold's pLDDT confidence bands are named categorically (`VERY_HIGH` /
  `CONFIDENT` / `LOW`, `confidence_band()` in
  `geper/pipeline/alphafold/models.py`), which the same instinct that
  correctly flags ClinGen's Score column could misread as "another coded
  scale, don't threshold it." It is not: pLDDT is a genuinely continuous
  0-100 confidence score with no non-ordinal jump anywhere in its range
  (verified directly against `confidence_band()`'s own `>=` comparisons in
  the current source), and `PLDDT_VERY_HIGH_THRESHOLD` /
  `PLDDT_CONFIDENT_THRESHOLD` / `PLDDT_LOW_THRESHOLD` are legitimate
  deployer-tunable cut-points on it, defaulting to AlphaFold DB's own
  published >90 / 70-90 / <70 bands. Reading the value's own definition (a
  float with no special codes) rather than trusting its categorical-sounding
  names is what clears this one.

**Why the pair is sharper than either half alone:** the ClinGen defect was
cleared by resembling its continuous *neighbours* -- inference from
context. A pLDDT-shaped false positive would be manufactured by resembling
a categorical scale through its *names* -- inference from nomenclature.
Both are failures of pattern-matching on something adjacent to the value
rather than reading the value's own definition. A reader applying this rule
to a future config knob must actively hold both directions in mind, or the
"fix" for one becomes a new blanket rule exactly as wrong as the one it
replaces.

**Applies to:** any sub-criterion or future audit pass evaluating whether a
configurable evidence-weight threshold in this codebase (D2 in particular,
and any future config-knob sweep) is legitimate. Not written as a new
scored sub-criterion with its own weight -- a methodology this rubric's
existing and future criteria must apply, the same shape as the "Scope
constraints" subsection above, not new scope of its own.

### Scoring-axis stability: a row may not be restructured around where evidence currently happens to come from

**The rule, the human's own formulation, carried verbatim because nothing
sharper was found while writing this:** *"A row whose scoring axis moves
when the evidence moves would be measuring the observer."* A
sub-criterion's structure -- whether it is one row or several, and what
each row's satisfier/falsifier actually asks -- must be fixed by the
**property being measured**, never by **which specific fact currently
happens to be checkable**. If a proposed split, merge, or re-scoping would
need undoing the moment the observable evidence moved back, the
restructuring was never about the property; it was a snapshot of this
week's evidence wearing the shape of a permanent criterion.

**Worked example (6c, v1.11.0):** 6c's property is singular -- is
package auto-install sandboxed, or made safe against interruption? Before
`3b0ede6`, both the test-run and the pipeline-run path demonstrated the
falsifier being met; after `3b0ede6`, only the pipeline-run path does. The
UNMET score never moved (the satisfier -- sandboxing or atomicity -- was
never reached either way), but *which run-type currently supplies the
observable evidence* shifted. Splitting 6c into "test-run" and
"pipeline-run" halves at that moment would have created two rows whose
existence was justified by which fact happened to be checkable that week,
not by two independently meaningful properties a deployer or reviewer
would actually want scored apart. Declined for exactly this reason -- see
6c's own row and the v1.11.0 amendment entry for the full reasoning; this
subsection is where that reasoning is generalised, so a future editor
proposing a similar split does not have to re-derive it from one row's
history.

**The test for a future editor, stated so it is applicable without
re-reading 6c:** before splitting or restructuring any row on the grounds
that "right now, the evidence for this only covers case X, not case Y,"
ask whether the split survives the evidence for X and Y trading places
again. If it would need undoing, the split is keyed to the observer's
current view, not to the property, and the correct move is a **dated
scope note on the existing row** (as 6c already carries, and as the
"Scope constraints" subsection above already establishes for claims
broader than their evidence) -- not a new row.

**What this is not:** a rule against ever splitting a row. D1's 1a/1e
split (rank-order integrity carved out as its own falsifier, v1.7.0) and
3d's split (mandatory-review enforcement named apart from label
consistency, v1.4.0) are both legitimate splits under this test, because
each names a **structurally distinct question** a single satisfier could
not ask at once -- neither depends on which fact happened to be
observable when it was written, and neither would need undoing if the
evidence available today changed tomorrow.

**Applies to:** any future proposal to split, merge, or re-scope a
sub-criterion. Not a new scored sub-criterion with its own weight -- a
methodology this rubric's existing and future criteria must apply, same
standing as the two subsections above.

### Unanchored temporal claims are a prohibited form, not a monitored one

**Decision: prohibited, not merely watched for.** A Satisfier or Falsifier
cell may not assert a bare, unanchored temporal claim about codebase
state -- "currently", "as of today", "now", "newly surfaced", "already" --
unless the same sentence (or the one immediately adjacent) names the
specific commit, tag, or dated CI run the reader can check it against.
An unanchored temporal claim is not a phrasing preference: it is a claim
with a hidden, moving denominator (the instant it was written), and unlike
a stale citation it carries **no signal in its own text** that a later
reader should doubt it. It reads exactly the same the day it goes stale as
the day it was true.

**Why prohibition, not detection, and why this document is the evidence
for that choice rather than merely the beneficiary of someone else's:**
this exact class was named explicitly, in the sharpest terms available, in
v1.1.0's correction of 2c -- *"a falsifier tests the codebase, not whether
an annotation describing the codebase is still current."* The document
then produced **two further instances after that diagnosis was written
down**: 6a and 6b (v1.9.0, a `geper/`-side claim that had never been true)
and 6c (v1.11.0, a claim true when written that expired silently). Three
instances, one of them predating the diagnosis and two of them following
it, is direct evidence that naming the class did not defend this document
against it. The same conclusion was reached independently the same night
on two other parts of this floor -- `VALIDATION_STUDY_DESIGN.md`'s EJ-13
forbids a pooled SNV/indel accuracy figure as a **form**, regardless of
its value, rather than relying on a reader to notice when pooling
misleads; and the board's own audit tonight reached "naming a class does
not defend against it" as a general finding, citing this rubric's v1.1.0
diagnosis as one of its two proof cases. Three independent routes to the
same rule is stronger grounds than this document reasoning about itself
alone.

**The cost, stated rather than left implied, because a prohibition that
hides its price is the same failure this subsection exists to prevent:**
an anchored claim is harder to read at a glance -- a reader has to resolve
"as of `3b0ede6`" against the current tree rather than trust an adjective
-- and anchoring does not remove the failure mode, it changes its shape.
An anchored claim goes stale as a **pointer** (the cited commit ages, and
the sentence may no longer describe HEAD) rather than as a **claim** (the
sentence asserts something false with nothing in its own text to flag
it). That is accepted as a strictly better failure: a stale pointer is
self-diagnosing -- a reader can check whether the named commit is still
the relevant one, and the citation itself tells them what to check against
-- where a stale unanchored claim gives no way to tell from the sentence
alone.

**Why this document specifically has no legitimate claims the prohibition
would wrongly burden:** every Satisfier and Falsifier in this rubric is,
by Tier A/B/C's own definitions above, a claim about a **repeatable check
against the current state of an evolving codebase** -- there is no class
of timeless architectural sentence here (the kind a general-purpose
document is mostly made of) that an anchor requirement would tax for no
reason. The prohibition costs verbosity uniformly and protects against a
failure that has already happened three times.

**Practical form:** any Satisfier/Falsifier sentence using a temporal
qualifier about codebase state must name the commit hash, tag, CI run, or
date it was checked against, in the same or the immediately adjacent
sentence. A temporal claim with no anchor anywhere near it is to be read
as Tier D (Asserted) regardless of how confidently it is phrased, until
anchored.

**Applies to:** every sub-criterion below and any future one. Not a new
scored sub-criterion with its own weight -- same standing as the three
subsections above; existing rows are not swept to retrofit anchors under
this version (that is separate work, not this correction), but any row
touched from here forward is held to this form.

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

**The rubric version in that citation is not decorative -- a domain's own
sub-criterion count or within-domain weights can change between rubric
versions even when the domain's total weight does not** (D3 is the current
example: 3 rows at 4 each through v1.3.0, 4 rows at 3 each from v1.4.0 on,
same 12-point domain total either time -- see D3's own cross-version
comparability caveat). Two domain-level numbers citing different rubric
versions are not safe to compare on sight for that reason, even when both
say "D3" and both say "out of 12." Comparing scores across rubric versions
without checking each affected domain's own amendment history is a citation
error, not a harmless simplification.

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

D1 measures the accuracy of GEPER's proposed ACMG classification -- the input
to the mandatory human review the product's own intended-use statement
requires, not a claim that this classification is used clinically without
that review. **C-8, wording only, no falsifier change** -- inserted per the
EJ-01 survey against the ratified intended-use statement; "classification" is
prefixed with "proposed" below at each point where the sentence names
GEPER's own output specifically (not the truth set's, and not a term-of-art
borrowed from another document), for the same reason.

**Adds sub-criterion 1e in v1.7.0, ratifying Vic's F5 anchor
(`VALIDATION_STUDY_DESIGN.md` @ current tree, section 5.9.8) -- the answer to
my own EJ-01 open item 2.1, which I explicitly declined to resolve myself and
routed to study-design.** I had held, without confidence, that 1a's falsifier
might already subsume a ranking-shaped concern; Vic tested that subsumption
directly rather than assuming either answer, and it fails: 1a's falsifier
quantifies over `combine_result.classification`
(`geper/pipeline/acmg_rules.py:957`) only, and `prioritization_engine.py`'s
rank is a many-to-many function of eleven weighted factors (classification
is one input among eleven, `:153-163`) plus a multiplicative conflict penalty
(`:170-174`) -- so a variant GEPER classifies **correctly** as Pathogenic can
still rank below the review cutoff and never reach a human, producing
**zero** F1/1a-shaped events, because the one quantity 1a inspects is right
in exactly this failure. Confirmed not speculative: `prioritization_engine.py`
`:184-193`'s own comment records this exact failure mode having occurred and
been partially patched once already. **Named `1e`, not `1d`, deliberately --
`1d` would collide with `geper/REPRODUCIBILITY_PROTOCOL.md`'s own "section
1d," already cross-referenced in row 1b below**, the same collision-avoidance
reasoning already applied to this rubric's "Scope constraints" subsection
under section 1 (kept cross-domain rather than numbered "1d" for the same
reason).
**Weight: carved from 1a, not split evenly across 1a/1b/1c.** 1e is a
narrower special case of what 1a already tests -- the same ratified
P/LP<->B/LB boundary at the same zero tolerance, applied to a second output
surface -- not a peer to 1b/1c, which test unrelated properties
(reproducibility, external QA) that diluting for a classification-adjacent
addition would be the same arbitrary-tax problem already named for D3's 3d
reallocation. 1a moves 15 -> 10; 1e takes the remaining 5; 1b (10) and 1c (5)
are untouched. D1's domain total stays 30. Reversible if the human would
rather weight this differently.

**CROSS-VERSION COMPARABILITY, same shape as D3's caveat, applied here
prospectively:** because D1 currently sits at zero across every
sub-criterion (pre- and post-v1.7.0 alike), this reweight changes **nothing
about today's D1 number** -- 0 either way. It matters for the **next** real
measurement: a future D1 pass that fully satisfies 1a/1b/1c under the
pre-v1.7.0 weights would have read 30/30; the same underlying concordance
and reproducibility evidence, satisfying the same rows under v1.7.0+
weights, tops out at 25/30 until 1e is also satisfied -- not because
anything got worse, but because 1e is new, real scope that was previously
uncovered rather than previously met. State which rubric version produced
any future D1 score before comparing it to another.

**v1.10.0 adds a second, sharper instance of the same shape, worth its own
sentence rather than assumed folded into the paragraph above**: 1a/1c's
evidence bar doubled (single-engine to dual-engine) with their weights
unchanged. Again 0 either way today, so nothing about today's number moves
-- but any future D1 score citing 1a/1c must state whether it was measured
pre- or post-v1.10.0, since a single-engine result that would have fully
satisfied the pre-ruling text does not satisfy the post-ruling one. This is
not a regression in what geper/'s engine achieved; it is the same "the
instrument got honest" drop the ruling itself names.

**Scope, made explicit in v1.9.0's rubric-wide sweep: 1a and 1c currently
score `geper/`'s own ACMG classification only** (`combine_result.classification`,
`geper/pipeline/acmg_rules.py:957`, the same field 1e's own row already
cites) -- true today, previously unstated. **1b does not need this note**:
its own satisfier already names both trees explicitly (cross-engine
agreement as one valid reproducibility-evidence path), a design choice
already disclosed where a reader meets it, not a silent gap.
**This is not a pure legibility fact for 1a/1c, and is flagged rather than
quietly folded into a one-line scope tag: `kim_pipeline` runs its own
independent, live ACMG/AMP 2015 classifier**
(`kim_pipeline/pipeline/acmg/classifier.py`, wired into `main.py` and
`pipeline/orchestration/shared.py`; its own evidence aggregator states
outright that "the ACMG classification is the clinically-meaningful
result") **that D1 had never scored, in either direction.** This was not
narrower evidence for the same claim -- it was a second, independently
operating classification engine this domain did not address at all.

**RULED, v1.10.0, dual-engine: 1a and 1c now score both engines.** The
human's own reasoning, carried here since it should shape how every future
reader interprets a D1 score: *"Ruling it out-of-scope would leave a
clinician-facing ACMG classification produced by an engine no readiness
criterion measures -- and D1 exists precisely to keep that line visible.
The asymmetry you named is the argument."* This is a **CONTENT change, not
a legibility one** -- the opposite boundary from the v1.9.0 sweep that
found the gap. Both rows' satisfier/falsifier now require evidence from
`geper/`'s engine AND `kim_pipeline`'s engine independently; neither engine
substitutes for the other.

**The score is EXPECTED to drop, and that is recorded as a finding, dated,
not smoothed into the ruling's prose.** The human's own words, carried
verbatim because the framing is the point: *"Accept that this changes what
a D1 score means and likely lowers it. That is correct: the previous score
measured less than it claimed, and a number that drops because the
instrument got honest is the right kind of drop."* **2026-08-22: D1's
maximum achievable score is unchanged (still 30, reallocated as it was
under v1.7.0's 1a/1e split), but the EVIDENCE BAR under 1a and 1c has
doubled** -- a future assessor who previously needed to satisfy one
engine's concordance/EQA story now needs two, independently, for the same
weighted points. This is not a lowered bar producing a coincidentally lower
number; it is the same bar, honestly priced, after "GEPER's classification"
stopped meaning only the engine that happens to write the clinical report.

**Guardrail, the same one applied to 3d -- read before scoring either
engine: if an engine cannot satisfy a criterion on available data, that is
a STATED GAP, not a lowered bar.** Do not re-aim 1a or 1c so that a tree
that lacks evidence can pass on easier terms; record it as unmet, with the
reason, the same way 3d stays red rather than being re-aimed to something
achievable. The two engines are not expected to need identical evidence:
different predictors, different truth-set availability. **Named explicitly
so the widening isn't read as broader than it is: this covers `kim_pipeline`'s
ACMG classifier specifically** (`pipeline/acmg/classifier.py`, the P/LP/VUS/
LB/B engine) **-- not `kim_pipeline`'s separate PGx stage**
(`pipeline/pgx/`, star-allele/diplotype calls, a different clinical
question with no P/LP/VUS/LB/B boundary to compare against at all). Per the
human's own example of the asymmetry this guardrail exists to price
honestly: kim's PGx stage has no usable ground truth at all -- a harder,
adjacent case than its ACMG classifier's, and out of 1a/1c's scope
entirely rather than folded in as if the same widening covered it.

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 1a | Concordance against a known-truth variant set | 10 | A | **Tree: both, RULED dual-engine 2026-08-22 (v1.10.0) -- content change, not legibility. Evidence needed per engine, separately, since they are not expected to be the same:**<br><br>**`geper/`'s engine** (`combine_result.classification`, `geper/pipeline/acmg_rules.py:957`): a persisted, re-runnable comparison against a named **classification** truth set (e.g. the ClinVar expert-panel/practice-guideline subset -- per `geper/GROUND_TRUTH_DATASET_AUDIT.md` @ `6f27e7b`, the only dataset that document found offering graded, ACMG-relevant classification ground truth at scale, with its own flagged circularity caveat since GEPER also consumes ClinVar as live evidence for some criteria -- or an internal curated ground-truth set with documented provenance), with stated sensitivity/specificity/PPV and a pass threshold agreed in advance. **GIAB does not satisfy this criterion and must not be cited as an example here** -- a genotype truth set, no ACMG classification attached to any record. An earlier draft of this row cited GIAB as an example by mistake; corrected per Vic's flag, 2026-08-21.<br><br>**`kim_pipeline`'s engine** (`AcmgResult.classification`, `kim_pipeline/pipeline/acmg/classifier.py`): the SAME shape of concordance study, run independently against this engine's own output -- not satisfied by `geper/`'s study, and not satisfied by anything currently in `kim_pipeline`. **Zero infrastructure exists for this today**, checked directly: no ground-truth/concordance file anywhere in `kim_pipeline`. What DOES exist -- `kim_pipeline/tests/test_acmg.py`, 13 tests -- does not reduce this gap: those are hand-constructed unit tests of the classifier's scoring arithmetic (does PVS1+PS1 combine to Pathogenic), not a comparison against a named truth set's real variants; arithmetic correctness and real-world concordance are different claims. **Different predictors than `geper/`'s engine, stated so the two studies are not mistaken for interchangeable**: `kim_pipeline`'s `VariantEvidence` draws on a leaner, precomputed-score set (gnomAD AF, CADD, REVEL, SpliceAI, ClinVar significance, LoF/hotspot flags) with no AI-model inference pipeline of its own, versus `geper/`'s engine drawing on 10+ AI models (SpliceFormer, SpliceBERT, MMSplice, Enformer, Borzoi, ESM2, HyenaDNA, Evo2, RNA-FM, AlphaMissense) plus live ClinVar/gnomAD/ClinGen lookups -- a truth-set comparison against `kim_pipeline`'s engine tests a materially different prediction mechanism, not the same logic on different code. **Not in scope here**: `kim_pipeline`'s separate PGx stage (`pipeline/pgx/`, star-allele/diplotype calls) -- a different clinical question with no P/LP/VUS/LB/B boundary to compare against, and, per the human's own example, no usable ground truth at all; a harder, adjacent case this widening deliberately does not fold in. | **Either engine failing, independently, fails this criterion -- stated gap, not a lowered bar, per the guardrail above.** For `geper/`'s engine: any variant in the truth set where its classification and the truth set's expected classification disagree across a Pathogenic/Likely-Pathogenic vs Benign/Likely-Benign boundary is an automatic fail, no tolerance; VUS-boundary drift beyond a pre-agreed count also fails. For `kim_pipeline`'s engine: the same boundary, same zero tolerance, against its own truth-set comparison once one exists. **No truth-set comparison existing for EITHER engine is itself a falsifying observation for that engine** -- current state for both, as of this ruling: neither engine has one. |
| 1b | Inter-run reproducibility -- **SCOPED**: claimable only for a run that is both (a) pre-provisioned (every needed package already installed, so auto-install is never entered -- independently verifiable after the fact from a before/after package-freeze diff) and (b) isolated (interpreter not shared with any process that can install/remove packages during the measurement window -- not verifiable from the run's own output, must be true by arrangement). No claim is made or scoreable for GEPER's default/auto-install deployment mode. See the cross-domain "Scope constraints" subsection under section 1. | 10 | A | **Tree: both, by design** -- the primary evidence path is `geper/`-only (repeated runs of the same engine), with cross-engine agreement against `kim_pipeline`'s independent stack offered as one valid, stronger alternative form of the same evidence, not a second scored target. The same VCF input, run repeatedly under conditions (a) AND (b) above (or run through both `geper/` and `kim_pipeline`'s independent stacks for a variant both can classify, under the same conditions), produces an identical proposed classification, an identical `criteria_met`/`criteria_unknown` set, identical per-criterion `confidence` values (`acmg_rules.py:601` -- categorical, "High"/"Moderate"/"Low"; **exact match required, not a tolerance target**), and a `significance_score` within a stated numeric tolerance (the numeric field this criterion's tolerance actually applies to, not `confidence` -- an earlier draft named "confidence" here, which is the wrong field; corrected per Vic's flag, 2026-08-21) -- persisted as a test, with the before/after package-freeze diff for (a) captured as part of the test's own evidence. This categorical-vs-numeric split mirrors `geper/REPRODUCIBILITY_PROTOCOL.md` @ `71d0e37` section 1d's own numeric-jitter-vs-classification-flip distinction; the two documents should stay aligned on which fields are categorical and which are numeric. | **Either of:** (i) two runs meeting both (a) and (b) produce a different proposed classification, a different criteria set, a different per-criterion `confidence` value, or a `significance_score` delta outside the stated tolerance; **or** (ii) a reproducibility claim -- in this rubric's own scoring, a report, or a card -- is presented without stating its (a)/(b) bounding conditions, or is presented as applying to GEPER's default/unbounded deployment mode. (ii) fails this criterion regardless of what (i) would show: an unscoped claim is a Tier-D-shaped assertion no matter how favorable the underlying numbers are, and no amount of matching output rescues a claim that was never entitled to be general in the first place. |
| 1c | External QA / proficiency-testing panel | 5 | A (participation path) or C (documented-alternative path, a distinct sufficient path -- not a downgrade of the same evidence) | **Tree: both, RULED dual-engine 2026-08-22 (v1.10.0) -- content change, not legibility. Evidence needed per engine, separately -- PT/EQA schemes enrol laboratories around ONE tested workflow, so participation for one engine says nothing about the other; the two paths cannot be shared.**<br><br>**`geper/`'s engine:** **Either:** (A) documented participation in, or scored comparison against, an external PT/EQA panel (e.g. CAP, GenQA, or an equivalent scheme) with a result meeting the panel's own passing threshold; **or** (C) a written, dated ruling that external PT/EQA enrolment is currently **structurally unavailable, not merely not-yet-attempted** -- PT/EQA schemes enrol *laboratories*, and GEPER is software, not a laboratory, so there may be nothing to enrol *as* -- together with a documented alternative-verification plan on file. The second path resolves this rubric's own open item 2 (section 5, as of v1.0.0): ISO 15189:2022 is understood to provide a documented-alternative limb specifically for the case where EQA participation is unavailable. **Caveat, stated as plainly as the argument itself:** the specific ISO 15189:2022 clause number behind this is UNVERIFIED -- named from training knowledge, no primary source fetched this session (Vic's own flag, carried forward rather than smoothed over).<br><br>**`kim_pipeline`'s engine:** the same A/C shape, independently -- its own PT/EQA participation, or its own written structural-unavailability ruling with its own alternative-verification plan. **Checked directly: zero PT/EQA references anywhere in `kim_pipeline`** (grepped for proficiency/EQA/CAP/GenQA/ISO 15189 -- no real hits). Neither path currently exists for this engine. **Not in scope here**, same as 1a: `kim_pipeline`'s separate PGx stage, a different clinical question this widening does not fold in. | **Either engine failing, independently, fails this criterion -- stated gap, not a lowered bar.** For each engine: no external panel result exists **and** no written structural-unavailability ruling with an alternative-verification plan exists either -- **silence is still a fail**; only a documented ruling of the second kind converts "unmet" into "inapplicable," per-engine. Also fails if a panel result exists but falls below the panel's own passing threshold, or if an "alternative-verification plan" is claimed but not actually written down anywhere reachable -- an asserted-but-unwritten alternative is Tier D, not C, regardless of how sound the underlying reasoning is. **Current state for both engines, as of this ruling: unmet, silently, on both counts.** |
| 1e | Rank-order integrity for the ranking output (F5 -- threshold-independent, inherited boundary, not a new number) | 5 | A | **Tree: `geper/` only** (stated again here, at the row itself, not only below -- see the fuller reasoning later in this same cell). A persisted, re-runnable test over `geper/pipeline/prioritization_engine.py`'s `rank_batch` output for a ranked run confirms **no truth-P/LP variant is ordered below a variant that is both truth-B/LB and GEPER-classified B/LB** -- the same P/LP<->B/LB boundary 1a already scores, at the same zero tolerance, **inherited from 1a rather than independently chosen** (per `VALIDATION_STUDY_DESIGN.md` @ current tree, section 5.9.8: "had the anchor required a number I selected, it would be an EJ token and not an anchor"). A variant assigned no rank at all (`rank_batch`'s own `None` case, `prioritization_engine.py:574-575`/`:583`) counts as ranked **last** for this purpose, per section 5.9.8's ruling, not as excluded. **Deliberately narrow, and the narrowness is load-bearing, not a hedge:** the second clause (GEPER's own classification, not just the truth label, must agree B/LB) is what survives the strongest objection to this criterion -- that the engine ranks review *urgency*, not pathogenicity, so an uncertain variant may legitimately outrank an unambiguous one needing only a signature. Requiring GEPER's own classification to already say "benign" on the higher-ranked variant removes that reading entirely: no urgency story explains placing a variant *GEPER itself calls benign* ahead of one it calls pathogenic. **Explicitly does NOT test:** (i) tie-breaking across the boundary (a separate, undecided question, `VALIDATION_STUDY_DESIGN.md` EJ-24) -- not folded in here since deciding it would be picking a number this criterion is designed not to need; (ii) recall/queue-depth K, i.e. what fraction of truth-P/LP variants appear within a reviewed cutoff (a genuinely different, threshold-setting question, EJ-25, currently blocked on operator/review-capacity scope) -- conflating the two would be the exact pooling error the source document itself warns against. **Scoped to `geper/` only**, checked directly rather than assumed: `kim_pipeline` has no variant-prioritisation stage or ranked output at all (its only `priorit`/`rank` hits are a deterministic transcript-selection order and a deterministic allele-specificity order, neither a review queue). Current: **UNMET.** Grepped `geper/tests/test_prioritization_engine.py` directly before writing this row -- it covers only the `critical_conflict` floor mechanism (the earlier, partial patch for a related failure, `prioritization_engine.py:184-199`), not this criterion's own pairwise-ordering property across a full ranked, truth-labelled batch; no such test currently exists. | Any ranked run in which a truth-P/LP variant is ordered below a variant that is both truth-B/LB and GEPER-classified B/LB, **including** via an unranked (`None`-rank) truth-P/LP variant, which counts as ranked last for this purpose and therefore already below any ranked B/LB variant. |

### D2. ACMG Evidence Integrity -- weight 15

Does GEPER's ACMG rule engine -- which produces the proposed classification a
reviewing clinician relies on -- ever let a failure, an uncalibrated model, or
a lookup crash masquerade as real evidence. **C-9, wording only, no falsifier
change** -- reworded per the EJ-01 survey against the ratified intended-use
statement, which specifically avoids "the interpreter" for GEPER's own role.

**Scoped to `geper/` only, v1.8.0, made explicit for the first time -- same
treatment as 3d and 1e, and for the same reason: it was already true and
unstated.** Every satisfier/current-state note below (2a-2d) discusses only
`geper/`'s own lookups, models, and tests -- 2b's own text already cited
`orchestration/shared.py`, a `geper/` file, without saying the domain itself
was scoped that way. **This is a wording/legibility fix, not a widening or
narrowing of what is currently scored** -- nothing below was ever actually
evaluated against `kim_pipeline/`, so making the scope explicit changes zero
existing tier/weight/falsifier outcomes.
**Named open item outside this scope, cross-referenced rather than
re-reported, same shape as 3d's own B1:** `kim_pipeline`'s
`GnomadConstraintLookup.is_lof_intolerant()`
(`kim_pipeline/pipeline/constraint/lookup.py:302-327`) wraps its
ClinGen-fallback call in a bare `except Exception: return False`,
indistinguishable from a gene ClinGen genuinely curated as
dosage-tolerant -- the exact shape 2b's own falsifier names ("the exact
shape of the original gnomAD defect, in any lookup, old or new"). **This is
not a new defect and this rubric is not the one flagging it**: it is carded
(`clingen-lof-fallback-collapses-lookup-failure-into-confirmed-tolerant`)
and the human has **ruled HOLD on it as a Phase-3 priority** -- deliberately
deferred, not missed, documented in the code itself
(`kim_pipeline/tests/test_clingen_dosage_lookup.py`'s
`TestKnownHeldDefectClinGenFallbackExceptionCollapsesToTolerant`, which
exists to keep the defect visible without certifying it, per that class's
own docstring). **What is new is narrower and belongs to this rubric alone:
before this scoping note, D2 read as product-wide while actually only ever
having scored `geper/`, so a reader could have mistaken 2b's "MET at A for
gnomAD" for coverage this exact failure shape already has in the other
tree. It does not.** Whether D2 should be widened to cover both trees
(which would make this a live, currently-unmet instance of 2b rather than
an out-of-scope note) is the human's call, not this rubric's own -- surfaced
as available, not decided here.

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 2a | Uncalibrated-model disclosure reaches the evidence text | 4 | A | **Tree: `geper/` only** (D2's own domain-level scope note applies). Model name / calibration status / clinical-validation status is present adjacent to any criterion evidence derived from an uncalibrated model, verified by a test that calls the real production evidence-builder (not a hand-authored fixture). Current: MET at A (`test_bp7_spliceformer_disclosure.py`, calls the real `ACMGRuleEngine._bp7`). | Any criterion output derived from an uncalibrated model (SpliceFormer, SpliceBERT, or any future AI-model plugin) appears in a rendered report without the disclosure substrings, or with the disclosure present but not colocated with the evidence it describes. |
| 2b | Lookup-failure sentinel integrity | 4 | A | **Tree: `geper/` only** -- see D2's own header for the named `kim_pipeline` open item (`GnomadConstraintLookup`, held card) this row does not cover. For every external lookup feeding an ACMG criterion, an exception or unavailable path returns a state distinguishable from, and never defaulting to, a confirmed positive or negative -- backed by a test asserting on the sentinel itself, not a downstream consumer whose own guard might mask the bug. **Current, dated 2026-08-22, stated as the sequence rather than flattened to the outcome, because the sequence is itself the evidence this criterion works:** originally carded as untested in `geper` -- this row's own prior citation (`test_gnomad_lookup_failure_not_absent.py`) pointed at a `kim_pipeline` file, despite this row's own scope note above reading `Tree: geper/ only`; found and reported during a no-CI dependent-claims sweep (2026-08-22), confirmed directly by checking the file did not exist anywhere under `geper/tests/`. Kelly then wrote the genuine `geper`-tree test at that same path, and it went **RED FIRST**: 8 of its assertions failed on the very first run, not merely absent -- the property itself was broken, not just unmeasured. Ten consumer guard sites checked `gnomad_result.get("error")` for truthiness rather than presence, so `str(exc) == ""` (any exception raised with no message -- `RuntimeError()`, `MemoryError()`, `TimeoutError()`) walked through every guard as a confirmed absence and manufactured unearned PM2 pathogenic evidence. Fixed in `d1128ee`: all ten guard sites now check `error is not None`; the producer falls back to the exception's type name when `str(exc)` is empty. 17 green post-fix -- re-verified independently on the current tree by re-running the file directly, not taken from the commit message alone. **Now MET at A for gnomAD**, `geper/tests/test_gnomad_lookup_failure_not_absent.py`. Not yet independently verified for every other lookup (ClinVar, constraint/hotspot, PS1/PM5) -- those currently use the correct `None` pattern per my own prior read of `orchestration/shared.py`, but none of the three has its own dedicated regression test the way gnomAD now does. Scoring this as MET at A only for the specific lookups with a persisted test; the domain total below reflects a partial score, not a full one, until the others are covered the same way. | Any lookup's exception or unavailable path produces a criterion status indistinguishable from (or silently defaulting to) a confirmed answer -- the exact shape of the original gnomAD defect, in any lookup, old or new. |
| 2c | Error-classification honesty in model loading | 4 | A | **Tree: `geper/` only** (evidence is `geper/tests/test_plugin_network_error_handling.py`; D2's own domain-level scope note applies). Model-load failures propagate with their real exception class and message when the cause is local; only genuine network/HTTP failures are generalized to a sanitized "model unavailable," and this distinction is backed by a persisted test per plugin (not manual verification at review time). Current: **tier A -- corrected in v1.1.0.** `geper/tests/test_plugin_network_error_handling.py` (`TestNoBareOSErrorInNetworkTuples` x10 + `TestOSErrorPropagationSurvivesRelabeling` x2, 12 passed, confirmed on the current tree) covers exactly this property, landed in `470a993`. **This corrects a claim in the COMMITTED v1.0.0 text (`89bf125`), which is wrong, not merely stale by the time of this edit -- see the Amendment history block for the original wording, quoted, and why the rubric's own falsifier machinery could not have caught this class of defect on its own** (a falsifier tests the codebase; this was a claim with a shelf life that had already expired at the moment it was committed). | A plugin's exception handler catches bare `OSError` (or an equivalent overbroad catch) under a network-shaped label, **or** a local resource-exhaustion error is relabeled "model unavailable" with no trace of the real cause surviving at a visible (non-debug) log level. |
| 2d | This domain's own supporting tests are not cannot-fail | 3 | A | **Tree: `geper/` only, by this row's own satisfier text** ("every test backing 2a-2c" -- 2a-2c are `geper/`-only per this row's own siblings). A persisted, periodically re-run sweep confirms every test backing 2a-2c is not proxy-pinned, logic-duplicating, non-discriminating, or fixture-masked, re-run whenever a supporting test file changes. Current: tier B -- I ran this sweep once, by hand, and found the four previously-known instances genuinely fixed. **MOVED, dated 2026-08-22 (v1.10.0), ruled content not legibility -- same defect shape as 6a/6b, evidence attributed to the wrong criterion:** two `kim_pipeline/tests/test_gap_fixes.py` findings previously listed here (a suite-wide collection-abort risk; `test_gap7_pytest_anyio_mark_recognised`'s cannot-fail construction) are removed from this row's own evidence and now recorded under **8a** instead -- they were surfaced during this same hand-run sweep but back 8a's full-suite scope (both `geper/` and `kim_pipeline/`), not 2a-2c's `geper/`-only tests, which never had a `kim_pipeline` counterpart for them to be backing. **Tier B is unaffected by the move**: it already rested on this sweep being hand-run, once, not a re-runnable artifact -- true with or without these two findings counted here -- so the four remaining, genuinely-fixed instances alone carry the same tier basis the row already had. | Any test backing 2a-2c is found to pass identically whether or not the defect it claims to guard is present, **or** the sweep has not been re-run since the last change to a file it covers. |

### D3. Governance & Chain of Custody -- weight 12

**Reweighted 3/3/3/3 in v1.4.0** (was 4/4/4, three rows) to admit new
sub-criterion 3d without changing D3's own domain weight or this rubric's
100-point total -- my own call, within the sub-criterion-weight ownership
section 2 already claims. No principled reason 3d deserves more or less
than 3a/3b/3c: all four test the same category (a single-source-of-truth or
enforcement property surviving every output surface), so an even split is
the default absent a stated reason otherwise. Diluting only the three
already-MET rows to fund a new, currently-RED row is a real trade-off,
named rather than smoothed: 3a/3b/3c's earned Tier-A credit is worth fewer
weighted points after this change than before it, for a reason unrelated to
any regression in what they test. Reversible if the human would rather grow
D3's total from 12 (which would require either taking 4 points from
another domain or accepting a rubric total above 100) instead.

**Scope, made explicit in v1.9.0's rubric-wide sweep: all four rows (3a-3d)
are scoped to `geper/` only, with no widening question to card.** Unlike
D1's or D2's kim_pipeline gaps, this is not a silent exclusion of a
counterpart that exists elsewhere -- `kim_pipeline/` has no document-level
review/sign-off or export-rendering apparatus of any kind (confirmed
directly when drafting 3d, re-confirmed here rather than assumed carried
forward: its only `review_status` hit anywhere is ClinVar's own external
metadata field, unrelated). There is nothing in the other tree for these
four rows to have missed.

**CROSS-VERSION COMPARABILITY CAVEAT -- read this before comparing any two
D3 scores.** A D3 score measured under v1.0.0-v1.3.0 (three rows, 4/4/4) is
**not directly comparable** to one measured under v1.4.0+ (four rows,
3/3/3/3), even if nothing about the pipeline changed between the two
measurements. **Stated where a score is read, not only in the version
history above, because the person who needs this is someone glancing at two
numbers and concluding something moved, not someone already investigating
the changelog.** **This specific change points one direction, not both:** a
post-v1.4.0 D3 score will tend to look *worse* than a pre-v1.4.0 one for the
same, unregressed pipeline state -- not better -- because 3d starts unmet
(3 points unreachable until Meredith's A-2 sign-off-scope work closes it,
see 3d's own row) while 3a/3b/3c simultaneously dropped from 4 points each
to 3, so even a run where 3a/3b/3c stay fully MET now tops out at 9/12
instead of the 12/12 a pre-v1.4.0 measurement of the same three criteria
would have shown. A reader who sees a lower D3 number after this rubric's
own version bumped should check which rubric version produced each number
before reading it as regression. (In general this class of caveat can cut
either direction -- a later score could also look *better* for reasons
unrelated to the tree, e.g. a domain gaining a criterion that starts MET --
this instance does not, and is stated as the specific direction it is
rather than left as a general disclaimer.)

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 3a | Review-status single source of truth | 3 | A | **Tree: `geper/` only** (D3's own domain-level note applies -- no counterpart exists in `kim_pipeline`). Every rendered output (PDF, Markdown, JSON, LIMS export) derives "reviewed" state from the same document field; no renderer accepts an unrelated free-text field as a proxy. Current: MET at A (0fdf874 + 3 test files). | Any two output surfaces for the same document disagree on reviewed / draft / overridden status, or any renderer's review-state claim can be changed by an input other than the real sign-off record. |
| 3b | Consent single source of truth | 3 | A | **Tree: `geper/` only** (same as 3a). Same shape as 3a for `patient_consent`. Current: MET at A (2cc57a6 + `test_consent_metadata.py`). | Any two output surfaces disagree on consent status for the same document. |
| 3c | Run-level caveat parity | 3 | A | **Tree: `geper/` only** (same as 3a/3b). A degraded-run caveat (e.g. an unreachable data source) appears in every shipping output format when triggered. Current: MET at A (`test_export_lims_caveats.py` + JSON/Markdown pins). | The caveat appears in fewer than all shipping output formats for the same triggering run condition. |
| 3d | Mandatory-review enforcement, not just label consistency -- **RED BY DESIGN ON ARRIVAL, 2026-08-21, against the state Meredith established** (draft and reviewed outputs are content-identical except one banner string). A future reader must be able to tell this was a newly-visible truth rather than a regression -- same principle as the superseded-not-rewritten ruling on Ryan's baseline: the record shows a conclusion with a date, not a silent rewrite. | 3 | A | **Tree: `geper/` only** (fuller justification below, in this same cell -- confirmed directly, not assumed, when this row was drafted). Every documented, currently-possible code path that renders or exports a document defaults to the least-permissive (DRAFT/unreviewed) state absent an explicit `approve()`/`override()` call, backed by a test that drives every export surface from a document with **no `review_status` key at all** and confirms none renders or labels it as reviewed. A fixture carrying the right default would satisfy a weaker version of this test and prove nothing -- the missing key is what makes it discriminating. Current: UNMET. `geper/report/export_lims.py`'s own `_require_reviewed` gate is called at exactly one site (`:501`), which does not cover the PDF/JSON/Markdown render paths -- Meredith's finding, independently confirmed this session. **Scoped to `geper/` only**: `kim_pipeline/` has no document-level review/sign-off or export-rendering apparatus at all to check -- confirmed directly, not assumed; its only `review_status` reference (`kim_pipeline/pipeline/clinvar/lookup.py:390`) is ClinVar's own external metadata field, unrelated to this criterion. **Guardrail: do not remediate this toward green by making draft and reviewed outputs superficially distinguishable** (e.g. a cosmetic banner-string fix) -- that would satisfy this falsifier's literal text while leaving the underlying enforcement unaddressed, a cannot-fail fix for a cannot-fail check. The remedy is Meredith's A-2 sign-off-scope decision, not a change that makes this criterion pass. **Update, v1.7.0: A-2 landed (`agents/meredith-msydpy6m/A2-signoff-scope-options-with-costs.md`) -- the human adopted Options 2 (non-LIMS gate, pattern-only, nothing live to gate today) and 3 (withdrawal/erasure workflow). Their own summary matrix marks BOTH "Makes 3d pass honestly: No" -- neither option is even claimed to close this criterion, so their landing must not be read as progress toward green.** Meredith's own report names the reason precisely: this criterion's falsifier is really about her finding **B1** (the same content-identical-except-banner-string fact already in this row's own annotation, not `ROUND_CANDIDATES.md`'s unrelated round-14 B1 -- disambiguated here on purpose, since a label resolving to two things is exactly the citation defect this floor spent a session on), and **none of her five costed options (1-5) touch B1** -- her report states directly that **B1 may not be closeable by any change to `signoff.py` at all**, because GEPER has no access-mediation layer between "file exists" and "someone opens it," and the reviewing clinician structurally needs to see full content pre-approval, which is in tension with making an unreviewed artifact substantively different from a reviewed one. **The human's ruling, carried forward rather than softened: that is a true fact about the product, and this criterion should keep saying so -- re-aiming 3d to something achievable would be making the instrument fit the tree, not measuring the tree.** 3d therefore stays exactly as originally drafted; this update only adds what has since been learned about why it may stay red. | Any export or render path is found where a document lacking a genuine sign-off record is indistinguishable, in its output, from a reviewed one. **Currently true; the falsifier is presently observed** (same shape as 7b's own "currently fails its own falsifier" scoring). |

### D4. QC & Data Traceability -- weight 10

**Scope, made explicit in v1.9.0's rubric-wide sweep -- 4a and 4b split
differently, and this is the sharpest example this sweep found of why the
two disclosure directions have to be recorded distinctly rather than one
domain-wide tag:**
- **4a is `geper/`-only, same as D3** -- storage/re-render/renderer
  machinery is geper's document-governance apparatus, which does not exist
  in `kim_pipeline`. No widening question; nothing there to have missed.
- **4b is inherently cross-tree, by what it measures, not by a scoping
  choice**: the "upstream instrument/tool output" it compares against
  (samtools/fastqc) is actually invoked inside **`kim_pipeline`**
  (`pipeline/alignment/bam_utils.py`, `bwa_runner.py`, `stage.py`, and
  others -- checked directly, not assumed from the row's own "e.g."), not
  `geper/`, which only *mentions* samtools in validation error strings and
  never runs it (`geper/pipeline/raw_input_validator.py`). A real 4b
  measurement would need to compare `kim_pipeline`'s own QC-generation
  output against samtools/fastqc's real output, and separately confirm
  `geper/`'s rendered numbers match what `kim_pipeline` produced -- two
  different comparisons, in two different trees, both required for the
  claim 4b actually makes. Not a widening question to card (nothing here
  changes what 4b measures -- it always meant this, just didn't say so);
  a legibility fix, same as 4a's.

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 4a | QC durability through storage and re-render | 6 | A | **Tree: `geper/` only** (storage/re-render/renderer machinery; no counterpart in `kim_pipeline`, per D4's own header). QC metrics persist on the document and survive a sign-off re-render (approve/override) into every renderer. Current: MET at A (`test_qc_metrics_durability.py`, exercises both branches through a real `approve()`). | A re-render of a document that had real QC metrics ever produces the "no QC observed" text instead of the real values, in any renderer. |
| 4b | QC accuracy against the source instrument/tool output | 4 | A | **Tree: cross-tree, inherently** (per D4's own header) -- the tool output is `kim_pipeline`'s, the rendered report is `geper/`'s; both comparisons are required for this claim. The QC values a report shows match the actual upstream tool's own output (e.g. samtools/fastqc, invoked in `kim_pipeline/`'s alignment stage, not `geper/`) exactly or within a stated, pre-agreed rounding tolerance, backed by a persisted comparison test. **Currently UNSCORED, not zero-by-default** -- I have verified 4a (durability/parity) directly but have not personally checked whether the numbers themselves are ever compared back against the tool that produced them. Flagging this as a gap in my own audit coverage rather than assuming a tier because a neighboring sub-criterion is strong. | Any rendered QC number differs from the source tool's own reported value beyond the stated tolerance. |

### D5. Documentation & Provenance -- weight 10

Tier C is the **correct and sufficient** expected tier for every sub-criterion
in this domain -- deliberately different treatment from D2-D4. A license
citation cannot become "more evidenced" by adding a test; there is nothing to
test. This is also the domain where the human's finding from my last audit
("one criterion was documentation wearing an evidence label") lives -- 5c's
falsifier exists specifically because of that finding.

**Scope, made explicit in v1.9.0's rubric-wide sweep:**
- **5a is `geper/`-only, silently, and this is the confirmed third instance
  that triggered this whole sweep**: `LICENSE_AUDIT.md` (what 5a scores)
  already discloses its own limit in its own text -- "does not cover
  kim_pipeline's own model/dependency license posture" -- but 5a's row
  never carried that forward. `kim_pipeline` retains and actively uses
  DNABERT-2 independently, entirely outside anything 5a scores. Legibility
  fix only, since the underlying document already drew this boundary; 5a
  is just now saying what it has always meant.
- **5b was checked for the same defect and does NOT have it** -- do not
  assume a sibling row shares a neighbor's problem without checking:
  `DATA_SOURCE_LICENSE_AUDIT.md` is genuinely product-wide by design,
  already covering OMIM (`kim_pipeline`'s own retired integration) and
  IndiGenomes (`geper`'s) side by side. No note needed.
- **5c was also checked and needs no note**: it grades a documentation
  *practice* (is a design-boundary ruling written down, reachable, with
  rationale), not a specific artifact tied to one tree -- the practice
  applies identically to a `geper/` or a `kim_pipeline` decision, so it is
  tree-agnostic by nature, the same shape as 1b and 8a.

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 5a | AI-model license audit currency | 4 | C | **Tree: `geper/` only** -- `LICENSE_AUDIT.md`'s own text states it "does not cover kim_pipeline's own model/dependency license posture" (e.g. its DNABERT-2 usage); this row was silent about that limit until this pass (see D5's own header for the fuller account -- this was the confirmed third instance that triggered the rubric-wide sweep). `LICENSE_AUDIT.md` accurately reflects every currently-integrated model's license, each re-verified against a primary source. | Any currently-integrated model is absent from `LICENSE_AUDIT.md`, or a documented license claim contradicts its primary source when re-checked. |
| 5b | Data-source license audit currency | 3 | C | **Tree: both, genuinely product-wide by design** -- checked directly, not assumed clean because 5a needed a note: `DATA_SOURCE_LICENSE_AUDIT.md` already covers OMIM (`kim_pipeline`'s own retired integration) and IndiGenomes (`geper`'s) side by side, unlike `LICENSE_AUDIT.md`. Positive finding, stated as one. `DATA_SOURCE_LICENSE_AUDIT.md` accurately reflects every currently-integrated external data source's license. | Any currently-integrated data source is absent from the audit doc, or a documented claim contradicts its primary source when re-checked. |
| 5c | Design-boundary decisions are recorded, not merely claimed | 3 | C | **Tree: both, tree-agnostic by nature** -- this grades a documentation *practice* (is a ruling written down, reachable, with rationale), not an artifact tied to one tree; it applies identically to a `geper/` or a `kim_pipeline` decision. Positive finding, stated as one. When a human rules an omission or a behavior intentional, that ruling exists in writing with rationale (e.g. the LIMS export boundaries, 13c458e), reachable from the component it governs. | A component's absence or behavior is described anywhere (a report, a card, a docstring) as "intentional" or "by design" with **no** corresponding written decision record to point to -- an assertion wearing documentation's clothes, which is Tier D regardless of how confident the surrounding prose sounds. |

### D6. Security & Supply-Chain Integrity -- weight 10

**Scope, made explicit in v1.9.0's rubric-wide sweep -- the OTHER
direction from 5a, worth naming as its own case rather than folding into
the same "geper-only" language used elsewhere:** 6a and 6b's "Current: MET"
evidence is actually **`kim_pipeline`-sourced**, previously without saying
so. This matters more than it would have before this sweep started: by
making 1a/1c, 3a-3d, 4a, 5a, and 1e explicitly `geper/`-only, this document
has taught a reader a convention -- an unmarked row now reads as
`geper/`-only *by implication*. Leaving 6a/6b unmarked under that new
convention would make them actively misleading in a way they were not
before this sweep began; partial disclosure is not partway to safe here.
Both corrected below, in place, no tier/weight/falsifier change:

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 6a | RCE-shaped surfaces are pinned | 4 | A | **Tree: `kim_pipeline` (the evidence), scored against both trees (the falsifier).** Every `trust_remote_code=True` (or equivalent) load resolves to a specific, reviewed commit SHA, not a floating/default branch ref, with an in-code review note. **CORRECTED, dated 2026-08-22 -- this row previously asserted something false, not merely silent about scope.** This row's text from `v1.0.0` (`89bf125`) through `v1.8.0`, verbatim, read: *"Current: MET at A (DNABERT-2, committed)."* Read plainly, and read the way every other unmarked row in this domain was read before this sweep, that sentence asserts a `geper/`-side DNABERT-2 pin. **No such pin exists**: `geper/` removed DNABERT-2 entirely (HyenaDNA replaced it as the default DNA model, per `LICENSE_AUDIT.md`) -- there was never a `geper/`-side load for this row's evidence to be citing. **What the row was actually, correctly citing all along is `kim_pipeline`'s own, separate DNABERT-2 pin** (`1fa8c56`, `kim_pipeline/pipeline/ai/engine.py`) -- the tier and MET status were never wrong, only the unstated (and, once D1-D5's `geper/`-only convention was established, actively misleading) attribution. Checked directly against `1fa8c56`'s own commit message before correcting, not assumed. The record should show this was found, not that it was always right. | Any `trust_remote_code=True` load point, in either tree, resolves to a floating ref rather than a pinned, reviewed commit. |
| 6b | No unresolved commercial-license conflict | 3 | A (closure) + C (record) | **Tree: `kim_pipeline` (the evidence), scored against both trees (the falsifier).** No active integration has an unresolved non-commercial or copyleft-conflict license; any past conflict has both the offending code removed/gated **and** a decision record. **CORRECTED, dated 2026-08-22 -- same shape as 6a: previously ambiguous in a way that reads as false, not merely silent.** This row's text from `v1.0.0` (`89bf125`) through `v1.8.0`, verbatim, read: *"Current: MET (OMIM retired, 51558c1/bd86103, with a decision record matching the IndiGenomes precedent's rigor)."* Naming IndiGenomes only as a *precedent* for OMIM's rigor, without ever stating that OMIM itself is a `kim_pipeline` finding (per `DATA_SOURCE_LICENSE_AUDIT.md`: "removed from kim_pipeline") while IndiGenomes is `geper/`'s own, let a reader assume both examples came from the same tree D1-D5's convention would suggest. **The MET status and record quality were never wrong** -- OMIM's retirement is real and well-documented -- only the tree attribution was left for a reader to guess rather than stated. The record should show this was found, not that it was always right. | An integration is found, in either tree, whose upstream license forbids commercial use or redistribution and no decision record or code change addresses it. |
| 6c | Auto-install/pip safety against the shared interpreter | 3 | A | **Tree: both, product-wide by the falsifier's own wording; `kim_pipeline` coverage unverified (see note below).** Package auto-install is either sandboxed (isolated venv / separate site-packages per the pattern Andy already uses) or made safe against interruption (no live `pip install` shelled against a shared interpreter mid-test-run; any install step is atomic against a kill). **Currently UNMET at any tier** -- newly surfaced by the floor-wide pip advisory that originally raised this row; nothing in the repo satisfies it yet, and `3b0ede6` (see Falsifier correction, dated 2026-08-23) does not change that -- it makes the unsafe path unreachable under one specific condition (running under pytest), a third thing distinct from both sandboxing and atomicity-against-interruption, so it satisfies neither disjunct this row's satisfier actually asks for. **Scope note, v1.9.0**: this row's own falsifier ("any test run or pipeline run") already reads as product-wide in intent, and it is -- `kim_pipeline` has its own, separate pip-install call sites (`main.py`, `pipeline/ai/engine.py`, `pipeline/orchestration/runner.py`, `pipeline/reporting/stage.py`, `pipeline/utils/reference_cache.py`, `serve_api.py`; `geper/`'s own fixed instance was `geper/utils/auto_install.py`, closed by `47748b6`) that have **not been independently checked** against this same falsifier -- named as an open coverage question, not folded silently into the existing UNMET score (which was already UNMET before this note and stays UNMET after it). | Any test run or pipeline run, in either tree, can be observed shelling real `pip install`/`uninstall` against the shared global interpreter. **CORRECTED, dated 2026-08-23 (original v1.9.0-through-v1.10.0 text, quoted verbatim, had gone stale):** *"This falsifier is already met as of today, in `geper/`"* was true when written and is no longer true for half of what it claims. As of `3b0ede6`, `geper/utils/auto_install.py::_auto_install_disabled_for_tests()` makes `ensure_pip_package_available` return `False` under pytest without shelling pip -- so the **test-run half** of this falsifier is no longer observable in `geper/` (measured, not inferred: CI run `32602931028` @ `3b0ede6` completed with zero live pip installs). The falsifier remains met via the **pipeline-run half**, untouched by `3b0ede6` -- a real, non-test `geper/` run still auto-installs against the shared interpreter exactly as before, so the UNMET score is unchanged. `kim_pipeline`'s own six call sites remain unverified either way, per the Satisfier cell's scope note. |

### D7. Operational Resilience -- weight 8

**Scope, made explicit in v1.9.0's rubric-wide sweep: both rows are
`geper/`-only, with no widening question, same shape as D3.** The
mechanism both rows grade (`geper/utils/service_health.py`'s probe/
retry/latch state machine) has no counterpart in `kim_pipeline` -- checked
directly, not assumed: `kim_pipeline`'s own external-lookup code
(`pipeline/clingen/lookup.py`) explicitly documents avoiding this exact
import (`from utils.service_health import HEALTH`) as part of its
deliberate decoupling from `geper/`'s stack, and no independent
health-probe/latch implementation exists anywhere in `kim_pipeline`.
Nothing in the other tree for these two rows to have missed.

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 7a | Retry before latch | 4 | A | **Tree: `geper/` only** (D7's own header -- `kim_pipeline` has no health-probe/latch mechanism of its own; its own external-lookup code explicitly documents avoiding this import). A single failed health probe does not latch a service offline; only repeated failures within a stated window do. Current: MET at A (5094620, `test_service_health.py`). | A single probe failure latches a service offline for the remainder of a run. |
| 7b | Bounded reprobe / recovery | 4 | A | **Tree: `geper/` only** (same as 7a). A latched-offline service that genuinely recovers is un-latched within a bounded number of attempts or elapsed time, without every remaining call paying a full timeout, backed by a passing test. **Currently FAILS its own falsifier** -- `test_service_health_bounded_reprobe_pending.py`'s three tests are deliberately RED (the feature does not exist yet; 6a52384 only instruments latency/latch state to size the feature later). Scored unmet, not partial -- instrumentation towards a fix is not the fix. | A genuinely-recovered service remains latched offline for the rest of a run. Currently true; the falsifier is presently observed. |

### D8. Test-Suite Integrity -- weight 5

Small weight, load-bearing role: every Tier-A claim elsewhere in this rubric
is only as trustworthy as this domain being sound.

| # | Sub-criterion | Weight | Expected tier | Satisfier | Falsifier |
|---|---|---|---|---|---|
| 8a | Full-suite cannot-fail sweep, repeatable | 5 | A | **Tree: both, explicitly, already stated correctly since this row was written -- no change needed, confirmed rather than assumed during this sweep.** A persisted, periodically re-run sweep across the full test suite (both `geper/` and `kim_pipeline/`) for proxy-pinned, logic-duplicating, non-discriminating, and fixture-masked tests, with every flagged instance either fixed or explicitly accepted with recorded rationale. Current: tier B -- my sweep was real, manual, and covered the highest-risk files, but is a one-time pass, not a re-runnable artifact, and explicitly did not cover all ~2900 tests across both suites. **RECEIVED, dated 2026-08-22 (v1.10.0), from 2d** -- two findings originally surfaced during my D2 cannot-fail sweep properly belong here instead, since both are `kim_pipeline` findings within this row's own full-suite scope, not tests backing 2a-2c: (i) `kim_pipeline/tests/test_gap_fixes.py`'s suite-wide collection-abort risk (collecting it aborts on `ModuleNotFoundError: fastapi`); (ii) that same file's `test_gap7_pytest_anyio_mark_recognised` (lines 606-611), cannot-fail by construction -- `pytest.mark.<any attribute name>` always returns a non-None `MarkDecorator` regardless of whether the mark is registered or the underlying package is even installed (found by Andy in the infra lane, 2026-08-21, independently verified before folding in). **Tier is unaffected by their arrival here**: 8a's tier B already rested on the sweep being manual, one-time, and not covering all ~2900 tests -- two more hand-found instances, now correctly attributed, don't change that basis. | A test is found, anywhere in the suite, where no change to the production path it claims to guard could make it fail, **and** it is not already flagged/tracked. (`test_gap_fixes.py`'s suite-wide collection-abort risk and its `test_gap7_pytest_anyio_mark_recognised` cannot-fail construction, both above, are exactly this domain's own open items as of today.) |

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
2. **RESOLVED in v1.1.0.** 1c now carries an explicit "structurally
   unavailable, with a documented alternative" path (Tier C, distinct from
   the A-tier participation path), per Vic's input: PT/EQA schemes enrol
   *laboratories*, and GEPER is software, so enrolment may not be a thing to
   attempt rather than a thing not yet attempted, and ISO 15189:2022 is
   understood to recognise a documented-alternative limb for exactly this
   case. **The ISO clause number behind that limb is unverified** -- flagged
   in 1c's own row, not smoothed over here. Still the human's call to
   overrule if this reading of the regulatory path is wrong.
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
