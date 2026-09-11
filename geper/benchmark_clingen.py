# *** THE VERDICT BELOW IS UNDATED. ***
#
# WHAT THIS IS, in its own words: Performance benchmark for the ClinGen integration, run through the REAL
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
Performance benchmark for the ClinGen integration, run through the REAL
`pipeline.clingen.lookup.ClinGenLookup` -> `CompositeClinGenProvider` ->
`LocalDatasetClinGenProvider` code path end to end -- against a
synthetic 300-gene gene-validity + dosage-sensitivity TSV fixture
(`testdata_bench/clingen_gene_validity_bench.tsv` /
`clingen_dosage_bench.tsv`).

This does NOT benchmark the live-API fallback path's real network
latency -- this sandbox has no route to clinicalgenome.org (confirmed
HTTP 403 with `x-deny-reason: host_not_allowed` from the egress proxy
during development; see `pipeline/clingen/provider.py`'s module
docstring), so any number this script could produce for that path
would be a fabricated guess, not a measurement. Everything below is a
genuine, reproducible measurement of the local-dataset + caching +
concurrency code paths, mirroring `benchmark_gnomad.py`'s exact
structure and disclaimers for the equivalent gnomAD comparison.

WHAT'S COMPARED
-----------------
  A: Sequential, uncached  -- one query_gene() call per gene, in a
     plain for-loop, cold cache.
  B: Batch (thread-pooled) -- one query_variants_batch() call for all
     variants at once, cold cache.
  C: Sequential, warm cache -- A's exact same loop again, immediately
     after A, so every lookup is a cache hit.
"""

import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

_GENE_VALIDITY_FIXTURE = os.path.join(_SCRIPT_DIR, "testdata_bench", "clingen_gene_validity_bench.tsv")
_DOSAGE_FIXTURE = os.path.join(_SCRIPT_DIR, "testdata_bench", "clingen_dosage_bench.tsv")

os.environ.setdefault("GEPER_CLINGEN_GENE_VALIDITY_FILE", _GENE_VALIDITY_FIXTURE)
os.environ.setdefault("GEPER_CLINGEN_DOSAGE_FILE", _DOSAGE_FIXTURE)
os.environ.setdefault("GEPER_CLINGEN_API_ENABLED", "false")  # isolate the local-dataset path; see module docstring
os.environ.setdefault("GEPER_CLINGEN_CACHE_ENABLED", "true")

from pipeline.clingen.lookup import ClinGenLookup  # noqa: E402
from pipeline.vcf_parser import Variant  # noqa: E402
from unittest import mock  # noqa: E402


def _load_gene_list():
    genes = []
    with open(_GENE_VALIDITY_FIXTURE) as fh:
        next(fh)  # header
        for line in fh:
            genes.append(line.split("\t")[0])
    return genes


def _fake_variants(genes):
    """One synthetic variant per gene -- gene-symbol resolution is mocked below (see main())."""
    return [
        Variant(chrom="1", pos=1000 + i, variant_id=".", ref="A", alt="G", qual=None, filter_status=None)
        for i in range(len(genes))
    ]


def main() -> None:
    genes = _load_gene_list()
    variants = _fake_variants(genes)
    gene_by_pos = {f"GRCh38:1:{v.pos}": g for v, g in zip(variants, genes)}

    def fake_resolve(chrom, pos, build="GRCh38"):
        return gene_by_pos.get(f"{build}:{chrom}:{pos}")

    print(f"ClinGen benchmark -- {len(genes)} genes, local dataset only (no network)\n")
    print(f"{'Strategy':<35}{'Wall time':>12}{'Genes/sec':>15}")
    print("-" * 62)

    with mock.patch("pipeline.clingen.lookup.resolve_gene_symbol", side_effect=fake_resolve):
        # A: sequential, cold cache
        lookup_a = ClinGenLookup()
        start = time.perf_counter()
        for v in variants:
            lookup_a.query_variant(v, assembly="GRCh38")
        elapsed_a = time.perf_counter() - start
        print(f"{'A: sequential, cold cache':<35}{elapsed_a * 1000:>10.1f}ms{len(genes) / elapsed_a:>13.1f}/s")

        # B: batch (thread-pooled), cold cache
        lookup_b = ClinGenLookup()
        start = time.perf_counter()
        lookup_b.query_variants_batch(variants, assembly="GRCh38")
        elapsed_b = time.perf_counter() - start
        print(f"{'B: batch (thread-pooled)':<35}{elapsed_b * 1000:>10.1f}ms{len(genes) / elapsed_b:>13.1f}/s")

        # C: sequential, warm cache (reuse lookup_a's now-populated cache)
        start = time.perf_counter()
        for v in variants:
            lookup_a.query_variant(v, assembly="GRCh38")
        elapsed_c = time.perf_counter() - start
        print(f"{'C: sequential, warm cache':<35}{elapsed_c * 1000:>10.1f}ms{len(genes) / elapsed_c:>13.1f}/s")

    print()
    print(f"Batch (thread-pooled) vs sequential speedup: {elapsed_a / elapsed_b:.1f}x")
    print(f"Warm-cache vs cold-cache speedup: {elapsed_a / elapsed_c:.1f}x")


if __name__ == "__main__":
    main()
