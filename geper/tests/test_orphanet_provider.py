"""Tests for pipeline/orphanet/provider.py's CompositeOrphanetProvider routing (offline mode, error handling) -- the single-tier (no live-API fallback) shape documented in that module's docstring."""

import unittest
from unittest import mock

from pipeline.orphanet.models import OrphanetGeneEvidence
from pipeline.orphanet.provider import CompositeOrphanetProvider


class _StubProvider:
    name = "local_dataset"

    def __init__(self, result):
        self._result = result

    def query(self, gene_symbol):
        return self._result


class TestCompositeOrphanetProvider(unittest.TestCase):
    def test_local_hit_returned(self):
        hit = OrphanetGeneEvidence(gene_symbol="FBN1", source="local_dataset", found=True)
        composite = CompositeOrphanetProvider(local_provider=_StubProvider(hit))
        with mock.patch("pipeline.orphanet.provider.CONFIG") as fake_config:
            fake_config.orphanet.OFFLINE_MODE = False
            result = composite.query("FBN1")
        self.assertTrue(result.found)

    def test_local_miss_returns_not_found(self):
        miss = OrphanetGeneEvidence.not_found("ZZZ", "local_dataset")
        composite = CompositeOrphanetProvider(local_provider=_StubProvider(miss))
        with mock.patch("pipeline.orphanet.provider.CONFIG") as fake_config:
            fake_config.orphanet.OFFLINE_MODE = False
            result = composite.query("ZZZ")
        self.assertFalse(result.found)
        self.assertIsNone(result.error)

    def test_offline_mode_still_uses_local_dataset(self):
        """Unlike ClinGen/HPO, offline mode changes nothing here -- the local dataset IS the only tier (see provider.py's docstring)."""
        hit = OrphanetGeneEvidence(gene_symbol="FBN1", source="local_dataset", found=True)
        composite = CompositeOrphanetProvider(local_provider=_StubProvider(hit))
        with mock.patch("pipeline.orphanet.provider.CONFIG") as fake_config:
            fake_config.orphanet.OFFLINE_MODE = True
            result = composite.query("FBN1")
        self.assertTrue(result.found)

    def test_provider_exception_becomes_error_result_not_raise(self):
        class _Raising:
            name = "local_dataset"

            def query(self, gene_symbol):
                raise RuntimeError("boom")

        composite = CompositeOrphanetProvider(local_provider=_Raising())
        with mock.patch("pipeline.orphanet.provider.CONFIG") as fake_config:
            fake_config.orphanet.OFFLINE_MODE = False
            result = composite.query("FBN1")
        self.assertFalse(result.found)
        self.assertIn("boom", result.error)

    def test_batch_query_preserves_order(self):
        results_by_gene = {
            "BRCA1": OrphanetGeneEvidence(gene_symbol="BRCA1", source="local_dataset", found=True),
            "FBN1": OrphanetGeneEvidence.not_found("FBN1", "local_dataset"),
        }

        class _MultiStub:
            name = "local_dataset"

            def query(self, gene_symbol):
                return results_by_gene[gene_symbol]

        composite = CompositeOrphanetProvider(local_provider=_MultiStub())
        with mock.patch("pipeline.orphanet.provider.CONFIG") as fake_config:
            fake_config.orphanet.OFFLINE_MODE = False
            results = composite.batch_query(["BRCA1", "FBN1"])
        self.assertEqual([r.gene_symbol for r in results], ["BRCA1", "FBN1"])
        self.assertTrue(results[0].found)
        self.assertFalse(results[1].found)


if __name__ == "__main__":
    unittest.main()
