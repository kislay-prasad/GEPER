"""
Before/after benchmark for the BLAST performance optimization, run
through the REAL orchestrator (GeperPipeline.run()) end to end -- not
just the BLASTClient in isolation (see verify_blast_optimization.py
for that). Only the network-touching internals are replaced with
lightweight, deterministic fakes (Ensembl sequence fetch, ClinVar,
dbSNP, each model's load/inference, and BLAST's remote submission --
the last one with a simulated NCBI queue latency), matching the same
"stub only the network, run every real line of pipeline/orchestrator
code" approach as dry_run_harness.py. This sandbox's outbound network
doesn't reach Ensembl/NCBI/HuggingFace, so this is the only way to
produce an apples-to-apples timing comparison here; run the real
`python main.py --vcf ...` yourself against a live network for the
final confirmation.

WHAT'S COMPARED
-----------------
  BEFORE : blast_enable_prefetch=False, blast_disk_cache=False
           -- one blocking BLAST call per variant, serially, inside
           the main loop; no reuse across runs. This reproduces the
           original (pre-Phase-3) BLAST call pattern exactly.
  AFTER  : blast_enable_prefetch=True,  blast_disk_cache=True
           -- distinct sequences BLASTed concurrently up front; a
           second ("AFTER, rerun") pass over the same VCF reuses the
           on-disk cache instead of BLASTing anything at all.

Every other stage (Ensembl, ClinVar, dbSNP, each model) is stubbed
identically in both runs, so the *only* variable between BEFORE and
AFTER is the BLAST layer being benchmarked -- isolating its
contribution to total wall-clock time.
"""

import os
import shutil
import sys
import tempfile
import time
from typing import Dict
from unittest import mock

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# This script measures BLAST's on-disk cache behavior, so it must
# start from a guaranteed-empty cache every time it's run -- otherwise
# a second invocation of this same script would find its own first
# invocation's leftover blast_cache.sqlite already warm, making the
# "AFTER" (first-run) phase falsely look like a rerun. Setting this
# before importing config/pipeline (both read it once, at import
# time) gives every run of this script its own fresh, temporary cache
# directory rather than the default persistent `./model_cache`.
os.environ.setdefault("GEPER_CACHE_DIR", tempfile.mkdtemp(prefix="geper_bench_cache_"))

import _fake_heavy_deps  # noqa: E402

_fake_heavy_deps.install()

# Simulated per-submission NCBI queue latency. Real remote BLAST is
# commonly 30s-several minutes per submission; a small constant here
# is enough to demonstrate the scheduling/caching behavior accurately
# (the speedup ratios are latency-independent) without making this
# script itself slow to run.
_FAKE_REMOTE_LATENCY_SECS = 0.5

_blast_call_count = {"n": 0}


def _fake_search_remote(self, sequence, program, database, max_hits):
    _blast_call_count["n"] += 1
    time.sleep(_FAKE_REMOTE_LATENCY_SECS)
    return {
        "mode": "remote",
        "database": database,
        "hits": [{"hit_id": f"hit-{hash(sequence) % 10_000}", "e_value": 0.0}],
        "hit_count": 1,
    }


def _fake_load_impl(self):
    self.tokenizer = "fake-tokenizer"
    self.model = "fake-model"


def _fake_infer_impl(self, sequence, **kwargs):
    return {"embedding_mean": [0.0] * 8, "embedding_dim": 8, "num_tokens": max(1, len(sequence) // 4)}


def _fake_verify_materialized(self):
    return None


def _install_common_mocks():
    """
    Same non-BLAST stubs as dry_run_harness.py (Ensembl fetch, model
    load/inference, ClinVar, dbSNP) -- these stand in for network
    calls this sandbox cannot make, and are IDENTICAL between the
    BEFORE and AFTER runs so they can't be the source of any timing
    difference observed below.
    """
    from models import MODEL_REGISTRY
    from models.base_model import BaseGenomicModel
    from pipeline.sequence_context import SequenceContext, SequenceContextGenerator
    from database.clinvar_client import ClinVarClient
    from database.dbsnp_client import DbSNPClient

    patches = []
    for model_cls in MODEL_REGISTRY.values():
        patches.append(mock.patch.object(model_cls, "_load_impl", _fake_load_impl))
        patches.append(mock.patch.object(model_cls, "_infer_impl", _fake_infer_impl))
        # is_available() is what the orchestrator's startup validation
        # report calls on every model up front; for HyenaDNA/RNA-FM
        # this is *also* the trigger point for their real one-time
        # auto-install (git clone / pip install) -- stub it to a plain
        # True so this benchmark never touches the network for setup,
        # matching the fact that _load_impl is faked below anyway.
        patches.append(mock.patch.object(model_cls, "is_available", classmethod(lambda cls: True)))
    patches.append(mock.patch.object(BaseGenomicModel, "_verify_materialized", _fake_verify_materialized))

    def _fake_build_context(self, variant, flank_size=None):
        flank = flank_size if flank_size is not None else 500
        # Sequence content derived from the variant's own position so
        # different variants BLAST as genuinely distinct sequences
        # (exercising real dedup/concurrency, not an accidental
        # all-identical shortcut).
        base_cycle = "ACGT"
        ref_seq = "".join(base_cycle[(variant.pos + i) % 4] for i in range(200))
        alt_seq = ref_seq[:100] + variant.alt + ref_seq[101:]
        return SequenceContext(
            chrom=variant.chrom,
            window_start=max(1, variant.pos - flank),
            window_end=variant.pos + flank,
            flank_size=flank,
            ref_sequence=ref_seq,
            alt_sequence=alt_seq,
            variant_offset=flank,
        )

    patches.append(mock.patch.object(SequenceContextGenerator, "build_context", _fake_build_context))
    patches.append(
        mock.patch.object(
            SequenceContextGenerator,
            "prefetch_regions",
            lambda self, variants, flank_lookup: None,
        )
    )
    patches.append(
        mock.patch.object(
            ClinVarClient,
            "query_variant",
            lambda self, variant, rsid=None, assembly=None: {"query": None, "found": False},
        )
    )
    patches.append(
        mock.patch.object(
            DbSNPClient,
            "lookup_variant",
            lambda self, variant, assembly=None: {"rsid": None, "found": False},
        )
    )
    return patches


def _run_pipeline(vcf_path: str, output_dir: str, *, prefetch: bool, disk_cache: bool):
    from pipeline.orchestrator import GeperPipeline

    pipeline = GeperPipeline(
        blast_mode="remote",
        species="human",
        assembly="GRCh38",
        output_dir=output_dir,
        blast_enable_prefetch=prefetch,
        blast_disk_cache=disk_cache,
    )
    wall_start = time.perf_counter()
    pipeline.run(vcf_path, resume=False)
    wall_elapsed = time.perf_counter() - wall_start
    return wall_elapsed, pipeline.profiler


def main() -> int:
    vcf_path = os.path.join(_SCRIPT_DIR, "testdata_bench", "bench.vcf")
    if not os.path.exists(vcf_path):
        print(f"Benchmark VCF not found at {vcf_path}.")
        return 1

    with open(vcf_path) as fh:
        n_variants = sum(1 for line in fh if line.strip() and not line.startswith("#"))

    common_patches = _install_common_mocks()
    blast_patch = mock.patch("database.blast_client.BLASTClient._search_remote", _fake_search_remote)

    results: Dict[str, Dict[str, float]] = {}

    for p in common_patches:
        p.start()
    blast_patch.start()
    try:
        print("=" * 78)
        print(
            f"BLAST performance benchmark -- {n_variants} variants, "
            f"simulated NCBI latency {_FAKE_REMOTE_LATENCY_SECS}s/submission"
        )
        print("=" * 78)

        # --- BEFORE: no prefetch, no disk cache (original behavior) --------
        before_dir = tempfile.mkdtemp(prefix="geper_bench_before_")
        _blast_call_count["n"] = 0
        before_wall, before_profiler = _run_pipeline(vcf_path, before_dir, prefetch=False, disk_cache=False)
        before_blast_calls = _blast_call_count["n"]
        before_blast_secs = before_profiler.total_secs_by_stage().get("blast", 0.0)
        results["BEFORE (serial, no cache)"] = {
            "wall_clock_secs": before_wall,
            "real_blast_calls": before_blast_calls,
            "blast_stage_secs": before_blast_secs,
        }
        print("\nBEFORE (serial BLAST, no disk cache):")
        print(f"  Wall-clock run() time : {before_wall:.2f}s")
        print(f"  Real BLAST submissions: {before_blast_calls}")
        print(f"  Time attributed to 'blast' stage: {before_blast_secs:.2f}s")

        # --- AFTER: batch/concurrent prefetch + disk cache ------------------
        after_dir = tempfile.mkdtemp(prefix="geper_bench_after_")
        _blast_call_count["n"] = 0
        after_wall, after_profiler = _run_pipeline(vcf_path, after_dir, prefetch=True, disk_cache=True)
        after_blast_calls = _blast_call_count["n"]
        after_blast_secs = after_profiler.total_secs_by_stage().get(
            "blast", 0.0
        ) + after_profiler.total_secs_by_stage().get("blast_prefetch", 0.0)
        results["AFTER (concurrent prefetch + disk cache)"] = {
            "wall_clock_secs": after_wall,
            "real_blast_calls": after_blast_calls,
            "blast_stage_secs": after_blast_secs,
        }
        print("\nAFTER (concurrent BLAST prefetch, disk cache ENABLED):")
        print(f"  Wall-clock run() time : {after_wall:.2f}s")
        print(f"  Real BLAST submissions: {after_blast_calls}")
        print(f"  Time attributed to BLAST (prefetch + stage): {after_blast_secs:.2f}s")

        # --- AFTER, RERUN: same VCF again, disk cache from the AFTER run ----
        # Simulates re-running the pipeline later (e.g. after a Colab
        # disconnect, or reprocessing an overlapping cohort) -- every
        # sequence was already BLASTed and cached on disk in the
        # "AFTER" run above, so this pass should make ~zero real BLAST
        # calls at all.
        _blast_call_count["n"] = 0
        rerun_wall, rerun_profiler = _run_pipeline(vcf_path, after_dir, prefetch=True, disk_cache=True)
        rerun_blast_calls = _blast_call_count["n"]
        rerun_blast_secs = rerun_profiler.total_secs_by_stage().get(
            "blast", 0.0
        ) + rerun_profiler.total_secs_by_stage().get("blast_prefetch", 0.0)
        results["AFTER, RERUN (disk cache reused, 0 new BLAST calls expected)"] = {
            "wall_clock_secs": rerun_wall,
            "real_blast_calls": rerun_blast_calls,
            "blast_stage_secs": rerun_blast_secs,
        }
        print("\nAFTER, RERUN (same VCF, reusing the on-disk BLAST cache):")
        print(f"  Wall-clock run() time : {rerun_wall:.2f}s")
        print(f"  Real BLAST submissions: {rerun_blast_calls} (expected 0)")
        print(f"  Time attributed to BLAST (prefetch + stage): {rerun_blast_secs:.2f}s")

        shutil.rmtree(before_dir, ignore_errors=True)
        shutil.rmtree(after_dir, ignore_errors=True)
    finally:
        blast_patch.stop()
        for p in common_patches:
            p.stop()

    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    header = f"{'Scenario':<55} {'Wall (s)':>10} {'BLAST calls':>12} {'BLAST time (s)':>16}"
    print(header)
    print("-" * len(header))
    for name, row in results.items():
        print(
            f"{name:<55} {row['wall_clock_secs']:>10.2f} {row['real_blast_calls']:>12} {row['blast_stage_secs']:>16.2f}"
        )

    before = results["BEFORE (serial, no cache)"]
    after = results["AFTER (concurrent prefetch + disk cache)"]
    rerun = results["AFTER, RERUN (disk cache reused, 0 new BLAST calls expected)"]

    speedup_first_run = (
        before["wall_clock_secs"] / after["wall_clock_secs"] if after["wall_clock_secs"] else float("inf")
    )
    speedup_rerun = before["wall_clock_secs"] / rerun["wall_clock_secs"] if rerun["wall_clock_secs"] else float("inf")

    print()
    print(f"First-run speedup (concurrent prefetch vs serial): {speedup_first_run:.1f}x")
    print(f"Rerun speedup (on-disk cache vs serial, no cache):  {speedup_rerun:.1f}x")

    ok = (
        after["real_blast_calls"] == before["real_blast_calls"]  # same number of *distinct* sequences BLASTed
        and rerun["real_blast_calls"] == 0
        and after["wall_clock_secs"] < before["wall_clock_secs"]
        and rerun["wall_clock_secs"] < after["wall_clock_secs"]
    )
    print()
    print(
        "Correctness check (same distinct-sequence BLAST count before/after,"
        " 0 real calls on rerun, monotonically faster):",
        "PASSED" if ok else "FAILED",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
