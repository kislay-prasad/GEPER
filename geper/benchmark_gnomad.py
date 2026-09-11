# *** THE VERDICT BELOW IS UNDATED. ***
#
# WHAT THIS IS, in its own words: Performance benchmark for the gnomAD integration, run through the REAL
#
# NOTHING RE-RUNS THIS FILE. Measured 2026-09-11 across the 17 files matching
# geper/verify_*.py, geper/benchmark_*.py and dry_run_harness.py: ZERO are
# referenced in .github/workflows, and pytest does not collect any of them,
# because they are not named test_*. So whatever this script printed, it
# printed on the day somebody ran it by hand -- AND WHICH DAY THAT WAS IS
# RECORDED NOWHERE. `git log` on this file gives the date it was EDITED,
# which is a different fact and must not be quoted as if it were this one.
#
# WHY THE NOTE RATHER THAN A FIX: the file is not broken. Its stubs are
# honest -- it fakes everything EXCEPT the thing it verifies, and it
# propagates its exit code -- and all 59 stub targets across these harnesses
# were still defined when this was written, so they can all still be applied.
# The risk is CITATION: 'verify' is in the filename, which invites someone to
# quote this file's green as evidence. That is the `docker history` shape --
# something that reads as a record and is not one.
#
# IF YOU ARE ABOUT TO CITE THIS FILE: run it, and say when you ran it.
#
"""
Performance benchmark for the gnomAD integration, run through the REAL
`pipeline.gnomad.lookup.GnomadLookup` -> `CompositeGnomadProvider` ->
`LocalIndexedGnomadProvider` code path end to end -- against a real,
synthetic, bgzip'd + tabix-indexed gnomAD-format fixture VCF (300
records, built by `testdata_bench/` at repo-prep time), queried via a
real `tabix` subprocess. No network mocking is needed for this
benchmark (unlike benchmark_blast.py) because it deliberately runs in
`GEPER_GNOMAD_OFFLINE=true` mode against the local index, which is
exactly the "local indexed database" code path requirement #4
describes -- so this timing is real, not simulated.

WHAT'S COMPARED
-----------------
  A: Sequential, uncached  -- one query() call per variant, in a
     plain for-loop, cold cache. Reproduces calling gnomAD lookup the
     naive way, once per variant, inside the main per-variant loop.
  B: Batch (thread-pooled)  -- one `batch_query()` call for all
     variants at once, cold cache.
  C: Async batch            -- one `async_query_variants_batch()` call,
     cold cache.
  D: Sequential, warm cache -- A's exact same loop again, immediately
     after A, so every lookup is a cache hit.

This does NOT benchmark the GraphQL fallback path's real network
latency -- this sandbox cannot reach gnomAD's live API (see
verify_environment.py's "Internet connectivity" check), so any number
this script could produce for that path would be a fabricated
guess, not a measurement. Confirm GraphQL-path latency yourself against
a live network; everything below is a genuine, reproducible measurement
of the local-index + caching + concurrency code paths.
"""

import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

_FIXTURE_PATH = os.path.join(_SCRIPT_DIR, "testdata_bench", "gnomad_bench.vcf.gz")

os.environ.setdefault("GEPER_GNOMAD_GRCH38_LOCAL_VCF", _FIXTURE_PATH)
os.environ.setdefault("GEPER_GNOMAD_OFFLINE", "true")  # isolate the local-index path; see module docstring
os.environ.setdefault("GEPER_GNOMAD_CACHE_ENABLED", "true")

from pipeline.gnomad.lookup import GnomadLookup  # noqa: E402
from pipeline.vcf_parser import Variant  # noqa: E402


def _load_bench_variants() -> list:
    import gzip

    variants = []
    with gzip.open(os.path.join(_SCRIPT_DIR, "testdata_bench", "gnomad_bench.vcf.gz"), "rt") as fh:
        for line in fh:
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            chrom, pos, ref, alt = fields[0], int(fields[1]), fields[3], fields[4]
            variants.append(
                Variant(chrom=chrom, pos=pos, variant_id=".", ref=ref, alt=alt, qual=None, filter_status=None)
            )
    return variants


def main() -> int:
    if not os.path.exists(_FIXTURE_PATH):
        print(
            f"Benchmark fixture not found at {_FIXTURE_PATH}. Re-generate it (see this repo's test-data prep step) first."
        )
        return 1

    variants = _load_bench_variants()
    n = len(variants)

    print("=" * 78)
    print(f"gnomAD performance benchmark -- {n} variants, real local tabix-indexed fixture")
    print("=" * 78)

    # A: sequential, cold cache
    lookup_a = GnomadLookup()
    start = time.perf_counter()
    results_a = [lookup_a.query_variant(v, assembly="GRCh38") for v in variants]
    elapsed_a = time.perf_counter() - start
    found_a = sum(1 for r in results_a if r.get("found"))

    # B: batch (thread-pooled), cold cache
    lookup_b = GnomadLookup()
    start = time.perf_counter()
    results_b = lookup_b.query_variants_batch(variants, assembly="GRCh38")
    elapsed_b = time.perf_counter() - start
    found_b = sum(1 for r in results_b if r.get("found"))

    # C: async batch, cold cache
    import asyncio

    lookup_c = GnomadLookup()
    start = time.perf_counter()
    results_c = asyncio.run(lookup_c.async_query_variants_batch(variants, assembly="GRCh38"))
    elapsed_c = time.perf_counter() - start
    found_c = sum(1 for r in results_c if r.get("found"))

    # D: sequential, warm cache (reuse lookup_a, which already cached every variant in A)
    start = time.perf_counter()
    results_d = [lookup_a.query_variant(v, assembly="GRCh38") for v in variants]
    elapsed_d = time.perf_counter() - start
    cache_hit_sources = sum(1 for r in results_d if r.get("source") == "cache")

    assert found_a == found_b == found_c, "All three cold-cache runs must agree on how many variants were found"
    assert [r.get("found") for r in results_a] == [r.get("found") for r in results_d], (
        "Cached results must match original"
    )

    print(f"Variants found in gnomAD fixture: {found_a} / {n}\n")
    print(f"{'Strategy':<32} {'Wall time':>12} {'Variants/sec':>14}")
    print("-" * 60)
    for label, elapsed in (
        ("A: sequential, cold cache", elapsed_a),
        ("B: batch (thread-pooled)", elapsed_b),
        ("C: async batch", elapsed_c),
        ("D: sequential, warm cache", elapsed_d),
    ):
        rate = n / elapsed if elapsed > 0 else float("inf")
        print(f"{label:<32} {elapsed * 1000:>10.1f}ms {rate:>12.1f}/s")

    print()
    print(
        f"Cache: {cache_hit_sources}/{n} of run D's lookups served from cache "
        f"(expect {n}/{n} -- every variant from run A should be warm)."
    )
    speedup_batch = elapsed_a / elapsed_b if elapsed_b > 0 else float("inf")
    speedup_cache = elapsed_a / elapsed_d if elapsed_d > 0 else float("inf")
    print(f"Batch (thread-pooled) vs sequential speedup: {speedup_batch:.1f}x")
    print(f"Warm-cache vs cold-cache speedup: {speedup_cache:.1f}x")
    print("=" * 78)
    print("NOTE: this measures the local-indexed-VCF + caching + concurrency code")
    print("paths only, using synthetic data and this sandbox's CPU/disk. It does")
    print("NOT measure GraphQL fallback latency against gnomAD's live API, which")
    print("requires a real network connection this sandbox does not have.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
