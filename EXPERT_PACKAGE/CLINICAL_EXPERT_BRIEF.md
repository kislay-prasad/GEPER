# Clinical Expert Brief

**Written 2026-09-08.** This document exists so a clinical-lab / bioinformatics-validation
expert (the role is currently unfilled — see item 1) can be handed one document rather than
a decade of accumulated board history. It covers every card currently assigned to
`clinical-expert (ROLE UNFILLED)` in `tasks.json` — twelve cards, corrected twice from an
original miscount of ten; two of the twelve had been sitting invisible in the expert queue
because they carried the name of the engineer who found them rather than the role that is
stuck on them (an assignee is who is stuck, not who discovered it).

Three of the twelve cards are one decision wearing three names (the PP3/BP4 trilemma) and are
merged into a single item below, per the instruction that two items which are the same
decision under different names should say so and merge. **That leaves ten items, not twelve.**

Per item: what is settled (cited, not summarised from memory), what is specifically being
asked (a question with a decidable answer, not a topic), what it blocks concretely, and where
the evidence is. Ordered by consequence, not by card age or id. This document does not answer
any of the ten questions — that is explicitly out of scope for whoever wrote it.

---

## RECONCILED 2026-09-10 — read this before anything below

**This document was written 2026-09-08 on a branch (`clinical-expert-brief`, commit
`ddfdd85c`) that was never merged.** Master moved seven times in the two days between then
and this reconciliation pass, and a separate, narrower brief (`EXPERT_PACKAGE/F5_EXPERT_BRIEF.md`,
covering only the ranking/F5 topic) was written and anchored independently in the meantime —
the two are different documents about different (if adjacent) subsets of the expert queue, and
neither should be read as superseding the other. **This file is the reconciled version of the
391-line, ten-item, four-citation document** — content is carried forward from that branch as
the base, per instruction, not rebuilt from nothing. Everything below this notice is either
unchanged from the 2026-09-08 version, or is marked **RECONCILED**/**STALE, CORRECTED**/**RE-SCOPED**
at the exact point it changed. Nothing was silently updated.

**What changed under this brief since 2026-09-08, checked directly against current master
(`d1b4db8`), one item at a time:**

1. **Item 4 (PP3/BP4) is substantially stale and is corrected below.** A second, independent,
   more severe PP3/BP4-licensing problem was found and is *currently being closed, right now,
   as this reconciliation is written* — `spliceai_score` also reaches the PP3/BP4 vote via a
   **second path** this brief's 2026-09-08 version did not know about: native `DS_AG`/`DS_AL`/
   `DS_DG`/`DS_DL` fields read directly out of an input VCF's own INFO field
   (`kim_pipeline/pipeline/annotation/stage.py:733-743`), independent of the VEP-plugin path
   the 2026-08-22 licence fix closed. See the item for the full, current picture — the
   citations and even the exact numbers in the 2026-09-08 text (below, preserved) are now
   incomplete, not merely dated.
2. **Item 6's card was re-scoped OFF the clinical-expert queue on 2026-09-09** — one day after
   this brief was written — to `assignee: human`, with the explicit note *"BLOCKED ON A HUMAN
   PRODUCT-SCOPE DECISION, NOT ON THE CLINICAL EXPERT. Booking the expert will not clear it."*
   It no longer belongs in an expert-booking package at all. Left in this document, marked
   **RE-SCOPED**, rather than deleted — the question is still real and still blocking, only the
   decider changed.
3. **The `<<<EXPERT-JUDGEMENT-REQUIRED>>>` token count cited in item 1 (19) is stale.**
   Measured directly against current `VALIDATION_STUDY_DESIGN.md` this pass: **25 distinct
   tokens now exist** (EJ-01 through EJ-25, with EJ-03 split into EJ-03a/EJ-03b and EJ-23
   retired/absent). The gate item 1 describes is unchanged in kind; the number quantifying its
   current scope has grown by six since this brief was written.
4. **Every other item (1's substance, 2, 3, 5, 7, 8, 9, 10) was checked for whether its cited
   code/file locations still hold and was found current** — not re-derived from scratch, but
   spot-checked against the current tree (see each item's own **RECONCILED** note for what was
   specifically re-verified). None needed a substantive correction beyond what is marked.
5. **Board reconciliation:** the current board carries exactly **eleven** cards at
   `status: waiting, assignee: clinical-expert (ROLE UNFILLED)`. All eleven map onto items 1,
   2, 3 (2 cards), 4 (3 cards), 5, 7, 8, 9 below — **1+1+2+3+1+1+1+1 = 11, a complete match**,
   once item 6's card (re-scoped to `human`, point 2 above) is correctly excluded from the
   count. **No card is missing from this brief and no item in this brief lacks a live card.**
   A twelfth, separate card exists on the board today (`HUMAN-COUNSEL-the-shipped-image-bakes-in-GPL-3-0-and-GPL-2-0-OS-BINARIES...`,
   found 2026-09-10) but is assigned to counsel, not the clinical expert, and is deliberately
   **not** included here — it belongs to a different booking.
6. **Anchors, per the instruction to carry the F5 finding into this document and check whether
   these ten items have real external anchors or are also `[INVENTED]`-backed:** added inline,
   per item, below. **Summary: two of ten items (2, 7) cite a real external standard (CDSCO
   MDSW Guidance, NABL 112A) with their own pre-existing verification caveats, carried forward
   unchanged. One item (4) cites real, verified literature (ACMG/AMP 2015, Pejaver et al. 2022)
   inside GEPER's own code docstring, not this document's assertion. The remaining seven items
   (1, 3, 5, 6, 8, 9, 10) have no external regulatory anchor at all and were never claiming
   one — they are gate/process/product-scope questions or, for items 8 and 9 specifically,
   questions this floor's own `VALIDATION_STUDY_DESIGN.md` explicitly marks `[INVENTED]` in its
   own text (§5.9: "no external source supplies a study-level failure declaration for a
   variant-interpretation validation") — the same finding just confirmed independently for the
   F5 brief. That is not a defect in this document; it is the honest state of what does and does
   not have external backing, stated so the expert is not left to assume otherwise.**

**Location note:** this reconciled version is filed at `EXPERT_PACKAGE/CLINICAL_EXPERT_BRIEF.md`
— see this task's report for why this path was chosen over `docs/`. The `clinical-expert-brief`
branch (`ddfdd85c`) was read for its content and left untouched, per instruction; it has not been
merged or rebased by this pass and its disposition is not decided here.

---

## 1. The hard gate itself — no threshold is set and no concordance result is interpreted without this role being filled

**Card:** `phase2-human-clinical-expert-is-a-hard-gate-on-validation` (status: waiting)

**What is settled:** GEPER's validation strategy can be drafted from published guidance
(CAP/CLIA, ACMG/AMP, ICMR) but the professional judgement that sets acceptance thresholds for
a clinical genomics assay — what concordance is good enough, on which variant classes, at what
confidence — cannot come from an agent. The human ruled this explicitly a hard gate, not a
nice-to-have, and confirmed twice more that it remains unowned because they have not yet
identified a person: *"I don't have one identified yet, and I'm not going to pretend
otherwise. Keep the gate as-is, unowned, hard."* Sourcing routes were named (a clinical-lab
director at a NABL-accredited lab; a bioinformatics validation lead at a diagnostics company;
an academic in clinical genomics who consults) but sourcing is explicitly the human's action,
not an agent's.

**What is specifically being asked:** Not a question for the expert — this is the
precondition for every other item in this document. The actionable question is for whoever is
sourcing the role: is a name available yet?

**What it blocks, concretely:** All nine substantive items below, at three separate points:
(1) filling any `<<<EXPERT-JUDGEMENT-REQUIRED>>>` placeholder in `VALIDATION_STUDY_DESIGN.md`
(**RECONCILED 2026-09-10: 25 now exist, not 19** — measured directly, see the notice above);
(2) interpreting any concordance result, even after a number legitimately exists — a measured
number still needs professional judgement to mean "pass"; (3) — added later and now the
primary purpose — review of the drafted validation design **before** anything is built against
it: *"Even a few hours of their time to review the drafted design before you build against it
would be worth more than weeks of further engineering."*

**Where the evidence is:** `VALIDATION_STUDY_DESIGN.md` (Vic's draft, now 2709 lines,
**RECONCILED: was 1132 lines when this item was written**, written to this exact audience);
`VALIDATION_READINESS_RUBRIC.md` D1 (weight 30, currently scoring zero — the domain this gate
governs); `hive/DISPATCH-READY-validation-scientist.md` addendum 1 (assessor targeting, already
implemented).

> **Anchor:** none, and none is owed. This is a process/governance gate the human imposed
> directly, not a claim resting on any clause. **What is being asked of the expert:** nothing
> in this item — it states the precondition the other nine sit behind.

---

## 2. Where the critical/serious severity boundary falls for GEPER's validation panel — sets the CDSCO device risk class

**Card:** `EXPERT-GATE-where-does-the-critical-serious-boundary-fall-for-genomic-conditions`
(status: waiting) — self-described as the decision with the largest downstream regulatory
consequence of all twelve, because it sets the initial risk classification governing the
entire validation pathway and every future scope expansion.

**What is settled:** CDSCO MDSW Guidance Table 2 maps situation severity to device class:
Critical → Class C, Serious → Class B, Non-serious → Class A, using stated rubric definitions
(Critical: life-threatening or requires major intervention, time-critical, fragile
populations, specialised trained users; Serious: moderate progression, often curable, not
time-critical, not fragile populations, specialised or non-clinical untrained users;
Non-serious: slow predictable progression, manageable, minor interventions). The provisional
direction (not a decision) aims at Class B — a first validated panel of serious-but-not-critical
conditions.

**What is specifically being asked:** Which genomic conditions in GEPER's own fixtures and
intended first validation panel — specifically BRCA1, TP53, and PRNP — fall into "critical" vs
"serious" severity under the CDSCO rubric. Plausible readings are noted but not decided:
BRCA1/TP53 (high-penetrance hereditary cancer, prophylactic-intervention pathways) could
plausibly be "critical"; PRNP (prion disease, currently untreatable) is harder to place
(serious outcome, limited intervention); carrier-screening conditions (cystic fibrosis, SMA)
might be "non-serious."

**What it blocks, concretely:** The device risk classification for the entire product. If
BRCA1/TP53 are placed in "critical," the class moves from the provisional B to C. Widening the
validation panel later to include any condition the expert places in "critical" moves the
class upward and triggers CDSCO re-submission.

**Where the evidence is:** Card notes cite CDSCO MDSW Guidance Table 2 and its severity
definitions directly (not yet independently re-verified by me against the CDSCO source
document itself — the card's citation is to the guidance, not to a file in this repo).

> **Anchor: real, external, and its verification status is unchanged since 2026-09-08 — carried
> forward rather than silently upgraded.** CDSCO MDSW Guidance Table 2 is the clause this
> question arises from. **I did not independently fetch and verify this document in this
> reconciliation pass** (out of scope for what I was dispatched to check this round — the
> instruction was to reconcile against code and the board, not to newly fetch external
> regulatory PDFs); the "not yet independently re-verified" caveat from the 2026-09-08 version
> stands exactly as written. **What is being asked of the expert, in one sentence:** do BRCA1,
> TP53, and PRNP fall under CDSCO's "critical" or "serious" severity definition, and does that
> place the device at Class B or Class C?

---

## 3. Does the expert re-derive the classification independently, or ratify GEPER's draft? — and first, a frame finding that must be read alone, before this question

**Cards:** `the-ratified-positioning-sits-outside-the-frame-published-guidance-assumes`
(frame, read first and alone) and
`expert-gate-top-item-does-the-reviewer-re-derive-or-ratify-the-draft` (the question itself,
originally placed at the top of the expert-gate list by explicit instruction). Both waiting.
The human fixed the presentation order explicitly: the frame finding must arrive alone, not
bundled with the question it governs, because *"if the frame finding arrives bundled with the
question it governs, it reads as a caveat on that question rather than as a condition on how
they should approach the whole engagement."* That is why both are combined into one item here
rather than two adjacent ones — presenting them as sequential-but-separate items would recreate
exactly the bundling the human ruled against.

**What is settled (the frame):** Vic checked, while splitting a related token (EJ-03), whether
the published guidance he had already verified for analytical sensitivity actually covers a
DRAFT-AWAITING-REVIEW output — and found it does not. This is a harder problem than a bad
citation: the standards are real, correctly identified, on-topic, and verified — they simply
were not written for this case. The consequence: **the expert will be deciding, not applying.**
An expert who believes they are applying an existing standard will look for one, and will find
three real, on-topic standards that appear to fit a case they were not written for.

**What is specifically being asked:** (0, alone) Told explicitly, before anything else: no
published guidance covers analytical-sensitivity definition for a draft-awaiting-review output;
what follows requires their own judgement, not citation of an existing standard. (1) Does the
reviewer independently re-derive the ACMG/AMP classification from the evidence, or ratify
GEPER's draft classification? The two answers diverge widely and nothing currently
distinguishes them.

**What it blocks, concretely:** The four re-keyed intended-use tokens EJ-02/03/06/20; the
definitional half of EJ-03 (Vic's split task); operator scope, which itself gates six of the
eighteen EJ-01-dependent decisions in the intended-use statement.

**Where the evidence is:** `hive/EXPERT_PACKAGE_pp3_bp4.md` is the assembled package for a
*different* item (item 4 below) — do not confuse the two; this item does not yet have its own
assembled package referenced in the card notes beyond the board entries themselves.

> **Anchor: this is the frame finding itself — there is no anchor, and that absence IS the
> content, not a gap in it.** The frame's entire point is that three real, verified standards
> exist and do not cover this case; citing one here as "the anchor" would be exactly the error
> the frame exists to prevent. **What is being asked of the expert, in one sentence:** having
> been told no published standard applies, does the reviewer independently re-derive GEPER's
> ACMG/AMP classification from the evidence, or ratify the draft GEPER already produced?

---

## 4. The PP3/BP4 trilemma — SpliceAI's licensing removal silently lowered a pathogenicity evidence bar, and the "denominator fix" is a no-op because the real problem is structural

**Cards (three, one decision):** `kelly-classifier-denominator-fix-pp3-bp4`,
`kelly-pp3-bp4-invariance-vs-single-predictor-incompatibility`, and
`expert-gate-pp3-bp4-trilemma-read-gepers-written-rejection-first` — all waiting, all the same
underlying question. The first two were re-owned to `clinical-expert (ROLE UNFILLED)` today
(2026-09-08) after sitting under Kelly's name, invisible in every prior count of the expert
queue including the one reported an hour before this brief. The third is explicitly recorded
as "the board pointer to" the assembled package and was never re-titled off its original
framing, but is the same question. **This is a third card on the same decision beyond the pair
named in the dispatch — flagging it as an additional finding, not silently folding it in.**

> **RECONCILED 2026-09-10 — READ THIS BEFORE THE "What is settled" SECTION BELOW, WHICH IS NOW
> INCOMPLETE, NOT WRONG.** A second, independent PP3/BP4-licensing problem was found today and
> **is being actively closed as this document is written** (card
> `RULED-BY-HUMAN-close-the-SpliceAI-DS-field-paths-RED-must-show-the-VOTE-MOVING-not-the-fields-being-read`,
> status `doing`, assignee Kelly). The 2026-08-22 fix that made the VEP-CSQ `spliceai_score`
> path permanently `None` did not close the predictor entirely: a **separate, un-fixed
> fallback** (`kim_pipeline/pipeline/annotation/stage.py:733-743`) reads `SpliceAI=`/`DS_AG`/
> `DS_AL`/`DS_DG`/`DS_DL` straight out of an input VCF's own INFO field whenever the guard
> `if var.spliceai_score is None:` is true — which the 2026-08-22 fix made **unconditionally
> true**, making this fallback fire on **every run**, not a dormant edge case. A test in the
> repo since 2026-08-28 (`kim_pipeline/tests/test_undetermined_aa_guard.py:388-389`) documents
> and exercises exactly this path and passes. **Consequence, verified against current
> `docs/BIJ_AI_CAPABILITY_AUDIT.md` (corrected 2026-09-10, the citation below is to the
> corrected text, not the version this brief originally relied on):** the PP3/BP4 majority
> threshold does not sit permanently at 2-of-3 as the original text below states — it **varies
> per input**, 3-of-4 when the caller's VCF happens to carry SpliceAI's native `DS_*` fields
> (a standard, independent upstream step many labs already run) and 2-of-3 when it does not.
> **Two identical variants can be classified differently depending on what the submitting lab
> ran upstream, and nothing in the report says why.** The human has ruled to close this second
> path (same remedy class as the VEP path in August); that work is in progress, not complete,
> as of this reconciliation. **This does not resolve the trilemma described below — closing the
> DS_* path removes the input-dependent variance and restores a single, consistent (but still
> undecided) evidence bar; it does not answer which of properties (a)/(b)/(c) that bar should
> sacrifice.** The two problems are related (both trace to the same 2026-08-22 SpliceAI
> licensing removal) but are not the same decision, and closing one does not close the other.

**What is settled:**
- SpliceAI was removed from `kim_pipeline` 2026-08-22 for licensing (CC BY-NC 4.0 pretrained
  models, GPL-3.0 code) — `kim_pipeline/pipeline/vep/stage.py:13-25` (verified directly: the
  docstring states this exactly).
- The PP3/BP4 vote-counting code still references SpliceAI as a source; the field is always
  `None` **via the VEP-CSQ path specifically** (see the RECONCILED note above for the second,
  still-live path), and each vote append is guarded `is not None`, so it silently drops out
  rather than raising — `kim_pipeline/pipeline/acmg/classifier.py:814` (PP3) and `:1171` (BP4
  mirror), verified directly, unchanged location.
- The majority threshold is computed over however many voters are actually present:
  `met = n_dam >= max(1, len(votes) // 2 + 1)` at `classifier.py:831` (PP3), mirrored for BP4
  at `:1188` — verified directly, unchanged location. **With SpliceAI present this required
  3-of-4 damaging calls; with it silently absent, it now requires 2-of-3.** Nobody chose that
  as a clinical position; it is a side effect of removing one voter from a majority-of-present
  formula. **(RECONCILED: "silently absent" is no longer the universal case — see above.)**
- **The system is running on this moved value today.** `classifier.py`'s last commit
  is `c7b9859`, a *revert* of the change (`d1128ee`) that would have addressed it —
  `geper/REVIEW_MANIFEST.md:17-19`, verified directly. The held candidate fix,
  `_MIN_CONCORDANT_PREDICTORS = 2`, exists only in the shared uncommitted working tree, not in
  `HEAD` — **this line reference (`VALIDATION_STUDY_DESIGN.md:288-291`) was not re-verified in
  this pass given how much the file has grown (1132 → 2709 lines); treat the file section
  number, not the line number, as durable.**
- **On the specific claim that "a customer-facing document records it as awaiting this
  decision": I could not confirm this as stated, and am reporting that rather than dropping
  it.** I searched the actual report-rendering code that produces what a clinician or patient
  sees (`kim_pipeline/pipeline/reporting/pdf_report.py`, `geper/report/clinical_report_builder.py`)
  for any PP3/BP4/SpliceAI-specific disclosure text and found none — the only "pending" text
  in the real report builder concerns confidence/priority scoring generally, not this issue
  specifically. The documents that *do* record this as awaiting the decision are internal:
  `docs/BIJ_AI_CAPABILITY_AUDIT.md:379-396,577-605` **(RECONCILED: this citation range is now
  stale — the corrected text as of 2026-09-10 runs approximately :579-633; see the RECONCILED
  note above, which quotes the corrected version directly)** and `VALIDATION_STUDY_DESIGN.md:288-334`
  (the assessor-facing validation design draft, not a patient- or clinician-facing report).
  Neither is what most people would call "customer-facing." This may be exactly the sentence god
  was told exists and has not himself seen — worth resolving before this brief is finalised for
  the expert, since the phrase changes how urgently this reads.
- **Kelly's deeper finding, which is why this is a trilemma and not a two-line fix:** every
  rule tested against the data satisfies exactly two of three properties — (a) a single
  predictor alone may suffice (pinned by five existing tests); (b) the verdict is invariant to
  removing a predictor (pinned by four of Pam's tests); (c) PP3 and BP4 never both fire on the
  same variant (both trees independently treat this as a real requirement — `kim_pipeline`
  found and fixed it as a bug; `geper` never allows it structurally). Majority-of-present
  satisfies (a)+(c), not (b). Absolute-≥2 satisfies (b)+(c), not (a). Absolute-≥1 satisfies
  (a)+(b), not (c).
- **`geper/` (the other tree) already considered and rejected majority voting in writing.**
  `geper/pipeline/acmg_rules.py::_pp3_bp4`'s docstring gives three arguments (no validated
  ranking to justify outvoting; internal consistency with `_bp7`'s any-dissent-blocks
  precedent; majority-vote explicitly rejected), citing ACMG/AMP 2015 and Pejaver et al. 2022
  — assembled in full at `hive/EXPERT_PACKAGE_pp3_bp4.md`, extracted programmatically from
  source with an assertion that the decisive clause survived extraction (an earlier relayed
  summary of this docstring had dropped two of the three arguments). The human declined to
  rule on these grounds specifically: *"Overriding a reasoned rejection requires engaging with
  the literature it cites and judging whether the evidence base has changed."* This is why the
  card sits with the clinical-expert role rather than being an engineering choice.
- SpliceAI licensing pressure is not a factor in this specific decision: `geper/`'s PP3/BP4
  never used SpliceAI at all, so this is decided purely on clinical merits.

**What is specifically being asked:** Which of the three properties (a), (b), (c) should PP3/BP4
sacrifice, and does `geper/`'s written rejection of majority voting (prior art on this exact
question, inside this codebase) still hold, or has the evidence base changed enough to
override it? `hive/EXPERT_PACKAGE_pp3_bp4.md` is built to be read in this order: geper's
written rejection first, then the trilemma itself, then the shared-bug finding, then the
confirmation that licensing is not a factor here.

**What it blocks, concretely:** Both engines' final PP3/BP4 evidence weight for every variant
evaluated on in-silico predictors — a live, currently-running clinical criterion, not a future
one. `kim_pipeline`'s held candidate fix (`_MIN_CONCORDANT_PREDICTORS = 2`) cannot land on
master until this rules, and remains parked in the shared working tree in the meantime — a
state with its own hazard (`kim_pipeline/pipeline/acmg/classifier.py` carries an in-code
warning immediately above the constant, naming the five tests that fail on purpose, so that a
well-intentioned attempt to make them pass does not silently overturn an undecided clinical
question).

**Where the evidence is:** `hive/EXPERT_PACKAGE_pp3_bp4.md` (the assembled package, human-ruled
ordering); `geper/pipeline/acmg_rules.py::_pp3_bp4` docstring (the written rejection);
`kim_pipeline/pipeline/acmg/classifier.py:814,831,1171,1188` and the warning comment above
`_MIN_CONCORDANT_PREDICTORS`; `geper/REVIEW_MANIFEST.md`; `VALIDATION_STUDY_DESIGN.md:288-334`
(section number durable, line numbers not re-verified this pass); **`kim_pipeline/pipeline/annotation/stage.py:733-743`
(RECONCILED: the second, DS_*-field path); `docs/BIJ_AI_CAPABILITY_AUDIT.md` §"CORRECTED
2026-09-10" (RECONCILED: the current, corrected account).**

> **Anchor: real, and it lives inside GEPER's own code, not this document.** The clinical
> question this item asks the expert to rule on arises from `geper/pipeline/acmg_rules.py::_pp3_bp4`'s
> own written rejection of majority voting, which itself cites **ACMG/AMP 2015** and **Pejaver
> et al. 2022** — real, published literature, quoted (not paraphrased) in full at
> `hive/EXPERT_PACKAGE_pp3_bp4.md`. **I did not re-fetch either citation against its own primary
> source in this pass** (that verification, if it happened, lives with whoever assembled
> `hive/EXPERT_PACKAGE_pp3_bp4.md` — not re-attempted here since this reconciliation was scoped
> to code and the board, not to re-verifying literature citations already inside a docstring).
> **What is being asked of the expert, in one sentence:** which of single-predictor sufficiency,
> removal-invariance, or PP3/BP4 mutual exclusivity should this system's PP3/BP4 rule give up,
> and does GEPER's own written 2015/2022-literature-based rejection of majority voting still
> hold?

---

## 5. What text should GEPER show for Benign / Likely Benign classifications? — currently deliberately empty

**Card:** `benign-replacement-guidance-is-a-question-for-the-clinical-expert` (status: waiting)

**What is settled:** The human already ruled the interim state: Benign/Likely Benign
recommendations stay **empty** rather than have an engineer invent plausible-sounding clinical
text. *"This being the most visible consequence of the positioning is the point, not a
problem."* The renderer prints "No specific recommendations generated." for this state —
**RECONCILED 2026-09-10: re-verified directly against current master,
`geper/report/report_generator.py:1129`, unchanged** — so a reader sees an honest empty section
rather than a silently missing one — a distinction that is invisible in the code but not on the
page. This ruling is final and is not itself part of what the expert is being asked.

**What is specifically being asked:** What should GEPER say, clinically, for a Benign/Likely
Benign classification, if anything beyond the current empty state? This is the replacement
text itself — a decidable question (write it, or rule that empty stays permanent), not a topic.

**What it blocks, concretely:** The Benign/Likely Benign section of every generated report
remains empty (by design, not by defect) until this is answered. No engineering work is
blocked; only the content of that report section is.

**Where the evidence is:** Card notes only — no file:line citation given for the renderer
behaviour beyond "confirmed by Kelly in a real render"; **RECONCILED: I independently
re-verified the exact rendered string directly against current source this pass** (see above),
which the 2026-09-08 version had not done.

> **Anchor:** none, and none is owed — this is a content-writing question about what GEPER
> should say, not a claim resting on any clause. **What is being asked of the expert, in one
> sentence:** should GEPER's Benign/Likely Benign section stay permanently empty, or is there
> safe, clinically appropriate text to show instead — and if so, what?

---

## 6. Does kim_pipeline need an actual review gate, or is an absent one a correct scope boundary? — depends on a product-scope question the intended-use statement left open

**Card:** `kim-pipeline-needs-a-real-review-gate-or-does-not-ship-clinically`

> **RE-SCOPED 2026-09-09 — THIS ITEM NO LONGER BELONGS IN THIS BOOKING.** One day after this
> brief was written, the card's assignee was changed from `clinical-expert (ROLE UNFILLED)` to
> `human`, with the note recorded verbatim: *"RE-LABELLED 2026-09-09: kim_pipeline review gate
> -- BLOCKED ON A HUMAN PRODUCT-SCOPE DECISION, NOT ON THE CLINICAL EXPERT. Booking the expert
> will not clear it."* **Retained here, marked, rather than deleted** — the question is still
> real and still blocking something, but the clinical-expert booking this document exists to
> prepare for will not resolve it, and presenting it to the expert would spend part of the
> booking on a question they cannot decide. The board reconciliation in this document's opening
> notice already accounts for this: the current eleven-card clinical-expert queue does **not**
> include this card, and this item is excluded from that count for that reason.

**What is settled:** This cannot be answered from the code: whether an absent review gate is a
defect or a correct scope boundary depends entirely on whether `kim_pipeline` ships clinically
at all — a question the intended-use statement settled only for the other tree, leaving this
one open. Answering it from the code would mean assuming the scope answer. The human
re-confirmed the deferral twice, the second time unprompted and more sharply: *"kim_pipeline
review gate stays deferred. Product scope is unresolved and I'm not settling whether
kim_pipeline ships clinically as a side-effect of a licensing thread."* Separately, and already
authorised regardless of this question: `kim_pipeline`'s PDF report currently carries a
signature block implying review where none occurs, which is wrong under either answer to this
question and is being removed independently (see related card
`kim-pipeline-pdf-carries-a-signature-block-that-gates-nothing`) — that fix does not wait on
this one.

**What is specifically being asked:** Does `kim_pipeline` ship as a clinical product? (If yes,
it needs a real review gate before it can. If no, an absent gate is not a defect and no gate
needs building.) **This is a question for the human, not the expert, as of 2026-09-09.**

**What it blocks, concretely:** Whether a review-gate feature gets built for `kim_pipeline` at
all, and whether its current absence of one should be read as a gap or as correct scope.

**Where the evidence is:** Card notes only, citing the human's two deferral statements
verbatim; the related signature-block card is authorised and in flight independently.

> **Anchor:** none — a product-scope question, not a regulatory one. **What is being asked, in
> one sentence, and of whom:** does `kim_pipeline` ship as a clinical product — a question now
> routed to the human directly, not to this booking.

---

## 7. Should GEPER surface patient zygosity, and if so, in what vocabulary and with what interpretive weight?

**Card:**
`DEFERRED-expert-gate-zygosity-reporting-decision-NABL-7-8-5-b-implies-it-GEPER-never-surfaced-it-VAF-fix-scoped-only`
(status: todo)

**What is settled:** NABL 112A Issue 01 s.7.8.5(b)(iii) requires a laboratory to define the VAF
range corresponding to heterozygous and homozygous states. GEPER currently surfaces **no**
patient zygosity call for any variant — measured directly: zero patient-zygosity call sites
across `geper/`, excluding tests and `hyena-dna`; all twelve grep hits for zygosity-adjacent
terms are gnomAD population homozygote counts, a sample-column-name comment, an FHIR hierarchy
mention, or explicit statements that GEPER does not infer phase/compound-het status
(`config.py:2415-2421`; `summary.py:1553-1554,1605-1606` — the original card cited
`summary.py:1547,1600`, which I re-checked directly and found had drifted a few lines since
the card was written; corrected here to the current location of the same text, verified
verbatim: *"GEPER has no phase data and does not infer compound heterozygosity or a cis/trans
relationship from this."*).

**What is specifically being asked, as five distinct decidable questions, all required:**
(1) Does GEPER report the raw GT string, a derived zygosity label, or both? (2) Which
vocabulary — `kim_pipeline`'s existing seven-value set
(heterozygous/homozygous_ref/homozygous_alt/hemizygous/no_call/multi_allelic/unknown,
`zygosity/extractor.py:123-164`) is a clinical vocabulary or an engineering one; which is it?
(3) Does zygosity change any ACMG criterion's weight in this build? (PM3/BP2 would want phase
information, which GEPER does not have and already states it does not infer.) (4) On a
multi-sample/trio VCF, whose zygosity is "the" zygosity? (5) Does a `no_call` render as a
zygosity value or as an absence — the same tri-state discipline already applied to the VAF
card?

**What it blocks, concretely:** All five must be answered before this ships; it is explicitly
not a plumbing question. The related VAF-range card ships **without** this — zygosity is
scoped out of it deliberately, not silently dropped.

**Where the evidence is:** `config.py:2415-2421`; `summary.py:1553-1554,1605-1606` (corrected
from the card's original `:1547,1600`, both verified directly);
`zygosity/extractor.py:123-164` (the seven-value vocabulary, verified directly — `no_call`,
`homozygous_ref`, etc. present exactly as cited). NABL 112A Issue 01 s.7.8.5(b)(iii) is an
external standard, not a repo citation.

> **Anchor: real, external, and — unlike ISO 15189:2022 broadly — this specific clause family
> is NOT paywalled.** `VALIDATION_STUDY_DESIGN.md` §0.3 records, for a related token (EJ-18):
> *"NABL 112A §7.8.5, the NGS clause EJ-18 cites, turned out to be free — confirmed
> 2026-08-31."* **This confirms the clause family is accessible, not that this exact
> sub-clause, s.7.8.5(b)(iii), was independently re-fetched and read in this pass** — I have not
> done that re-fetch myself this round, and say so rather than letting the sibling
> confirmation stand in for it. **What is being asked of the expert, in one sentence:** given
> NABL 112A requires a defined VAF-to-zygosity range, should GEPER surface patient zygosity at
> all, and if so in what form, vocabulary, ACMG weight, and multi-sample handling (the five
> sub-questions above)?

---

## 8. S2 (GIAB variant-calling validation) has no zero-tolerance failure anchor equivalent to S1's — inventing one would itself be setting an acceptance threshold

**Card:** `s2-variant-calling-has-no-threshold-independent-failure-anchor` (status: waiting)

**What is settled:** S1 (ACMG classification concordance) has F1: any clinically-opposed call
(E1) fails the study regardless of where thresholds land elsewhere, because Angela's rubric
1a falsifier already ratified it at zero tolerance. S2 (GIAB variant-calling) has no such
anchor — no ratified zero-tolerance falsifier exists for variant *calling* the way one exists
for classification. Vic named this gap himself, inside his own §5.9 addition, in two places,
rather than leaving it to be found — and did not supply one himself, because inventing a
zero-tolerance condition for variant-calling would itself be setting the acceptance threshold
the entire study design exists to keep out of engineering's hands.

**What is specifically being asked:** What is S2's threshold-independent failure anchor — the
GIAB variant-calling equivalent of "any E1 fails regardless of thresholds"? This requires
either a ratified zero-tolerance falsifier from the rubric owner (the same authority that
supplied S1's) or a direct human decision; neither is available to the engineering floor.

**What it blocks, concretely:** Every S2 failure condition is currently threshold-dependent,
meaning S2's failure conditions can in principle be tuned away by threshold choice — unlike
S1's F1, which cannot. The variant-calling arm of validation has no defensible, threshold-proof
definition of "this fails" until this is answered.

**Where the evidence is:** `VALIDATION_STUDY_DESIGN.md` §5.9.6 and §9 (where Vic states the
gap); rubric citation at `hive/ITEM5_config_knob_rubric_536-671.md`: *"A criterion without a
falsifier is not yet a criterion, it's a wish."*

> **Anchor: none — confirmed directly this pass, carrying the F5 finding into this item as
> instructed.** `VALIDATION_STUDY_DESIGN.md` §5.9 (which §5.9.6, cited above, sits inside)
> opens with its own explicit marker: **`"[INVENTED] -- all of §5.9. No external source
> supplies a study-level failure declaration for a variant-interpretation validation."`**
> quoted verbatim, verified against the current file this session. This is not a citation this
> brief lost — the document that defines S2's gap states, in its own words, that no external
> standard was ever available to supply the missing anchor. **What is being asked of the
> expert, in one sentence:** what threshold-independent failure condition should S2
> (variant-calling validation) use, since none exists in any published source and inventing one
> is an acceptance-threshold decision this floor has deliberately not made unilaterally?

---

## 9. F1's threshold-independent anchor has no defined equivalent for a ranking/prioritisation output — and the ratified prioritisation claim makes this live, not hypothetical

**Card:** `f1-has-no-ranking-equivalent-and-the-ratified-claim-makes-that-live` (status: waiting)

**What is settled:** Three independent analyses reached the identical gap without coordinating,
which is the strongest evidence available that it is real rather than an artifact of any one
of them: (1) the S2 gap above; (2) Vic's cost analysis of a hypothetical "Statement B"
(prioritisation framing), which found F1 has no defined equivalent for a ranking output — since
ratified, this is no longer a hypothetical cost, it is scheduled rework on the adopted
position; (3) Angela's rubric survey, which flagged that criterion 1a's falsifier is
precision-shaped (does GEPER's verdict match truth) while a prioritisation framing also demands
a recall-shaped question (does GEPER ever fail to surface a truly pathogenic variant at all) —
she believes 1a mostly subsumes this but is not confident, and correctly refused to decide it
herself, calling it validation-design territory rather than rubric territory. Under the ratified
claim, §5.5's E1–E6 error taxonomy and much of the arm structure are built on classification
agreement; the operative failure under a prioritisation product is ranking and surfacing. The
taxonomy does not reword onto that axis — it rebuilds on it.

**What is specifically being asked:** What is the ranking-appropriate threshold-independent
failure anchor — the equivalent of F1 for a prioritisation output rather than a classification
output? This is explicitly named as **currently unowned by any lane on this floor**, not merely
undecided: Vic says the replacement must come from the rubric (a readiness criterion, not a
study parameter); Angela says the recall question is study-design territory. Each lane points
at the other — precisely the shape of an unowned problem, per the human's standing instruction
to say so rather than assign by proximity.

**What it blocks, concretely:** Cannot be closed ahead of the remaining EJ-01 scope decisions
(what counts as "surfaced" depends on the reporting destination and review step, both still
open) — but is named here so it is not left silently unowned while those resolve.

**Where the evidence is:** `VALIDATION_STUDY_DESIGN.md` §5.1.1 (Vic's Statement B cost
analysis) and §5.5 (the E1–E6 taxonomy under discussion); Angela's rubric survey (referenced in
card notes, not independently re-read for this brief).

> **RECONCILED 2026-09-10 — this gap has since been given a name and a dedicated subsection,
> `EXPERT_PACKAGE/F5_EXPERT_BRIEF.md` (anchored today, merged `d1b4db8`), which this item's
> question directly overlaps.** That document traces this same gap through to `VALIDATION_STUDY_DESIGN.md`
> §5.9.8 (F5), and confirms the same `[INVENTED]` finding independently: no external source
> supplies a ranking-appropriate failure anchor either. It also surfaces a directly relevant,
> adjacent fact this item did not previously carry: EJ-25 (the recall/queue-depth question this
> gap feeds into) considered two real, verified citations — Richards et al. 2015 and CLIA
> §493.1253(b)(2) — and explicitly ruled them "verified-but-off-target" for a ranked-queue
> recall question, rather than silently omitting them. **Read the two documents together**:
> this item is the origin-and-ownership question ("who defines the anchor, and it's currently
> nobody's job"); the F5 brief is the fuller technical trace (the subsumption test, the code
> corroboration, the Arm A safeguard asymmetry) and the specific EJ-25 remedy question.

> **Anchor: none, confirmed twice now — independently, by two different passes on two different
> days, reaching the same conclusion.** Same `VALIDATION_STUDY_DESIGN.md` §5.9 `[INVENTED]`
> marker as item 8. **What is being asked of the expert, in one sentence:** who should own
> defining a ranking-appropriate, threshold-independent failure anchor (the rubric, the study
> design, or a decision this floor cannot make either way), and what should that anchor be?

---

## 10. On the F1/S2/ranking family of gaps: one procedural note for whoever reviews this brief

Items 8 and 9 above are two cards, not one — they were not merged, because they name two
distinct (if related) gaps: S2 has no failure anchor for variant-calling specifically; the
ranking/prioritisation axis has no failure anchor for *any* study arm once the prioritisation
claim is live. Both are currently unowned by design (see each item), and both trace back to the
same underlying rule cited in item 8. Flagging the relationship here rather than forcing a
merge that would obscure that they are asking two different questions.

**RECONCILED 2026-09-10:** this procedural note is unchanged in substance; the sibling document
now exists (see item 9's note) but does not merge items 8 and 9 either — it adds detail to 9
specifically, and the reasoning for keeping 8 and 9 separate stated here still holds.

> **Anchor:** none — this is a document-structure note about this brief, not a clinical or
> regulatory question. Nothing is being asked of the expert in this item.

---

*Boundaries observed while writing this: read-only against code and cards throughout. Did not
answer any of the ten questions above. Did not edit `tasks.json` or `board.md`. One extraction
script was used to pull all twelve matching cards from `tasks.json` in a single pass, per the
standing rule adopted this session.*

**Reconciliation boundaries, 2026-09-10:** read-only against code, `tasks.json`, and
`VALIDATION_STUDY_DESIGN.md` throughout this pass as well. Did not merge or rebase the
`clinical-expert-brief` branch — its content was read and carried forward; the branch itself is
untouched and its disposition is not decided by this document. Did not answer any of the ten
questions, including the two (4, 9) that gained substantial new material this pass. No ISO,
NABL, or CDSCO wording of this floor's own construction is offered anywhere above — every
quotation is either this repository's own text (code, `VALIDATION_STUDY_DESIGN.md`, card notes)
or a plain statement of a citation's verification status, never a paraphrase of an external
standard's own wording presented as that standard's text.
