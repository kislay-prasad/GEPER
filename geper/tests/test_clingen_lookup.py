"""Unit tests for pipeline/clingen/lookup.py -- the orchestrator-facing facade."""

import unittest
from unittest import mock

from pipeline.clingen.lookup import ClinGenLookup
from pipeline.clingen.models import ClinGenGeneEvidence
from pipeline.clingen.utils import GeneResolution, GeneResolutionStatus
from pipeline.vcf_parser import Variant


def _variant(chrom="17", pos=41197701, ref="A", alt="G"):
    return Variant(chrom=chrom, pos=pos, variant_id=".", ref=ref, alt=alt, qual=None, filter_status=None)


def _resolved(symbol, source="ensembl_single_candidate"):
    return GeneResolution(GeneResolutionStatus.RESOLVED, symbol, source, reason="test fixture.")


def _not_found():
    return GeneResolution(GeneResolutionStatus.NOT_FOUND, None, "none", reason="no gene overlaps this position.")


class TestClinGenLookupDisabled(unittest.TestCase):
    def test_disabled_returns_skipped_without_touching_provider(self):
        provider = mock.Mock()
        lookup = ClinGenLookup(provider=provider, cache=None)
        with mock.patch("pipeline.clingen.lookup.CONFIG") as fake_config:
            fake_config.clingen.ENABLED = False
            result = lookup.query_gene("BRCA1")
        self.assertTrue(result["skipped"])
        provider.query.assert_not_called()


class TestClinGenLookupQueryGene(unittest.TestCase):
    def test_repeated_queries_for_same_gene_are_case_insensitive(self):
        provider = mock.Mock()
        provider.query.return_value = ClinGenGeneEvidence(gene_symbol="BRCA1", source="local_dataset", found=True)
        # An explicit no-op cache stand-in (rather than `cache=None`,
        # which falls back to `CONFIG.clingen`'s *default* cache, not
        # "no cache at all" -- see `ClinGenLookup.__init__`) so this
        # test exercises the provider directly on every call.
        no_cache = mock.Mock()
        no_cache.get.return_value = None
        lookup = ClinGenLookup(provider=provider, cache=no_cache)
        with mock.patch("pipeline.clingen.lookup.CONFIG") as fake_config:
            fake_config.clingen.ENABLED = True
            r1 = lookup.query_gene("BRCA1")
            r2 = lookup.query_gene("brca1")  # different case, same gene
        self.assertEqual(provider.query.call_count, 2)
        self.assertTrue(r1["found"])
        self.assertTrue(r2["found"])

    def test_uses_cache_when_configured(self):
        provider = mock.Mock()
        provider.query.return_value = ClinGenGeneEvidence(gene_symbol="BRCA1", source="local_dataset", found=True)
        cache = mock.Mock()
        cache.get.return_value = None
        lookup = ClinGenLookup(provider=provider, cache=cache)
        with mock.patch("pipeline.clingen.lookup.CONFIG") as fake_config:
            fake_config.clingen.ENABLED = True
            lookup.query_gene("BRCA1")
        cache.put.assert_called_once()

    def test_cache_hit_short_circuits_provider(self):
        provider = mock.Mock()
        cache = mock.Mock()
        cache.get.return_value = {"found": True, "gene_symbol": "BRCA1", "source": "local_dataset"}
        lookup = ClinGenLookup(provider=provider, cache=cache)
        with mock.patch("pipeline.clingen.lookup.CONFIG") as fake_config:
            fake_config.clingen.ENABLED = True
            result = lookup.query_gene("BRCA1")
        provider.query.assert_not_called()
        self.assertEqual(result["source"], "cache")

    def test_empty_gene_symbol(self):
        lookup = ClinGenLookup(provider=mock.Mock(), cache=None)
        with mock.patch("pipeline.clingen.lookup.CONFIG") as fake_config:
            fake_config.clingen.ENABLED = True
            result = lookup.query_gene("")
        self.assertFalse(result["found"])
        self.assertIn("error", result)


class TestClinGenLookupQueryVariant(unittest.TestCase):
    def test_resolves_gene_then_delegates(self):
        provider = mock.Mock()
        provider.query.return_value = ClinGenGeneEvidence(gene_symbol="BRCA1", source="local_dataset", found=True)
        lookup = ClinGenLookup(provider=provider, cache=None)
        with mock.patch("pipeline.clingen.lookup.CONFIG") as fake_config, mock.patch(
            "pipeline.clingen.lookup.resolve_gene_symbol_detail", return_value=_resolved("BRCA1")
        ):
            fake_config.clingen.ENABLED = True
            result = lookup.query_variant(_variant(), assembly="GRCh38")
        self.assertEqual(result["gene_symbol"], "BRCA1")
        self.assertTrue(result["found"])

    def test_no_gene_overlap_returns_not_found_without_error(self):
        provider = mock.Mock()
        lookup = ClinGenLookup(provider=provider, cache=None)
        with mock.patch("pipeline.clingen.lookup.CONFIG") as fake_config, mock.patch(
            "pipeline.clingen.lookup.resolve_gene_symbol_detail", return_value=_not_found()
        ):
            fake_config.clingen.ENABLED = True
            result = lookup.query_variant(_variant())
        self.assertFalse(result["found"])
        self.assertIsNone(result.get("gene_symbol"))
        provider.query.assert_not_called()

    def test_gene_symbol_resolution_is_memoized_per_position(self):
        provider = mock.Mock()
        provider.query.return_value = ClinGenGeneEvidence(gene_symbol="BRCA1", source="local_dataset", found=True)
        lookup = ClinGenLookup(provider=provider, cache=None)
        with mock.patch("pipeline.clingen.lookup.CONFIG") as fake_config, mock.patch(
            "pipeline.clingen.lookup.resolve_gene_symbol_detail", return_value=_resolved("BRCA1")
        ) as mocked_resolve:
            fake_config.clingen.ENABLED = True
            lookup.query_variant(_variant(pos=100), assembly="GRCh38")
            lookup.query_variant(_variant(pos=100, ref="C", alt="T"), assembly="GRCh38")
        mocked_resolve.assert_called_once()  # same chrom:pos, second call reuses the memo


class TestClinGenLookupBatch(unittest.TestCase):
    def test_batch_dedupes_by_gene(self):
        provider = mock.Mock()
        provider.batch_query.return_value = [
            ClinGenGeneEvidence(gene_symbol="BRCA1", source="local_dataset", found=True)
        ]
        cache = mock.Mock()
        cache.get.return_value = None
        lookup = ClinGenLookup(provider=provider, cache=cache)

        variants = [_variant(pos=1), _variant(pos=2), _variant(pos=3)]
        with mock.patch("pipeline.clingen.lookup.CONFIG") as fake_config, mock.patch(
            "pipeline.clingen.lookup.resolve_gene_symbol_detail", return_value=_resolved("BRCA1")
        ):
            fake_config.clingen.ENABLED = True
            results = lookup.query_variants_batch(variants, assembly="GRCh38")

        self.assertEqual(len(results), 3)
        self.assertTrue(all(r["found"] for r in results))
        provider.batch_query.assert_called_once_with(["BRCA1"])  # only 1 distinct gene despite 3 variants

    def test_batch_disabled_returns_all_skipped(self):
        lookup = ClinGenLookup(provider=mock.Mock(), cache=None)
        with mock.patch("pipeline.clingen.lookup.CONFIG") as fake_config:
            fake_config.clingen.ENABLED = False
            results = lookup.query_variants_batch([_variant(), _variant(pos=2)])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["skipped"] for r in results))


if __name__ == "__main__":
    unittest.main()
