"""
Verification harness for the Ensembl fetch caching + batch-prefetch
performance optimization.

WHAT'S BEING VERIFIED
-----------------------
1. Caching: two variants that need the identical (chrom, start, end)
   window (e.g. a multi-allelic site normalized into two Variant
   records) trigger exactly ONE network fetch, not two -- and both
   variants still get the exact same, correct sequence.
2. Batching: prefetch_regions() issues one POST per batch instead of
   one GET per variant, and the sequences it caches are IDENTICAL to
   what the unbatched per-variant GET path would have produced for the
   same regions (this is the correctness property that matters most:
   the optimization must be invisible to prediction output).
3. Fail-safe fallback: if the batch POST fails, or returns a malformed/
   wrong-length entry, prefetch_regions must not raise, and
   build_context must still succeed afterwards (falling back to its
   normal per-variant GET), producing the exact same sequence it
   always would have.

WHY A MOCK ENSEMBL LAYER
--------------------------
This sandbox's outbound network doesn't reach Ensembl (only package
registries are allowlisted). Only `requests.Session.get` /
`requests.Session.post` are replaced with a small fake genome server;
every other line of real GEPER code (SequenceContextGenerator's window
math, its cache, its batching/fallback logic) runs unmodified.
"""

import sys
from typing import Dict
from unittest import mock

import os as _os

_SCRIPT_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# A trivial deterministic "genome": every position's base is derived
# from its own coordinate, so any two fetches of the same region are
# trivially checkable for exact equality, and different regions are
# trivially checkable for inequality.
_BASES = "ACGT"


def _fake_genome_base(pos: int) -> str:
    return _BASES[pos % 4]


def _fake_genome_region(chrom: str, start: int, end: int) -> str:
    return "".join(_fake_genome_base(p) for p in range(start, end + 1))


class _FakeResponse:
    def __init__(self, json_body):
        self._json_body = json_body

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_body


_get_call_count = {"n": 0}
_post_call_count = {"n": 0}


def _fake_session_get(self, url, params=None, timeout=None):
    _get_call_count["n"] += 1
    # url is ".../sequence/region/human/1:990-1010"
    region = url.rsplit("/", 1)[-1]
    chrom, coords = region.split(":")
    start_s, end_s = coords.split("-")
    start, end = int(start_s), int(end_s)
    return _FakeResponse({"seq": _fake_genome_region(chrom, start, end)})


def _fake_session_post(self, url, params=None, json=None, headers=None, timeout=None):
    _post_call_count["n"] += 1
    entries = []
    for region_str in json["regions"]:
        chrom, coords = region_str.split(":")
        start_s, end_s = coords.split("..")
        start, end = int(start_s), int(end_s)
        entries.append({"seq": _fake_genome_region(chrom, start, end)})
    return _FakeResponse(entries)


def main() -> int:
    import requests
    from pipeline.sequence_context import SequenceContextGenerator
    from pipeline.vcf_parser import Variant

    all_ok = True

    print("=" * 78)
    print("PART 1 -- Cache dedupes identical windows (multi-allelic site)")
    print("=" * 78)
    _get_call_count["n"] = 0
    with mock.patch.object(requests.Session, "get", _fake_session_get):
        gen = SequenceContextGenerator(species="human", assembly="GRCh38")
        v1 = Variant("1", 1000, ".", "A", "G", None, None, {})
        v2 = Variant("1", 1000, ".", "A", "T", None, None, {})  # same site, 2nd ALT

        ctx1 = gen.build_context(v1, flank_size=10)
        ctx2 = gen.build_context(v2, flank_size=10)

        print(f"  Fetches made: {_get_call_count['n']} (expected 1)")
        print(f"  ctx1.ref_sequence == ctx2.ref_sequence: {ctx1.ref_sequence == ctx2.ref_sequence}")

        if _get_call_count["n"] != 1:
            print("  FAILURE: expected exactly 1 network fetch for the shared window.")
            all_ok = False
        if ctx1.ref_sequence != ctx2.ref_sequence:
            print("  FAILURE: same window produced different reference sequences!")
            all_ok = False
        if ctx1.alt_sequence == ctx2.alt_sequence:
            print("  FAILURE: different ALT alleles produced identical alt_sequence!")
            all_ok = False
        else:
            print("  OK: alt_sequence still correctly differs per-ALT despite the shared cache entry.")

    print("PART 1:", "PASSED" if all_ok else "FAILED")

    print()
    print("=" * 78)
    print("PART 2 -- Batch prefetch produces identical sequences to unbatched GET")
    print("=" * 78)
    part2_ok = True
    variants = [
        Variant("1", 1000, ".", "A", "G", None, None, {}),
        Variant("1", 5000, ".", "C", "T", None, None, {}),
        Variant("2", 2000, ".", "G", "A", None, None, {}),
    ]

    # Reference run: no prefetch, pure per-variant GET (the original,
    # already-correct code path) -- this is the ground truth.
    _get_call_count["n"] = 0
    with mock.patch.object(requests.Session, "get", _fake_session_get):
        gen_unbatched = SequenceContextGenerator(species="human", assembly="GRCh38")
        baseline = {
            (v.chrom, v.pos): gen_unbatched.build_context(v, flank_size=10).ref_sequence
            for v in variants
        }
    baseline_gets = _get_call_count["n"]

    # Optimized run: prefetch first (batched POST), then build_context
    # for every variant -- should hit the cache every time (0 GETs).
    _get_call_count["n"] = 0
    _post_call_count["n"] = 0
    with mock.patch.object(requests.Session, "get", _fake_session_get), \
         mock.patch.object(requests.Session, "post", _fake_session_post):
        gen_batched = SequenceContextGenerator(species="human", assembly="GRCh38")
        gen_batched.prefetch_regions(variants, lambda v: 10)
        optimized = {
            (v.chrom, v.pos): gen_batched.build_context(v, flank_size=10).ref_sequence
            for v in variants
        }

    print(f"  Baseline (unbatched) GETs: {baseline_gets}")
    print(f"  Optimized POSTs: {_post_call_count['n']}, GETs after prefetch: {_get_call_count['n']}")
    for key in baseline:
        match = baseline[key] == optimized[key]
        print(f"  {key}: baseline == optimized -> {match}")
        if not match:
            part2_ok = False
    if _get_call_count["n"] != 0:
        print("  FAILURE: expected 0 individual GETs after a fully successful prefetch.")
        part2_ok = False
    if _post_call_count["n"] != 1:
        print(f"  FAILURE: expected exactly 1 batch POST for 3 variants under batch_size=50, got {_post_call_count['n']}.")
        part2_ok = False

    print("PART 2:", "PASSED" if part2_ok else "FAILED")
    all_ok = all_ok and part2_ok

    print()
    print("=" * 78)
    print("PART 3 -- Batch failure falls back safely to per-variant GET")
    print("=" * 78)
    part3_ok = True

    def _broken_post(self, url, params=None, json=None, headers=None, timeout=None):
        raise requests.exceptions.ConnectionError("simulated Ensembl outage")

    _get_call_count["n"] = 0
    with mock.patch.object(requests.Session, "get", _fake_session_get), \
         mock.patch.object(requests.Session, "post", _broken_post):
        gen_fallback = SequenceContextGenerator(species="human", assembly="GRCh38")
        try:
            gen_fallback.prefetch_regions(variants, lambda v: 10)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILURE: prefetch_regions raised instead of degrading gracefully: {exc}")
            part3_ok = False

        fallback_result = {
            (v.chrom, v.pos): gen_fallback.build_context(v, flank_size=10).ref_sequence
            for v in variants
        }

    print(f"  GETs after failed prefetch: {_get_call_count['n']} (expected {len(variants)}, one per variant)")
    if _get_call_count["n"] != len(variants):
        print("  FAILURE: fallback did not issue the expected per-variant GETs.")
        part3_ok = False
    for key in baseline:
        match = baseline[key] == fallback_result[key]
        print(f"  {key}: baseline == fallback -> {match}")
        if not match:
            part3_ok = False

    # Malformed-entry case: batch "succeeds" but returns a wrong-length
    # sequence for one region -- must be discarded, not cached.
    def _malformed_post(self, url, params=None, json=None, headers=None, timeout=None):
        return _FakeResponse([{"seq": "AC"}] * len(json["regions"]))  # wrong length

    _get_call_count["n"] = 0
    with mock.patch.object(requests.Session, "get", _fake_session_get), \
         mock.patch.object(requests.Session, "post", _malformed_post):
        gen_malformed = SequenceContextGenerator(species="human", assembly="GRCh38")
        gen_malformed.prefetch_regions(variants, lambda v: 10)
        malformed_result = {
            (v.chrom, v.pos): gen_malformed.build_context(v, flank_size=10).ref_sequence
            for v in variants
        }
    print(f"  GETs after malformed-batch prefetch: {_get_call_count['n']} (expected {len(variants)})")
    if _get_call_count["n"] != len(variants):
        print("  FAILURE: malformed batch entries were cached instead of discarded.")
        part3_ok = False
    for key in baseline:
        match = baseline[key] == malformed_result[key]
        if not match:
            print(f"  FAILURE: {key} used a malformed cached sequence instead of falling back!")
            part3_ok = False

    print("PART 3:", "PASSED" if part3_ok else "FAILED")
    all_ok = all_ok and part3_ok

    print()
    print("=" * 78)
    print("OVERALL:", "PASSED" if all_ok else "FAILED")
    print("=" * 78)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
