"""
Tests for pipeline/gnomad/provider.py.

`LocalIndexedGnomadProvider` tests run against a real, synthetic,
bgzip'd + tabix-indexed gnomAD-format VCF fixture (built once in
setUpClass with the real `tabix`/`bgzip` binaries, not mocked) --
these genuinely exercise the subprocess + INFO-parsing code path.

`GraphQLGnomadProvider` tests mock `requests.post` (no real network
call to gnomAD's live API), matching how `database/clinvar_client.py`
would be tested -- these verify the retry/backoff/error-translation
logic, not gnomAD's actual current API response shape.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline.gnomad.provider import (
    CompositeGnomadProvider,
    GraphQLGnomadProvider,
    LocalIndexedGnomadProvider,
)
from utils.exceptions import ExternalAPIError

_HAS_TABIX = shutil.which("tabix") is not None and shutil.which("bgzip") is not None


@unittest.skipUnless(_HAS_TABIX, "tabix/bgzip not available in this environment")
class TestLocalIndexedGnomadProviderRealFixture(unittest.TestCase):
    """Builds a real tabix-indexed fixture VCF and queries it via a real subprocess call."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        vcf_path = Path(cls.tmpdir) / "gnomad_test.vcf"
        vcf_path.write_text(
            "##fileformat=VCFv4.2\n"
            "##reference=GRCh38\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "chr1\t100\t.\tA\tT\t.\tPASS\t"
            "AC=120;AN=250000;AF=0.00048;nhomalt=2;"
            "AC_afr=80;AN_afr=40000;nhomalt_afr=2;"
            "AC_nfe=40;AN_nfe=110000;nhomalt_nfe=0\n"
            "chr1\t200\t.\tG\tGA\t.\tPASS\t"
            "AC=15000;AN=250000;AF=0.06;nhomalt=900;"
            "AC_afr=5000;AN_afr=40000;nhomalt_afr=300;"
            "AC_nfe=9000;AN_nfe=110000;nhomalt_nfe=550\n"
            "chrX\t300\t.\tC\tG\t.\tPASS\t"
            "AC=3;AN=180000;AF=0.0000167;nhomalt=0;AC_hemi=1\n"
        )
        subprocess.run(["bgzip", "-f", str(vcf_path)], check=True)
        cls.gz_path = str(vcf_path) + ".gz"
        subprocess.run(["tabix", "-s", "1", "-b", "2", "-e", "2", "-f", cls.gz_path], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _provider(self) -> LocalIndexedGnomadProvider:
        provider = LocalIndexedGnomadProvider()
        provider._local_path_for_build = lambda build: self.gz_path if build == "GRCh38" else None
        return provider

    def test_finds_snv_and_parses_populations(self):
        provider = self._provider()
        ann = provider.query("1", 100, "A", "T", "GRCh38")
        self.assertTrue(ann.found)
        self.assertAlmostEqual(ann.genome_af, 0.00048)
        self.assertEqual(ann.hom, 2)
        self.assertIn("afr", ann.population_breakdown)
        self.assertEqual(ann.population_breakdown["afr"].ac, 80)

    def test_finds_insertion_with_chr_prefixed_query(self):
        provider = self._provider()
        ann = provider.query("chr1", 200, "G", "GA", "GRCh38")
        self.assertTrue(ann.found)
        self.assertAlmostEqual(ann.genome_af, 0.06)

    def test_hemi_only_reported_for_sex_chromosome_site(self):
        provider = self._provider()
        ann = provider.query("X", 300, "C", "G", "GRCh38")
        self.assertTrue(ann.found)
        self.assertEqual(ann.hemi, 1)

    def test_not_found_for_absent_position(self):
        provider = self._provider()
        ann = provider.query("1", 999999, "C", "G", "GRCh38")
        self.assertFalse(ann.found)
        self.assertIsNone(ann.error)

    def test_mismatched_alt_at_same_position_is_not_a_false_match(self):
        provider = self._provider()
        ann = provider.query("1", 100, "A", "C", "GRCh38")  # real fixture has A>T at this position, not A>C
        self.assertFalse(ann.found)

    def test_returns_none_when_build_not_configured(self):
        provider = LocalIndexedGnomadProvider()
        provider._local_path_for_build = lambda build: None
        self.assertIsNone(provider.query("1", 100, "A", "T", "GRCh37"))


class TestGraphQLGnomadProviderMocked(unittest.TestCase):
    def test_successful_response_parsed(self):
        provider = GraphQLGnomadProvider(endpoint="https://example-not-real.invalid/api")
        fake_response = mock.Mock()
        fake_response.json.return_value = {
            "data": {
                "variant": {
                    "genome": {
                        "ac": 10, "an": 2000, "af": 0.005,
                        "homozygote_count": 0, "hemizygote_count": None,
                        "populations": [{"id": "afr", "ac": 5, "an": 500, "homozygote_count": 0, "hemizygote_count": None}],
                    },
                    "exome": None,
                }
            }
        }
        fake_response.raise_for_status.return_value = None
        with mock.patch("pipeline.gnomad.provider.CONFIG") as fake_config:
            fake_config.gnomad.ENABLE_GRAPHQL_FALLBACK = True
            fake_config.gnomad.OFFLINE_MODE = False
            fake_config.gnomad.MAX_RETRIES = 3
            fake_config.gnomad.RETRY_BACKOFF_SECS = 0.01
            fake_config.gnomad.QUERY_TIMEOUT_SECS = 5
            with mock.patch("requests.post", return_value=fake_response) as mock_post:
                ann = provider.query("1", 100, "A", "T", "GRCh38")
        self.assertTrue(mock_post.called)
        self.assertTrue(ann.found)
        self.assertAlmostEqual(ann.genome_af, 0.005)
        self.assertIn("afr", ann.population_breakdown)

    def test_not_found_response(self):
        provider = GraphQLGnomadProvider(endpoint="https://example-not-real.invalid/api")
        fake_response = mock.Mock()
        fake_response.json.return_value = {"data": {"variant": None}}
        fake_response.raise_for_status.return_value = None
        with mock.patch("pipeline.gnomad.provider.CONFIG") as fake_config:
            fake_config.gnomad.ENABLE_GRAPHQL_FALLBACK = True
            fake_config.gnomad.OFFLINE_MODE = False
            fake_config.gnomad.MAX_RETRIES = 1
            fake_config.gnomad.RETRY_BACKOFF_SECS = 0.01
            fake_config.gnomad.QUERY_TIMEOUT_SECS = 5
            with mock.patch("requests.post", return_value=fake_response):
                ann = provider.query("1", 100, "A", "T", "GRCh38")
        self.assertFalse(ann.found)
        self.assertIsNone(ann.error)

    def test_retries_then_raises_translated_error(self):
        import requests as real_requests

        provider = GraphQLGnomadProvider(endpoint="https://example-not-real.invalid/api")
        with mock.patch("pipeline.gnomad.provider.CONFIG") as fake_config:
            fake_config.gnomad.ENABLE_GRAPHQL_FALLBACK = True
            fake_config.gnomad.OFFLINE_MODE = False
            fake_config.gnomad.MAX_RETRIES = 3
            fake_config.gnomad.RETRY_BACKOFF_SECS = 0.001
            fake_config.gnomad.QUERY_TIMEOUT_SECS = 5
            with mock.patch("requests.post", side_effect=real_requests.exceptions.Timeout("boom")) as mock_post:
                ann = provider.query("1", 100, "A", "T", "GRCh38")
        self.assertEqual(mock_post.call_count, 3)  # exhausted all retries
        self.assertFalse(ann.found)
        self.assertIsNotNone(ann.error)

    def test_offline_mode_short_circuits_without_network_call(self):
        provider = GraphQLGnomadProvider(endpoint="https://example-not-real.invalid/api")
        with mock.patch("pipeline.gnomad.provider.CONFIG") as fake_config:
            fake_config.gnomad.ENABLE_GRAPHQL_FALLBACK = True
            fake_config.gnomad.OFFLINE_MODE = True
            with mock.patch("requests.post") as mock_post:
                result = provider.query("1", 100, "A", "T", "GRCh38")
        mock_post.assert_not_called()
        self.assertIsNone(result)


class TestCompositeGnomadProvider(unittest.TestCase):
    def test_local_result_short_circuits_graphql(self):
        local = mock.Mock()
        local.query.return_value = mock.Mock(error=None, found=True)
        graphql = mock.Mock()
        with mock.patch("pipeline.gnomad.provider.CONFIG") as fake_config:
            fake_config.gnomad.OFFLINE_MODE = False
            fake_config.gnomad.MAX_CONCURRENT_ASYNC = 4
            composite = CompositeGnomadProvider(local_provider=local, graphql_provider=graphql, max_concurrent_async=4)
            composite.query("1", 100, "A", "T", "GRCh38")
        graphql.query.assert_not_called()

    def test_falls_through_to_graphql_when_local_returns_none(self):
        local = mock.Mock()
        local.query.return_value = None  # not configured for this build
        graphql = mock.Mock()
        graphql.query.return_value = mock.Mock(error=None, found=True)
        with mock.patch("pipeline.gnomad.provider.CONFIG") as fake_config:
            fake_config.gnomad.OFFLINE_MODE = False
            composite = CompositeGnomadProvider(local_provider=local, graphql_provider=graphql, max_concurrent_async=4)
            result = composite.query("1", 100, "A", "T", "GRCh38")
        graphql.query.assert_called_once()
        self.assertTrue(result.found)

    def test_provider_exception_is_caught_not_raised(self):
        local = mock.Mock()
        local.query.side_effect = RuntimeError("boom")
        local.name = "local_index"
        graphql = mock.Mock()
        graphql.query.return_value = None
        with mock.patch("pipeline.gnomad.provider.CONFIG") as fake_config:
            fake_config.gnomad.OFFLINE_MODE = False
            composite = CompositeGnomadProvider(local_provider=local, graphql_provider=graphql, max_concurrent_async=4)
            result = composite.query("1", 100, "A", "T", "GRCh38")  # must not raise
        self.assertFalse(result.found)
        self.assertIsNotNone(result.error)

    def test_batch_query_preserves_order(self):
        local = mock.Mock()

        def fake_query(chrom, pos, ref, alt, build):
            return mock.Mock(error=None, found=True, pos=pos)

        local.query.side_effect = fake_query
        graphql = mock.Mock()
        composite = CompositeGnomadProvider(local_provider=local, graphql_provider=graphql, max_concurrent_async=4)
        with mock.patch("pipeline.gnomad.provider.CONFIG") as fake_config:
            fake_config.gnomad.OFFLINE_MODE = False
            variants = [("1", i, "A", "T", "GRCh38") for i in range(1, 6)]
            results = composite.batch_query(variants)
        self.assertEqual([r.pos for r in results], [1, 2, 3, 4, 5])

    def test_async_batch_query(self):
        import asyncio

        local = mock.Mock()
        local.query.return_value = mock.Mock(error=None, found=True)
        graphql = mock.Mock()
        composite = CompositeGnomadProvider(local_provider=local, graphql_provider=graphql, max_concurrent_async=4)
        with mock.patch("pipeline.gnomad.provider.CONFIG") as fake_config:
            fake_config.gnomad.OFFLINE_MODE = False
            variants = [("1", i, "A", "T", "GRCh38") for i in range(1, 4)]
            results = asyncio.run(composite.async_batch_query(variants))
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.found for r in results))


if __name__ == "__main__":
    unittest.main()
