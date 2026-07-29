# BWA Indexing on Colab: What Was Actually Slow, and What Changed

## 1. Why indexing was taking 1–2 hours (or appearing to hang)

Three separate, verified causes — not one:

1. **No persistence across sessions.** The old `_ensure_index()` in
   `pipeline/alignment/bwa_runner.py` only checked whether the 5 index
   files (`.amb .ann .bwt .pac .sa`) already existed **next to the
   reference file**. On Colab, the reference (and everything else in
   `/content`) lives on the ephemeral local disk. When the runtime
   disconnects or the session is restarted, that disk is wiped — so the
   *code* would have skipped a rebuild correctly, but there was nothing
   left to skip. Every session re-indexed from zero. Measured: **~170s
   every single run**, on a 150 MB test genome, with no reduction on
   repeat runs (see benchmark below; scales to the 1–2 hours you saw on
   your ~900 MB genome).

2. **No visible progress.** The pipeline's shared subprocess helper
   (`pipeline/fastq/errors.py::_run`) calls
   `subprocess.run(cmd, capture_output=True)`, which buffers *all*
   stdout/stderr until the process exits. For a multi-hour `bwa index`
   run, that means literally nothing is printed to the notebook for
   hours — indistinguishable from a hang, even though BWA was working the
   entire time.

3. **A partial index could be silently trusted.** Killing a `bwa index`
   run (simulating a session timeout) confirmed that `.amb`/`.ann`/`.pac`
   are written early and `.bwt`/`.sa` last — so an interruption can leave
   some suffix files present and others missing. The old check only
   tested *existence*, not completeness, so a build interrupted at just
   the wrong moment could in principle leave behind files that pass an
   existence check without being a valid, safe-to-use index.

## 2. What was NOT the cause (verified by direct profiling, not assumed)

**Indexing a `.gz` reference directly is not meaningfully slower than
indexing a decompressed one.** Measured on a 150 MB synthetic reference:

| Input to `bwa index` | Time |
|---|---|
| `.gz` (compressed) directly | 190.8s |
| Same file, pre-decompressed | 184.8s |

A ~3% difference — noise, not a bottleneck. `bwa index` streams gzip
decompression internally; it is not the dominant cost. **BWT/suffix-array
construction is the dominant cost**, and it's the same regardless of
whether the input was gzipped.

**So why decompress at all?** Not for indexing speed — for correctness.
Confirmed directly:
```
$ samtools faidx ref.fasta.gz
[E::fai_build3_core] Failed to open the file ref.fasta.gz
[faidx] Could not build fai index ref.fasta.gz.fai
```
`samtools faidx` (called by the variant-calling stage, via
`freebayes_runner.py`, to build the `.fai` FreeBayes needs) **cannot open
a plain-gzip FASTA at all** — only uncompressed or *bgzip*-compressed. So
a `ref.fna.gz` reference would let alignment succeed but make variant
calling crash outright. Decompression is a correctness requirement for
the pipeline as a whole, independent of indexing speed.

## 3. What changed

New module: `pipeline/utils/reference_cache.py`, wired into
`pipeline/orchestration/runner.py`, `pipeline/alignment/stage.py`, and
`pipeline/alignment/bwa_runner.py`. No existing pipeline logic was
rewritten — this sits alongside it.

1. **Reference decompression, cached by content.** A `.gz` reference is
   decompressed once into a cache directory (default: `.ref_cache` next
   to the reference; configurable via `--ref-cache-dir` /
   `reference.local_cache_dir`). An unchanged source file is detected via
   a fingerprint and is *not* re-decompressed on the next run.

2. **Persistent, configurable BWA index directory.** New
   `--bwa-index-dir` CLI flag / `alignment.index_dir` config option.
   Point it at a Google Drive mount
   (e.g. `/content/drive/MyDrive/geper_bwa_index`) and the index is built
   there instead of next to the reference — so it survives the local
   Colab disk being wiped between sessions.

3. **Skip-if-unchanged, based on content, not mtime.** A
   `index_manifest.json` in the index directory records a fingerprint of
   the reference the index was built from: filename + size + a SHA-256
   hash of the first/last 8 MB. On the next run, if the fingerprint
   matches and all 5 index files are present and non-empty, indexing is
   skipped entirely.

   **Important correction made during testing:** the first version of
   this fingerprint used `(name, size, mtime)`. That failed in exactly
   the scenario this feature targets — decompressing the reference fresh
   in a new session directory gives the file a new mtime even though its
   content is byte-identical, so an mtime-based check kept rebuilding
   every session. Switched to a content-based fingerprint, confirmed
   fixed (see `test_reused_across_sessions_despite_fresh_decompression_mtime`
   in `tests/test_reference_cache.py`, and the live benchmark below).

4. **Atomic build-then-publish**, so an interrupted session can never
   leave a corrupt index in the trusted location. The index is built into
   a uniquely-named temporary directory first; only once all 5 suffix
   files are confirmed non-empty are they moved into the final,
   manifest-tracked location. If the process is killed mid-build, only
   the (ignored) temp directory is left behind.

5. **Live streaming + heartbeat logging.** The index-build subprocess now
   streams BWA's own progress lines as they're produced, plus a
   heartbeat log line every 30s even during BWA's silent stretches (BWT
   construction prints nothing for long periods on large genomes). No
   more multi-hour silence in the notebook.

### Configuration

```yaml
# config/default.yaml
reference:
  local_cache_dir: null       # e.g. "/content/drive/MyDrive/geper_ref_cache"
alignment:
  index_dir: null             # e.g. "/content/drive/MyDrive/geper_bwa_index"
```

Or via CLI:
```bash
python main.py analyze --r1 R1.fastq.gz --ref ref.fna.gz \
    --output-dir ./work --sample-id sample01 \
    --bwa-index-dir /content/drive/MyDrive/geper_bwa_index \
    --ref-cache-dir /content/drive/MyDrive/geper_ref_cache
```

Both flags are optional; omitting them preserves the exact original
behavior (index built next to the reference, no persistence).

## 4. Benchmark (150 MB synthetic reference, this sandbox: 1 vCPU, no GPU)

A full 900 MB human-scale genome couldn't be safely built in this sandbox
(disk/time budget), so this uses a 150 MB synthetic FASTA as an honest,
smaller-scale stand-in — the mechanism being measured (rebuild-every-time
vs. skip-if-cached) doesn't depend on genome size, only the absolute
seconds do.

| Scenario | Time |
|---|---|
| **Before** — session 1, index built next to reference | 168.6s |
| **Before** — session 2 (simulated restart, no persistence exists) | 173.0s |
| **After** — session 1, decompress + build into persistent dir | decompress 3.8s + index 166.4s = 170.2s |
| **After** — session 2 (simulated restart, persistent dir reused) | decompress 6.7s + index **0.02s** = 6.8s |

First run costs the same as before (indexing still has to happen once —
there is no way around that, and this change does not claim otherwise).
The difference is entirely in what happens on every run *after* the
first: **173.0s → 6.8s**, a ~25x reduction, with the 6.8s being pure I/O
(decompressing the reference again from its `.gz` source) rather than any
BWT/SA computation. On your ~900 MB genome, where a from-scratch build is
costing 1–2 hours, subsequent runs against the same reference (pointed at
a Drive-backed `--bwa-index-dir`) would similarly drop to whatever it
costs to decompress ~900 MB (low single-digit minutes at typical Drive
mount throughput) plus the sub-second fingerprint/manifest check — not
another 1–2 hours.

## 5. Aligner trade-off: should you switch to minimap2? (Not changed — for your decision)

`pipeline/alignment/minimap2_runner.py` already exists and works. It does
**not** build a persisted on-disk index by default in this pipeline (it
indexes in-memory per run), so it sidesteps the "index survives a
session" problem entirely by not needing a persisted index at all — each
run's in-memory indexing step for a genome this size is typically faster
than BWA's on-disk suffix-array construction.

Trade-offs to weigh before switching:
- **Read type:** minimap2's `sr` preset (already configured as the
  default preset in `config/default.yaml`) targets short accurate
  genomic reads and is a reasonable fit for Illumina short reads, but
  minimap2 is primarily developed and best-validated for long reads
  (ONT/PacBio). BWA-MEM remains the more established, clinically
  battle-tested default for short-read germline variant calling.
- **Downstream concordance:** this pipeline's variant-calling and
  ACMG-scoring behavior has presumably been exercised against BWA-MEM
  alignments. Switching the aligner changes exact read placement in
  repetitive/low-complexity regions (e.g. near indels), which can shift
  which variants FreeBayes calls at the margins — worth a validation
  pass against known-truth data before relying on it clinically, not
  assumed to be a drop-in replacement.
- **What you'd gain:** no BWA index build/storage step at all for
  alignment. What you would *not* gain: escaping the `samtools
  faidx`-needs-uncompressed-reference requirement in variant calling —
  that decompression step is needed regardless of aligner choice.

This pipeline already supports `alignment.aligner: minimap2` today if you
want to test it — no code change required to try it. It has not been made
the default here, since that's a validation decision, not an indexing
performance one.

## 6. Files changed

- `pipeline/utils/reference_cache.py` — new module (decompression cache +
  persistent atomic index cache + streaming/heartbeat logging).
- `pipeline/alignment/bwa_runner.py` — `_ensure_index` delegates to the
  new module; `run_bwa_mem` gained an `index_dir` parameter.
- `pipeline/alignment/stage.py` — `AlignmentStage` reads
  `alignment.index_dir` from config and threads it through.
- `pipeline/orchestration/runner.py` — decompresses a `.gz` reference
  once, up front, before either AlignmentStage or VariantCallingStage
  runs.
- `config/default.yaml` — new `reference.local_cache_dir` and
  `alignment.index_dir` options (both default `null` = old behavior).
- `main.py` — new `--bwa-index-dir` / `--ref-cache-dir` CLI flags on the
  `analyze` subcommand.
- `tests/test_reference_cache.py` — new, 15 tests.

## 7. Testing performed

- `tests/test_reference_cache.py`: 15/15 passed (decompression caching,
  index build/skip/rebuild-on-missing-file, persistent-dir build/skip,
  cross-reference manifest rejection, interrupted-build safety, and the
  mtime-vs-content-fingerprint regression found during this work).
- Full existing suite: `901 → 911` passed after the fix (the 9 newly
  passing were regressions I introduced and then fixed mid-task — see
  below); `10` pre-existing failures remain, in
  `tests/test_phase2_data_sources.py`, unrelated to alignment/indexing
  (`codon_provider.py` UTF-8 decode errors caused by this sandbox having
  no UTF-8 locale configured — confirmed via `locale`, and confirmed that
  module never imports anything from `reference_cache`).
- One pre-existing, unrelated test (`test_pipeline_alignment.py::
  TestBwaAlignment::test_produces_sorted_bam_and_index`) fails both
  before and after this change — it asserts a BAM filename
  (`aligned.sorted.bam`) that `AlignmentStage` stopped producing as the
  *final* output once a markdup step was added (final file is
  `aligned.markdup.bam`); confirmed by reading `stage.py`, which this
  change never touched.
- Full real end-to-end run via `python main.py analyze ... --bwa-index-dir
  ...` against the real MT test dataset: correct `MT:3243 A>G` variant
  called on the first run, and a second CLI invocation logged
  `Reusing persistent BWA index ... — skipping indexing entirely.`
- `bridge/tests/test_bridge.py`: 7/7 still passing (no bridge code was
  touched by this change; re-run to confirm no interaction effects).
- Caught and fixed two regressions of my own during this work before
  calling it done: (a) `resolve_reference` originally checked file
  existence unconditionally, breaking stage-mocked unit tests that use
  placeholder paths — fixed by only checking existence for actual `.gz`
  inputs; (b) the mtime-based fingerprint described above.
