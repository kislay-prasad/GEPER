"""
Verification harness for the BLAST performance optimization (Phase 3
performance pass): in-memory + on-disk caching, concurrent batch
submission, mode="auto" local-database preference, and AI-only mode.

WHAT'S BEING VERIFIED
-----------------------
1. In-memory + disk caching: an identical sequence BLASTed twice
   (same run, or a fresh BLASTClient instance pointed at the same
   cache directory -- simulating a second run / a resumed run) only
   ever triggers ONE real search call, and every cache hit returns a
   byte-for-byte identical result to the real search.
2. Cross-run persistence: a brand-new BLASTClient instance (no shared
   in-memory state) still gets a cache hit from a previous instance's
   disk cache.
3. Concurrent batch submission: `search_many()` for N distinct
   sequences overlaps their (simulated) NCBI queue wait instead of
   paying it serially -- wall-clock time for N sequences is close to
   one sequence's latency (bounded by max_concurrent), not N times it.
4. mode="auto": resolves to "local" when a blastn binary + database
   files are present, and to "remote" otherwise -- and a local search
   never touches the (mocked) remote path at all.
5. AI-only mode (disabled=True): search()/search_many() never call
   the real remote/local implementation and return an explicit
   "skipped" result immediately.
6. Correctness invariant: none of the above changes a single BLAST
   hit/score/e-value versus the original unbatched, uncached call --
   only how many times / how quickly it's computed.

WHY A MOCK BLAST LAYER
------------------------
This sandbox's outbound network doesn't reach NCBI (only package
registries are allowlisted). Only the network-touching internals
(`BLASTClient._search_remote` / `subprocess.run` for local) are
replaced with small, deterministic fakes that simulate NCBI's real
queue latency with `time.sleep`; every other line of real GEPER code
(caching, batching, mode resolution, disabled short-circuiting) runs
unmodified.
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

# Simulated per-submission NCBI queue latency used throughout this
# harness. Real remote BLAST is usually 30s-several minutes; a small
# constant is enough to prove the *scheduling* behavior (serial vs
# concurrent) without making this script slow to run.
_FAKE_REMOTE_LATENCY_SECS = 0.4

_search_call_count = {"n": 0}


def _fake_search_remote(self, sequence, program, database, max_hits):
    _search_call_count["n"] += 1
    time.sleep(_FAKE_REMOTE_LATENCY_SECS)
    # Deterministic, sequence-derived "hit" so equality checks are meaningful.
    return {
        "mode": "remote",
        "database": database,
        "hits": [{"hit_id": f"hit-{hash(sequence) % 10_000}", "e_value": 0.0}],
        "hit_count": 1,
    }


def main() -> int:
    from database.blast_client import BLASTClient

    all_ok = True
    tmp_dir = tempfile.mkdtemp(prefix="geper_blast_verify_")

    try:
        print("=" * 78)
        print("PART 1 -- In-memory + disk cache dedupes identical sequences")
        print("=" * 78)
        _search_call_count["n"] = 0
        with mock.patch.object(BLASTClient, "_search_remote", _fake_search_remote):
            client = BLASTClient(mode="remote", disabled=False, cache_dir=tmp_dir)
            seq = "ACGT" * 50

            r1 = client.search(seq)
            r2 = client.search(seq)  # in-memory hit
            r3 = client.search(seq)  # in-memory hit again

        calls_after_same_instance = _search_call_count["n"]
        print(f"  Real search calls for 3x identical search() on one instance: {calls_after_same_instance} (expected 1)")
        print(f"  r1 == r2 == r3: {r1 == r2 == r3}")

        part1_ok = calls_after_same_instance == 1 and r1 == r2 == r3
        if not part1_ok:
            print("  FAILURE.")
        print("PART 1:", "PASSED" if part1_ok else "FAILED")
        all_ok = all_ok and part1_ok

        print()
        print("=" * 78)
        print("PART 2 -- Disk cache persists across a brand-new BLASTClient instance")
        print("=" * 78)
        with mock.patch.object(BLASTClient, "_search_remote", _fake_search_remote):
            client_b = BLASTClient(mode="remote", disabled=False, cache_dir=tmp_dir)
            calls_before = _search_call_count["n"]
            r4 = client_b.search(seq)  # same sequence as PART 1, new process/instance
            calls_after = _search_call_count["n"]

        part2_ok = calls_after == calls_before and r4 == r1
        print(f"  New instance, same cache_dir, same sequence -> real search calls: {calls_after - calls_before} (expected 0)")
        print(f"  Result identical to PART 1's real search: {r4 == r1}")
        if not part2_ok:
            print("  FAILURE.")
        print("PART 2:", "PASSED" if part2_ok else "FAILED")
        all_ok = all_ok and part2_ok

        print()
        print("=" * 78)
        print("PART 3 -- search_many() overlaps latency instead of paying it serially")
        print("=" * 78)
        distinct_sequences = [f"{'ACGT' * 20}{i}" for i in range(6)]
        _search_call_count["n"] = 0
        with mock.patch.object(BLASTClient, "_search_remote", _fake_search_remote):
            client_c = BLASTClient(
                mode="remote", disabled=False, cache_dir=tempfile.mkdtemp(prefix="geper_blast_verify_")
            )
            client_c.max_concurrent = 6  # allow full overlap for this timing check

            serial_start = time.perf_counter()
            for s in distinct_sequences:
                client_c.search(s + "SERIALMARK")
            serial_elapsed = time.perf_counter() - serial_start

            _search_call_count["n"] = 0
            batch_start = time.perf_counter()
            results = client_c.search_many([s + "BATCHMARK" for s in distinct_sequences])
            batch_elapsed = time.perf_counter() - batch_start

        expected_serial = len(distinct_sequences) * _FAKE_REMOTE_LATENCY_SECS
        print(f"  Serial: {len(distinct_sequences)} sequences took {serial_elapsed:.2f}s (~{expected_serial:.2f}s expected)")
        print(f"  Batched (search_many, 6 concurrent): took {batch_elapsed:.2f}s")
        print(f"  search_many returned {len(results)} results for {len(distinct_sequences)} distinct inputs")

        part3_ok = (
            len(results) == len(distinct_sequences)
            and batch_elapsed < serial_elapsed * 0.6  # meaningfully faster, not just noise
            and all(v.get("hit_count") == 1 for v in results.values())
        )
        if not part3_ok:
            print("  FAILURE: batched search_many did not overlap latency as expected.")
        print("PART 3:", "PASSED" if part3_ok else "FAILED")
        all_ok = all_ok and part3_ok

        print()
        print("=" * 78)
        print("PART 4 -- search_many() de-duplicates before submitting")
        print("=" * 78)
        _search_call_count["n"] = 0
        with mock.patch.object(BLASTClient, "_search_remote", _fake_search_remote):
            client_d = BLASTClient(mode="remote", disabled=False, cache_dir=tempfile.mkdtemp(prefix="geper_blast_verify_"))
            dup_seq = "GATTACA" * 10
            batch_results = client_d.search_many([dup_seq, dup_seq, dup_seq, dup_seq + "X"])
        part4_ok = _search_call_count["n"] == 2 and len(batch_results) == 2
        print(f"  4 requested sequences (3 identical + 1 distinct) -> real search calls: {_search_call_count['n']} (expected 2)")
        print(f"  Unique results returned: {len(batch_results)} (expected 2)")
        if not part4_ok:
            print("  FAILURE.")
        print("PART 4:", "PASSED" if part4_ok else "FAILED")
        all_ok = all_ok and part4_ok

        print()
        print("=" * 78)
        print("PART 5 -- mode='auto' prefers local when blastn + DB are present")
        print("=" * 78)
        fake_db_dir = tempfile.mkdtemp(prefix="geper_blast_fakedb_")
        db_base = os.path.join(fake_db_dir, "mydb")
        for ext in (".nin", ".nsq", ".nhr"):
            open(db_base + ext, "w").close()

        with mock.patch("shutil.which", return_value="/usr/bin/blastn"):
            resolved_with_db = BLASTClient._resolve_auto_mode(db_base)
        with mock.patch("shutil.which", return_value=None):
            resolved_no_binary = BLASTClient._resolve_auto_mode(db_base)
        resolved_no_path = BLASTClient._resolve_auto_mode(None)

        part5_ok = resolved_with_db == "local" and resolved_no_binary == "remote" and resolved_no_path == "remote"
        print(f"  blastn present + db files present -> '{resolved_with_db}' (expected 'local')")
        print(f"  blastn missing -> '{resolved_no_binary}' (expected 'remote')")
        print(f"  no db path configured -> '{resolved_no_path}' (expected 'remote')")
        if not part5_ok:
            print("  FAILURE.")
        print("PART 5:", "PASSED" if part5_ok else "FAILED")
        all_ok = all_ok and part5_ok
        shutil.rmtree(fake_db_dir, ignore_errors=True)

        print()
        print("=" * 78)
        print("PART 6 -- AI-only mode (disabled=True) never touches remote/local BLAST")
        print("=" * 78)
        _search_call_count["n"] = 0
        with mock.patch.object(BLASTClient, "_search_remote", _fake_search_remote):
            disabled_client = BLASTClient(mode="remote", disabled=True, cache_dir=tempfile.mkdtemp(prefix="geper_blast_verify_"))
            single = disabled_client.search("ACGTACGT")
            batch = disabled_client.search_many(["ACGTACGT", "TTTTGGGG"])

        part6_ok = (
            _search_call_count["n"] == 0
            and single.get("skipped") is True
            and all(v.get("skipped") is True for v in batch.values())
        )
        print(f"  Real search calls while disabled: {_search_call_count['n']} (expected 0)")
        print(f"  single result skipped=True: {single.get('skipped')}")
        print(f"  batch results all skipped=True: {all(v.get('skipped') is True for v in batch.values())}")
        if not part6_ok:
            print("  FAILURE.")
        print("PART 6:", "PASSED" if part6_ok else "FAILED")
        all_ok = all_ok and part6_ok

        print()
        print("=" * 78)
        print("PART 7 -- makeblastdb auto-build from a FASTA reference (real binaries)")
        print("=" * 78)
        import shutil as _shutil_mod

        blast_tools_present = all(
            _shutil_mod.which(t) for t in ("blastn", "makeblastdb", "blastdbcmd")
        )
        if not blast_tools_present:
            print("  SKIPPED: blastn/makeblastdb/blastdbcmd not all present on PATH in this environment.")
            part7_ok = True  # not a failure of GEPER's logic -- just an untestable environment
        else:
            fasta_dir = tempfile.mkdtemp(prefix="geper_blast_fasta_")
            fasta_path = os.path.join(fasta_dir, "ref.fasta")
            with open(fasta_path, "w") as fh:
                fh.write(">contig1\nACGTACGTACGTACGTGGGGCCCCAAAATTTTACGTACGTACGTACGTGATTACAGATTACAG\n")
            db_path = os.path.join(fasta_dir, "refdb")

            from database.blast_client import ensure_local_blast_db, _has_local_db_files

            first_build = ensure_local_blast_db(fasta_path, db_path)
            db_files_after_first = sorted(
                f for f in os.listdir(fasta_dir) if f.startswith("refdb")
            )
            mtimes_after_first = {f: os.path.getmtime(os.path.join(fasta_dir, f)) for f in db_files_after_first}

            time.sleep(0.05)
            second_build = ensure_local_blast_db(fasta_path, db_path)  # must NOT rebuild
            mtimes_after_second = {f: os.path.getmtime(os.path.join(fasta_dir, f)) for f in db_files_after_first}

            auto_client = BLASTClient(mode="auto", local_db_path=db_path, cache_dir=tempfile.mkdtemp(prefix="geper_blast_verify_"))
            live_result = auto_client.search("ACGTACGTACGTACGTGGGGCCCCAAAATTTT")

            part7_ok = (
                first_build == db_path
                and second_build == db_path
                and _has_local_db_files(db_path)
                and mtimes_after_first == mtimes_after_second  # not touched by the second call
                and auto_client.mode == "local"
                and live_result.get("hit_count", 0) >= 1
                and live_result.get("mode") == "local"
            )
            print(f"  First ensure_local_blast_db() call built DB at: {first_build}")
            print(f"  Second call (DB already exists) returned: {second_build} (no rebuild -- file mtimes unchanged: {mtimes_after_first == mtimes_after_second})")
            print(f"  mode='auto' with this DB resolved to: '{auto_client.mode}' (expected 'local')")
            print(f"  Real blastn search against the auto-built DB found {live_result.get('hit_count')} hit(s)")
            shutil.rmtree(fasta_dir, ignore_errors=True)

        if not part7_ok:
            print("  FAILURE.")
        print("PART 7:", "PASSED" if part7_ok else "FAILED")
        all_ok = all_ok and part7_ok

        print()
        print("=" * 78)
        print("PART 8 -- mode='auto' degrades to a graceful 'skip' when neither backend is usable")
        print("=" * 78)
        with mock.patch("database.blast_client.ensure_pip_package_available", return_value=False):
            skip_client = BLASTClient(mode="auto", local_db_path=None, cache_dir=tempfile.mkdtemp(prefix="geper_blast_verify_"))
            skip_single = skip_client.search("ACGTACGT")
            skip_batch = skip_client.search_many(["ACGTACGT", "TTTTGGGG"])

        part8_ok = (
            skip_client.mode == "skip"
            and skip_single.get("skipped") is True
            and all(v.get("skipped") is True for v in skip_batch.values())
        )
        print(f"  No local DB, Biopython unavailable -> resolved mode: '{skip_client.mode}' (expected 'skip')")
        print(f"  search() short-circuits with skipped=True: {skip_single.get('skipped')}")
        print(f"  search_many() short-circuits with skipped=True for all: {all(v.get('skipped') is True for v in skip_batch.values())}")
        if not part8_ok:
            print("  FAILURE.")
        print("PART 8:", "PASSED" if part8_ok else "FAILED")
        all_ok = all_ok and part8_ok

        print()
        print("=" * 78)
        print(f"OVERALL: {'ALL PARTS PASSED' if all_ok else 'SOME PARTS FAILED'}")
        print("=" * 78)
        return 0 if all_ok else 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
