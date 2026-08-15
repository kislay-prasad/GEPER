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

---

## Round 11 (ClinVar variant-match investigation)

A reported bug -- ClinVar record UID 440440 (a GRCh37-titled large
deletion, `variant_match: null`) apparently being treated as the
attribution source for BRCA1 17:43106534's "Pathogenic (reviewed by
expert panel)" classification -- did not reproduce. Traced live against
the real captured run (commit `3c854c9`, this exact variant, read
straight from `geper/tests/fixtures/offline_evidence/raw_geper_results.json`
-- no pipeline run needed): `primary_record` is UID 37404, the correct
splice-acceptor SNV, `variant_match: true`, and every consumer
(`_bp6`, `_clinvar_crossref`, the interpretation's supporting-evidence
text, `report/clinical_report_builder.py::_clinical_evidence`) reads
`primary_record`/`matched_records`, never raw `records[]`, for
attribution. No code change came out of this round. Two things worth
recording anyway, surfaced while tracing it:

### 1. `variant_match: None` has no attached reason -- indistinguishable from "we didn't check," and `records[]` invites exactly the misreading that caused this round's own false alarm

**What:** UID 440440's title is `NC_000017.10:g.(41258551_41267742)_(41276114_?)del` -- an open/uncertain-breakpoint structural deletion (note the trailing `?`). ClinVar itself has no exact `canonical_spdi` for a variant it can't pin down to precise coordinates, so `ClinVarClient._variant_match` correctly returns `None` ("could not check") rather than `False` or a fabricated `True` -- confirmed correct, and confirmed to change no downstream output (a `None` is excluded from `matched_records` exactly as reliably as a `False` would be, since only `is True` qualifies). But nothing in the returned record explains *why* it's `None` -- a reader looking at raw JSON sees `"variant_match": null` with no reason attached, and has to go read `_variant_match`'s source to learn that ClinVar's own open breakpoints, not a shortcoming in GEPER's matching, are why this couldn't be checked.

Separately, and this is the part that actually caused this round's false alarm: `database/clinvar_client.py`'s own module docstring already states plainly that `records` is "kept for context/audit display... never for attributing a classification" -- and it was still misread as the attribution source during this investigation, corrected only by reading `primary_record` directly instead. If that misreading happened here, with the docstring available, a future reader doing a quick "does this variant have a ClinVar hit" scan by eyeballing `records[0]`/`records[]` is a predictable repeat of the exact `records[0]` bug this module's whole allele-matching design already exists to prevent (see the module docstring's own BRCA1 17:43094298 case).

**Why this is a candidate, not a round-11 fix:** both are clarity/auditability improvements with zero behavior change -- confirmed live that every current consumer already reads the right field and no classification is affected. This round was scoped to determining whether the reported mismatch was real; it wasn't, so no fix belongs to it.

**Decision needed:** (a) whether to attach a reason to the `None` case -- ESummary already carries enough (`variation_set[].variant_type`, or just the title's own `"del"` substring) to say something like "structural deletion with uncertain breakpoints; no exact allele to compare" instead of a bare `null`; (b) whether `records[]` is worth a stronger deterrent than the existing module-docstring note -- either an inline comment directly at `"records": records,` in `query_variant`'s return (a reader jumping straight to a JSON dump won't see the module docstring), or a rename to something less attribution-shaped (e.g. `all_candidate_records` -- bigger footprint, touches every current consumer/test reading the `records` key, so weigh that against just strengthening the comment).

### 2. Positional vs. rsID ClinVar search asymmetry: a deletion's full genomic span surfaces under every position it contains; an rsID doesn't carry that breadth

**What:** confirmed live (one read-only NCBI query, no pipeline run): `17[chr] AND 43106534[chrpos38]` returns `['440440', '373864', '245755', '54215', '37404']` -- including the two large deletions -- while `rs80358158[rs]` (the queried SNV's own rsID) returns only `['245755', '54215', '37404']`, the three actual point-substitution alleles at that locus. The deletions are absent from the rsID search because an rsID identifies one specific small variant, not a genomic span, while ClinVar indexes a large structural variant's `chrpos38` field across its *entire* covered range -- any position search landing anywhere inside a large deletion's span surfaces it, however far that position sits from the deletion's own titled/described locus. This is exactly why `query_variant`'s positional search will keep returning large structural variants as `records[]` noise at any position they span, and why `_variant_match`'s per-record allele check (not merely "was this returned by search") is load-bearing, not decorative.

**Why this is a candidate, not a round-11 fix:** documentation of existing, already-correct behavior discovered while investigating a bug that didn't reproduce -- no code to change, only knowledge worth not re-deriving next time this path gets traced.

**Decision needed:** none -- a pointer for future debugging, like round 8's item 3. Worth folding into `database/clinvar_client.py`'s module docstring or `_positional_search_term`'s docstring the next time that file is touched, so the explanation lives next to the code it explains rather than only in this document.

---

## Round 12 (SpliceBERT manifest investigation)

A reported bug -- SpliceBERT rendering as a bare identifier string in the AI
model-checkpoint manifest, indistinguishable from a model that ran, after it
failed to load on every one of three Colab sessions in one day -- did not
reproduce. The renderer was proven correct three separate ways: synthetically
(`build_ai_model_status` -> `rollup_run_status` -> `finalize_model_checkpoint_provenance`
run end-to-end against a fabricated FAILED-splicebert scenario), at the data
layer (tracing `_probe_standalone_plugins_at_startup` / `ModelManager.get` /
`_standalone_plugin_status` confirms a demoted SpliceBERT is correctly recorded
as FAILED with its real timeout message, not silently dropped), and directly
against the one real, complete artifact available -- a live, offline
`ReportGenerator._render_provenance` call against the commit-`3c854c9`,
6-variant `geper_results.json` (byte-identical to
`geper/tests/fixtures/offline_evidence/raw_geper_results.json`) rendered
SpliceBERT as `-- attempted, failed to load/run this run` with its full
180-second-timeout reason text, exactly as designed. No artifact found on disk
(3 real `geper_results.json` files checked) reproduces the reported bare
string. No code change came out of this round. Two things worth recording
anyway, surfaced while tracing it:

### 1. An interim checkpoint write is structurally indistinguishable from a completed run's manifest

**What:** `model_checkpoints` starts as plain identifier strings from
`get_model_checkpoint_identifiers()` (config-only, set once at
`orchestrator.py:485`). It is enriched with per-model run status EXACTLY ONCE,
via `rollup_run_status` + `finalize_model_checkpoint_provenance` at
`orchestrator.py:850-851` -- only after the entire per-variant loop finishes.
The periodic in-loop checkpoint write (`orchestrator.py:829-830`,
`CHECKPOINT_INTERVAL` default 25) writes `geper_results.json` BEFORE that
enrichment ever runs. So any file left behind by a run that disconnects
mid-loop (a Colab disconnect, a crash, a kill -9) shows bare identifiers for
EVERY model, not just SpliceBERT, and that file is structurally
indistinguishable from a genuinely completed run's manifest -- there is
currently no field anywhere in the document that says "this enrichment step
never ran." This is a genuine missing-vs-empty instance in the provenance
layer, the same class of bug `StageStatus`/`VersionStatus`/`ClinVarMatchStatus`
already exist to prevent elsewhere.

**This gap was found while investigating a reported bare-string manifest, and
it never reproduced that report.** It is not the fix for the round-12
SpliceBERT manifest bug, because that bug does not exist -- see this section's
own opening paragraph. This entry stands on its own as a real, independently
found gap, not as the explanation for round 12's original report.

**Why this is a candidate, not a round-12 fix:** round 12 was scoped to
determining whether the reported manifest bug was real; it wasn't, and this
gap is a different, adjacent finding surfaced while checking. It also affects
every model's manifest entry, not a SpliceBERT-specific fix, so it doesn't
belong bundled into a SpliceBERT-scoped change.

**Decided shape, if this is ever built:** an explicit `"run_complete": false`
boolean at the top level of `geper_results.json` on every interim write, set
`true` only on the final write after the post-loop
rollup/enrichment (`orchestrator.py:850-851`). Absent must read as `false`
everywhere it's checked -- no reader may default a missing key to `true`.

**Explicitly REJECTED:** a partial rollup at the periodic write (i.e., calling
`rollup_run_status`/`finalize_model_checkpoint_provenance` over whatever
variants have been processed so far, at the in-loop checkpoint itself). This
creates a second code path producing enrichment from incomplete data, and
"splicebert: FAILED, based on 12 of 300 variants" is exactly the half-true
provenance line twelve rounds have been spent eliminating. Recording the
rejection and this reason here so it isn't re-proposed later without this
context.

**Decision needed:** whether to actually implement the `run_complete` marker
(and the report-layer branch that reads it: "this run did not complete;
per-model status is unavailable" instead of rendering bare identifiers), or
leave this as a documented, understood gap until an interim file actually
causes real confusion.

**RESOLVED, round 17:** implemented exactly this decided shape after
re-confirming it against current source (ordering unchanged since round 12:
in-loop write still strictly precedes the post-loop
`rollup_run_status`/`finalize_model_checkpoint_provenance` enrichment).
`report/json_builder.py::JSONResultBuilder` gained a `run_complete: bool`
field, `False` by default (every interim write), set `True` by
`pipeline/orchestrator.py::run()` only after the enrichment call, immediately
before the final write. Confirmed round 12's rejected alternative (partial
rollup at the interim write) is still the wrong call, and that round 16's
`NotEvaluatedReason` doesn't change this decision -- that machinery
partitions per-criterion inapplicability reasons; this is a document-level
"did the post-loop enrichment step run" flag, a different question.
Also fixed the report-layer half: `report/report_generator.py::
_render_provenance` and `report/summary.py::_build_provenance_flowables`
(the 2 of GEPER's 3 renderers that touch `model_checkpoints`/`provenance` at
all -- `summary_short.py` renders neither, confirmed by grep) now print an
explicit "this run did not complete" notice ahead of the checkpoint list
whenever `run_complete` is falsy, including when the key is absent entirely
(a pre-round-17 file), via `bool(document.get("run_complete"))`, never a
bare `.get("run_complete", True)`. See `tests/test_round17_run_complete_marker.py`.

### 2. The `transformers` pin: no verified SpliceBERT run has ever used the committed environment

**What:** `git log -p --follow -- geper/requirements.txt` -- four commits ever
touch the file (`cb79edc` initial commit, `15150f8`, `b82bd52`, `99ec99d`). The
`transformers` line appears exactly once across all four, as a `+` addition in
`cb79edc`, never as a `-`/`+` pair. The committed pin has always been
`transformers>=5.12.1,<6.0.0`. There is no `4.56.2` revert target anywhere in
this repo's history and no "the pin moved" regression to point at.
`enformer`/`borzoi` are not pinned in `requirements.txt` at all -- both are
config-gated and auto-installed at runtime, with the file's own comment saying
so -- so the file carries no record of an enformer/borzoi-vs-5.x conflict
either.

**The actual point of this entry:** every run where SpliceBERT loaded fast
(`smoke_test_7`'s 40s load, the `fix/report-review-A1-B7` run) recorded
`transformers==4.56.2` "per requirements.txt" -- but that was never the
committed pin, in any commit, at any point. Those were almost certainly ad-hoc
`pip install "transformers==4.56.2"` overrides made in-session and recorded as
if they described the file. So SpliceBERT has arguably never been proven to
load successfully under the environment this repo actually declares --
`transformers>=5.12.1,<6.0.0`. This is a reproducibility hole, not a
regression: nothing here was ever working and then broken by a later commit.

**Why this is a candidate, not a round-12 fix:** confirming or fixing the
actual v5 load behavior needs a real GPU session with network access to
attempt a real checkpoint load -- explicitly out of scope for an investigation
round on an 8GB machine with no GPU/network.

**Decision needed / diagnostic for whoever next gets a working GPU session:**
1. Check whether `transformers.utils.import_utils._tf_available` still exists
   under the actually-installed `5.x`. If it does not,
   `_force_transformers_to_prefer_torch_over_tf`'s own `try`/`except` is
   silently falling back to the env-var-only (`USE_TF=0`) behavior its own
   docstring (Fix #1) already documents as insufficient once `transformers`
   has been imported elsewhere first (`models/esm2.py` always does, before
   SpliceBERT loads) -- and that would be the confirmed second cause of the
   100%-reproducing load hang.
2. Separately, note the unreproduced warning already flagged in
   `splicebert_plugin.py`'s own docstring ("You are using a model of type
   'bert' to instantiate a model of type ''"), observed under `transformers
   5.13.1` immediately preceding the two most recent verified failures --
   consistent with a second, distinct, transformers-v5-specific issue Fix #1
   was never verified against, independent of whether (1) above also turns
   out to be true.

---

## Round 14 (mtDNA compartment gate)

Replaced round 8's whole-variant rejection with per-criterion, per-evidence-
source compartment gating (B1: fixed the `chrom` normalization blocking MT
transcript resolution and proved real MT-ND1/MT-ATP6 transcript structure
resolves; B2: 7 criteria pre-marked `not_evaluated`-with-reason for any chrM
variant, 6 more for confirmed non-protein-coding mitochondrial genes,
AlphaMissense/gnomAD/MMSplice/Enformer/Borzoi/SpliceFormer/SpliceBERT never
queried for chrM at all, a per-finding disclaimer on every chrM finding in
all three renderers). Two things deliberately left out of this round's scope:

### 1. The deferred second pass: gnomAD-mtDNA, MITOMAP, a real mtDNA-specific PVS1/PM2, heteroplasmy

**What:** round 14's own Task A investigation (see that report) scoped this
round to evaluation + honest gating, explicitly deferring: gnomAD's separate
mitochondrial callset (a genuinely different query shape, not just a new
`dataset_id`), MITOMAP integration (bulk-file bootstrap, same shape as
ClinGen/HPO/Orphanet, not a per-variant API), a real mtDNA-specific PVS1
implementation (McCormick et al. 2020 defines it per gene class, not as a
parameter tweak on the nuclear framework), and any heteroplasmy-level,
maternal-inheritance, or tissue-distribution handling at all -- GEPER
collects none of that data today. The per-finding disclaimer this round adds
states all four gaps explicitly on every chrM finding so a reviewer is never
left to infer the scope boundary themselves.

**Why this is a candidate, not a round-14 fix:** each of these is a real,
separately-scoped data-source integration or algorithm reimplementation, not
a smaller version of this round's gating work -- round 14's own report
argued for shipping a narrow, honest gate over a broad half-right one, and
that argument applies here too.

**Decision needed:** prioritization and scope for a genuine second pass --
likely gnomAD-mtDNA first (same provider module already being touched,
narrowest new-integration footprint), MITOMAP and the mtDNA-specific PVS1
rebuild as larger, separately-scoped efforts, heteroplasmy last (it isn't
just a new source -- it changes what a "variant" even means for scoring,
since GEPER's whole model today is one classification per genomic
position/allele, not per allele-fraction).

### 2. Six more chrom-normalization copies found during B1 -- two were provably wrong, so the rest are suspect, not just untidy

**What:** while fixing `pipeline/ensembl/provider.py::_normalize_chrom`
(round 14, B1), a search for the same `chrom[3:] if chrom.lower().startswith("chr")
else chrom` pattern (or the equivalent `.replace("chr", "")` chain) found it
independently duplicated in at least seven places across the codebase. Two
were confirmed live, provably wrong for at least one real spelling before B1
fixed them: `pipeline/ensembl/provider.py::_normalize_chrom` itself (`chrM`/
bare `M` never matched Ensembl's `MT` seqname) and
`pipeline/clingen/utils.py::_fetch_overlapping_genes`'s `bare_chrom` (same
bug, live Ensembl REST gene-overlap fallback). The other six were left
unfixed, deliberately out of B1's scope (chrM transcript resolution only):
`pipeline/conservation/utils.py` (two occurrences), `pipeline/gnomad/utils.py`
(three occurrences), `pipeline/sequence_context.py`, `database/clinvar_client.py`,
`database/dbsnp_client.py`, and `pipeline/models/spip/loader.py` (this last
one adds a `chr` prefix rather than stripping one, so it's the same class of
duplicated-not-shared logic, not necessarily the same specific bug).
`models/alphamissense.py::_normalize_chrom_for_catalogue` was checked and
confirmed already MT-aware (correctly canonicalizes `M`/`mt`/`Mt`/`MT`) --
the one of eight that isn't suspect.

**Why this matters, stated plainly:** seven independent copies of what
should be one rule existed in this codebase, and two of them were
confirmed wrong. That is not "some copies happen to be untidy" -- with a
50%+ live-defect rate on the copies actually audited, the remaining six are
suspect until individually checked, not merely a duplication-cleanup
nicety. `pipeline/hgvs_utils.py::_strip_chr` is the one version of this
logic that is already correct (handles `MT`/`M`/`mt`/`Mt`, bare or
`chr`-prefixed) and is what B1 pointed `ensembl/provider.py` at instead of
writing an eighth copy.

**Why this is a candidate, not a round-14 fix:** round 14 was scoped to the
mtDNA compartment gate specifically; `pipeline/gnomad/utils.py`'s three
copies are moot for MT until the gnomAD-mtDNA integration above exists
(gnomAD is never queried for chrM at all as of this round), and auditing
the other four needs the same "does this actually get called with an MT
chrom value in a real code path today" tracing B1 did for
`ensembl/provider.py` and `clingen/utils.py` -- not assumed, not batched
into this round's already-large diff.

**Decision needed:** audit each of the six remaining copies for the same
`chrM`/bare-`M` mismatch, prioritized by whether the call site can actually
be reached with an MT-spelled chrom today (`sequence_context.py` and the
two database clients are read on every variant regardless of compartment,
so likely highest priority; `conservation/utils.py`'s two copies and
`gnomad/utils.py`'s three are lower priority until/unless MT variants reach
them for real); then decide whether to fix each in place or replace all
seven with calls to `hgvs_utils.py::_strip_chr`, consolidating to the one
correct implementation this project already has.

## Round 15 (chrom-normalization audit + fixes)

Task A audited every chrom-normalization site the round-14 B1 sweep found
(and re-derived the list independently, turning up four more than the six
round 14 logged: `annotation/thousand_genomes_sas.py::_resolve_rsid`,
`annotation/indigenomes.py`, `pipeline/gnomad/utils.py::variant_key`, and
`review/signoff.py::_bare_chrom` -- the latter two correctly bespoke,
not bugs). Task B fixed the five sites confirmed reachable-and-wrong for MT,
using the already-validated "enumerate the M-family spellings, map to this
site's own real MT token" pattern (`hgvs_utils.py::_strip_chr`,
`alphamissense.py::_normalize_chrom_for_catalogue`) rather than a shared
`normalize_chrom(chrom, target=...)` -- the targets genuinely disagree
(UCSC wants `chrM`, NCBI Entrez wants `MT`, Ensembl wants `MT`), so a mode
flag would just be an eighth way to pick the wrong target instead of an
eighth way to forget the M-special-case:

- `pipeline/conservation/utils.py::normalize_chrom` -- `"MT"` was producing
  `"chrMT"` (not a UCSC contig); fixed to `"chrM"`. Feeds three sub-providers
  (local bigWig, UCSC REST, MyVariant GERP).
- `database/clinvar_client.py` -- extracted a module-local `_entrez_chrom`
  helper (previously an untested inline `.replace("chr", "")`); confirmed
  live that NCBI Entrez's `MT[chr]` returns hits, `M[chr]` returns zero
  with a "phrase not found" warning.
- `database/dbsnp_client.py` -- same fix, same live confirmation
  (`MT[CHR]` vs `M[CHR]`), own module-local `_entrez_chrom` copy (not
  shared with clinvar_client's, matching this codebase's per-module-utils
  convention).
- `pipeline/models/spip/loader.py::build_minimal_vcf` -- fixed as defence
  in depth (`"MT"` was producing the non-existent `chrMT` BSgenome contig),
  but documented in-line as currently unreachable: SPiP's own vendored
  `getGenomeSequenceFromBSgenome.r` filters `chr=="chrM"` out of its
  transcriptome before SPiP ever runs, so no MT variant reaches a
  transcript match regardless of this function's output. No test claims
  SPiP-for-MT works end-to-end.
- `annotation/thousand_genomes_sas.py::_resolve_rsid` -- same Ensembl-
  overlap bug pattern round 14 B1 fixed twice already (`M`/`chrM` input
  stripped to `"M"`, Ensembl wants `"MT"`); fixed the same way.

Two sites were investigated and left alone, on evidence:

- `pipeline/gnomad/utils.py::normalize_chrom` and `::gnomad_variant_id` --
  confirmed wrong for MT (same bare-strip bug) but unreachable: gnomAD's
  main GraphQL/tabix API indexes no mitochondrial variants under any
  spelling, and round 14's compartment gate stops chrM from being scored
  off gnomAD anyway. Deliberately NOT fixed -- fixing an unreachable path
  just invites a test that passes for the wrong reason.
- `annotation/indigenomes.py` -- checked live this round (2026-08-14):
  `POST data.php` with `chrM-8993-T-G`, `chrMT-8993-T-G`,
  `chrM-3243-A-G` (m.3243A>G, the MELAS variant), and gene searches for
  `MT-ATP6`/`MT-CO1` all returned `{"mydata":[]}`, while the module's own
  documented nuclear sanity case (`chr1-10190-C-A`) returns real data on
  the same endpoint. IndiGenomes indexes no mitochondrial variants at all.
  Also separately moot: `annotation/thousand_genomes_sas.py`'s own
  docstring records that IndiGenomes was retired from GEPER's active query
  path entirely on 2026-08-08 (licensing), so `pipeline/orchestrator.py`
  never calls this module for any variant, MT or nuclear, today.

`pipeline/sequence_context.py::_normalize_chrom` was already correct for
the four documented real-world spellings but used an exact-case
`("M","mt","Mt")` tuple that missed casings like `"chrm"`/`"CHRM"`; made
case-insensitive while the file was open for this round's other fix,
per instruction not to "while I'm here" refactor sites that were already
correct in substance.

New coverage: `tests/test_round15_mt_chrom_fixes.py` -- module-local,
offline, one test class per fixed site, each asserting its own site's
real expected target token for all four MT spellings (not one shared
parametrized expectation, since the targets differ) plus a nuclear-
chrom-unaffected case per site.

---

## Round 17 (checkpoint run_complete marker)

Implemented round 12's decided `run_complete` shape (see that round's own
entry above, now marked RESOLVED) -- no new candidate from the fix itself.
One thing found while running this round's regression pass, unrelated to
the fix:

### 1. `test_provenance.py::TestOrchestratorStageProvenanceCapture::test_functional_evidence_source_routes_to_the_right_record` fails on a clean checkout, before any round-17 change

**What:** `python -m pytest tests/test_provenance.py -q` fails this one test
on `master` @ `60493fe` (confirmed via `git stash push -u` back to a fully
clean tree, no round-17 changes present) -- `fake_self.provenance.get(
"Functional evidence (MaveDB)").status` is `VersionStatus.NOT_CONSULTED`,
the test expects `VersionStatus.TIMESTAMP_ONLY`, for a
`functional_evidence_result={"found": True, "source": "mavedb"}` input. Not
caused or touched by round 17 -- none of this round's changes (`report/
json_builder.py`, `pipeline/orchestrator.py`, `report/report_generator.py`,
`report/summary.py`) touch functional-evidence routing or `VersionStatus`.

**Why this is a candidate, not a round-17 fix:** out of scope for a
checkpoint-provenance round; this is PS3/BS3 functional-evidence provenance
routing (`pipeline/orchestrator.py`'s stage-provenance capture, MaveDB
source specifically), a different subsystem than `model_checkpoints`.

**Decision needed:** whether this is a genuine regression (something
already broke functional-evidence provenance routing and nobody's run this
specific test since) or the test itself is stale against a since-changed
`VersionStatus` default. Needs someone to trace `_capture_functional_evidence_provenance`
(or wherever MaveDB routing actually lives) against what this test assumes,
which round 17 didn't have scope to do.

**RESOLVED, round 18:** stale test, not a regression. `git log --follow --
geper/tests/test_provenance.py` shows exactly one commit ever touched the
file (its creation, `2a9dd41`) -- `e54e3f6` (which replaced provenance
routing's `source`-branching with `consulted_sources`-based routing,
specifically to stop a genuinely-queried-but-empty MaveDB/ClinGen ERepo
from reading as never-consulted) never touched it, despite its own commit
message scoping the fix to two *other* test files
(`test_ps3_bs3.py`, `test_functional_evidence_provenance_capture.py`).
`git show e54e3f6 -- geper/pipeline/orchestrator.py` confirms the
`source`-branching this test relied on was deleted outright, not kept
alongside the new logic. Fixed by updating the one stale test to the
`consulted_sources` shape `test_functional_evidence_provenance_capture.py`
already uses (and correcting its ERepo assertion to match: ERepo is always
tried before MaveDB, so a real MaveDB match means ERepo was also genuinely
consulted, not left `NOT_CONSULTED`). Grepped for every other harness
exercising `_capture_stage_provenance` -- only these two files do, and the
other one was already on the current contract. This was the only stale
instance.

---

## Round 18 (stale-test cleanup + SpliceBERT TensorFlow-detection myth removal)

Part A fixed the round-17-logged stale provenance test (see that entry
above, now RESOLVED). Part B removed `pipeline/models/splicebert/loader.py
::_force_transformers_to_prefer_torch_over_tf` and the `USE_TF=0` line: both
confirmed live, under this repo's own `transformers>=5.12.1,<6.0.0` pin
(installed `5.13.1`), to be no-ops -- `transformers.utils.import_utils.
_tf_available` and `transformers.TFAutoModel` do not exist under v5 at all,
since v5 dropped TensorFlow support entirely. Rewrote
`build_model_and_tokenizer`'s docstring, the `_load_with_timeout` timeout
error message, `splicebert_plugin.py`'s catch-site comment, and
`tests/test_splicebert_loader_live.py`'s docstring/failure message (all of
which asserted the now-disproven "TensorFlow-backend detection" causal
story) to instead state only what's actually confirmed: the load succeeds
quickly in isolation (13s cold, 0.2s warm, 108/108 weights) but has
repeatedly stalled inside the full orchestrator process specifically,
correlated with (not yet isolated to) transformers' "You are using a model
of type 'bert' to instantiate a model of type ''" warning. TensorFlow
itself remains installed (MMSplice genuinely requires it) -- only the env
var and the internal-flag override were removed, never conflated with
removing TensorFlow from the project.

One more pre-existing, unrelated failure found while regression-testing
this change:

### 1. `test_splicebert_plugin.py::TestSpliceBERTLoadImpl::test_network_failure_is_sanitized` fails on a clean checkout, before any round-18 change

**What:** `python -m pytest tests/test_splicebert_plugin.py -q` fails this
one test on a clean tree (confirmed via `git stash push -u`, no round-18
changes present). The test asserts `str(ctx.exception) ==
"SpliceBERT model unavailable"` for a mocked archive-download
`ConnectionError`, but the actual raised message is
`"SpliceBERT model unavailable: archive fetch for checkpoint
'SpliceBERT.1024nt' failed (ConnectionError): could not reach zenodo.org"`
-- the code now appends the real failure detail (arguably an improvement
for diagnosability -- "sanitized" no longer means "detail-free"), and the
test's exact-match assertion was never updated to match. Not caused or
touched by round 18 -- this is `pipeline/models/splicebert_plugin.py`'s
download-failure path, a different branch than the timeout path this
round's fix touched.

**Why this is a candidate, not a round-18 fix:** out of scope for a
TensorFlow-detection-myth cleanup round; needs its own decision about
whether the current (detailed) message or the test's expectation (bare
"SpliceBERT model unavailable") is the one that should change.

**Decision needed:** update the test's assertion to match the current,
more diagnostic message (likely correct -- a bare "unavailable" with no
detail is a worse error for exactly the kind of live-Colab debugging this
project has repeatedly needed), or confirm the message was deliberately
meant to stay generic and fix the code instead.

**RESOLVED, round 20 -- round 18's own guess above ("likely correct... the
test's assertion" should change) was wrong; the code was.** The test's
name is exactly what it protects: `test_network_failure_is_sanitized`
asserts `"zenodo.org" not in str(ctx.exception)`, and the actual message
contained the literal string "zenodo.org" -- a mocked stand-in for what a
real `requests`-level `ConnectionError` renders as (the complete Zenodo
archive URL, via `HTTPSConnectionPool(host=...)`'s own `str()`). Traced
where that raised message actually goes: `ModelManager.get()` folds it
into `self._failed[key]`, which round 16/17's own work routes into
`model_checkpoints[...]["reason"]` in `geper_results.json` and from there
into the clinical report's "Data Source Provenance" section (Markdown and
full PDF) -- a document a reader outside this codebase may see. `git log
-L` on the exact lines identified the actual origin: commit `d48a829`
("Group F", well before round 18) promoted this detail from a
`logger.debug` call to the *raised* exception itself, specifically to fix
a real problem (the detail was invisible in real runs at debug level) --
but did so by leaking it into the report-facing message instead of just
raising the log level, which alone would have fixed the stated problem.
Fixed in `pipeline/models/splicebert_plugin.py`'s network-error branch:
full diagnostic detail (exception class + `str(exc)`) stays in the
`logger.warning(..., exc_info=True)` call (kept at warning level, so
`d48a829`'s real fix survives); the raised `RuntimeError` is the original
bare `"SpliceBERT model unavailable"` again. No test assertion was
loosened -- the code now honestly meets the contract the test always
asserted.

**Related, NOT fixed this round (flagged, not built):** the adjacent
timeout branch in the same method has the identical shape --
`raise RuntimeError(message) from exc` where `message` embeds `{exc}`,
and that `TimeoutError`'s own text (rewritten in round 18) includes
`checkpoint_dir`, a local filesystem path. No test currently asserts
anything about this branch's sanitization, so nothing here proves it's
wrong the way `test_network_failure_is_sanitized` proved the download
branch was -- but the same class of report-facing leak is plausible.
Left alone rather than fixed speculatively without a failing test to
anchor the fix against.

---

## Round 19 (--output-dir startup writability validation)

A real 51-minute Colab run finished all 6 variants and every stage, then
crashed on the very last line (`JSONResultBuilder.write()`) with
`FileNotFoundError` on `--output-dir`. Investigated before changing
anything, per the round's own instruction to verify the reported premise
against source first -- and the premise was half right, half wrong:

**Wrong:** "nothing on the startup path creates --output-dir." `git log -p
-S"os.makedirs(self.output_dir"` shows `GeperPipeline.__init__` has called
`os.makedirs(self.output_dir, exist_ok=True)` since the very first commit
(`cb79edc`) -- the directory genuinely was created (or already existed) at
process start.

**Right:** nothing checked it was *writable*. `exist_ok=True` only
confirms the path exists; it never attempts a real write. Given the
directory demonstrably existed at minute 0 (the writability check this
round adds would have passed then too) and the crash only surfaced at
minute 51, the most likely real cause is not "never created" but a Google
Drive mount going stale mid-run -- a well-documented Colab failure mode
that no startup-only check can detect or prevent (that would need a
re-check immediately before every write, explicitly out of this round's
scope: fail-fast at startup was what was asked for, not write-time
resilience). Recorded here plainly, per the round's own request, rather
than overclaiming the fix "would have saved this exact run" -- what it
does prevent is the more common case (a genuinely unwritable or mistyped
path, wrong from the start), turning that from an hour of wasted
processing into a few-second startup rejection.

Fixed: `utils/output_paths.py::ensure_writable_output_dir` (new,
dependency-light module -- stdlib + `utils.exceptions` only, importable
without `pipeline.orchestrator`'s TensorFlow/absl chain) creates AND
verifies `--output-dir` via a real write-and-delete probe, called from
`GeperPipeline.__init__` (before any model load or network call) in place
of the old bare `os.makedirs`. `main.py` now wraps `GeperPipeline(...)`
construction itself inside the existing `try/except PipelineError` block
(previously only `pipeline.run()` was wrapped), so this new startup
failure gets the same clean one-line message as every other
`PipelineError` instead of a raw traceback.

Checked whether the checkpoint write path has the same problem: yes,
identically -- `pipeline/orchestrator.py`'s in-loop periodic checkpoint
write and the final write both call `result_builder.write(json_path)`
against the exact same `json_path`/`self.output_dir`, so one startup
check protects both.

Checked whether `--output-dir` is the only such path argument: no, but it
was uniquely dangerous. `--blast-db`'s auto-build (`ensure_local_blast_db`)
and the BLAST disk cache directory (`_BlastDiskCache.__init__`) are both
already defensive -- both wrap their filesystem operations in try/except
and degrade gracefully (skip caching / fall back to remote-or-skip) rather
than raising. `--patient-meta`/`--qc-metrics-json`/`--phenotype-file` are
read-only inputs, already documented as never-fatal on a bad path.
`--output-dir` was the one write target whose failure is both uncaught
(raises straight out of `JSONResultBuilder.write()`) and late (only
surfaces whenever `write()` is actually called -- for a small run, only at
the very end).

New coverage: `tests/test_round19_output_dir_validation.py` -- offline,
no `pipeline.orchestrator`/`main` import (both cost ~218s/275MB via
TensorFlow/absl on this machine); direct behavioral tests of
`ensure_writable_output_dir` (creation, writability probe, cleanup,
file-where-directory-expected, mocked-unwritable-directory) plus
source-text ordering checks (the same technique
`test_round17_run_complete_marker.py` established) confirming
`orchestrator.py` calls the new check in `__init__` before `run()`, the
old bare `makedirs` line is gone, both write call sites share one
`json_path`, and `main.py` now wraps pipeline construction inside its
`try/except PipelineError`.

---

## Round 20 (SpliceBERT sanitized-error-message investigation + fix)

Resolved the round-18-logged `test_network_failure_is_sanitized` failure
(see that entry above, now RESOLVED) -- the message was leaking a real
URL into a report-facing field via `d48a829`, an old round; not the test
being stale. Fixed in `pipeline/models/splicebert_plugin.py`; full
account in the round-18 entry above.

Two things found while investigating, logged rather than fixed:

### 1. The adjacent timeout branch has the identical shape, unverified

See the round-18 entry's own "Related, NOT fixed this round" note --
`build_model_and_tokenizer`'s `TimeoutError` path also does
`raise RuntimeError(message) from exc` with `message` embedding `{exc}`,
and that timeout message (rewritten in round 18) includes `checkpoint_dir`,
a local filesystem path. No failing test anchors a fix here the way
`test_network_failure_is_sanitized` did for the download branch -- left
alone rather than fixed speculatively.

**RESOLVED, round 21.** Confirmed live, not speculative: a real
2026-08-14 clinical PDF printed the full `checkpoint_dir` path
(`/content/geper_cache/plugin_model_cache/splicebert/SpliceBERT.1024nt`)
inside its Data Source Provenance section, via the exact same
`ModelManager._failed` -> `model_checkpoints[...]["reason"]` -> report
route round 20 traced for the network branch. Fixed the same way --
full detail (including the path) stays in `self.logger.warning(...,
exc_info=True)` only -- but, unlike round 20's network-branch fix
(which dropped to a bare "SpliceBERT model unavailable"), the raised
message deliberately keeps the "this load succeeds quickly in
isolation but has repeatedly stalled specifically inside the full
pipeline process" sentence: that's round 18's own correction of a
disproven "TensorFlow-backend detection" story that misled this
project for months, and it names no path/URL/identifier, so dropping
it too would have silently undone a correction this project already
paid to make. New test:
`test_load_timeout_message_is_sanitized_but_keeps_the_isolation_correction`
in `tests/test_splicebert_plugin.py`.

### 2. A constraint violation: running `test_model_manager.py` loaded real Enformer weights and ran real inference (~400s)

**What:** while regression-testing the round-20 fix, `python -m pytest
tests/test_model_manager.py tests/test_splicebert_plugin.py -q` was run as
a combined background check. This machine's own hardware constraint
("nothing loading model weights") was checked for the SpliceBERT live
test specifically (grepped for `skipUnless`, confirmed the checkpoint is
cached and skipped running it), but the same check was not applied to
`test_model_manager.py`, which turned out to have the identical risk under
a different name: `TestPendingPluginsRegisterAsUnavailable
::test_manager_predict_never_crashes_for_any_pending_plugin` calls
`ModelManager(registry=build_default_registry()).predict("enformer", ...)`
expecting `None` (the plugin assumed permanently unavailable/"pending"),
but in this environment Enformer actually loaded (161.4s) and ran a real
prediction (231.3s, a genuine Enformer-official-rough inference result),
so the test's own assumption -- not this round's fix -- is what failed.
Total real compute cost of this one avoidable run: ~400s and a full
Enformer load/inference on an 8GB machine the constraints were meant to
protect. Disclosed here plainly rather than omitted; the actual
round-20 fix was independently verified beforehand via
`tests/test_splicebert_plugin.py` alone (27/27 pass, fully mocked, ~2s).

**Why this is a candidate, not a round-20 fix:** unrelated to the
SpliceBERT message fix -- this is `pending_plugins`'s own registration
list disagreeing with what's actually installed/loadable in this
environment. Also: no further test runs were attempted this round to
avoid repeating the same cost investigating it.

**Decision needed:** (a) whether `test_model_manager.py`'s "pending
plugin" test needs the same `skipUnless`-style guard (or a mock) the
SpliceBERT live test already has, so a future scoped/offline test pass
can't trigger a real multi-hundred-second model load by surprise; (b)
separately, whether Enformer now genuinely working in this environment is
itself news worth acting on (`pending_plugins.py`'s registry may be
stale about which plugins are actually available here).

**RESOLVED (a), round 21** -- fixed with a mock, not a skip: the test's
own class docstring already says these plugins "become available
automatically whenever their optional pip package/dependency is
installed", so a `skipUnless`-style guard would just reintroduce the
same environment-dependence (pass/fail/skip depending on what happens
to be pip-installed) that caused this in the first place. Re-read what
`test_manager_predict_never_crashes_for_any_pending_plugin`'s own NAME
actually promises -- "never crashes", not "returns None" -- and
`ModelManager.predict()`'s own docstring confirms that contract is
enforced entirely by `model_cls.is_available()` gating `.get()` to
`PluginUnavailableError` before any real load is attempted. Mocking
`EnformerPlugin.is_available`/`BorzoiPlugin.is_available`/
`SpliceFormerPlugin.is_available` to `False` tests that exact contract
deterministically, regardless of what's actually installed in the
environment running the suite -- confirmed fast (34/34 tests in
`tests/test_model_manager.py`, ~1s total, no pip installs or weight
loads triggered) after the fix, versus ~400s before it. (b) not
investigated this round -- Enformer being genuinely loadable here now is
still worth someone's separate attention, but is a `pending_plugins.py`
registry question, not a test-correctness one.

---

## Round 21 (SpliceBERT timeout-branch leak + stale Enformer-availability test)

Full account of both fixes is in the round-20 entry above (timeout-branch
leak) and directly above this line (the Enformer test). One thing checked
and NOT extended, to keep this round's fix matched to what was actually
verified:

### 1. Audited every sibling plugin loader for the same leak class -- SpliceBERT was the only one broken

Checked `spliceformer_plugin.py`, `enformer_plugin.py`, `borzoi_plugin.py`,
`spip_plugin.py` (the full `pending_plugins.py` registry SpliceBERT
belongs to) for the same "raw `{exc}` folded into a raised, report-facing
message" shape round 20/21 fixed. All four already raise a bare
`RuntimeError("<Model> model unavailable")` on their own network-error
branches, with full diagnostic detail kept in a `logger.debug(...,
exc_info=True)` call only -- SpliceBERT (both branches) was the one
outlier in its own plugin family, not a project-wide pattern. Also spot-
checked one raise site outside this family while auditing:
`pipeline/models/mmsplice/loader.py:234-237` embeds `h5_path`/
`package_dir` (local filesystem paths) directly into a raised
`ModelLoadError` the same way SpliceBERT's branches used to -- NOT
confirmed to reach the clinical report the way SpliceBERT's does (MMSplice
isn't routed through `ModelManager`/`pending_plugins`; would need tracing
`_run_mmsplice_stage` in `orchestrator.py` to confirm), and NOT fixed
speculatively without that confirmation. The broader `models/` package
(AlphaMissense, HyenaDNA, Evo2, RNA-FM, ESM2 -- a different registry/
family entirely) was not audited this round.

**Decision needed:** whether to trace the MMSplice path-leak claim to
confirm/deny it reaches a report, the same way round 20 traced
SpliceBERT's; and separately, whether the broader `models/` foundation-
model package deserves the same audit this round gave the splicing-plugin
family.

**RESOLVED, round 22.** Confirmed reachable -- traced the full chain
rather than assumed. `mmsplice/loader.py:234-237`'s `ModelLoadError` (and
three sibling raise sites in the same method, lines 207-212/216/244, all
embedding a local path or raw exception text) is raised from
`MMSpliceModel._load_impl()`, which `pipeline/orchestrator.py`'s startup
validation loop calls via `instance.predict(dummy_sequence)` for every
model in the (separate, older) `MODEL_REGISTRY`. That loop's `except
(ModelLoadError, ModelInferenceError)` branch stores `str(exc)[:600]`
verbatim in `self._model_stage_errors["mmsplice"]`, which
`pipeline/models/status.py::_mmsplice_status` returns as-is as the
`FAILED` reason in `build_ai_model_status()`'s output --
`report/report_generator.py::_render_ai_model_status` renders that into
the "### AI Models" table on **every single report, unconditionally**
(that method's own docstring: never allowed to return empty). A second,
narrower route also exists: `mmsplice/service.py::predict()`'s own except
block folds `str(exc)` into `skip_reason`/`interpretation`, rendered by
`_render_mmsplice` for a variant whose per-call prediction fails. Fixed
at the source (all four raise sites in `_load_impl`) rather than at each
downstream consumer, closing both routes at once -- full detail now goes
to `self.logger.warning(..., exc_info=True)` only; what's raised keeps
actionable, non-sensitive content (env var names, bare filenames) with no
path. New `tests/test_mmsplice_loader.py` covers the two sites reachable
without a TensorFlow import; the other two (a missing `.h5` file, a
generic Keras-load exception) apply the identical fix but weren't
separately exercised, to avoid forcing a TensorFlow import for marginal
coverage on this machine.

The broader `models/` foundation-model package (AlphaMissense, HyenaDNA,
Evo2, RNA-FM, ESM2) still was not audited this round -- genuinely out of
scope, not re-checked.

---

## Round 22 (MMSplice leak trace + pending_plugins registry audit)

Both parts answered the question asked before changing anything, per
that round's own instruction. Part A traced and fixed a real, confirmed
leak (see the "RESOLVED, round 22" note on round 20's entry 1, directly
above this heading). Part B is below -- an investigation that concluded
"no correctness issue, don't build a fix," which is exactly as valid an
answer as finding one.

### 1. `pending_plugins.py`: naming artifact, not a correctness bug

**What:** traced whether the module's name describes a real "pending"
status anything downstream branches on. It doesn't. The module's own
docstring already says so: "this module predates Enformer/Borzoi/
SpliceFormer having their own real integration modules... Enformer,
Borzoi, and SpliceFormer are no longer placeholders here." Grepped every
other use of "pending" in the codebase -- all of it is `confidence_pending`/
`priority_pending` (Phase 3/4 engine-completion flags, an entirely
unrelated concept) or bare references to this module's import path.
There is no `PENDING` value anywhere in the actual status vocabulary
(`pipeline/models/status.py` uses USED/SKIPPED/DISABLED/FAILED, per round
16-18's own work); `CONFIG.splicing.ENABLE_ENFORMER`/`ENABLE_BORZOI`
default to `true`; `EnsembleManager` (`pipeline/models/ensemble.py`)
explicitly combines "every currently *available*" plugin via a live
`is_available()` check with no hardcoded exclusion, and its 0/1/2-models
routing rules already feed `pipeline/acmg_rules.py`'s PP3/BP4 directly --
Enformer/Borzoi becoming genuinely loadable in this environment is
exactly the case this machinery was built to handle, not an edge case it
mishandles. The one place "pending" caused an actual bug was
`test_model_manager.py`'s own stale assumption, already fixed in round 21
-- not this registry or anything downstream of it.

**Why this is a candidate, not a round-22 fix:** the finding itself
*is* the answer requested -- "establish what it affects before proposing
a change." Nothing downstream (routing, provenance, the model-status
rollup) makes a decision based on a plugin being registered in this
particular file; only the file's own name is stale relative to its
current contents, and its own docstring already discloses that. Renaming
it touches every file that imports `pending_plugins` (`ensemble.py`,
`manager.py`, `spip_plugin.py`, `splicebert_plugin.py`,
`spliceformer_plugin.py`, `orchestrator.py`, plus every test that imports
`build_default_registry`) for a purely cosmetic gain -- not something to
do inside a round scoped to answering a question.

**Decision needed:** whether a filename this stale (its own docstring
apologizing for its own name) is worth the multi-file rename anyway, or
whether the existing docstring disclaimer is sufficient and this should
just stay closed.

---

## Round 23 (Protein Knowledge duplicate sentence; 1000 Genomes SAS URL leak)

Both parts of this round were fixed, not left open -- this entry exists
for the one thing verified but deliberately not fixed: the same
raw-exception-into-report leak class round 20-22 fixed for SpliceBERT/
MMSplice turns out to also affect UniProt, InterPro, ClinGen, and
AlphaFold DB, on top of the 1000 Genomes SAS instance this round fixed.

### 1. UniProt/InterPro/ClinGen/AlphaFold all embed the raw request URL (and, for InterPro/ClinGen/AlphaFold, the wrapping provider's raised text) in the `error` string a report renders

**What:** `report/report_generator.py` renders `prot['uniprot_error']`,
`prot['interpro_error']`, `struct['error']` (AlphaFold), and
`clin['clinvar_error']`/`clin['clingen_error']` the same way this round's
Part B fixed for `sas_error` -- `f"... (external service issue: {X})."`
with no sanitization at the render site (`report/summary.py` renders the
PDF equivalents the same way). Traced each `X` back to its source, the
same way this round traced `sas_error`:

- `pipeline/uniprot/provider.py:217` raises
  `ExternalAPIError(f"UniProt REST API request to '{url}' failed after
  {N} attempts: {last_error}")`; `provider.py:191` and `:280` both fold
  that straight into `UniProtAnnotation.from_error(gene_symbol,
  str(exc))` / `f"{provider.name} raised: {exc}"` -- URL and raw
  exception both reach `uniprot_error`.
- `pipeline/interpro/provider.py:171` raises the identical shape
  (`"InterPro REST API request to '{url}' failed after {N} attempts:
  {last_error}"`); `:210` folds it into `interpro_error` via
  `f"{provider.name} raised: {exc}"`.
- `pipeline/clingen/provider.py:343` and `:482` -- same shape, feeding
  `clingen_error`.
- `pipeline/alphafold/provider.py:217` and `:291` -- same shape, feeding
  AlphaFold's `struct['error']`.

Not traced end-to-end to a live-run PDF this round (unlike `sas_error`,
which a real report -- GEPER-RUN-20260815T11442 -- was confirmed to
print) -- traced from source to the renderer's known unconditional
render calls only, the same standard round 22 applied to MMSplice's
sibling raise sites before fixing all four at once.

**Why this is a candidate, not a round-23 fix:** the round was scoped to
the one instance a real report actually showed (1000 Genomes SAS) plus
the Protein Knowledge duplicate-sentence bug, both fixed. This is four
separate provider modules, each with 2+ raise sites, each needing the
same source-level sanitization (short message raised, full detail with
the URL to `logger.warning(..., exc_info=True)`) plus a regression test
per module -- a materially larger change than fixing the one instance
actually observed, and better done as its own scoped round the same way
round 22 gave MMSplice its own round rather than folding it into round
21's SpliceBERT fix.

**Decision needed:** whether to schedule a dedicated round applying the
identical fix-pattern to all four provider modules at once (they share
the exact same shape, so one round could plausibly close all four), or
fix them one at a time as each surfaces in a real report the way SAS did
this round.

### 2. ReportLab's `Paragraph` silently mangles any unescaped `&` in report text -- not a bug in this codebase's own rendering logic, but a real hazard for any future f-string containing one

**What:** the "mangled `content-type;=`" artifact the round's own
instructions flagged was reproduced directly, not assumed: feeding
ReportLab's `Paragraph` the literal leaked URL text
(`...?pops=1&content-type=application%2Fjson`) and reading back
`Paragraph.getPlainText()` returns
`...?pops=1&content-type;=application%2Fjson` -- confirmed via a
standalone repro against the installed `reportlab` package, not
inferred. ReportLab's paragraph parser treats any unescaped `&word` as
an attempted (unterminated) XML entity reference and silently appends
the missing `;` rather than raising, which is exactly the `content-
type` -> `content-type;` transformation observed in the real PDF.
Grepped `report/` for an XML-escaping helper (`saxutils.escape`,
`html.escape`, a local `_escape`/`xml_escape` function) applied before
any `Paragraph(...)` call anywhere in `report/summary.py` or
`report/summary_short.py`: there is none. Every interpolated value
reaches `Paragraph` unescaped.

**Why this is a candidate, not a round-23 fix:** this round's actual
manifestation of the artifact (the leaked SAS URL) is already gone once
`sas_error` no longer contains a URL -- fixing the leak at the source
(this round's Part B fix) removes the only unescaped `&` currently
reachable through that render path, so there is nothing left to
mangle for this specific string. But the underlying gap -- no XML-
escaping discipline anywhere before a `Paragraph(...)` call -- is
structural, not specific to 1000 Genomes SAS, and would resurface the
instant any other rendered field (a gene symbol, an HGVS string, free-
text from an external source) happens to contain a bare `&`. Adding a
blanket escaping helper touches every `Paragraph(...)` call site in
`report/summary.py` (dozens) and is a correctness-hardening change
independent of anything this round's PDF actually showed -- out of
scope for a round whose two parts were both about specific, observed
rendering defects.

**Decision needed:** whether to add a shared `_escape_for_paragraph`
helper (wrapping `xml.sax.saxutils.escape`, called at every
`Paragraph(...)` site in `report/summary.py`/`report/summary_short.py`)
as its own round, given it's pure hardening with no currently-observed
failure now that item 1's four providers are the only other known
source of unescaped `&`-bearing text reaching those call sites.

---

## Round 24 (UniProt/InterPro/ClinGen/AlphaFold URL leaks; XML-escaping gap re-assessed)

### 1. `RESOLVED, round 24.` UniProt/InterPro/ClinGen/AlphaFold DB's raw-URL leak, traced end to end and fixed

Round 23's item 1 above was traced, not assumed, exactly the way round
22 traced MMSplice before fixing it. For each of the four:

- `pipeline/uniprot/provider.py::LiveAPIUniProtProvider._get` (raise
  site) -> `query()`'s `except ExternalAPIError` folds `str(exc)`
  straight into `UniProtAnnotation.from_error` -> `.to_dict()`'s
  `"error"` key -> `report/clinical_report_builder.py::
  _protein_knowledge`'s `uniprot_error` -> rendered unconditionally by
  `report/report_generator.py` ("### 8. Protein Knowledge" runs for
  every finding, no gate) -> ALSO embedded verbatim in
  `geper_results.json`'s top-level `uniprot` key by
  `report/json_builder.py` (both the primary shape and the
  `raw_evidence`-fallback shape). Two independent reach paths, both
  unconditional.
- `pipeline/interpro/provider.py::LiveAPIInterProProvider._get` -- same
  shape, feeding `interpro_error` / `geper_results.json`'s `interpro`
  key.
- `pipeline/clingen/provider.py::LiveAPIClinGenProvider._get` -- same
  shape, feeding `clingen_error` / `geper_results.json`'s `clingen`
  key.
- `pipeline/alphafold/provider.py::LiveAPIAlphaFoldProvider._get_json`
  -- same shape, feeding `struct['error']` (AlphaFold DB) /
  `geper_results.json`'s `alphafold` key.

Unlike SAS (round 23) and MMSplice (round 22), none of these four were
confirmed printed in an actual live-run PDF this round -- but the
Markdown/JSON reach is unconditional and mechanically identical to
SAS's, so "reaches a rendered report or geper_results.json" is
satisfied for all four, not just the subset that happened to have a
real PDF example. All four had no dedicated raise-site logging of the
full URL before the sanitized message, unlike SAS's two-site case --
added one `logger.warning(...)` immediately before each sanitized
raise, so full detail (URL + `last_error`) is not lost, only no longer
raised/returned. No existing test fixture in any of the four providers'
own test files pinned the raw URL/exception text (checked before
fixing, same as round 23's instruction) -- new regression tests added
to `tests/test_uniprot_provider.py`, `test_interpro_provider.py`,
`test_clingen_provider.py`, `test_alphafold_provider.py` (one new test
for AlphaFold, which had no existing network-failure test at all).

**Found but explicitly out of scope, not fixed:** `database/
clinvar_client.py` (lines ~486, ~515) has the exact same shape --
`raise ExternalAPIError(f"ClinVar request to '{url}' ...")`, reaching
`clinvar_error` the same way `clingen_error` does. Not one of the four
this round was scoped to ("UniProt, InterPro, ClinGen and AlphaFold
DB"); left alone rather than fixed speculatively, matching this round's
own instruction to "fix three honestly rather than four speculatively"
applied one module further out. Flagged here as the next honest
candidate, same fix pattern as the four just closed.

### 2. XML-escaping gap (round 23, item 2): re-assessed and CONFIRMED LIVE -- higher severity than originally scoped

Round 23 logged this as a dormant, purely structural gap ("no
currently-observed failure"), reasoning that the SAS leak was the only
known unescaped-`&` source and it was about to be fixed. This round
re-checked whether anything else still reaching `Paragraph(...)` after
both leak-fixing rounds (23 and 24, item 1 above) carries external,
unescaped free text. It does, and it's worse than the SAS artifact:

**`review/signoff.py::override()`'s `reason` parameter** -- a
clinician's own free-text justification for overriding GEPER's
classification (its own docstring: `override(output_dir, variant_key,
new_classification, reason, clinician_id)`), stored verbatim in
`geper_results.json` with zero sanitization, then interpolated
unescaped into a `Paragraph(...)` call in BOTH PDF renderers:
`report/summary.py:1982` (full PDF) and `report/summary_short.py:524`
(short PDF) -- `f"... Override: {override.get('new_classification')} --
{override.get('reason')} ..."`. `override()`'s own docstring confirms
both PDFs (plus the Markdown report) are regenerated from this call
every time a clinician runs the override command, so this isn't a rare
path -- it fires on every override, by design.

Reproduced directly against the installed `reportlab` package what an
unescaped clinician-typed `&`/`<`/`>` actually does here (not
inferred):

- `"Per company R&D findings"` -> renders as `"Per company R&D;
  findings"` -- the same silent-mangling artifact round 23 found in the
  SAS URL, now reachable via a human-typed sentence rather than a
  machine-generated one.
- `"See <this> for detail"` -> renders as `"See  for detail"` --
  `<this>` is silently DELETED WHOLESALE, not mangled. A clinician's
  own words can vanish from their own override justification with no
  error, no warning, nothing in the log -- worse than mangling, because
  mangling is at least visible as garbled text; deletion looks like
  nothing was ever there.
- `"Confirmed pathogenic <br> per review"` -> raises `ValueError:
  paragraph text '<para>Confirmed pathogenic <br> per review</para>'
  caused exception paraparser: syntax error: No content allowed in br
  tag` -- a clinician's override text containing something that happens
  to match one of ReportLab's own recognized tag names (`<br>`, `<b>`,
  `<i>`, `<font>`, ...) with syntax ReportLab doesn't accept CRASHES
  report regeneration for that variant, for every one of the three
  report formats `override()` regenerates in the same call.

**Why this is reported, not fixed, this round:** the round's own
instruction was explicit -- "don't build it speculatively; tell me if
it's live." It's live, confirmed by direct reproduction rather than
inferred from the presence of a gap; whether to fix it now (and how
broad a fix -- just this one call site's two `Paragraph(...)` calls, or
the shared `_escape_for_paragraph` helper round 23 already scoped for
every call site in both files) is a decision being handed back rather
than made unilaterally, per that instruction.

**Decision needed:** whether to fix `override()`'s two render call
sites specifically (narrowest fix, addresses the one confirmed-live
path), or build the shared escaping helper round 23 scoped (broader,
also closes off gene/condition/protein free-text fields should any of
those ever get added to either PDF's rendered fields in the future,
which they currently don't -- both PDFs were checked this round and
render no ClinVar condition/disease/review-status text and no
UniProt/InterPro protein/domain text at all, only the QC-metrics/
Indian-population-frequency/AI-model-status error strings already
covered by rounds 20-24's fixes, plus this clinician-override field).
