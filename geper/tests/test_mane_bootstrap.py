"""
Tests for `pipeline/mane/bootstrap.py` -- the self-provisioning fetch of
NCBI's MANE Select summary download. `requests.get` is mocked
throughout (both the directory-index fetch and the gzip-file download)
-- no live network calls in this test suite.

The directory-index HTML fixture below is a trimmed, verbatim excerpt of
NCBI's real Apache autoindex page at
https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human/current/ (fetched
2026-08-08), confirming `_discover_current_summary_url` parses the real
markup shape, not a synthetic guess at it.
"""

import gzip
import os
import tempfile
import unittest
from unittest import mock

import requests

from pipeline.mane import bootstrap as mane_bootstrap

# Trimmed, verbatim excerpt of the real directory-index HTML (only the
# lines relevant to filename parsing kept; other file entries in the
# real page -- gff/gtf/faa/fna/gpff/gbff -- are omitted here since
# `_SUMMARY_FILENAME_RE` must skip them regardless).
_REAL_INDEX_HTML_EXCERPT = """
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">
<html>
 <head>
  <title>Index of /refseq/MANE/MANE_human/current</title>
 </head>
 <body>
<h1>Index of /refseq/MANE/MANE_human/current</h1>
<pre>Name                                                     Last modified      Size  <hr><a href="/refseq/MANE/MANE_human/">Parent Directory</a>                                                              -
<a href="MANE.GRCh38.v1.5.changed_select_accessions.txt.gz">MANE.GRCh38.v1.5.changed_select_accessions.txt.gz</a>        2025-12-04 14:57  3.6K
<a href="MANE.GRCh38.v1.5.ensembl_genomic.gff.gz">MANE.GRCh38.v1.5.ensembl_genomic.gff.gz</a>                  2025-12-04 14:59  9.9M
<a href="MANE.GRCh38.v1.5.summary.txt.gz">MANE.GRCh38.v1.5.summary.txt.gz</a>                          2025-12-04 14:53  1.1M
<a href="README_versions.txt">README_versions.txt</a>                                      2025-12-04 14:59   96
</pre>
</body></html>
"""

_INDEX_URL = "https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human/current/"
_SUMMARY_TSV_BODY = (
    "#NCBI_GeneID\tEnsembl_Gene\tHGNC_ID\tsymbol\tname\tRefSeq_nuc\tRefSeq_prot\tEnsembl_nuc\tEnsembl_prot\t"
    "MANE_status\tGRCh38_chr\tchr_start\tchr_end\tchr_strand\n"
    "GeneID:6794\tENSG00000118046.20\tHGNC:11389\tSTK11\tserine/threonine kinase 11\tNM_000455.5\tNP_000446.1\t"
    "ENST00000326873.12\tENSP00000324856.6\tMANE Select\tNC_000019.10\t1205778\t1228431\t+\n"
)


def _fake_config(**overrides):
    cfg = mock.Mock()
    cfg.mane.AUTO_FETCH_ENABLED = True
    cfg.mane.OFFLINE_MODE = False
    cfg.mane.INDEX_URL = _INDEX_URL
    cfg.mane.AUTO_FETCH_TTL_HOURS = 168.0
    cfg.mane.AUTO_FETCH_TIMEOUT_SECS = 60
    for key, value in overrides.items():
        setattr(cfg.mane, key, value)
    return cfg


def _index_response():
    return mock.Mock(status_code=200, text=_REAL_INDEX_HTML_EXCERPT, raise_for_status=lambda: None)


def _gzip_response(body_text=_SUMMARY_TSV_BODY, headers=None):
    resp = mock.Mock()
    resp.status_code = 200
    resp.raise_for_status = lambda: None
    resp.content = gzip.compress(body_text.encode("utf-8"))
    resp.headers = headers or {"Last-Modified": "Thu, 04 Dec 2025 19:53:44 GMT", "ETag": '"abc123"'}
    return resp


class TestDiscoverCurrentSummaryUrl(unittest.TestCase):
    def test_parses_real_directory_listing_shape(self):
        with mock.patch("pipeline.mane.bootstrap.requests.get", return_value=_index_response()):
            url, version = mane_bootstrap._discover_current_summary_url(_INDEX_URL)
        self.assertEqual(url, _INDEX_URL + "MANE.GRCh38.v1.5.summary.txt.gz")
        self.assertEqual(version, "MANE 1.5")

    def test_ignores_non_summary_files_in_the_same_directory(self):
        # The fixture also lists .gff.gz and .changed_select_accessions.txt.gz
        # -- the regex must not accidentally match either.
        with mock.patch("pipeline.mane.bootstrap.requests.get", return_value=_index_response()):
            url, _ = mane_bootstrap._discover_current_summary_url(_INDEX_URL)
        self.assertNotIn("gff", url)
        self.assertNotIn("changed_select_accessions", url)

    def test_request_failure_returns_none_none(self):
        with mock.patch("pipeline.mane.bootstrap.requests.get", side_effect=requests.ConnectionError("refused")):
            url, version = mane_bootstrap._discover_current_summary_url(_INDEX_URL)
        self.assertIsNone(url)
        self.assertIsNone(version)

    def test_unrecognized_html_shape_returns_none_none(self):
        response = mock.Mock(status_code=200, text="<html>nothing useful here</html>", raise_for_status=lambda: None)
        with mock.patch("pipeline.mane.bootstrap.requests.get", return_value=response):
            url, version = mane_bootstrap._discover_current_summary_url(_INDEX_URL)
        self.assertIsNone(url)
        self.assertIsNone(version)


class TestDownloadAndDecompress(unittest.TestCase):
    def test_decompresses_and_writes_plain_tsv(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "mane_summary.tsv")
            with mock.patch("pipeline.mane.bootstrap.requests.get", return_value=_gzip_response()):
                ok = mane_bootstrap._download_and_decompress("https://x.invalid/f.gz", dest, "MANE 1.5")
            self.assertTrue(ok)
            with open(dest, "r", encoding="utf-8") as fh:
                content = fh.read()
            self.assertEqual(content, _SUMMARY_TSV_BODY)

    def test_writes_provenance_sidecar_with_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "mane_summary.tsv")
            with mock.patch("pipeline.mane.bootstrap.requests.get", return_value=_gzip_response()):
                mane_bootstrap._download_and_decompress("https://x.invalid/f.gz", dest, "MANE 1.5")
            sidecar_path = dest + ".provenance.json"
            self.assertTrue(os.path.exists(sidecar_path))
            import json

            with open(sidecar_path, "r", encoding="utf-8") as fh:
                sidecar = json.load(fh)
            self.assertEqual(sidecar["version"], "MANE 1.5")
            self.assertEqual(sidecar["http_last_modified"], "Thu, 04 Dec 2025 19:53:44 GMT")
            self.assertIsNotNone(sidecar["content_hash"])

    def test_empty_decompressed_body_is_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "mane_summary.tsv")
            with mock.patch("pipeline.mane.bootstrap.requests.get", return_value=_gzip_response(body_text="   ")):
                ok = mane_bootstrap._download_and_decompress("https://x.invalid/f.gz", dest, "MANE 1.5")
            self.assertFalse(ok)
            self.assertFalse(os.path.exists(dest))

    def test_request_failure_is_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "mane_summary.tsv")
            with mock.patch("pipeline.mane.bootstrap.requests.get", side_effect=requests.Timeout()):
                ok = mane_bootstrap._download_and_decompress("https://x.invalid/f.gz", dest, "MANE 1.5")
            self.assertFalse(ok)
            self.assertFalse(os.path.exists(dest))

    def test_non_gzip_content_is_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "mane_summary.tsv")
            bad_response = mock.Mock(status_code=200, raise_for_status=lambda: None, content=b"not actually gzip")
            with mock.patch("pipeline.mane.bootstrap.requests.get", return_value=bad_response):
                ok = mane_bootstrap._download_and_decompress("https://x.invalid/f.gz", dest, "MANE 1.5")
            self.assertFalse(ok)


class TestEnsureSummaryFile(unittest.TestCase):
    def test_disabled_auto_fetch_returns_none_without_any_request(self):
        with (
            mock.patch("pipeline.mane.bootstrap.CONFIG", _fake_config(AUTO_FETCH_ENABLED=False)),
            mock.patch("pipeline.mane.bootstrap.requests.get") as get,
        ):
            result = mane_bootstrap.ensure_summary_file()
        self.assertIsNone(result)
        get.assert_not_called()

    def test_offline_mode_returns_none_without_any_request(self):
        with (
            mock.patch("pipeline.mane.bootstrap.CONFIG", _fake_config(OFFLINE_MODE=True)),
            mock.patch("pipeline.mane.bootstrap.requests.get") as get,
        ):
            result = mane_bootstrap.ensure_summary_file()
        self.assertIsNone(result)
        get.assert_not_called()

    def test_fresh_cache_is_reused_without_any_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "mane_summary.tsv")
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(_SUMMARY_TSV_BODY)
            with (
                mock.patch("pipeline.mane.bootstrap.CONFIG", _fake_config(AUTO_FETCH_DIR=tmp)),
                mock.patch("pipeline.mane.bootstrap.requests.get") as get,
            ):
                result = mane_bootstrap.ensure_summary_file()
            self.assertEqual(result, dest)
            get.assert_not_called()

    def test_full_successful_fetch_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch("pipeline.mane.bootstrap.CONFIG", _fake_config(AUTO_FETCH_DIR=tmp)),
                mock.patch(
                    "pipeline.mane.bootstrap.requests.get",
                    side_effect=[_index_response(), _gzip_response()],
                ),
            ):
                result = mane_bootstrap.ensure_summary_file()
            self.assertEqual(result, os.path.join(tmp, "mane_summary.tsv"))
            with open(result, "r", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), _SUMMARY_TSV_BODY)

    def test_index_fetch_failure_falls_back_to_stale_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "mane_summary.tsv")
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(_SUMMARY_TSV_BODY)
            # Force staleness so a refresh is attempted.
            old_time = 0
            os.utime(dest, (old_time, old_time))
            with (
                mock.patch("pipeline.mane.bootstrap.CONFIG", _fake_config(AUTO_FETCH_DIR=tmp)),
                mock.patch("pipeline.mane.bootstrap.requests.get", side_effect=requests.ConnectionError("down")),
            ):
                result = mane_bootstrap.ensure_summary_file()
            self.assertEqual(result, dest)  # stale copy still returned, not discarded

    def test_index_fetch_failure_with_no_existing_cache_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch("pipeline.mane.bootstrap.CONFIG", _fake_config(AUTO_FETCH_DIR=tmp)),
                mock.patch("pipeline.mane.bootstrap.requests.get", side_effect=requests.ConnectionError("down")),
            ):
                result = mane_bootstrap.ensure_summary_file()
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
