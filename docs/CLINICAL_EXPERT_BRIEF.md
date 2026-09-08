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
(19 currently exist); (2) interpreting any concordance result, even after a number legitimately
exists — a measured number still needs professional judgement to mean "pass"; (3) — added
later and now the primary purpose — review of the drafted validation design **before**
anything is built against it: *"Even a few hours of their time to review the drafted design
before you build against it would be worth more than weeks of further engineering."*

**Where the evidence is:** `VALIDATION_STUDY_DESIGN.md` (Vic's 1132-line draft, written to
this exact audience); `VALIDATION_READINESS_RUBRIC.md` D1 (weight 30, currently scoring zero —
the domain this gate governs); `hive/DISPATCH-READY-validation-scientist.md` addendum 1
(assessor targeting, already implemented).

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

**What is settled:**
- SpliceAI was removed from `kim_pipeline` 2026-08-22 for licensing (CC BY-NC 4.0 pretrained
  models, GPL-3.0 code) — `kim_pipeline/pipeline/vep/stage.py:13-25` (verified directly: the
  docstring states this exactly).
- The PP3/BP4 vote-counting code still references SpliceAI as a source; the field is always
  `None` now, and each vote append is guarded `is not None`, so it silently drops out rather
  than raising — `kim_pipeline/pipeline/acmg/classifier.py:814` (PP3) and `:1171` (BP4
  mirror), verified directly.
- The majority threshold is computed over however many voters are actually present:
  `met = n_dam >= max(1, len(votes) // 2 + 1)` at `classifier.py:831` (PP3), mirrored for BP4
  at `:1188` — verified directly. **With SpliceAI present this required 3-of-4 damaging calls;
  with it silently absent, it now requires 2-of-3.** Nobody chose that as a clinical position;
  it is a side effect of removing one voter from a majority-of-present formula.
- **The system is running on this moved value (2-of-3) today.** `classifier.py`'s last commit
  is `c7b9859`, a *revert* of the change (`d1128ee`) that would have addressed it —
  `geper/REVIEW_MANIFEST.md:17-19`, verified directly. The held candidate fix,
  `_MIN_CONCORDANT_PREDICTORS = 2`, exists only in the shared uncommitted working tree, not in
  `HEAD` — `VALIDATION_STUDY_DESIGN.md:288-291`, verified directly.
- **On the specific claim that "a customer-facing document records it as awaiting this
  decision": I could not confirm this as stated, and am reporting that rather than dropping
  it.** I searched the actual report-rendering code that produces what a clinician or patient
  sees (`kim_pipeline/pipeline/reporting/pdf_report.py`, `geper/report/clinical_report_builder.py`)
  for any PP3/BP4/SpliceAI-specific disclosure text and found none — the only "pending" text
  in the real report builder concerns confidence/priority scoring generally, not this issue
  specifically. The documents that *do* record this as awaiting the decision are internal:
  `docs/BIJ_AI_CAPABILITY_AUDIT.md:379-396,577-605` (my own audit) and
  `VALIDATION_STUDY_DESIGN.md:288-334` (the assessor-facing validation design draft, not a
  patient- or clinician-facing report). Neither is what most people would call
  "customer-facing." This may be exactly the sentence god was told exists and has not himself
  seen — worth resolving before this brief is finalised for the expert, since the phrase
  changes how urgently this reads.
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
`_MIN_CONCORDANT_PREDICTORS`; `geper/REVIEW_MANIFEST.md`; `VALIDATION_STUDY_DESIGN.md:288-334`.

---

## 5. What text should GEPER show for Benign / Likely Benign classifications? — currently deliberately empty

**Card:** `benign-replacement-guidance-is-a-question-for-the-clinical-expert` (status: waiting)

**What is settled:** The human already ruled the interim state: Benign/Likely Benign
recommendations stay **empty** rather than have an engineer invent plausible-sounding clinical
text. *"This being the most visible consequence of the positioning is the point, not a
problem."* The renderer prints "No specific recommendations generated." for this state,
confirmed against a real render, so a reader sees an honest empty section rather than a
silently missing one — a distinction that is invisible in the code but not on the page. This
ruling is final and is not itself part of what the expert is being asked.

**What is specifically being asked:** What should GEPER say, clinically, for a Benign/Likely
Benign classification, if anything beyond the current empty state? This is the replacement
text itself — a decidable question (write it, or rule that empty stays permanent), not a topic.

**What it blocks, concretely:** The Benign/Likely Benign section of every generated report
remains empty (by design, not by defect) until this is answered. No engineering work is
blocked; only the content of that report section is.

**Where the evidence is:** Card notes only — no file:line citation given for the renderer
behaviour beyond "confirmed by Kelly in a real render"; I have not independently re-verified
the render output for this brief.

---

## 6. Does kim_pipeline need an actual review gate, or is an absent one a correct scope boundary? — depends on a product-scope question the intended-use statement left open

**Card:** `kim-pipeline-needs-a-real-review-gate-or-does-not-ship-clinically` (status: waiting)

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
needs building.)

**What it blocks, concretely:** Whether a review-gate feature gets built for `kim_pipeline` at
all, and whether its current absence of one should be read as a gap or as correct scope.

**Where the evidence is:** Card notes only, citing the human's two deferral statements
verbatim; the related signature-block card is authorised and in flight independently.

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
verbatim: *"Bij AI has no phase data and does not infer compound heterozygosity or a cis/trans
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

---

## 10. On the F1/S2/ranking family of gaps: one procedural note for whoever reviews this brief

Items 8 and 9 above are two cards, not one — they were not merged, because they name two
distinct (if related) gaps: S2 has no failure anchor for variant-calling specifically; the
ranking/prioritisation axis has no failure anchor for *any* study arm once the prioritisation
claim is live. Both are currently unowned by design (see each item), and both trace back to the
same underlying rule cited in item 8. Flagging the relationship here rather than forcing a
merge that would obscure that they are asking two different questions.

---

*Boundaries observed while writing this: read-only against code and cards throughout. Did not
answer any of the ten questions above. Did not edit `tasks.json` or `board.md`. One extraction
script was used to pull all twelve matching cards from `tasks.json` in a single pass, per the
standing rule adopted this session.*
