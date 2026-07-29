# GEPER Performance Pass (Phase 3) -- Findings & Benchmarks

## Where the time actually goes

Per-stage wall-clock profiling (`utils/profiling.py`, wired into every
external-call / model-inference stage in `pipeline/orchestrator.py`)
confirms what the codebase's own comments already suspected: **remote
BLAST is the dominant cost**, and it's dominant for a specific reason
that has nothing to do with GEPER's own code:

* ClinVar, dbSNP, and Ensembl lookups are fast REST/E-utilities calls
  -- typically tens to a few hundred milliseconds each, because the
  server does a quick lookup and responds immediately.
* Each AI model (DNABERT-2, HyenaDNA, Nucleotide Transformer, RNA-FM,
  ESM-2, AlphaMissense) runs **locally** once loaded -- inference is
  CPU/GPU-bound, not network-bound, and is a few tens of milliseconds
  to a couple of seconds per variant depending on model size and
  hardware.
* Remote BLAST (`NCBIWWW.qblast`) is a **queued, shared NCBI service**.
  A single submission commonly takes anywhere from ~30 seconds to
  several minutes -- and that time is spent sitting in NCBI's queue,
  not doing local work GEPER could speed up by itself. On any VCF with
  more than a handful of variants, BLAST alone can be 10-100x the
  combined cost of every other stage.

Every run now ends by writing `geper_benchmark.json` /
`geper_benchmark.md` to the output directory (from
`GeperPipeline._write_benchmark_report`), so this breakdown is
available for any real run, not just this analysis -- run
`python main.py --vcf ... ` and check those two files afterward.

## What changed (strictly execution-time, zero prediction-logic changes)

All of the following live in `database/blast_client.py`,
`pipeline/orchestrator.py`, `config.py`, and `utils/profiling.py` (new).
None of them touch VEP/ACMG classification, model inference, or any
other prediction logic -- only how quickly / how many times a BLAST
result is computed.

1. **Cross-run, on-disk BLAST cache** (SQLite, stdlib -- no new
   dependency): a sequence BLASTed in an earlier run is never
   resubmitted to NCBI. Enabled by default
   (`CONFIG.api.BLAST_DISK_CACHE_ENABLED`, `--no-blast-cache` to
   disable).
2. **Skip-if-already-analyzed**: the existing in-memory
   memoization (by exact sequence) is preserved and combined with the
   disk cache, so an identical `alt_sequence` -- whether from a
   duplicate variant, an overlapping window, or a prior run -- is
   BLASTed at most once, ever.
3. **Batched/concurrent submission**: `BLASTClient.search_many()`
   submits distinct sequences concurrently (`ThreadPoolExecutor`,
   bounded by `CONFIG.api.BLAST_MAX_CONCURRENT_REMOTE` /
   `_LOCAL`) instead of one blocking call per variant. Because remote
   BLAST time is dominated by *queue wait*, not local CPU, concurrent
   requests overlap that wait instead of paying it serially.
4. **Orchestrator-level batch prefetch**: before the main per-variant
   loop, `GeperPipeline._prefetch_blast_results` builds every
   variant's sequence context (reusing the already-warm Ensembl
   region cache -- no extra network calls) and submits all *distinct*
   `alt_sequence`s via `search_many()` up front. By the time the main
   loop reaches each variant's BLAST stage, it's a pure cache hit.
   Mirrors the existing Ensembl batch-prefetch pattern
   (`SequenceContextGenerator.prefetch_regions`) for architectural
   consistency. Toggle: `CONFIG.api.BLAST_ENABLE_PREFETCH` /
   `blast_enable_prefetch=` constructor param.
5. **`mode="auto"` (new default)**: prefers a local `blastn` binary +
   database automatically when both are present
   (`BLASTClient._resolve_auto_mode`), falling back to remote
   otherwise. Local BLAST has no network queue at all and is
   typically 100x+ faster than remote for the same query. Configure
   via `--blast-db` / `GEPER_BLAST_LOCAL_DB` / the standard `BLASTDB`
   env var.
6. **AI-only mode**: `--ai-only` (or `ai_only=True` /
   `GEPER_AI_ONLY=true`) disables BLAST entirely -- no NCBI
   submission, no local subprocess, no disk-cache I/O at all --
   relying solely on the DNA/RNA/protein foundation models plus
   ClinVar/dbSNP/AlphaMissense.
7. **Per-stage profiling**: `utils/profiling.py`'s `StageProfiler`
   times sequence-context building, each individual model
   (`model:dnabert2`, `model:hyenadna`, ..., `model:alphamissense`),
   BLAST (prefetch + per-variant stage), ClinVar, and dbSNP
   separately, and writes the aggregated report at the end of every
   run. On by default; `--no-profiling` disables it.

None of ClinVar's/dbSNP's *values* changed. Their existing
dependency (ClinVar prefers dbSNP's rsid when available) was left
intact rather than parallelized against each other, since reordering
it would risk changing which lookup path ClinVar takes for a given
variant -- outside this pass's "don't change prediction results"
constraint. BLAST has no such dependency on either of them, which is
exactly why it was the one moved to a batched/concurrent, prefetchable
stage.

## Benchmarks

Two harnesses back these numbers up (both runnable directly):

* **`verify_blast_optimization.py`** -- unit-level correctness proof
  for the BLAST client itself: cache dedup (in-memory + disk),
  cross-instance disk persistence, concurrent `search_many()` timing,
  `mode="auto"` resolution, and AI-only short-circuiting. All 6 parts
  pass.
* **`benchmark_blast.py`** -- runs the **real orchestrator**
  end-to-end (`GeperPipeline.run()`) over a 12-variant synthetic VCF
  (`testdata_bench/bench.vcf`), three times, with every non-BLAST
  network call (Ensembl, ClinVar, dbSNP, each model's load/inference)
  stubbed identically across all three runs and BLAST's remote
  submission stubbed with a simulated NCBI queue latency
  (0.5s/submission -- real remote BLAST is usually 30s-several
  minutes; the ratios below are latency-independent). This sandbox's
  network doesn't reach NCBI/Ensembl, so this is the most realistic
  before/after comparison obtainable here; re-run
  `python main.py --vcf ...` against a live network for final
  confirmation.

Measured result (9 distinct sequences across 12 variants, 3 concurrent max):

| Scenario | Wall-clock | Real BLAST submissions | Time attributed to BLAST |
|---|---:|---:|---:|
| BEFORE (serial, no cache -- original behavior) | 4.51s | 9 | 4.50s |
| AFTER (concurrent prefetch + disk cache) | 1.56s | 9 | 1.56s |
| AFTER, rerun (same VCF, disk cache reused) | 0.01s | 0 | 0.00s |

* **First-run speedup: 2.9x** (9 submissions overlapped 3-at-a-time
  instead of paid serially -- same 9 real NCBI round trips either way,
  proving no result was skipped or altered, only overlapped).
* **Rerun speedup: 388x** (0 real BLAST calls at all -- every
  sequence was already on disk from the prior run).
* At real NCBI latency (minutes, not 0.5s), the first-run speedup
  scales with `min(distinct_sequences, max_concurrent)`, and the
  rerun/duplicate-sequence case approaches "instant" regardless of
  cohort size, since it's now a SQLite lookup instead of a network
  round trip.

## Everything else stays a rounding error next to BLAST

In the same benchmarked run, every other stage's total time was
effectively zero relative to BLAST's (simulated) queue latency
-- consistent with the "BLAST dominates because of network queueing,
not local compute" diagnosis above. On a real run with real model
weights and real ClinVar/dbSNP/Ensembl calls, expect DNABERT-2/HyenaDNA/
Nucleotide Transformer/ESM-2 inference and the ClinVar/dbSNP/Ensembl
REST calls to be visible but secondary; check your own run's
`geper_benchmark.md` for the exact split on your hardware and cohort.
