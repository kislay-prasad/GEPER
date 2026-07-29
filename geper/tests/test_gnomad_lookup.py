"""Tests for pipeline/gnomad/lookup.py: caching behavior, offline mode, batch/async paths."""

import unittest
from unittest import mock

from pipeline.gnomad.cache import GnomadCache
from pipeline.gnomad.lookup import GnomadLookup
from pipeline.vcf_parser import Variant


def _make_variant(chrom="1", pos=100, ref="A", alt="T") -> Variant:
    return Variant(chrom=chrom, pos=pos, variant_id=".", ref=ref, alt=alt, qual=None, filter_status=None)


class TestGnomadLookupDisabled(unittest.TestCase):
    def test_disabled_flag_skips_entirely_without_touching_provider(self):
        provider = mock.Mock()
        lookup = GnomadLookup(provider=provider, cache=None)
        with mock.patch("pipeline.gnomad.lookup.CONFIG") as fake_config:
            fake_config.gnomad.ENABLED = False
            result = lookup.query_variant(_make_variant())
        provider.query.assert_not_called()
        self.assertTrue(result["skipped"])
        self.assertFalse(result["found"])


class TestGnomadLookupCaching(unittest.TestCase):
    def test_second_call_hits_cache_not_provider(self):
        provider = mock.Mock()
        annotation = mock.Mock()
        annotation.to_dict.return_value = {"found": True, "global_af": 0.01}
        annotation.error = None
        provider.query.return_value = annotation

        cache = GnomadCache(max_size=100, ttl_seconds=None)
        lookup = GnomadLookup(provider=provider, cache=cache)
        with mock.patch("pipeline.gnomad.lookup.CONFIG") as fake_config:
            fake_config.gnomad.ENABLED = True
            v = _make_variant()
            first = lookup.query_variant(v, assembly="GRCh38")
            second = lookup.query_variant(v, assembly="GRCh38")

        self.assertEqual(provider.query.call_count, 1)  # only the first call actually queried
        self.assertEqual(second["source"], "cache")
        self.assertTrue(first["found"])

    def test_error_results_are_not_cached(self):
        provider = mock.Mock()
        annotation = mock.Mock()
        annotation.to_dict.return_value = {"found": False, "error": "timeout"}
        annotation.error = "timeout"
        provider.query.return_value = annotation

        cache = GnomadCache(max_size=100, ttl_seconds=None)
        lookup = GnomadLookup(provider=provider, cache=cache)
        with mock.patch("pipeline.gnomad.lookup.CONFIG") as fake_config:
            fake_config.gnomad.ENABLED = True
            v = _make_variant()
            lookup.query_variant(v, assembly="GRCh38")
            lookup.query_variant(v, assembly="GRCh38")

        self.assertEqual(provider.query.call_count, 2)  # not cached -> queried again


class TestGnomadLookupBatch(unittest.TestCase):
    def test_batch_uses_cache_for_already_seen_variants(self):
        provider = mock.Mock()

        def fake_annotation(pos):
            ann = mock.Mock()
            ann.to_dict.return_value = {"found": True, "pos": pos}
            ann.error = None
            return ann

        provider.batch_query.return_value = [fake_annotation(2)]
        cache = GnomadCache(max_size=100, ttl_seconds=None)
        lookup = GnomadLookup(provider=provider, cache=cache)

        with mock.patch("pipeline.gnomad.lookup.CONFIG") as fake_config:
            fake_config.gnomad.ENABLED = True
            v1, v2 = _make_variant(pos=1), _make_variant(pos=2)
            # Pre-warm the cache for v1 only.
            from pipeline.gnomad.utils import variant_key
            cache.put(variant_key(v1.chrom, v1.pos, v1.ref, v1.alt, "GRCh38"), {"found": True, "pos": 1})

            results = lookup.query_variants_batch([v1, v2], assembly="GRCh38")

        provider.batch_query.assert_called_once()
        (called_args, _), = [provider.batch_query.call_args]
        self.assertEqual(len(called_args[0]), 1)  # only v2 was actually fetched
        self.assertEqual([r["pos"] for r in results], [1, 2])  # order preserved


class TestGnomadLookupAsync(unittest.TestCase):
    def test_async_batch_returns_in_order(self):
        import asyncio

        provider = mock.Mock()

        async def fake_async_batch_query(variants):
            return [self._ann(pos) for (_, pos, *_rest) in variants]

        provider.async_batch_query = fake_async_batch_query
        lookup = GnomadLookup(provider=provider, cache=None)

        with mock.patch("pipeline.gnomad.lookup.CONFIG") as fake_config:
            fake_config.gnomad.ENABLED = True
            variants = [_make_variant(pos=p) for p in (10, 20, 30)]
            results = asyncio.run(lookup.async_query_variants_batch(variants, assembly="GRCh38"))

        self.assertEqual([r["pos"] for r in results], [10, 20, 30])

    @staticmethod
    def _ann(pos):
        ann = mock.Mock()
        ann.to_dict.return_value = {"found": True, "pos": pos}
        ann.error = None
        return ann


if __name__ == "__main__":
    unittest.main()
