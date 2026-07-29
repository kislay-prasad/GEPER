"""Unit tests for pipeline/clingen/cache.py -- TTL, LRU eviction, disk persistence."""

import os
import tempfile
import time
import unittest

from pipeline.clingen.cache import ClinGenCache


class TestClinGenCacheInMemory(unittest.TestCase):
    def test_put_then_get(self):
        cache = ClinGenCache(max_size=10, ttl_seconds=None)
        cache.put("gene:BRCA1", {"found": True})
        self.assertEqual(cache.get("gene:BRCA1"), {"found": True})

    def test_miss_returns_none_and_counts(self):
        cache = ClinGenCache(max_size=10, ttl_seconds=None)
        self.assertIsNone(cache.get("missing"))
        self.assertEqual(cache.stats()["misses"], 1)

    def test_ttl_expiry(self):
        cache = ClinGenCache(max_size=10, ttl_seconds=0.05)
        cache.put("gene:BRCA1", {"found": True})
        self.assertIsNotNone(cache.get("gene:BRCA1"))
        time.sleep(0.1)
        self.assertIsNone(cache.get("gene:BRCA1"))

    def test_lru_eviction_when_over_max_size(self):
        cache = ClinGenCache(max_size=2, ttl_seconds=None)
        cache.put("gene:A", {"v": 1})
        cache.put("gene:B", {"v": 2})
        cache.put("gene:C", {"v": 3})  # should evict gene:A (least recently used)
        self.assertIsNone(cache.get("gene:A"))
        self.assertIsNotNone(cache.get("gene:B"))
        self.assertIsNotNone(cache.get("gene:C"))

    def test_clear_resets_stats_and_store(self):
        cache = ClinGenCache(max_size=10, ttl_seconds=None)
        cache.put("gene:A", {"v": 1})
        cache.get("gene:A")
        cache.clear()
        self.assertEqual(cache.stats(), {"size": 0, "hits": 0, "misses": 0})


class TestClinGenCacheDiskPersistence(unittest.TestCase):
    def test_persists_and_reloads_across_instances(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            disk_path = os.path.join(tmpdir, "clingen_cache.jsonl")
            cache1 = ClinGenCache(max_size=10, ttl_seconds=None, disk_path=disk_path)
            cache1.put("gene:BRCA1", {"found": True, "classification": "Definitive"})

            cache2 = ClinGenCache(max_size=10, ttl_seconds=None, disk_path=disk_path)
            self.assertEqual(cache2.get("gene:BRCA1"), {"found": True, "classification": "Definitive"})

    def test_missing_disk_path_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            disk_path = os.path.join(tmpdir, "does_not_exist.jsonl")
            cache = ClinGenCache(max_size=10, ttl_seconds=None, disk_path=disk_path)
            self.assertIsNone(cache.get("gene:X"))

    def test_read_only_filesystem_does_not_raise(self):
        cache = ClinGenCache(max_size=10, ttl_seconds=None, disk_path="/nonexistent_root_dir/x/cache.jsonl")
        # Must not raise even though the directory cannot be created.
        cache.put("gene:A", {"v": 1})
        self.assertEqual(cache.get("gene:A"), {"v": 1})


if __name__ == "__main__":
    unittest.main()
