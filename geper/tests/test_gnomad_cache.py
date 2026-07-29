"""Unit tests for pipeline/gnomad/cache.py -- TTL, LRU eviction, disk persistence."""

import os
import tempfile
import time
import unittest

from pipeline.gnomad.cache import GnomadCache


class TestGnomadCacheInMemory(unittest.TestCase):
    def test_put_then_get(self):
        cache = GnomadCache(max_size=10, ttl_seconds=None)
        cache.put("k1", {"found": True})
        self.assertEqual(cache.get("k1"), {"found": True})

    def test_miss_returns_none_and_counts(self):
        cache = GnomadCache(max_size=10, ttl_seconds=None)
        self.assertIsNone(cache.get("missing"))
        self.assertEqual(cache.stats()["misses"], 1)

    def test_ttl_expiry(self):
        cache = GnomadCache(max_size=10, ttl_seconds=0.05)
        cache.put("k1", {"found": True})
        self.assertIsNotNone(cache.get("k1"))
        time.sleep(0.1)
        self.assertIsNone(cache.get("k1"))

    def test_lru_eviction_when_over_max_size(self):
        cache = GnomadCache(max_size=2, ttl_seconds=None)
        cache.put("k1", {"v": 1})
        cache.put("k2", {"v": 2})
        cache.put("k3", {"v": 3})  # should evict k1 (least recently used)
        self.assertIsNone(cache.get("k1"))
        self.assertIsNotNone(cache.get("k2"))
        self.assertIsNotNone(cache.get("k3"))

    def test_get_refreshes_recency(self):
        cache = GnomadCache(max_size=2, ttl_seconds=None)
        cache.put("k1", {"v": 1})
        cache.put("k2", {"v": 2})
        cache.get("k1")  # k1 now most-recently-used
        cache.put("k3", {"v": 3})  # should evict k2, not k1
        self.assertIsNotNone(cache.get("k1"))
        self.assertIsNone(cache.get("k2"))

    def test_clear_resets_stats(self):
        cache = GnomadCache(max_size=10, ttl_seconds=None)
        cache.put("k1", {"v": 1})
        cache.get("k1")
        cache.clear()
        stats = cache.stats()
        self.assertEqual(stats["size"], 0)
        self.assertEqual(stats["hits"], 0)


class TestGnomadCacheDiskPersistence(unittest.TestCase):
    def test_entries_survive_a_new_cache_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk_path = os.path.join(tmp, "gnomad_cache.jsonl")
            cache1 = GnomadCache(max_size=100, ttl_seconds=None, disk_path=disk_path)
            cache1.put("k1", {"found": True, "global_af": 0.01})

            cache2 = GnomadCache(max_size=100, ttl_seconds=None, disk_path=disk_path)
            self.assertEqual(cache2.get("k1"), {"found": True, "global_af": 0.01})

    def test_missing_disk_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk_path = os.path.join(tmp, "does_not_exist_yet.jsonl")
            cache = GnomadCache(max_size=100, ttl_seconds=None, disk_path=disk_path)
            self.assertIsNone(cache.get("anything"))

    def test_corrupt_line_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk_path = os.path.join(tmp, "gnomad_cache.jsonl")
            with open(disk_path, "w") as fh:
                fh.write("not valid json\n")
                fh.write('{"key": "k2", "value": {"found": false}}\n')
            cache = GnomadCache(max_size=100, ttl_seconds=None, disk_path=disk_path)
            self.assertEqual(cache.get("k2"), {"found": False})

    def test_unwritable_disk_path_does_not_break_put(self):
        # A read-only filesystem must not turn a cache write into a
        # hard failure of the lookup itself.
        cache = GnomadCache(max_size=10, ttl_seconds=None, disk_path="/nonexistent_root_dir/x/cache.jsonl")
        cache.put("k1", {"v": 1})  # must not raise
        self.assertEqual(cache.get("k1"), {"v": 1})  # in-memory layer still works


if __name__ == "__main__":
    unittest.main()
