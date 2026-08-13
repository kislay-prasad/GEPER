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
