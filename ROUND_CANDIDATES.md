# Round candidates

Findings surfaced during a review round that were deliberately **not**
fixed in that same round -- either because the fix belongs to a
different decision than the one the round was scoped to make, or
because the decision itself needs a human call this document exists to
flag rather than resolve silently. Each entry names the round it was
found in, what it is, and what decision it's waiting on.

---

## Round 7 (QC-metrics wiring)

### 1. `kim_pipeline`'s generic `except Exception` in the QC stage creates a checkpoint-schema ambiguity

**What:** `kim_pipeline/pipeline/orchestration/runner.py`'s Stage 1b
(`qc`, the real `QCStage`) wraps `qc_stage.run(...)` in a generic
`except Exception: logger.warning(...); continue` (runner.py:409-412).
If `QCStage.run()` raises anything past its own internal error
handling, that `except` block logs a warning and moves on **without
ever executing** `checkpoint["qc_r1"] = result.qc_r1` / `checkpoint["qc_report_json"]
= ...` (runner.py:389-397). But `checkpoint["qc_r1"]` was already
written once, earlier, by Stage 1 (`fastq_validation`, always runs,
`FastqStats.to_dict()` -- no `q30_fraction` field at all). So a silent
QCStage failure leaves `checkpoint["qc_r1"]` holding Stage 1's older,
structurally different dict under the exact same key a caller might
expect Stage 1b's `QCMetrics.to_dict()` (which does have `q30_fraction`)
to have overwritten.

**Why this is a candidate, not a round-7 fix:** the round-7 fix
(`bridge/combined_pipeline.py::qc_metrics_from_kim_checkpoint`) works
around this ambiguity by checking `"qc" in checkpoint["completed_stages"]`
before ever trusting `checkpoint["qc_r1"]["q30_fraction"]` to exist --
that's a correct, narrow fix for the *consumer* side. It does not touch
`runner.py`'s error handling itself, because *what that handler should
do instead* is a separate decision (should a QC-stage failure be fatal
to the whole kim run instead of a soft-continue? should the checkpoint
explicitly record `"qc_failed": true` alongside `completed_stages`
rather than relying on the consumer to infer failure from an absence?)
that belongs to whoever owns kim_pipeline's own failure-handling
philosophy, not to the bridge/GEPER side making its best-effort read of
whatever kim produced.

**Decision needed:** should `runner.py`'s QC-stage exception handler
(a) keep soft-continuing but explicitly record the failure in the
checkpoint (e.g. `checkpoint["qc_failed_reason"] = str(qc_exc)`) so
downstream consumers don't have to infer it from `completed_stages`
absence, or (b) re-raise (matching `QCThresholdError`'s existing
hard-stop behavior just above it) on the theory that a QC stage that
crashes outright is a different, more serious problem than a QC stage
that ran and found genuine threshold violations?

---

### 2. Bases-at->20x coverage: permanently `NOT_RUN`, decide whether to build it or drop the row

**What:** `kim_pipeline` has no computation anywhere for "fraction of
bases at >=20x coverage depth" -- confirmed by tracing
`pipeline/alignment/bam_utils.py` (its `samtools coverage` call exposes
a "coverage" column meaning "% positions with depth>0", a different
question) and grepping the whole tree for `mosdepth`/`genomecov`/`20x`/
`bases_at`/`breadth` (all empty). Round 7 wires `bridge/combined_pipeline.py`
to report this metric as `NOT_RUN` with a reason naming the missing
tool step (`samtools depth -a` threshold count, or `mosdepth --thresholds 20`),
never fabricated and never left as a silent TODO.

**Why this is a candidate:** a row that is *permanently* inapplicable
on every single report, forever, is its own problem independent of
being honestly labeled -- it trains a reviewing scientist to skim past
that row the same way BLAST's permanent "Data-source version not
determinable" flag (see the round-5/6 provenance-honesty audit) trains
them to skim past that flag. An honestly-labeled permanent gap is
still a gap a reviewer stops reading.

**Decision needed:** either (a) actually add the missing coverage-
breadth step to `kim_pipeline`'s alignment stage (samtools depth -a
piped through a threshold count is the cheaper option; mosdepth is a
new external dependency), making this metric real, or (b) decide the
metric doesn't belong in the table at all if GEPER's own deployment
will never produce it, and drop the row entirely rather than carrying
a permanent NOT_RUN. Not decided by round 7 -- that would have been
scope creep past "wire what already exists."

---

## Round 8 (mitochondrial out-of-scope gate + reference-build forwarding)

### 1. The per-criterion compartment gate (option (b)) -- deferred, not abandoned

**What shipped this round (option (a)):** a single early gate in
`pipeline/orchestrator.py::GeperPipeline._process_variant` --
`is_mitochondrial_chrom(variant.chrom)` (see `pipeline/hgvs_utils.py`)
checked before normalization, sequence-context fetching, or any
evidence-source stage runs. A chrM variant never reaches the
28-criterion ACMG engine at all; it gets a minimal
`{"variant": ..., "out_of_scope": {"scope": "mitochondrial_genome",
"reason": ...}}` result instead, rendered distinctly (never
"Pathogenic"/"Uncertain significance", never "Not classified"/"Pending")
in the Markdown report, the full PDF's per-finding section AND its
Clinician Summary table, and the short PDF.

**What this defers:** a NOT_APPLICABLE status threaded through every
individual evidence source that has no validity on mtDNA, so a
mitochondrial variant could still be evaluated by whatever DOES apply
to it in principle (nothing currently does, but the distinction matters
for auditability -- "this criterion doesn't apply to this compartment"
is a different, more informative claim than "this variant was never
looked at"). Reuse `pipeline.stage_schemas.StageStatus` the same way
`report/summary.py::_parse_qc_metrics` (round 7) already does, rather
than inventing a fifth status vocabulary.

**Call sites option (b) would need to touch** (from this round's
investigation -- confirmed by grep, not assumed):
  - `pipeline/gnomad/provider.py` -- `_DATASET_BY_BUILD` is hardcoded
    to gnomAD's nuclear dataset ID (`gnomad_r4` for GRCh38); PM2/BA1/BS1
    in `pipeline/acmg_rules.py` all key off this.
  - `pipeline/mmsplice/`, `pipeline/models/ensemble.py` (Enformer/
    Borzoi), and the standalone SpliceFormer/SpliceBERT plugins
    (`_run_standalone_splice_plugin_stage`) -- all fetch a raw genomic
    sequence window directly, independent of transcript resolution,
    which is exactly why BP4 fired on MT:3243 despite "no transcript
    resolved for chrM" already being true for that run.
  - `pipeline/pvs1/` -- null-variant machinery, zero chrom-awareness
    (grepped, no hits).
  - `pipeline/ensembl/provider.py` -- transcript/gene overlap resolution
    against a nuclear-bootstrapped GTF cache; likely already degrades
    correctly for MT (no transcript resolves, so PM1/PM4/BP3/InterPro-
    dependent criteria already fall back to `not_evaluated` via their
    existing "protein_position is None" gates) but this needs
    confirming per-criterion, not assuming, before treating it as
    already-handled.
  - `models/alphamissense.py` -- see item 2 below, its own entry because
    it's a distinct finding, not just another item on this list.

**Why deferred:** per this round's own investigation report (accepted
by the user before implementation began), option (b) is roughly an
order of magnitude larger than (a) -- 8-10 independent call sites vs.
one gate -- and deserves its own Task-A-style scoping report before any
code is written, the same discipline this round itself followed.
Shipping (a) first stops the concrete harm (a MELAS-causing variant
reading VUS off two fabricated signals) without rushing (b)'s larger
audit. (a) does not foreclose (b): the compartment gate is a pure
early-return with no evidence-source-internal changes, so nothing about
it needs to be undone to build (b) later.

---

### 2. AlphaMissense has no chrM coverage either -- the ninth missing-vs-empty instance

**What:** `models/alphamissense.py::_normalize_chrom_for_catalogue`
explicitly maps `MT`/`chrM` variants toward `chrM` for its tabix
catalogue lookup -- defensive contig-naming robustness, not a
compartment decision. But AlphaMissense's own catalogue (Cheng et al.,
trained on nuclear-genome UniProt-canonical proteins) has no entries
for mitochondrially-encoded genes (MT-ND1, MT-CO1, MT-ATP6, ...) at
all. A chrM AlphaMissense query returns `not_found` -- which renders
today as "checked, nothing there," identical to a real negative lookup
against a source that genuinely could have answered.

**Why this matters for round 8 specifically:** this round's MT:3243
fixture never actually exercised this bug, because transcript
resolution already failed first and PP3/BP4's missense-eligibility gate
(which AlphaMissense also feeds) never got called at all for that
variant. That's not the same as the bug being absent -- a different
mitochondrial missense variant, on a gene/position where transcript
resolution for some reason *did* partially succeed, could still reach
AlphaMissense and get a silent, wrong "not found" read as a real
negative. This is the reason option (b) can't be dropped permanently
once option (a) ships: (a) only guards the chrM compartment as a whole
via the single gate at the top of `_process_variant` -- it happens to
also prevent this specific AlphaMissense case today, but only as a side
effect of blocking the whole variant, not because anyone fixed
AlphaMissense's own chrM blindness. If (a) is ever loosened or
bypassed for any reason without (b) having been built first, this gap
is immediately live again.

**Decision needed:** none yet -- this is scoping information for
whoever picks up item 1 (option (b)), not a standalone decision. Include
`models/alphamissense.py` explicitly in that round's call-site audit
rather than assuming its existing chrom-normalization code means it's
already handled.

---

### 3. kim_pipeline already has MT-aware annotation machinery -- read it first

**What:** `kim_pipeline/pipeline/annotation/mt_gff3_bootstrap.py` (an
opt-in, mitochondrial-only GFF3 auto-fetch bootstrap, because a
standard nuclear GTF doesn't cover MT genes) and
`kim_pipeline/pipeline/annotation/gff_index.py`'s `"NC_012920.1":
"chrMT"` mapping are real, already-working solutions to "how do I
resolve MT-TL1/MT-ND1/etc. gene/transcript structure from the rCRS
accession" -- built for kim's own VEP/annotation stage, not for GEPER,
but solving a closely related problem to GEPER's own "no transcript
resolved for chrM" gap this round's Task A investigation confirmed.

**Why this is a candidate, not a round-8 fix:** GEPER's compartment
gate (this round) makes the question moot for now -- a chrM variant
never reaches transcript resolution at all, so there's nothing to fix
in `pipeline/ensembl/provider.py` this round. But whoever eventually
builds option (b) (item 1 above) or decides mitochondrial transcript
resolution is worth adding to GEPER properly (rather than gating the
whole compartment out) will face exactly the "no standard GTF covers
MT genes" problem kim_pipeline already solved once.

**Decision needed:** none -- this is a pointer, not a choice. Whoever
picks up option (b), or any future work on GEPER-side mitochondrial
transcript resolution, should read `mt_gff3_bootstrap.py` and
`gff_index.py` first rather than re-deriving "MT needs its own GFF3,
a standard nuclear one doesn't have it" from scratch. Building parallel
machinery in GEPER when kim_pipeline already has a working answer to
the same underlying question would be redundant effort, not a genuine
architectural difference between the two projects.

---

## Round 10 (Tavtigian net-points exposure)

### 1. Whether `legacy_pre_acmg_significance_score`'s ClinVar-weight branch double-counts BP6's evidence -- untraced

**What:** `pipeline/interpretation.py::InterpretationEngine.interpret()`
computes its own pre-`ACMGRuleEngine` scoring system
(`legacy_pre_acmg_significance_score`, renamed this round from the
misleading `significance_score` -- see round 10's main fix). Its first
and heaviest-weighted term reads the variant's own matched ClinVar
record and adds `_SIGNIFICANCE_WEIGHT[sig]` (±3/±2/±1) for whatever
`clinical_significance` ClinVar reports.

`ACMGRuleEngine._combine`'s BP6 ("consistent with a benign ClinVar/
ClinGen classification") is verified -- confirmed in source, not
assumed -- to be structurally unable to reach `_combine`'s own
`net_points`: `acmg_rules.py`'s combining loop `continue`s past BP6
immediately after logging it to the trace, before either point
accumulator runs. That guarantee is round 10's whole point and is now
covered by `tests/test_acmg_net_points.py::
test_bp6_still_structurally_excluded_from_net`.

**What is NOT verified:** whether `legacy_pre_acmg_significance_score`'s
independent ClinVar-weight term is drawing on the *same underlying
fact* BP6 represents (ClinVar concordance with a benign call) for the
same variant, at the same time BP6 has also triggered. If so, a
variant could show BP6 triggered (correctly excluded from
`acmg_net_points`) while `legacy_pre_acmg_significance_score` still
moves in the benign direction from the very same ClinVar record BP6
cites -- not a bug in `_combine` (which never sees it), but exactly
the circularity concern `_bp6`'s own docstring and the ClinGen SVI 2018
PP5/BP6 deprecation warn against, now potentially reappearing one
layer over, in a field kept only as an exception-path fallback.

**Why this is a candidate, not a round-10 fix:** tracing this needs a
real matched-ClinVar-record variant where BP6 also triggers, run
through `InterpretationEngine.interpret()` end to end, and both
numbers compared -- round 10 was scoped to exposing `net_points`, not
auditing the legacy fallback path's own evidence sourcing. Also worth
weighing before investing effort here: `legacy_pre_acmg_significance_score`
is a fallback used only when the real ACMG rule engine raises
(`interpret()`'s own `except Exception: logger.exception("ACMG rule
engine failed; falling back to legacy summary only.")`) -- rare by
design, and confirmed this round not to be a documented LIMS export
field (`LIMS_EXPORT_MAPPING.md`) or to appear in any rendered report.

**Decision needed:** trace `_build_summary`/the ClinVar-weight branch
in `pipeline/interpretation.py` against a real triggered-BP6 variant
and determine whether the concern is real; if so, decide whether the
fallback path should independently exclude ClinVar-derived weight when
BP6 has triggered (mirroring `_combine`'s own exclusion), or whether
the fallback's rare, exception-only role makes that not worth the
added complexity.

---

## Round 11 (offline-verification harness investigation)

### 1. `transcript_structure` cold-cache cost -- ~1660s in a single call (Ensembl GTF bootstrap)

**What:** on a cold cache, the transcript-structure stage's Ensembl
GTF bootstrap alone took ~1660s (~28 of a 47-minute, 6-variant run) in
the Colab session used to verify round 9/10's `geper_results.json`
fixture. Every subsequent variant's transcript lookup was cheap once
the GTF was materialized; the cost is a one-time-per-cache-lifetime
bootstrap, not a per-variant cost, but it dominates any cold-cache
verification run's wall-clock time regardless of how small the VCF is.

**Why this is a candidate, not a round-11 fix:** round 11 was scoped
to investigating an offline path that avoids needing this bootstrap at
all for ACMG-engine/report-layer changes (see the round's report). A
faster or pre-warmed GTF bootstrap is a different, orthogonal
improvement that would still matter for any verification that
genuinely needs live transcript resolution (e.g. changes inside
`pipeline/ensembl/provider.py` itself, which recorded-evidence fixtures
can't cover).

**Decision needed:** whether it's worth pre-building and persisting a
warm GTF cache (e.g. committing a prepared cache artifact, or a
documented one-time Colab warm-up step reused across sessions) versus
just accepting the cost on the rare occasions a change actually
requires re-running this stage for real.

### 2. SpliceBERT has never loaded successfully -- hits `GEPER_SPLICEBERT_LOAD_TIMEOUT_SECS` on every run

**What:** in every observed run (local-disk cache path and Google
Drive cache path both), SpliceBERT hits the 180s
`GEPER_SPLICEBERT_LOAD_TIMEOUT_SECS` guard and is treated as a load
failure. The guard itself behaves exactly as designed -- the question
is that "treat as load failure" has become the permanent, 100%-of-runs
outcome rather than the rare exception the timeout was presumably
sized for. Two candidate root causes, neither confirmed: the timeout is
simply too short for this checkpoint's real load time, or the
transformers TensorFlow-backend-detection issue already described in
`build_model_and_tokenizer`'s own docstring is unresolved and adds
enough overhead to blow the budget regardless of its size. Separately:
the generated reports still list SpliceBERT in the model-checkpoint
manifest as though it were available, which is misleading given it has
never actually run.

**Why this is a candidate, not a round-11 fix:** diagnosing which of
the two causes (or both) is responsible requires a real, heavy,
network-and-model-loading Colab run instrumented specifically around
SpliceBERT's load path -- exactly the kind of run round 11's
investigation is trying to make verification *not* depend on for
routine ACMG/report-layer changes. Fixing the manifest's "available"
claim is a small, separate change but was left alone this round to
avoid mixing an unrelated report-accuracy fix into an investigation
task.

**Decision needed:** (a) get a real timed load of the SpliceBERT
checkpoint to see whether it's simply slower than 180s or genuinely
hanging on the TensorFlow-backend-detection issue; (b) once the cause
is known, either raise the timeout, fix the backend-detection issue, or
both; (c) separately, decide whether the model-checkpoint manifest
should stop listing SpliceBERT as available until it has a single
confirmed successful load.

### 3. The offline-evidence loader's schema guard covers 11 of ~19 captured evidence dicts -- 8 can still drift unnoticed

**What:** `geper/tests/fixtures/offline_evidence/loader.py::load_fixture`
validates every captured variant's evidence against
`pipeline/stage_schemas.py::RawEvidenceBundle` before handing it to a
test -- but `RawEvidenceBundle` only types 11 of the ~19 evidence dicts
`InterpretationEngine.interpret()`/`ACMGRuleEngine.evaluate()` actually
consume (clinvar, dbsnp, protein, blast, alphamissense, mmsplice,
gnomad, clingen, uniprot, interpro, alphafold). The other 8
(conservation, transcript, clinvar_codon, hpo, functional_evidence,
rna, ensemble, spliceformer/splicebert) have no Pydantic schema in
`stage_schemas.py` at all -- that module's own docstring documents this
as a deliberate scope decision ("Provider-internal shapes ... are NOT
typed here"), not an oversight the loader introduced. `load_fixture`
loads those 8 as-is, unvalidated, exactly as production itself treats
them.

**Why this matters:** this is the same class of risk the schema guard
was built to close (a recorded fixture silently no longer matching what
production's stages currently emit, passing green forever against a
shape production no longer produces) -- just narrower than the guard's
current coverage suggests at a glance. A future change to, say,
`pipeline/transcript`'s result shape, or a renamed key in the
functional-evidence result, would not be caught by
`load_fixture`'s validation at all; it would only surface as a
confusing downstream `KeyError`/`AttributeError` inside `interpret()`
itself (if the shape change is severe enough to break something) or,
worse, as a silently wrong `net_points` if the shape change is subtle
enough that `interpret()`'s `.get(...)`-based reads just quietly start
returning `None`/defaults instead of erroring.

**Why this is a candidate, not a round-11 fix:** closing this gap means
typing 8 more provider-internal shapes in `pipeline/stage_schemas.py`
-- exactly the "much larger effort" that module's own docstring already
called out and deferred when it was built (round 8-adjacent). That's a
scope decision for whoever owns `stage_schemas.py`'s typing effort, not
something to bolt onto the offline-evidence harness round in passing.

**Decision needed:** whether to extend `RawEvidenceBundle` (or add a
sibling schema) to cover some or all of the remaining 8 stage-result
shapes, prioritized by which ones actually feed `ACMGRuleEngine`
criteria most directly (transcript_result and clinvar_codon_result feed
PVS1/PS1/PM4/PM5/BP3 -- probably the highest-value targets) versus
accepting the narrower guard as "better than nothing" and relying on
`extract_fixture.py`'s regeneration-from-a-fresh-real-run discipline
(see this directory's README.md) to catch drift in the unguarded 8
instead.
