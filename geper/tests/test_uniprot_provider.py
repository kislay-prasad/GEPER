"""Tests for pipeline/uniprot/provider.py, with `requests` mocked -- see that module's docstring for the sandbox network caveat."""

import unittest
from unittest import mock

from pipeline.uniprot.provider import CompositeUniProtProvider, LiveAPIUniProtProvider, LocalDatasetUniProtProvider


class TestLiveAPIUniProtProvider(unittest.TestCase):
    def test_query_returns_annotation_on_success(self):
        provider = LiveAPIUniProtProvider()
        fake_response = mock.Mock()
        fake_response.json.return_value = {
            "results": [
                {
                    "primaryAccession": "P04637",
                    "uniProtkbId": "P53_HUMAN",
                    "entryType": "UniProtKB reviewed (Swiss-Prot)",
                    "proteinDescription": {"recommendedName": {"fullName": {"value": "Cellular tumor antigen p53"}}},
                    "organism": {"scientificName": "Homo sapiens"},
                    "sequence": {"length": 393},
                    "comments": [],
                    "features": [],
                }
            ]
        }
        fake_response.raise_for_status.return_value = None

        with mock.patch("pipeline.uniprot.provider.requests.get", return_value=fake_response) as fake_get, \
             mock.patch("pipeline.uniprot.provider.CONFIG") as fake_config:
            fake_config.uniprot.ENABLED = True
            fake_config.uniprot.OFFLINE_MODE = False
            fake_config.uniprot.ORGANISM_ID = "9606"
            fake_config.uniprot.REVIEWED_ONLY = True
            fake_config.uniprot.QUERY_TIMEOUT_SECS = 30
            fake_config.uniprot.MAX_RETRIES = 3
            fake_config.uniprot.RETRY_BACKOFF_SECS = 0

            result = provider.query("TP53")

        fake_get.assert_called_once()
        self.assertTrue(result.found)
        self.assertEqual(result.accession, "P04637")

    def test_empty_results_is_not_found(self):
        provider = LiveAPIUniProtProvider()
        fake_response = mock.Mock()
        fake_response.json.return_value = {"results": []}
        fake_response.raise_for_status.return_value = None

        with mock.patch("pipeline.uniprot.provider.requests.get", return_value=fake_response), \
             mock.patch("pipeline.uniprot.provider.CONFIG") as fake_config:
            fake_config.uniprot.ENABLED = True
            fake_config.uniprot.OFFLINE_MODE = False
            fake_config.uniprot.ORGANISM_ID = "9606"
            fake_config.uniprot.REVIEWED_ONLY = True
            fake_config.uniprot.QUERY_TIMEOUT_SECS = 30
            fake_config.uniprot.MAX_RETRIES = 3
            fake_config.uniprot.RETRY_BACKOFF_SECS = 0

            result = provider.query("NOPE1")

        self.assertFalse(result.found)
        self.assertIsNone(result.error)

    def test_network_failure_returns_error_annotation_not_raise(self):
        import requests as real_requests

        provider = LiveAPIUniProtProvider()
        with mock.patch("pipeline.uniprot.provider.requests.get", side_effect=real_requests.ConnectionError("no route")), \
             mock.patch("pipeline.uniprot.provider.CONFIG") as fake_config, \
             mock.patch("pipeline.uniprot.provider.time.sleep"):
            fake_config.uniprot.ENABLED = True
            fake_config.uniprot.OFFLINE_MODE = False
            fake_config.uniprot.ORGANISM_ID = "9606"
            fake_config.uniprot.REVIEWED_ONLY = True
            fake_config.uniprot.QUERY_TIMEOUT_SECS = 1
            fake_config.uniprot.MAX_RETRIES = 2
            fake_config.uniprot.RETRY_BACKOFF_SECS = 0

            result = provider.query("TP53")

        self.assertFalse(result.found)
        self.assertIsNotNone(result.error)

    def test_disabled_or_offline_returns_none(self):
        provider = LiveAPIUniProtProvider()
        with mock.patch("pipeline.uniprot.provider.CONFIG") as fake_config:
            fake_config.uniprot.ENABLED = False
            fake_config.uniprot.OFFLINE_MODE = False
            self.assertIsNone(provider.query("TP53"))

            fake_config.uniprot.ENABLED = True
            fake_config.uniprot.OFFLINE_MODE = True
            self.assertIsNone(provider.query("TP53"))


class TestLocalDatasetUniProtProvider(unittest.TestCase):
    def test_reads_jsonl_dataset(self):
        import json
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps({"gene_symbol": "TP53", "entry": {
                "primaryAccession": "P04637", "uniProtkbId": "P53_HUMAN",
                "entryType": "UniProtKB reviewed (Swiss-Prot)", "comments": [], "features": [],
            }}) + "\n")
            path = fh.name

        try:
            provider = LocalDatasetUniProtProvider(dataset_path=path)
            self.assertTrue(provider.is_available())
            result = provider.query("TP53")
            self.assertTrue(result.found)
            self.assertEqual(result.accession, "P04637")

            missing = provider.query("BRCA1")
            self.assertFalse(missing.found)
        finally:
            os.unlink(path)

    def test_unavailable_without_path(self):
        provider = LocalDatasetUniProtProvider(dataset_path=None)
        self.assertFalse(provider.is_available())
        self.assertIsNone(provider.query("TP53"))


class TestCompositeUniProtProvider(unittest.TestCase):
    def test_falls_back_to_api_when_local_unavailable(self):
        local = mock.Mock()
        local.query.return_value = None
        api = mock.Mock()
        api.query.return_value = mock.Mock(found=True, error=None)

        composite = CompositeUniProtProvider(local_provider=local, api_provider=api)
        with mock.patch("pipeline.uniprot.provider.CONFIG") as fake_config:
            fake_config.uniprot.OFFLINE_MODE = False
            result = composite.query("TP53")

        api.query.assert_called_once()
        self.assertTrue(result.found)

    def test_offline_mode_never_calls_api(self):
        local = mock.Mock()
        local.query.return_value = None
        api = mock.Mock()

        composite = CompositeUniProtProvider(local_provider=local, api_provider=api)
        with mock.patch("pipeline.uniprot.provider.CONFIG") as fake_config:
            fake_config.uniprot.OFFLINE_MODE = True
            result = composite.query("TP53")

        api.query.assert_not_called()
        self.assertFalse(result.found)
        self.assertIsNotNone(result.error)

    def test_provider_exception_is_caught_gracefully(self):
        local = mock.Mock()
        local.query.side_effect = RuntimeError("boom")
        api = mock.Mock()
        api.query.return_value = mock.Mock(found=True, error=None)

        composite = CompositeUniProtProvider(local_provider=local, api_provider=api)
        with mock.patch("pipeline.uniprot.provider.CONFIG") as fake_config:
            fake_config.uniprot.OFFLINE_MODE = False
            result = composite.query("TP53")  # must not raise despite local provider blowing up

        # Matches the established `CompositeClinGenProvider`/`CompositeGnomadProvider`
        # convention: a provider that raises produces a terminal error
        # result (not a signal to silently try the next provider) --
        # api is deliberately not called here.
        api.query.assert_not_called()
        self.assertFalse(result.found)
        self.assertIsNotNone(result.error)


if __name__ == "__main__":
    unittest.main()
