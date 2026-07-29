"""Tests for pipeline/alphafold/provider.py, with `requests` mocked -- see that module's docstring for the sandbox network caveat."""

import unittest
from unittest import mock

from pipeline.alphafold.provider import CompositeAlphaFoldProvider, LiveAPIAlphaFoldProvider, LocalDatasetAlphaFoldProvider

_SAMPLE_PDB = (
    "ATOM      2  CA  MET A   1      12.560  13.207   2.100  1.00 95.63           C\n"
    "ATOM      6  CA  ALA A   2      13.560  14.207   3.100  1.00 62.10           C\n"
)


def _cfg(fake_config, enabled=True, offline=False, fetch_structure=True):
    fake_config.alphafold.ENABLED = enabled
    fake_config.alphafold.OFFLINE_MODE = offline
    fake_config.alphafold.FETCH_STRUCTURE_FILE = fetch_structure
    fake_config.alphafold.MAX_STRUCTURE_FILE_BYTES = 20 * 1024 * 1024
    fake_config.alphafold.QUERY_TIMEOUT_SECS = 30
    fake_config.alphafold.MAX_RETRIES = 3
    fake_config.alphafold.RETRY_BACKOFF_SECS = 0
    fake_config.alphafold.PLDDT_VERY_HIGH_THRESHOLD = 90
    fake_config.alphafold.PLDDT_CONFIDENT_THRESHOLD = 70
    fake_config.alphafold.PLDDT_LOW_THRESHOLD = 50


class TestLiveAPIAlphaFoldProvider(unittest.TestCase):
    def test_query_fetches_summary_and_structure(self):
        provider = LiveAPIAlphaFoldProvider()
        summary_response = mock.Mock(status_code=200)
        summary_response.json.return_value = [
            {"latestVersion": 4, "pdbUrl": "https://example.org/model.pdb", "cifUrl": "https://example.org/model.cif",
             "uniprotStart": 1, "uniprotEnd": 2}
        ]
        summary_response.raise_for_status.return_value = None

        structure_response = mock.Mock(headers={}, text=_SAMPLE_PDB)
        structure_response.raise_for_status.return_value = None

        with mock.patch("pipeline.alphafold.provider.requests.get", side_effect=[summary_response, structure_response]), \
             mock.patch("pipeline.alphafold.provider.CONFIG") as fake_config:
            _cfg(fake_config)
            result = provider.query("P04637", protein_position=1)

        self.assertTrue(result.found)
        self.assertEqual(result.model_version, "4")
        self.assertAlmostEqual(result.mean_plddt, (95.63 + 62.10) / 2)
        self.assertEqual(result.affected_residue_plddt, 95.63)
        self.assertEqual(result.affected_residue_band, "very_high")
        self.assertTrue(result.structure_fetched)

    def test_structure_download_skipped_when_disabled(self):
        provider = LiveAPIAlphaFoldProvider()
        summary_response = mock.Mock(status_code=200)
        summary_response.json.return_value = [{"latestVersion": 4, "pdbUrl": "https://example.org/model.pdb"}]
        summary_response.raise_for_status.return_value = None

        with mock.patch("pipeline.alphafold.provider.requests.get", return_value=summary_response) as fake_get, \
             mock.patch("pipeline.alphafold.provider.CONFIG") as fake_config:
            _cfg(fake_config, fetch_structure=False)
            result = provider.query("P04637")

        fake_get.assert_called_once()  # only the summary call, no structure download
        self.assertTrue(result.found)
        self.assertIsNone(result.mean_plddt)
        self.assertFalse(result.structure_fetched)

    def test_structure_download_is_cached_across_calls(self):
        provider = LiveAPIAlphaFoldProvider()
        summary_response = mock.Mock(status_code=200)
        summary_response.json.return_value = [{"latestVersion": 4, "pdbUrl": "https://example.org/model.pdb"}]
        summary_response.raise_for_status.return_value = None
        structure_response = mock.Mock(headers={}, text=_SAMPLE_PDB)
        structure_response.raise_for_status.return_value = None

        with mock.patch(
            "pipeline.alphafold.provider.requests.get",
            side_effect=[summary_response, structure_response, summary_response],
        ) as fake_get, mock.patch("pipeline.alphafold.provider.CONFIG") as fake_config:
            _cfg(fake_config)
            provider.query("P04637", protein_position=1)
            provider.query("P04637", protein_position=2)  # same accession -> structure re-used from in-memory cache

        self.assertEqual(fake_get.call_count, 3)  # 2 summary calls + only 1 structure download

    def test_404_summary_is_not_found(self):
        provider = LiveAPIAlphaFoldProvider()
        fake_response = mock.Mock(status_code=404)

        with mock.patch("pipeline.alphafold.provider.requests.get", return_value=fake_response), \
             mock.patch("pipeline.alphafold.provider.CONFIG") as fake_config:
            _cfg(fake_config)
            result = provider.query("Q99999")

        self.assertFalse(result.found)
        self.assertIsNone(result.error)

    def test_disabled_or_offline_returns_none(self):
        provider = LiveAPIAlphaFoldProvider()
        with mock.patch("pipeline.alphafold.provider.CONFIG") as fake_config:
            _cfg(fake_config, enabled=False)
            self.assertIsNone(provider.query("P04637"))
            _cfg(fake_config, enabled=True, offline=True)
            self.assertIsNone(provider.query("P04637"))


class TestCompositeAlphaFoldProvider(unittest.TestCase):
    def test_falls_back_to_api_when_local_unavailable(self):
        local = mock.Mock()
        local.query.return_value = None
        api = mock.Mock()
        api.query.return_value = mock.Mock(found=True, error=None)

        composite = CompositeAlphaFoldProvider(local_provider=local, api_provider=api)
        with mock.patch("pipeline.alphafold.provider.CONFIG") as fake_config:
            fake_config.alphafold.OFFLINE_MODE = False
            result = composite.query("P04637")

        api.query.assert_called_once()
        self.assertTrue(result.found)

    def test_offline_mode_never_calls_api(self):
        local = mock.Mock()
        local.query.return_value = None
        api = mock.Mock()

        composite = CompositeAlphaFoldProvider(local_provider=local, api_provider=api)
        with mock.patch("pipeline.alphafold.provider.CONFIG") as fake_config:
            fake_config.alphafold.OFFLINE_MODE = True
            result = composite.query("P04637")

        api.query.assert_not_called()
        self.assertIsNotNone(result.error)


if __name__ == "__main__":
    unittest.main()
