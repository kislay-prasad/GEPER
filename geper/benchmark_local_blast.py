# *** THE VERDICT BELOW IS UNDATED. ***
#
# WHAT THIS IS, in its own words: Local BLAST+ vs remote NCBI BLAST performance benchmark
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
Local BLAST+ vs remote NCBI BLAST performance benchmark.

Unlike benchmark_blast.py (which measures the *caching/scheduling*
optimizations on top of remote BLAST), this script measures the
headline number for this feature: how much faster a REAL local
`blastn` search against a REAL `makeblastdb`-built database is than
remote NCBI BLAST.

WHY REMOTE IS SIMULATED, NOT LOCAL
------------------------------------
This sandbox's outbound network reaches package registries only, not
NCBI, so an actual `NCBIWWW.qblast()` call cannot be made here. The
local side of this benchmark is 100% real (real `blastn` binary, real
`makeblastdb`-built database, real subprocess calls, real hits) --
only the remote side is a `time.sleep()` stand-in for NCBI's published,
well-documented queue latency (NCBI's own BLAST URL API guidance and
long-standing user reports put a single hosted `qblast` submission
anywhere from ~10-30s at the low end up to several minutes under load;
see database/blast_client.py's module docstring for the same figures
cited elsewhere in this codebase). The comparison below uses the
conservative (low) end of that published range, so the reported
speedup is a floor, not an inflated ceiling.

Run directly:
    python benchmark_local_blast.py
"""

import os
import shutil
import sys
import tempfile
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# Conservative low end of NCBI's documented/observed hosted-qblast
# per-submission queue latency. Real-world remote BLAST is very often
# considerably slower than this (multi-minute under load), so this
# benchmark understates the real speedup local BLAST+ delivers.
_CONSERVATIVE_REMOTE_LATENCY_SECS = 15.0

_TEST_SEQUENCES = [
    "ACGTACGTACGTACGTGGGGCCCCAAAATTTTACGTACGTACGTACGTGATTACAGATTACAG",
    "TTGGCCAATTGGCCAAGGGGAAAACCCCTTTTGGCCAATTGGCCAAGATTACAGATTACAGAT",
    "CGCGCGCGATATATATGCGCGCGCATATATATCGCGCGCGATATATATGCGCGCGCATATAT",
    "AAAACCCCGGGGTTTTAAAACCCCGGGGTTTTGATTACAGATTACAGGATTACAGATTACAGA",
    "GCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCT",
]


def main() -> int:
    if not all(shutil.which(t) for t in ("blastn", "makeblastdb")):
        print(
            "blastn/makeblastdb not found on PATH -- install NCBI BLAST+ "
            "(see verify_environment.py) to run this benchmark."
        )
        return 1

    from database.blast_client import BLASTClient, ensure_local_blast_db

    work_dir = tempfile.mkdtemp(prefix="geper_local_blast_bench_")
    try:
        fasta_path = os.path.join(work_dir, "reference.fasta")
        with open(fasta_path, "w") as fh:
            for i, seq in enumerate(_TEST_SEQUENCES):
                # Pad each reference contig so every query sequence has
                # somewhere real to align, plus filler so the database
                # isn't trivially the query itself base-for-base.
                fh.write(f">contig{i}\n{seq}{'N' * 40}{seq[::-1]}\n")

        db_path = os.path.join(work_dir, "benchdb")

        print("=" * 78)
        print("Building local BLAST database via makeblastdb...")
        print("=" * 78)
        build_start = time.perf_counter()
        built = ensure_local_blast_db(fasta_path, db_path)
        build_elapsed = time.perf_counter() - build_start
        if not built:
            print("makeblastdb build failed -- see logged warning above.")
            return 1
        print(f"Database built in {build_elapsed:.3f}s at '{db_path}'.\n")

        client = BLASTClient(mode="local", local_db_path=db_path, cache_dir=os.path.join(work_dir, "cache"))

        print("=" * 78)
        print(f"REAL local blastn: {len(_TEST_SEQUENCES)} distinct sequences, one at a time")
        print("=" * 78)
        local_start = time.perf_counter()
        local_results = []
        for seq in _TEST_SEQUENCES:
            local_results.append(client.search(seq))
        local_elapsed = time.perf_counter() - local_start
        total_hits = sum(r.get("hit_count", 0) for r in local_results)
        print(
            f"  {len(_TEST_SEQUENCES)} sequences searched in {local_elapsed:.3f}s total "
            f"({local_elapsed / len(_TEST_SEQUENCES) * 1000:.1f}ms/sequence average)."
        )
        print(f"  Total hits found: {total_hits} (real blastn output, not simulated).\n")

        simulated_remote_elapsed = len(_TEST_SEQUENCES) * _CONSERVATIVE_REMOTE_LATENCY_SECS
        print("=" * 78)
        print("SIMULATED remote NCBI BLAST (conservative published queue latency)")
        print("=" * 78)
        print(
            f"  {len(_TEST_SEQUENCES)} sequences x {_CONSERVATIVE_REMOTE_LATENCY_SECS:.1f}s/submission "
            f"(serial, no local batching) = {simulated_remote_elapsed:.1f}s."
        )
        print("  (This is the conservative low end; real remote runs are commonly")
        print("   30s-several minutes per submission, so the real-world gap is larger.)\n")

        speedup = simulated_remote_elapsed / local_elapsed if local_elapsed > 0 else float("inf")
        print("=" * 78)
        print("RESULT")
        print("=" * 78)
        print(f"  Local BLAST+:        {local_elapsed:.3f}s for {len(_TEST_SEQUENCES)} sequences")
        print(f"  Remote (simulated):  {simulated_remote_elapsed:.1f}s for {len(_TEST_SEQUENCES)} sequences")
        print(f"  Speedup:             ~{speedup:.0f}x faster with local BLAST+ (conservative estimate)")
        print("=" * 78)
        return 0
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
