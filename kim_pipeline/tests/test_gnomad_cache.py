"""
tests/test_gnomad_cache.py
───────────────────────────
Tests for pipeline.gnomad.cache.GnomadDiskCache and the
disk-cache/batch/retry additions to pipeline.gnomad.lookup.GnomadLookup.

All network access is mocked. All disk-cache paths use tmp_path so
nothing here touches a real ~/.cache directory or persists between
test runs.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from pipeline.gnomad.cache import GnomadDiskCache
from pipeline.gnomad.lookup import GnomadLookup, GnomadHit, GnomadLookupOutcome


def _api_cfg(tmp_path, **overrides):
    cfg = {
        "gnomad": {
            "vcf_path": "/nonexistent/path.vcf.gz",  # forces API backend
            "cache_dir": str(tmp_path / "cache"),
        }
    }
    cfg["gnomad"].update(overrides)
    return cfg


def _mock_graphql_response(af=0.001, populations=None, variant=True, variant_id_missing=False):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    if variant_id_missing:
        # Simulates a malformed/unrecognized variant ID — the API's
        # "variant" key itself is null. This codebase treats that as
        # UNAVAILABLE (see tests/test_gnomad.py::test_variant_not_in_gnomad_returns_none),
        # distinct from a real "genome: null" confirmed-absent response.
        resp.json.return_value = {"data": {"variant": None}}
    elif not variant:
        # Genuine confirmed-absent: variant object exists, genome is null.
        resp.json.return_value = {"data": {"variant": {"genome": None}}}
    else:
        resp.json.return_value = {
            "data": {"variant": {"genome": {"af": af, "populations": populations or []}}}
        }
    return resp


# ─── GnomadDiskCache unit tests ─────────────────────────────────────────────────

class TestGnomadDiskCache:
    def test_put_and_get_present(self, tmp_path):
        cache = GnomadDiskCache(path=str(tmp_path / "c.sqlite3"))
        cache.put("17:100:A:T", "present", af=0.01, af_popmax=0.02, ac=5, an=1000, backend="api")
        entry = cache.get("17:100:A:T")
        assert entry is not None
        assert entry["outcome"] == "present"
        assert entry["af"] == 0.01
        assert entry["af_popmax"] == 0.02

    def test_put_and_get_absent(self, tmp_path):
        cache = GnomadDiskCache(path=str(tmp_path / "c.sqlite3"))
        cache.put("17:200:A:T", "absent")
        entry = cache.get("17:200:A:T")
        assert entry is not None
        assert entry["outcome"] == "absent"

    def test_get_miss_returns_none(self, tmp_path):
        cache = GnomadDiskCache(path=str(tmp_path / "c.sqlite3"))
        assert cache.get("nonexistent:1:A:T") is None

    def test_unavailable_outcome_is_never_persisted(self, tmp_path):
        """Critical correctness rule: caching a transient network failure
        forever would permanently block PM2 evidence for that variant."""
        cache = GnomadDiskCache(path=str(tmp_path / "c.sqlite3"))
        cache.put("17:300:A:T", "unavailable")  # should be silently refused
        assert cache.get("17:300:A:T") is None
        assert cache.stats()["size"] == 0

    def test_persistence_across_new_instances(self, tmp_path):
        """The whole point of a disk cache: a second process/session
        opening the same file sees the first one's data."""
        path = str(tmp_path / "c.sqlite3")
        cache1 = GnomadDiskCache(path=path)
        cache1.put("17:400:A:T", "present", af=0.5, af_popmax=0.6, ac=1, an=2, backend="api")
        cache1.close()

        cache2 = GnomadDiskCache(path=path)  # simulates a fresh Colab session
        entry = cache2.get("17:400:A:T")
        assert entry is not None
        assert entry["af"] == 0.5

    def test_get_many_bulk_read(self, tmp_path):
        cache = GnomadDiskCache(path=str(tmp_path / "c.sqlite3"))
        cache.put("a", "present", af=0.1, af_popmax=0.1, ac=1, an=10, backend="api")
        cache.put("b", "absent")
        result = cache.get_many(["a", "b", "c"])
        assert set(result.keys()) == {"a", "b"}
        assert result["a"]["outcome"] == "present"
        assert result["b"]["outcome"] == "absent"

    def test_ttl_expiry(self, tmp_path):
        cache = GnomadDiskCache(path=str(tmp_path / "c.sqlite3"), ttl_hours=1e-9)  # ~3.6us
        cache.put("17:500:A:T", "present", af=0.1, af_popmax=0.1, ac=1, an=10, backend="api")
        time.sleep(0.01)
        assert cache.get("17:500:A:T") is None  # expired

    def test_no_ttl_never_expires(self, tmp_path):
        cache = GnomadDiskCache(path=str(tmp_path / "c.sqlite3"), ttl_hours=None)
        cache.put("17:600:A:T", "present", af=0.1, af_popmax=0.1, ac=1, an=10, backend="api")
        assert cache.get("17:600:A:T") is not None

    def test_unopenable_path_degrades_gracefully(self, tmp_path):
        """An invalid/unwritable path must never raise — cache.available
        becomes False and the pipeline continues without persistence."""
        bad_path = str(tmp_path / "not_a_dir" / "sub" / "x.sqlite3")
        # Make the parent unwritable to force an open failure in a
        # cross-platform-safe way: create a file where a directory is expected.
        (tmp_path / "not_a_dir").write_text("i am a file, not a directory")
        cache = GnomadDiskCache(path=bad_path)
        assert cache.available is False
        # get/put must be silent no-ops, not exceptions
        assert cache.get("x") is None
        cache.put("x", "present", af=0.1, af_popmax=0.1, ac=1, an=1, backend="api")

    def test_concurrent_writes_thread_safe(self, tmp_path):
        """ThreadPoolExecutor in lookup_batch() means many threads may
        write to the same cache concurrently — must not corrupt the file
        or raise 'database is locked'."""
        cache = GnomadDiskCache(path=str(tmp_path / "c.sqlite3"))
        errors = []

        def _writer(i):
            try:
                for j in range(20):
                    cache.put(f"17:{i}:{j}:T", "present", af=0.01, af_popmax=0.02, ac=1, an=100, backend="api")
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert cache.stats()["size"] == 8 * 20


# ─── lookup() backward compatibility ────────────────────────────────────────────

class TestLookupBackwardCompatibility:
    def test_lookup_signature_and_return_types_unchanged(self, tmp_path):
        gn = GnomadLookup(cfg=_api_cfg(tmp_path))
        with patch("pipeline.gnomad.lookup.requests.post", return_value=_mock_graphql_response(af=0.01)):
            result = gn.lookup("17", 1000, "A", "T")
        assert isinstance(result, GnomadHit)
        assert result.af == 0.01

    def test_lookup_absent_variant(self, tmp_path):
        gn = GnomadLookup(cfg=_api_cfg(tmp_path))
        with patch("pipeline.gnomad.lookup.requests.post", return_value=_mock_graphql_response(variant=False)):
            result = gn.lookup("17", 2000, "A", "T")
        assert result == GnomadLookupOutcome.ABSENT

    def test_lookup_disabled_returns_unavailable_without_network(self, tmp_path):
        cfg = _api_cfg(tmp_path, enabled=False)
        gn = GnomadLookup(cfg=cfg)
        with patch("pipeline.gnomad.lookup.requests.post") as mock_post:
            result = gn.lookup("17", 3000, "A", "T")
        assert result == GnomadLookupOutcome.UNAVAILABLE
        mock_post.assert_not_called()

    def test_existing_config_without_new_keys_still_works(self, tmp_path):
        """Backward compatibility: a config that predates cache_dir/
        max_concurrent/retry_backoff_secs must still construct and work."""
        cfg = {"gnomad": {"vcf_path": "/nonexistent/path.vcf.gz"}}
        gn = GnomadLookup(cfg=cfg)
        with patch("pipeline.gnomad.lookup.requests.post", return_value=_mock_graphql_response(af=0.02)):
            result = gn.lookup("17", 4000, "A", "T")
        assert isinstance(result, GnomadHit)


# ─── Cache hits/misses (memory + disk) ──────────────────────────────────────────

class TestLookupCaching:
    def test_second_lookup_is_memory_cache_hit(self, tmp_path):
        gn = GnomadLookup(cfg=_api_cfg(tmp_path))
        with patch("pipeline.gnomad.lookup.requests.post", return_value=_mock_graphql_response(af=0.03)) as mock_post:
            gn.lookup("17", 5000, "A", "T")
            gn.lookup("17", 5000, "A", "T")
        assert mock_post.call_count == 1
        assert gn.cache_stats()["hits"] == 1

    def test_lookup_persists_to_disk_and_new_instance_hits_cache(self, tmp_path):
        cfg = _api_cfg(tmp_path)
        gn1 = GnomadLookup(cfg=cfg)
        with patch("pipeline.gnomad.lookup.requests.post", return_value=_mock_graphql_response(af=0.04)) as mock_post:
            gn1.lookup("17", 6000, "A", "T")

        # Fresh instance (simulates a new pipeline run / Colab session)
        # pointed at the same cache_dir — must not hit the network at all.
        gn2 = GnomadLookup(cfg=cfg)
        with patch("pipeline.gnomad.lookup.requests.post") as mock_post2:
            result = gn2.lookup("17", 6000, "A", "T")
        mock_post2.assert_not_called()
        assert isinstance(result, GnomadHit)
        assert result.af == 0.04

    def test_unavailable_result_not_reused_across_instances(self, tmp_path):
        """An UNAVAILABLE result must NOT persist to disk — a fresh
        instance must retry the network rather than being permanently
        stuck with a transient failure."""
        cfg = _api_cfg(tmp_path)
        gn1 = GnomadLookup(cfg=cfg)
        with patch("pipeline.gnomad.lookup.requests.post", side_effect=ConnectionError("boom")):
            result1 = gn1.lookup("17", 7000, "A", "T")
        assert result1 == GnomadLookupOutcome.UNAVAILABLE

        gn2 = GnomadLookup(cfg=cfg)
        with patch("pipeline.gnomad.lookup.requests.post", return_value=_mock_graphql_response(af=0.05)) as mock_post2:
            result2 = gn2.lookup("17", 7000, "A", "T")
        mock_post2.assert_called()  # retried, not silently blocked by a stale cache entry
        assert isinstance(result2, GnomadHit)


# ─── lookup_batch() ─────────────────────────────────────────────────────────────

class TestLookupBatch:
    def test_batch_returns_result_for_every_distinct_variant(self, tmp_path):
        gn = GnomadLookup(cfg=_api_cfg(tmp_path))
        variants = [("17", 1, "A", "T"), ("17", 2, "C", "G"), ("MT", 3243, "A", "G")]
        with patch("pipeline.gnomad.lookup.requests.post", return_value=_mock_graphql_response(af=0.01)):
            results = gn.lookup_batch(variants)
        assert len(results) == 3
        for chrom, pos, ref, alt in variants:
            key = gn._cache_key(chrom, pos, ref, alt)
            assert key in results

    def test_duplicate_variants_trigger_only_one_remote_request(self, tmp_path):
        gn = GnomadLookup(cfg=_api_cfg(tmp_path))
        variants = [("17", 100, "A", "T")] * 10  # 10x the same variant
        with patch("pipeline.gnomad.lookup.requests.post", return_value=_mock_graphql_response(af=0.02)) as mock_post:
            results = gn.lookup_batch(variants)
        assert mock_post.call_count == 1
        assert len(results) == 1

    def test_batch_respects_max_concurrent_bound(self, tmp_path):
        """Bounded concurrency: never more than max_workers in flight at once."""
        gn = GnomadLookup(cfg=_api_cfg(tmp_path, max_concurrent=2))
        variants = [("17", i, "A", "T") for i in range(20)]

        in_flight = {"count": 0, "max_seen": 0}
        lock = threading.Lock()

        def slow_post(url, json=None, **kwargs):
            with lock:
                in_flight["count"] += 1
                in_flight["max_seen"] = max(in_flight["max_seen"], in_flight["count"])
            time.sleep(0.02)
            with lock:
                in_flight["count"] -= 1
            return _mock_graphql_response(af=0.01)

        with patch("pipeline.gnomad.lookup.requests.post", side_effect=slow_post):
            gn.lookup_batch(variants)

        assert in_flight["max_seen"] <= 2

    def test_batch_prefetch_makes_subsequent_lookup_calls_free(self, tmp_path):
        """The shared.py integration pattern: lookup_batch() first, then
        lookup() per-variant should be pure cache hits."""
        gn = GnomadLookup(cfg=_api_cfg(tmp_path))
        variants = [("17", 10, "A", "T"), ("17", 20, "C", "G")]
        with patch("pipeline.gnomad.lookup.requests.post", return_value=_mock_graphql_response(af=0.06)) as mock_post:
            gn.lookup_batch(variants)
            for chrom, pos, ref, alt in variants:
                gn.lookup(chrom, pos, ref, alt)
        assert mock_post.call_count == 2  # only the batch's own two calls

    def test_batch_uses_disk_cache_before_any_network_call(self, tmp_path):
        cfg = _api_cfg(tmp_path)
        gn1 = GnomadLookup(cfg=cfg)
        with patch("pipeline.gnomad.lookup.requests.post", return_value=_mock_graphql_response(af=0.07)):
            gn1.lookup("17", 900, "A", "T")

        gn2 = GnomadLookup(cfg=cfg)
        with patch("pipeline.gnomad.lookup.requests.post") as mock_post2:
            results = gn2.lookup_batch([("17", 900, "A", "T")])
        mock_post2.assert_not_called()
        key = gn2._cache_key("17", 900, "A", "T")
        assert results[key].af == 0.07

    def test_batch_disabled_returns_unavailable_for_all_without_network(self, tmp_path):
        gn = GnomadLookup(cfg=_api_cfg(tmp_path, enabled=False))
        with patch("pipeline.gnomad.lookup.requests.post") as mock_post:
            results = gn.lookup_batch([("17", 1, "A", "T"), ("17", 2, "C", "G")])
        mock_post.assert_not_called()
        assert all(r == GnomadLookupOutcome.UNAVAILABLE for r in results.values())

    def test_batch_one_failure_does_not_affect_others(self, tmp_path):
        gn = GnomadLookup(cfg=_api_cfg(tmp_path))
        variants = [("17", 1, "A", "T"), ("17", 2, "C", "G")]

        def flaky_post(url, json=None, **kwargs):
            if "1-A-T" in json["query"]:
                raise ConnectionError("simulated failure")
            return _mock_graphql_response(af=0.08)

        with patch("pipeline.gnomad.lookup.requests.post", side_effect=flaky_post):
            results = gn.lookup_batch(variants)

        key_fail = gn._cache_key("17", 1, "A", "T")
        key_ok = gn._cache_key("17", 2, "C", "G")
        assert results[key_fail] == GnomadLookupOutcome.UNAVAILABLE
        assert isinstance(results[key_ok], GnomadHit)


# ─── Retry / backoff / jitter ───────────────────────────────────────────────────

class TestRetryBehavior:
    def test_retries_on_429_then_succeeds(self, tmp_path):
        gn = GnomadLookup(cfg=_api_cfg(tmp_path, max_retries=3, retry_backoff_secs=0.01))
        call_count = {"n": 0}

        def flaky(url, json=None, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 3:
                resp = MagicMock(status_code=429, headers={})
                return resp
            return _mock_graphql_response(af=0.09)

        with patch("pipeline.gnomad.lookup.requests.post", side_effect=flaky), \
             patch("pipeline.gnomad.lookup.time.sleep") as mock_sleep:
            result = gn.lookup("17", 8000, "A", "T")
        assert isinstance(result, GnomadHit)
        assert call_count["n"] == 3
        assert mock_sleep.call_count == 2

    def test_exhausts_retries_returns_unavailable(self, tmp_path):
        import requests as req_lib

        gn = GnomadLookup(cfg=_api_cfg(tmp_path, max_retries=2, retry_backoff_secs=0.01))
        resp = MagicMock(status_code=429, headers={})
        resp.raise_for_status.side_effect = req_lib.exceptions.HTTPError("429 Too Many Requests")
        with patch("pipeline.gnomad.lookup.requests.post", return_value=resp), \
             patch("pipeline.gnomad.lookup.time.sleep"):
            result = gn.lookup("17", 8100, "A", "T")
        assert result == GnomadLookupOutcome.UNAVAILABLE

    def test_backoff_delay_doubles_and_is_jittered(self):
        from pipeline.gnomad.lookup import GnomadLookup as GL

        resp = MagicMock(headers={})  # no Retry-After
        delays = [GL._retry_delay(resp, fallback_delay=d, max_sleep=30.0) for d in (1.0, 2.0, 4.0)]
        # Each delay should be >= the base and < base*1.25 (jitter range)
        for base, observed in zip((1.0, 2.0, 4.0), delays):
            assert base <= observed < base * 1.25

    def test_retry_after_header_seconds_is_honored(self):
        from pipeline.gnomad.lookup import GnomadLookup as GL

        resp = MagicMock(headers={"Retry-After": "5"})
        delay = GL._retry_delay(resp, fallback_delay=1.0, max_sleep=30.0)
        assert delay == 5.0

    def test_retry_after_header_is_capped_at_max_sleep(self):
        from pipeline.gnomad.lookup import GnomadLookup as GL

        resp = MagicMock(headers={"Retry-After": "9999"})
        delay = GL._retry_delay(resp, fallback_delay=1.0, max_sleep=30.0)
        assert delay == 30.0

    def test_retry_after_http_date_is_parsed(self):
        from email.utils import format_datetime
        from datetime import datetime, timedelta, timezone
        from pipeline.gnomad.lookup import GnomadLookup as GL

        future = datetime.now(timezone.utc) + timedelta(seconds=10)
        resp = MagicMock(headers={"Retry-After": format_datetime(future, usegmt=True)})
        delay = GL._retry_delay(resp, fallback_delay=1.0, max_sleep=30.0)
        assert 8.0 <= delay <= 11.0  # small tolerance for test execution time

    def test_connection_error_retries_with_jitter_then_succeeds(self, tmp_path):
        from requests.exceptions import ConnectionError as ReqConnErr

        gn = GnomadLookup(cfg=_api_cfg(tmp_path, max_retries=3, retry_backoff_secs=0.01))
        call_count = {"n": 0}

        def flaky(url, json=None, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise ReqConnErr("transient")
            return _mock_graphql_response(af=0.11)

        with patch("pipeline.gnomad.lookup.requests.post", side_effect=flaky), \
             patch("pipeline.gnomad.lookup.time.sleep"):
            result = gn.lookup("17", 8200, "A", "T")
        assert isinstance(result, GnomadHit)


# ─── Thread safety ──────────────────────────────────────────────────────────────

# ─── HTML report renders real AF values, not just "Found" ──────────────────────

class TestGnomadReportRendering:
    def test_html_report_shows_numeric_af_when_present(self):
        from pipeline.reporting.stage import _acmg_to_html_table

        html = _acmg_to_html_table([{
            "chrom": "MT", "pos": 3243, "ref": "A", "alt": "G",
            "gene": "MT-TL1", "classification": "Uncertain_Significance",
            "score": 0, "criteria_met": [], "criteria_unknown": [],
            "gnomad_unavailable_reason": None,
            "gnomad_af": 0.00012, "gnomad_af_popmax": 0.00034,
        }])
        assert "1.20e-04" in html
        assert "3.40e-04" in html
        # The gnomAD cell itself must be the number, not the bare word —
        # ClinVar's own column legitimately still says "Found" here since
        # this fixture didn't set clinvar_unavailable_reason, so check the
        # specific gnomAD cell rather than the whole table's text.
        assert "<td>AF: 1.20e-04 (popmax: 3.40e-04)</td>" in html

    def test_html_report_shows_reason_when_unavailable(self):
        from pipeline.reporting.stage import _acmg_to_html_table

        html = _acmg_to_html_table([{
            "chrom": "MT", "pos": 3243, "ref": "A", "alt": "G",
            "gene": "MT-TL1", "classification": "Uncertain_Significance",
            "score": 0, "criteria_met": [], "criteria_unknown": [],
            "gnomad_unavailable_reason": "gnomAD lookup unavailable (network/tabix error)",
            "gnomad_af": None, "gnomad_af_popmax": None,
        }])
        assert "gnomAD lookup unavailable" in html

    def test_shared_availability_fields_carries_af_forward(self, tmp_path):
        """Regression: the dict shared.py builds for the report must
        actually include gnomad_af/gnomad_af_popmax, not just a status
        string — this was silently dropped before Issue 2."""
        from pipeline.orchestration.shared import _availability_fields

        fields = _availability_fields(
            gene="MT-TL1", gene_unavailable_reason=None,
            clinvar_enabled=True, clinvar_significance=None,
            gnomad_enabled=True, gnomad_af=0.001, gnomad_af_popmax=0.002,
            gnomad_af_absent=False,
        )
        assert fields["gnomad_af"] == 0.001
        assert fields["gnomad_af_popmax"] == 0.002
        assert fields["gnomad_status"] == "found"


class TestThreadSafety:
    def test_concurrent_lookup_calls_do_not_corrupt_cache(self, tmp_path):
        gn = GnomadLookup(cfg=_api_cfg(tmp_path))
        errors = []

        def _do_lookup(i):
            try:
                with patch("pipeline.gnomad.lookup.requests.post", return_value=_mock_graphql_response(af=0.01 * i)):
                    gn.lookup("17", i, "A", "T")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=_do_lookup, args=(i,)) for i in range(1, 21)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert gn.cache_stats()["size"] == 20
