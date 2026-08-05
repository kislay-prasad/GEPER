"""
Tests for `annotation/indigenomes.py` (India-deployment feature:
IndiGenomes population-frequency evidence). `requests.post` is mocked
throughout -- no real network calls in the test suite -- following the
same pattern `tests/test_gnomad_provider.py::TestGraphQLGnomadProviderMocked`
already established for gnomAD's own live-query fallback.

The exact response shape mocked here (`{"mydata": [{...,"Ref_VCF":
..., "Alt_VCF": ..., "Start": ..., "Info": "AC=..;AF=..;AN=..;..."}]}`)
was captured from a real, live call to
`https://clingen.igib.res.in/indigen/data.php` during development (see
`annotation/indigenomes.py`'s module docstring for the full live-
verification writeup) -- these tests exercise this module's own
parsing/matching/caching logic against that real shape, not the live
network path itself.
"""

import unittest
from unittest import mock

from annotation import indigenomes as m


def _fake_record(chrom="chr12", start=109586107, ref="A", alt="G", info="AC=1;AF=0.000;AN=2048;DP=26929"):
    return {
        "Chr": chrom,
        "Start": start,
        "Ref": ref,
        "Alt": alt,
        "Ref_VCF": ref,
        "Alt_VCF": alt,
        "Start_VCF ": start,  # real API's own trailing-space key typo
        "Info": info,
    }


def _fake_response(payload):
    resp = mock.Mock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _patch_config(**overrides):
    cfg = mock.Mock()
    cfg.ENABLED = True
    cfg.ENDPOINT = "https://example-not-real.invalid/indigen/data.php"
    cfg.MAX_RETRIES = 3
    cfg.RETRY_BACKOFF_SECS = 0.001
    cfg.QUERY_TIMEOUT_SECS = 5
    cfg.CACHE_MAX_SIZE = 20000
    for key, value in overrides.items():
        setattr(cfg, key, value)
    patcher = mock.patch("annotation.indigenomes.CONFIG")
    fake_config = patcher.start()
    fake_config.indigenomes = cfg
    return patcher


class _CacheClearingTestCase(unittest.TestCase):
    """Every test starts with a clean module-level cache -- `_lookup` caches by (chrom, pos, ref, alt), and several tests intentionally reuse the same coordinates to test different response shapes."""

    def setUp(self):
        m._CACHE.clear()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(m._CACHE.clear)


class TestParseInfoAfAcAn(unittest.TestCase):
    def test_parses_real_shape(self):
        result = m._parse_info_af_ac_an("AC=1;AF=0.000;AN=2048;DP=26929;FS=0.000")
        self.assertEqual(result, {"af": 0.0, "ac": 1, "an": 2048})

    def test_missing_fields_are_none(self):
        self.assertEqual(m._parse_info_af_ac_an(""), {"af": None, "ac": None, "an": None})

    def test_partial_info_string(self):
        result = m._parse_info_af_ac_an("AF=0.031")
        self.assertEqual(result, {"af": 0.031, "ac": None, "an": None})


class TestSearchName(unittest.TestCase):
    def test_matches_sites_own_example_format(self):
        # IndiGenomes' own "Example Search" box shows chr12-109586107-A-G
        self.assertEqual(m._search_name("chr12", 109586107, "A", "G"), "chr12-109586107-A-G")

    def test_bare_chrom_gets_chr_prefix(self):
        self.assertEqual(m._search_name("12", 109586107, "A", "G"), "chr12-109586107-A-G")


class TestQueryIndigenomesFound(_CacheClearingTestCase):
    def test_found_returns_af_ac_an(self):
        _patch_config()
        payload = {"mydata": [_fake_record()]}
        with mock.patch("requests.post", return_value=_fake_response(payload)) as mock_post:
            result = m.query_indigenomes("chr12", 109586107, "A", "G")
        self.assertTrue(mock_post.called)
        self.assertEqual(result, {"af": 0.0, "ac": 1, "an": 2048})

    def test_second_call_uses_cache_not_network(self):
        _patch_config()
        payload = {"mydata": [_fake_record()]}
        with mock.patch("requests.post", return_value=_fake_response(payload)) as mock_post:
            m.query_indigenomes("chr12", 109586107, "A", "G")
            m.query_indigenomes("chr12", 109586107, "A", "G")
        self.assertEqual(mock_post.call_count, 1)


class TestQueryIndigenomesNotFound(_CacheClearingTestCase):
    def test_empty_mydata_is_none(self):
        _patch_config()
        with mock.patch("requests.post", return_value=_fake_response({"mydata": []})):
            result = m.query_indigenomes("chr1", 100, "A", "T")
        self.assertIsNone(result)

    def test_allele_mismatch_at_same_position_is_none(self):
        # Position AND allele match required -- never position alone
        # (matching database/clinvar_client.py's own discipline).
        _patch_config()
        payload = {"mydata": [_fake_record(ref="A", alt="C")]}  # response carries a different ALT
        with mock.patch("requests.post", return_value=_fake_response(payload)):
            result = m.query_indigenomes("chr12", 109586107, "A", "G")
        self.assertIsNone(result)


class TestQueryIndigenomesDisabledAndErrors(_CacheClearingTestCase):
    def test_disabled_short_circuits_without_network_call(self):
        _patch_config(ENABLED=False)
        with mock.patch("requests.post") as mock_post:
            result = m.query_indigenomes("chr12", 109586107, "A", "G")
        mock_post.assert_not_called()
        self.assertIsNone(result)

    def test_query_failure_after_retries_is_none(self):
        import requests as real_requests

        _patch_config(MAX_RETRIES=2)
        with mock.patch("requests.post", side_effect=real_requests.exceptions.Timeout("boom")) as mock_post:
            result = m.query_indigenomes("chr12", 109586107, "A", "G")
        self.assertEqual(mock_post.call_count, 2)
        self.assertIsNone(result)  # query_indigenomes collapses error -> None, per its documented contract


class TestIndiGenomesLookupDistinguishesStates(_CacheClearingTestCase):
    """Unlike `query_indigenomes`, `IndiGenomesLookup.query_variant` must tell found/not_found/error/skipped apart."""

    def _variant(self, chrom="12", pos=109586107, ref="A", alt="G"):
        from pipeline.vcf_parser import Variant

        return Variant(chrom=chrom, pos=pos, variant_id=".", ref=ref, alt=alt, qual=None, filter_status=None)

    def test_found(self):
        _patch_config()
        payload = {"mydata": [_fake_record()]}
        with mock.patch("requests.post", return_value=_fake_response(payload)):
            result = m.IndiGenomesLookup().query_variant(self._variant(), assembly="GRCh38")
        self.assertEqual(result, {"skipped": False, "found": True, "af": 0.0, "ac": 1, "an": 2048})

    def test_not_found(self):
        _patch_config()
        with mock.patch("requests.post", return_value=_fake_response({"mydata": []})):
            result = m.IndiGenomesLookup().query_variant(self._variant(), assembly="GRCh38")
        self.assertEqual(result, {"skipped": False, "found": False})

    def test_query_error_is_reported_not_hidden(self):
        import requests as real_requests

        _patch_config(MAX_RETRIES=1)
        with mock.patch("requests.post", side_effect=real_requests.exceptions.Timeout("boom")):
            result = m.IndiGenomesLookup().query_variant(self._variant(), assembly="GRCh38")
        self.assertFalse(result["found"])
        self.assertIn("error", result)
        self.assertIsNotNone(result["error"])

    def test_disabled_reports_skipped_reason(self):
        _patch_config(ENABLED=False)
        with mock.patch("requests.post") as mock_post:
            result = m.IndiGenomesLookup().query_variant(self._variant(), assembly="GRCh38")
        mock_post.assert_not_called()
        self.assertTrue(result["skipped"])
        self.assertIn("disabled", result["reason"])

    def test_grch37_is_skipped_never_queried(self):
        # IndiGenomes is GRCh38-only -- see this module's own docstring
        # and IndiGenomesConfig's docstring for why a GRCh37 run must
        # never be queried against it (coordinate collision risk).
        _patch_config()
        with mock.patch("requests.post") as mock_post:
            result = m.IndiGenomesLookup().query_variant(self._variant(), assembly="GRCh37")
        mock_post.assert_not_called()
        self.assertTrue(result["skipped"])
        self.assertIn("GRCh38-only", result["reason"])


if __name__ == "__main__":
    unittest.main()
