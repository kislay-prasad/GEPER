"""
Unit tests for pipeline/clingen/provider.py.

`LiveAPIClinGenProvider` is tested with `requests` mocked out --
this sandbox has no network route to clinicalgenome.org (see that
module's docstring), so these tests verify the retry/timeout/parsing
logic against a synthetic response shape, not against a live payload.
"""

import os
import unittest
from unittest import mock

from pipeline.clingen.provider import (
    CompositeClinGenProvider,
    LiveAPIClinGenProvider,
    LocalDatasetClinGenProvider,
)
from pipeline.clingen.models import ClinGenGeneEvidence

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "..", "testdata", "clingen_fixture")
_GENE_VALIDITY_FIXTURE = os.path.join(_FIXTURE_DIR, "gene_validity_test.tsv")
_DOSAGE_FIXTURE = os.path.join(_FIXTURE_DIR, "dosage_sensitivity_test.tsv")


class TestLocalDatasetClinGenProvider(unittest.TestCase):
    def test_not_available_with_no_files_configured_and_auto_fetch_off(self):
        # `auto_fetch=False` opts out of `pipeline/clingen/bootstrap.py`'s
        # self-provisioning (see that module's docstring) so this stays
        # a network-free assertion of the "truly nothing configured"
        # case; by default (auto_fetch=True, the production setting)
        # the provider becomes available even with no explicit file,
        # since it can fetch ClinGen's own dataset on first use.
        provider = LocalDatasetClinGenProvider(gene_validity_path=None, dosage_sensitivity_path=None, auto_fetch=False)
        self.assertFalse(provider.is_available())
        self.assertIsNone(provider.query("BRCA1"))

    def test_available_with_no_files_configured_when_auto_fetch_enabled(self):
        provider = LocalDatasetClinGenProvider(gene_validity_path=None, dosage_sensitivity_path=None, auto_fetch=True)
        self.assertTrue(provider.is_available())

    def test_finds_known_gene(self):
        provider = LocalDatasetClinGenProvider(
            gene_validity_path=_GENE_VALIDITY_FIXTURE, dosage_sensitivity_path=_DOSAGE_FIXTURE
        )
        result = provider.query("BRCA1")
        self.assertTrue(result.found)
        self.assertEqual(result.source, "local_dataset")
        self.assertEqual(len(result.gene_disease_validities), 1)
        self.assertEqual(result.gene_disease_validities[0].classification, "Definitive")
        self.assertEqual(result.dosage_sensitivity.haploinsufficiency_score, 3)

    def test_gene_symbol_lookup_is_case_insensitive(self):
        provider = LocalDatasetClinGenProvider(
            gene_validity_path=_GENE_VALIDITY_FIXTURE, dosage_sensitivity_path=_DOSAGE_FIXTURE
        )
        self.assertTrue(provider.query("brca1").found)

    def test_unknown_gene_returns_not_found(self):
        provider = LocalDatasetClinGenProvider(
            gene_validity_path=_GENE_VALIDITY_FIXTURE, dosage_sensitivity_path=_DOSAGE_FIXTURE
        )
        result = provider.query("NOTAREALGENE")
        self.assertFalse(result.found)
        self.assertIsNone(result.error)

    def test_missing_file_path_logs_warning_and_returns_not_found(self):
        provider = LocalDatasetClinGenProvider(gene_validity_path="/no/such/file.tsv")
        result = provider.query("BRCA1")
        self.assertFalse(result.found)

    def test_clingen_gene_id_populated_from_hgnc_column(self):
        """
        Regression test for a defect where `clingen_gene_id` was always
        `None` on the local-dataset path even for genes with rich,
        fully-populated curation data -- the fixture's "GENE ID (HGNC)"
        column was parsed off of each row but never surfaced onto
        `ClinGenGeneEvidence.clingen_gene_id`. Covers every gene in the
        bundled positive-control fixture (a stand-in for real
        ClinGen-curated genes such as BRCA1/BRCA2/TP53/CFTR/LDLR/MYH7).
        """
        provider = LocalDatasetClinGenProvider(
            gene_validity_path=_GENE_VALIDITY_FIXTURE, dosage_sensitivity_path=_DOSAGE_FIXTURE
        )
        expected = {
            "BRCA1": "HGNC:1100",
            "TTN": "HGNC:12403",
            "SCN5A": "HGNC:10593",
            "GJB2": "HGNC:4284",
            "MTHFR": "HGNC:7436",
        }
        for gene, hgnc_id in expected.items():
            with self.subTest(gene=gene):
                result = provider.query(gene)
                self.assertTrue(result.found)
                self.assertEqual(result.clingen_gene_id, hgnc_id)
                self.assertEqual(result.to_dict()["clingen_gene_id"], hgnc_id)

    def test_unknown_gene_clingen_gene_id_is_none(self):
        provider = LocalDatasetClinGenProvider(
            gene_validity_path=_GENE_VALIDITY_FIXTURE, dosage_sensitivity_path=_DOSAGE_FIXTURE
        )
        result = provider.query("NOTAREALGENE")
        self.assertIsNone(result.clingen_gene_id)

    def test_lazy_load_happens_once(self):
        provider = LocalDatasetClinGenProvider(
            gene_validity_path=_GENE_VALIDITY_FIXTURE, dosage_sensitivity_path=_DOSAGE_FIXTURE
        )
        provider.query("BRCA1")
        self.assertTrue(provider._loaded)
        # A second query must not re-parse the file.
        with mock.patch.object(provider, "_load_gene_validity") as mocked_load:
            provider.query("TTN")
            mocked_load.assert_not_called()


class TestLiveAPIClinGenProvider(unittest.TestCase):
    def test_unavailable_when_disabled(self):
        with mock.patch("pipeline.clingen.provider.CONFIG") as fake_config:
            fake_config.clingen.API_ENABLED = False
            fake_config.clingen.OFFLINE_MODE = False
            provider = LiveAPIClinGenProvider(endpoint="https://example.invalid/api")
            self.assertFalse(provider.is_available())
            self.assertIsNone(provider.query("BRCA1"))

    def test_unavailable_in_offline_mode(self):
        with mock.patch("pipeline.clingen.provider.CONFIG") as fake_config:
            fake_config.clingen.API_ENABLED = True
            fake_config.clingen.OFFLINE_MODE = True
            provider = LiveAPIClinGenProvider(endpoint="https://example.invalid/api")
            self.assertFalse(provider.is_available())

    def test_successful_response_is_parsed(self):
        fake_payload = {
            "clingenGeneId": "CGGV:12345",
            "geneValidityCurations": [
                {
                    "diseaseLabel": "Test disease",
                    "diseaseId": "MONDO:0000001",
                    "classification": "Strong",
                    "moi": "Autosomal dominant",
                    "gcep": "Test GCEP",
                }
            ],
            "dosageSensitivity": {
                "haploinsufficiencyScore": 3,
                "haploinsufficiencyDescription": "Sufficient evidence",
            },
        }
        with mock.patch("pipeline.clingen.provider.CONFIG") as fake_config:
            fake_config.clingen.API_ENABLED = True
            fake_config.clingen.OFFLINE_MODE = False
            fake_config.clingen.MAX_RETRIES = 3
            fake_config.clingen.RETRY_BACKOFF_SECS = 0.0
            fake_config.clingen.QUERY_TIMEOUT_SECS = 5
            provider = LiveAPIClinGenProvider(endpoint="https://example.invalid/api")
            fake_response = mock.Mock()
            fake_response.status_code = 200
            fake_response.raise_for_status = mock.Mock()
            fake_response.json.return_value = fake_payload
            with mock.patch("pipeline.clingen.provider.requests.get", return_value=fake_response):
                result = provider.query("TESTGENE")
        self.assertTrue(result.found)
        self.assertEqual(result.gene_disease_validities[0].classification, "Strong")
        self.assertEqual(result.dosage_sensitivity.haploinsufficiency_score, 3)
        self.assertEqual(result.clingen_gene_id, "CGGV:12345")

    def test_404_returns_not_found_without_retrying(self):
        with mock.patch("pipeline.clingen.provider.CONFIG") as fake_config:
            fake_config.clingen.API_ENABLED = True
            fake_config.clingen.OFFLINE_MODE = False
            fake_config.clingen.MAX_RETRIES = 3
            fake_config.clingen.RETRY_BACKOFF_SECS = 0.0
            fake_config.clingen.QUERY_TIMEOUT_SECS = 5
            provider = LiveAPIClinGenProvider(endpoint="https://example.invalid/api")
            fake_response = mock.Mock()
            fake_response.status_code = 404
            with mock.patch("pipeline.clingen.provider.requests.get", return_value=fake_response) as mocked_get:
                result = provider.query("NOTAREALGENE")
        self.assertFalse(result.found)
        self.assertIsNone(result.error)
        self.assertEqual(mocked_get.call_count, 1)  # 404 is a definitive answer, not a transient failure

    def test_network_failure_reports_error_after_retries(self):
        import requests as requests_module

        with mock.patch("pipeline.clingen.provider.CONFIG") as fake_config:
            fake_config.clingen.API_ENABLED = True
            fake_config.clingen.OFFLINE_MODE = False
            fake_config.clingen.MAX_RETRIES = 2
            fake_config.clingen.RETRY_BACKOFF_SECS = 0.0
            fake_config.clingen.QUERY_TIMEOUT_SECS = 5
            provider = LiveAPIClinGenProvider(endpoint="https://example.invalid/api")
            with mock.patch(
                "pipeline.clingen.provider.requests.get",
                side_effect=requests_module.ConnectionError("boom"),
            ):
                result = provider.query("BRCA1")
        self.assertFalse(result.found)
        self.assertIsNotNone(result.error)


class TestCompositeClinGenProvider(unittest.TestCase):
    def test_prefers_local_when_found(self):
        local = mock.Mock()
        local.name = "local_dataset"
        local.query.return_value = ClinGenGeneEvidence(gene_symbol="BRCA1", source="local_dataset", found=True)
        api = mock.Mock()
        api.name = "api"

        composite = CompositeClinGenProvider(local_provider=local, api_provider=api)
        with mock.patch("pipeline.clingen.provider.CONFIG") as fake_config:
            fake_config.clingen.OFFLINE_MODE = False
            result = composite.query("BRCA1")
        self.assertTrue(result.found)
        api.query.assert_not_called()

    def test_falls_through_to_api_when_local_not_found(self):
        local = mock.Mock()
        local.name = "local_dataset"
        local.query.return_value = ClinGenGeneEvidence.not_found("XYZ", "local_dataset")
        api = mock.Mock()
        api.name = "api"
        api.query.return_value = ClinGenGeneEvidence(gene_symbol="XYZ", source="api", found=True)

        composite = CompositeClinGenProvider(local_provider=local, api_provider=api)
        with mock.patch("pipeline.clingen.provider.CONFIG") as fake_config:
            fake_config.clingen.OFFLINE_MODE = False
            result = composite.query("XYZ")
        self.assertTrue(result.found)
        self.assertEqual(result.source, "api")

    def test_offline_mode_never_calls_api(self):
        local = mock.Mock()
        local.name = "local_dataset"
        # A local provider with no dataset configured returns None
        # from query() (not "found=False") -- see
        # `LocalDatasetClinGenProvider.is_available()`/`.query()`.
        # That's the case offline mode should report as an error,
        # distinct from "checked the local dataset, gene isn't in it".
        local.query.return_value = None
        api = mock.Mock()
        api.name = "api"

        composite = CompositeClinGenProvider(local_provider=local, api_provider=api)
        with mock.patch("pipeline.clingen.provider.CONFIG") as fake_config:
            fake_config.clingen.OFFLINE_MODE = True
            result = composite.query("XYZ")
        api.query.assert_not_called()
        self.assertIsNotNone(result.error)

    def test_offline_mode_returns_local_not_found_without_error(self):
        local = mock.Mock()
        local.name = "local_dataset"
        local.query.return_value = ClinGenGeneEvidence.not_found("XYZ", "local_dataset")
        api = mock.Mock()
        api.name = "api"

        composite = CompositeClinGenProvider(local_provider=local, api_provider=api)
        with mock.patch("pipeline.clingen.provider.CONFIG") as fake_config:
            fake_config.clingen.OFFLINE_MODE = True
            result = composite.query("XYZ")
        api.query.assert_not_called()
        self.assertFalse(result.found)
        self.assertIsNone(result.error)

    def test_provider_exception_is_caught_and_reported(self):
        local = mock.Mock()
        local.name = "local_dataset"
        local.query.side_effect = RuntimeError("kaboom")
        api = mock.Mock()
        api.name = "api"
        api.query.return_value = ClinGenGeneEvidence.not_found("XYZ", "api")

        composite = CompositeClinGenProvider(local_provider=local, api_provider=api)
        with mock.patch("pipeline.clingen.provider.CONFIG") as fake_config:
            fake_config.clingen.OFFLINE_MODE = False
            result = composite.query("XYZ")
        # Must never raise out of query(); local's exception is
        # recorded but the API fallback still gets a chance to answer.
        self.assertIsInstance(result, ClinGenGeneEvidence)

    def test_empty_gene_symbol_returns_error_without_calling_providers(self):
        local = mock.Mock()
        api = mock.Mock()
        composite = CompositeClinGenProvider(local_provider=local, api_provider=api)
        result = composite.query("")
        self.assertIsNotNone(result.error)
        local.query.assert_not_called()
        api.query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
