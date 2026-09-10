"""
tests/test_lookup_cache.py
──────────────────────────
Tests for Task 3: per-run in-memory lookup caches.

Covers:
- Calling lookup() twice with same args only calls underlying method once
- cache_stats() reports correct hit/miss counts
- clear_cache() resets the cache and next lookup is a miss
- All 4 lookup classes: ClinVar, gnomAD, Constraint, Hotspot
"""

from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.clinvar.lookup import ClinVarLookup, ClinVarHit
from pipeline.gnomad.lookup import GnomadLookup, GnomadHit
from pipeline.constraint.lookup import GnomadConstraintLookup, ConstraintRecord
from pipeline.hotspot.lookup import HotspotLookup


# ─── ClinVar Cache Tests ──────────────────────────────────────────────────────


class TestClinVarCache(unittest.TestCase):
    def _make_lookup(self) -> ClinVarLookup:
        """Create ClinVarLookup with no real backend."""
        lkp = ClinVarLookup(cfg={})
        lkp._backend = "api"
        return lkp

    def test_double_lookup_calls_api_once(self):
        """Two identical lookups should only call _api_lookup once."""
        lkp = self._make_lookup()
        fake_hit = ClinVarHit("Pathogenic", 3, 1, "CV123", False, "BRCA1")
        with patch.object(lkp, "_api_lookup", return_value=fake_hit) as mock_api:
            r1 = lkp.lookup("chr17", 43057051, "A", "T")
            r2 = lkp.lookup("chr17", 43057051, "A", "T")
            self.assertEqual(mock_api.call_count, 1)
        self.assertIs(r1, fake_hit)
        self.assertIs(r2, fake_hit)

    def test_cache_stats_hit_miss(self):
        """cache_stats() should report correct hits and misses."""
        lkp = self._make_lookup()
        fake_hit = ClinVarHit("Benign", 1, 1, "CV001", False)
        with patch.object(lkp, "_api_lookup", return_value=fake_hit):
            lkp.lookup("1", 100, "A", "G")  # miss
            lkp.lookup("1", 100, "A", "G")  # hit
            lkp.lookup("1", 200, "C", "T")  # miss

        stats = lkp.cache_stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 2)
        self.assertEqual(stats["size"], 2)

    def test_clear_cache_resets(self):
        """clear_cache() should cause the next call to be a miss."""
        lkp = self._make_lookup()
        fake_hit = ClinVarHit("Pathogenic", 2, 1, "CV999", False)
        with patch.object(lkp, "_api_lookup", return_value=fake_hit) as mock_api:
            lkp.lookup("2", 500, "G", "A")  # miss #1
            lkp.clear_cache()
            lkp.lookup("2", 500, "G", "A")  # miss #2 after clear
            self.assertEqual(mock_api.call_count, 2)

        stats = lkp.cache_stats()
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["misses"], 1)

    def test_none_result_is_cached(self):
        """A None return value should be cached to avoid re-querying."""
        lkp = self._make_lookup()
        with patch.object(lkp, "_api_lookup", return_value=None) as mock_api:
            r1 = lkp.lookup("X", 999, "T", "C")
            r2 = lkp.lookup("X", 999, "T", "C")
        self.assertIsNone(r1)
        self.assertIsNone(r2)
        self.assertEqual(mock_api.call_count, 1)
        stats = lkp.cache_stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)

    def test_different_variants_are_separate_cache_entries(self):
        """Different (chrom, pos, ref, alt) tuples get separate cache entries."""
        lkp = self._make_lookup()
        h1 = ClinVarHit("Pathogenic", 3, 1, "CV1", False)
        h2 = ClinVarHit("Benign", 1, 1, "CV2", False)
        with patch.object(lkp, "_api_lookup", side_effect=[h1, h2]) as mock_api:
            lkp.lookup("1", 100, "A", "G")
            lkp.lookup("1", 200, "C", "T")
        self.assertEqual(mock_api.call_count, 2)
        self.assertEqual(lkp.cache_stats()["size"], 2)

    def test_chr_prefix_normalisation_in_cache_key(self):
        """chr1 and 1 should resolve to the same cache entry."""
        lkp = self._make_lookup()
        fake = ClinVarHit("VUS", 0, 0, "CV0", False)
        with patch.object(lkp, "_api_lookup", return_value=fake) as mock_api:
            lkp.lookup("chr1", 100, "A", "G")
            lkp.lookup("1", 100, "A", "G")  # same after normalisation
        self.assertEqual(mock_api.call_count, 1)


# ─── gnomAD Cache Tests ───────────────────────────────────────────────────────


class TestGnomadCache(unittest.TestCase):
    def _make_lookup(self) -> GnomadLookup:
        lkp = GnomadLookup(cfg={})
        lkp._backend = "api"
        return lkp

    def test_double_lookup_calls_api_once(self):
        lkp = self._make_lookup()
        fake = GnomadHit(af=0.001, af_popmax=0.002, ac=10, an=10000, backend_used="api")
        with patch.object(lkp, "_api_lookup", return_value=fake) as mock_api:
            r1 = lkp.lookup("17", 43057051, "A", "T")
            r2 = lkp.lookup("17", 43057051, "A", "T")
        self.assertIs(r1, fake)
        self.assertIs(r2, fake)
        self.assertEqual(mock_api.call_count, 1)

    def test_cache_stats_correct(self):
        lkp = self._make_lookup()
        with patch.object(lkp, "_api_lookup", return_value=None):
            lkp.lookup("1", 100, "A", "G")
            lkp.lookup("1", 100, "A", "G")
        stats = lkp.cache_stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["size"], 1)

    def test_clear_cache_causes_miss(self):
        """clear_cache() resets the in-memory (per-run) cache and its
        hit/miss counters — but gnomAD now also has a persistent SQLite
        disk cache (Issue 2) that intentionally continues to serve an
        already-known PRESENT/ABSENT result without re-hitting the
        network, even across a clear_cache() call — that persistence
        across a fresh session/cache-clear is the entire point of the
        disk cache. Assert on the counter reset (what clear_cache() is
        actually documented to do) and on the disk cache being what
        served the second call, not on a second live network hit."""
        lkp = self._make_lookup()
        fake = GnomadHit(0.0, 0.0, 0, 1000, "api")
        with patch.object(lkp, "_api_lookup", return_value=fake) as mock_api:
            lkp.lookup("2", 200, "C", "T")
            self.assertEqual(lkp.cache_stats()["size"], 1)
            lkp.clear_cache()
            self.assertEqual(lkp.cache_stats()["hits"], 0)
            self.assertEqual(lkp.cache_stats()["misses"], 0)
            self.assertEqual(lkp.cache_stats()["size"], 0)
            result = lkp.lookup("2", 200, "C", "T")
        # Served from the persistent disk cache, not a second network call.
        self.assertEqual(mock_api.call_count, 1)
        self.assertIsInstance(result, GnomadHit)

    def test_none_cached(self):
        lkp = self._make_lookup()
        with patch.object(lkp, "_api_lookup", return_value=None) as mock_api:
            lkp.lookup("3", 300, "G", "A")
            lkp.lookup("3", 300, "G", "A")
        self.assertEqual(mock_api.call_count, 1)


# ─── Constraint Cache Tests ───────────────────────────────────────────────────


class TestConstraintCache(unittest.TestCase):
    def _make_lookup(self) -> GnomadConstraintLookup:
        lkp = GnomadConstraintLookup(cfg={})
        lkp._backend = "api"
        return lkp

    def test_double_lookup_calls_internal_once(self):
        lkp = self._make_lookup()
        fake = ConstraintRecord("BRCA1", pli=0.99, loeuf=0.1, backend_used="api")
        with patch.object(lkp, "_uncached_lookup", return_value=fake) as mock_internal:
            r1 = lkp.lookup("BRCA1")
            r2 = lkp.lookup("BRCA1")
        self.assertIs(r1, fake)
        self.assertIs(r2, fake)
        self.assertEqual(mock_internal.call_count, 1)

    def test_cache_stats(self):
        lkp = self._make_lookup()
        with patch.object(lkp, "_uncached_lookup", return_value=None):
            lkp.lookup("GENE1")
            lkp.lookup("GENE1")
            lkp.lookup("GENE2")
        stats = lkp.cache_stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 2)
        self.assertEqual(stats["size"], 2)

    def test_clear_cache(self):
        lkp = self._make_lookup()
        with patch.object(lkp, "_uncached_lookup", return_value=None) as mock_internal:
            lkp.lookup("SCN5A")
            lkp.clear_cache()
            lkp.lookup("SCN5A")
        self.assertEqual(mock_internal.call_count, 2)

    def test_none_result_cached(self):
        lkp = self._make_lookup()
        with patch.object(lkp, "_uncached_lookup", return_value=None) as mock_internal:
            lkp.lookup("UNKNOWN_GENE")
            lkp.lookup("UNKNOWN_GENE")
        self.assertEqual(mock_internal.call_count, 1)


# ─── Hotspot Cache Tests ──────────────────────────────────────────────────────


class TestHotspotCache(unittest.TestCase):
    def _make_lookup(self) -> HotspotLookup:
        lkp = HotspotLookup(cfg={})
        return lkp

    def test_cache_stats_initial(self):
        lkp = self._make_lookup()
        stats = lkp.cache_stats()
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["misses"], 0)
        self.assertEqual(stats["size"], 0)

    def test_clear_cache(self):
        lkp = self._make_lookup()
        # Manually insert something into cache
        lkp._cache["1:100"] = None
        lkp._cache_hits = 5
        lkp._cache_misses = 3
        lkp.clear_cache()
        stats = lkp.cache_stats()
        self.assertEqual(stats["size"], 0)
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["misses"], 0)

    def test_is_in_hotspot_uses_no_external_call(self):
        """is_in_hotspot with empty config and no local data returns None (not-evaluated)."""
        lkp = self._make_lookup()
        result = lkp.is_in_hotspot("1", 12345, "UNKNOWN_GENE")
        self.assertIsNone(result)

    def test_lookup_caches_result(self):
        """lookup() method caches results between calls."""
        lkp = self._make_lookup()
        with patch.object(lkp, "_lookup_internal", return_value=None) as mock_internal:
            lkp.lookup("1", 100)
            lkp.lookup("1", 100)
        # _lookup_internal should only be called once
        self.assertEqual(mock_internal.call_count, 1)
        stats = lkp.cache_stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)


if __name__ == "__main__":
    unittest.main()
