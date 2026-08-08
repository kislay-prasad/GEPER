"""Tests for pipeline/uniprot/provider.py, with `requests` mocked -- see that module's docstring for the sandbox network caveat."""

import os
import unittest
from unittest import mock

from pipeline.uniprot.provider import CompositeUniProtProvider, LiveAPIUniProtProvider, LocalDatasetUniProtProvider

_BRCA1_TP53_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "uniprot_local_dataset_brca1_tp53.jsonl")


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

        with (
            mock.patch("pipeline.uniprot.provider.requests.get", return_value=fake_response) as fake_get,
            mock.patch("pipeline.uniprot.provider.CONFIG") as fake_config,
        ):
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

        with (
            mock.patch("pipeline.uniprot.provider.requests.get", return_value=fake_response),
            mock.patch("pipeline.uniprot.provider.CONFIG") as fake_config,
        ):
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
        with (
            mock.patch("pipeline.uniprot.provider.requests.get", side_effect=real_requests.ConnectionError("no route")),
            mock.patch("pipeline.uniprot.provider.CONFIG") as fake_config,
            mock.patch("pipeline.uniprot.provider.time.sleep"),
        ):
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
            fh.write(
                json.dumps(
                    {
                        "gene_symbol": "TP53",
                        "entry": {
                            "primaryAccession": "P04637",
                            "uniProtkbId": "P53_HUMAN",
                            "entryType": "UniProtKB reviewed (Swiss-Prot)",
                            "comments": [],
                            "features": [],
                        },
                    }
                )
                + "\n"
            )
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
        # `auto_fetch=False` -- without it this now defaults to trying
        # `pipeline/uniprot/bootstrap.py`'s self-fetch, which would
        # make this test a live network call.
        provider = LocalDatasetUniProtProvider(dataset_path=None, auto_fetch=False)
        self.assertFalse(provider.is_available())
        self.assertIsNone(provider.query("TP53"))

    def test_bootstrap_is_only_attempted_once_per_process_even_if_it_raises_unexpectedly(self):
        """
        Regression for the secondary symptom of the real 2026-08-08
        production bug: because the UnknownPosition crash used to
        propagate out of `ensure_dataset_file()` before
        `LocalDatasetUniProtProvider._ensure_loaded` reached its
        `self._loaded = True` line, that guard never got set, so every
        one of 20 variants in one real run re-triggered the full
        ~15s fetch+parse (~297s wasted) instead of hitting the
        already-attempted-this-session cache. `_ensure_loaded` now
        catches an unexpected bootstrap exception itself, so `_loaded`
        is set exactly once regardless of whether bootstrap succeeds,
        finds nothing, or blows up.
        """
        provider = LocalDatasetUniProtProvider(dataset_path=None, auto_fetch=True)
        with (
            mock.patch(
                "pipeline.uniprot.bootstrap.ensure_dataset_file", side_effect=RuntimeError("simulated bootstrap crash")
            ) as fake_ensure,
            mock.patch("pipeline.uniprot.provider.CONFIG") as fake_config,
        ):
            fake_config.uniprot.AUTO_FETCH_ENABLED = True
            fake_config.uniprot.OFFLINE_MODE = False

            first = provider.query("TP53")
            second = provider.query("BRCA1")

        self.assertEqual(
            fake_ensure.call_count, 1, "bootstrap must only be attempted once per process, not once per query"
        )
        # Neither call raises or hangs -- both degrade to a clean not_found.
        self.assertFalse(first.found)
        self.assertFalse(second.found)


class TestLocalDatasetUniProtProviderRealGroundTruth(unittest.TestCase):
    """
    `tests/fixtures/uniprot_local_dataset_brca1_tp53.jsonl` carries
    real BRCA1 (P38398) / TP53 (P04637) field values confirmed live
    against UniProt's REST API (fetched 2026-08-08) -- verifies
    `LocalDatasetUniProtProvider` returns exactly what the live API
    would for the same two genes, the same real-ground-truth pattern
    `tests/test_mane_provider.py` uses for STK11/CBARP.
    """

    def _provider(self):
        return LocalDatasetUniProtProvider(dataset_path=_BRCA1_TP53_FIXTURE, auto_fetch=False)

    def test_brca1_matches_live_api_values(self):
        result = self._provider().query("BRCA1")
        self.assertTrue(result.found)
        self.assertEqual(result.accession, "P38398")
        self.assertEqual(result.entry_name, "BRCA1_HUMAN")
        self.assertEqual(result.protein_name, "Breast cancer type 1 susceptibility protein")
        self.assertEqual(result.organism, "Homo sapiens")
        self.assertTrue(result.reviewed)
        self.assertEqual(result.sequence_length, 1863)
        self.assertIn("E3 ubiquitin-protein ligase", result.function_text)
        self.assertTrue(any("Breast cancer" in d for d in result.disease_comments))
        self.assertEqual(result.features[0].feature_type, "CHAIN")
        self.assertEqual(result.features[0].begin, 1)
        self.assertEqual(result.features[0].end, 1863)

    def test_tp53_matches_live_api_values(self):
        result = self._provider().query("TP53")
        self.assertTrue(result.found)
        self.assertEqual(result.accession, "P04637")
        self.assertEqual(result.entry_name, "P53_HUMAN")
        self.assertEqual(result.protein_name, "Cellular tumor antigen p53")
        self.assertEqual(result.sequence_length, 393)
        self.assertIn("Multifunctional transcription factor", result.function_text)
        self.assertTrue(any("Li-Fraumeni" in d for d in result.disease_comments))

    def test_gene_symbol_lookup_is_case_insensitive(self):
        result = self._provider().query("brca1")
        self.assertTrue(result.found)
        self.assertEqual(result.accession, "P38398")

    def test_gene_not_in_fixture_is_a_clean_not_found(self):
        result = self._provider().query("SOMEGENE_NOT_IN_FIXTURE")
        self.assertFalse(result.found)
        self.assertIsNone(result.error)


class TestCompositeFallsThroughOnCleanLocalMiss(unittest.TestCase):
    """
    Regression test for the specific gap this feature's task called
    out: `LocalDatasetUniProtProvider` is now auto-populated by default
    (via `pipeline/uniprot/bootstrap.py`), so a clean "not found" from
    it must not be treated as authoritative the way it could safely be
    before, when only a deployer-provisioned file (rare, curated) could
    ever be configured.
    """

    def test_local_not_found_still_tries_live_api(self):
        local = LocalDatasetUniProtProvider(dataset_path=_BRCA1_TP53_FIXTURE, auto_fetch=False)
        api = mock.Mock()
        api.query.return_value = mock.Mock(found=True, error=None, accession="P99999")

        composite = CompositeUniProtProvider(local_provider=local, api_provider=api)
        with mock.patch("pipeline.uniprot.provider.CONFIG") as fake_config:
            fake_config.uniprot.OFFLINE_MODE = False
            result = composite.query("GENE_NOT_IN_FIXTURE")

        api.query.assert_called_once()
        self.assertTrue(result.found)
        self.assertEqual(result.accession, "P99999")

    def test_local_found_never_calls_api(self):
        local = LocalDatasetUniProtProvider(dataset_path=_BRCA1_TP53_FIXTURE, auto_fetch=False)
        api = mock.Mock()

        composite = CompositeUniProtProvider(local_provider=local, api_provider=api)
        with mock.patch("pipeline.uniprot.provider.CONFIG") as fake_config:
            fake_config.uniprot.OFFLINE_MODE = False
            result = composite.query("BRCA1")

        api.query.assert_not_called()
        self.assertEqual(result.accession, "P38398")

    def test_local_error_short_circuits_without_calling_api(self):
        """Preserves the pre-existing, deliberate convention (see
        `test_provider_exception_is_caught_gracefully` above): an
        actual local-provider failure is a terminal result, not a
        'try the next source' signal -- only a clean not_found falls
        through."""
        local = mock.Mock()
        local.query.return_value = mock.Mock(found=False, error="local dataset parse error: boom")
        api = mock.Mock()

        composite = CompositeUniProtProvider(local_provider=local, api_provider=api)
        with mock.patch("pipeline.uniprot.provider.CONFIG") as fake_config:
            fake_config.uniprot.OFFLINE_MODE = False
            result = composite.query("BRCA1")

        api.query.assert_not_called()
        self.assertIsNotNone(result.error)


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
