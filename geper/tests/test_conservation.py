"""
Tests for the conservation-score integration (pipeline/conservation/)
and its PP3/BP4 extension in pipeline/acmg_rules.py.

Mirrors tests/test_gnomad_provider.py's mocking boundary: the local
bigWig provider is tested via a mocked `subprocess.run` (never a real
`bigWigSummary` binary/file), the UCSC API provider via a mocked
`requests.get` (never a real network call) -- consolidated into one
file rather than gnomAD's five, matching the "keep tests lightweight"
scope for this integration.
"""

import subprocess
import unittest
from unittest import mock

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.conservation.cache import ConservationCache
from pipeline.conservation.lookup import ConservationLookup
from pipeline.conservation.models import ConservationAnnotation
from pipeline.conservation.provider import (
    CompositeConservationProvider,
    LocalBigWigProvider,
    MyVariantGerpProvider,
    UCSCApiProvider,
    _SingleScoreProvider,
)
from pipeline.conservation.utils import normalize_build, normalize_chrom, position_key, ucsc_genome_id
from pipeline.vcf_parser import Variant


def _variant(chrom="chr17", pos=7674858, ref="C", alt="T") -> Variant:
    return Variant(chrom=chrom, pos=pos, variant_id=".", ref=ref, alt=alt, qual=None, filter_status=None)


class TestConservationUtils(unittest.TestCase):
    def test_normalize_build_accepts_common_spellings(self):
        self.assertEqual(normalize_build("hg38"), "GRCh38")
        self.assertEqual(normalize_build("GRCh37"), "GRCh37")
        self.assertEqual(normalize_build("hg19"), "GRCh37")
        self.assertEqual(normalize_build(None), "GRCh38")
        self.assertEqual(normalize_build("nonsense"), "GRCh38")

    def test_ucsc_genome_id_mapping(self):
        self.assertEqual(ucsc_genome_id("GRCh38"), "hg38")
        self.assertEqual(ucsc_genome_id("GRCh37"), "hg19")

    def test_normalize_chrom_always_adds_chr_prefix(self):
        self.assertEqual(normalize_chrom("17"), "chr17")
        self.assertEqual(normalize_chrom("chr17"), "chr17")

    def test_position_key_is_build_qualified_and_allele_independent(self):
        key_a = position_key("chr17", 100, "GRCh38")
        key_b = position_key("17", 100, "GRCh38")
        self.assertEqual(key_a, key_b)  # chr-prefix-insensitive
        self.assertNotEqual(key_a, position_key("chr17", 100, "GRCh37"))


class TestLocalBigWigProvider(unittest.TestCase):
    def test_returns_none_when_not_configured_for_build(self):
        provider = LocalBigWigProvider("phylop")
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config:
            fake_config.conservation.PHYLOP_GRCH38_LOCAL_BIGWIG = ""
            result = provider.query("chr17", 100, "C", "T", "GRCh38")
        self.assertIsNone(result)

    def test_parses_phylop_score_from_successful_bigwigsummary_call(self):
        provider = LocalBigWigProvider("phylop")
        fake_proc = mock.Mock(returncode=0, stdout="7.7619\n", stderr="")
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "subprocess.run", return_value=fake_proc
        ) as mock_run, mock.patch.object(LocalBigWigProvider, "is_available", return_value=True):
            fake_config.conservation.PHYLOP_GRCH38_LOCAL_BIGWIG = "/fake/hg38.phyloP100way.bw"
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            ann = provider.query("chr17", 7674858, "C", "T", "GRCh38")
        self.assertTrue(mock_run.called)
        self.assertTrue(ann.found)
        self.assertAlmostEqual(ann.phylop_score, 7.7619)
        self.assertIsNone(ann.phastcons_score)

    def test_parses_phastcons_score_from_successful_bigwigsummary_call(self):
        provider = LocalBigWigProvider("phastcons")
        fake_proc = mock.Mock(returncode=0, stdout="1.0\n", stderr="")
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "subprocess.run", return_value=fake_proc
        ) as mock_run, mock.patch.object(LocalBigWigProvider, "is_available", return_value=True):
            fake_config.conservation.PHASTCONS_GRCH38_LOCAL_BIGWIG = "/fake/hg38.phastCons100way.bw"
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            ann = provider.query("chr17", 7674858, "C", "T", "GRCh38")
        self.assertTrue(mock_run.called)
        self.assertTrue(ann.found)
        self.assertAlmostEqual(ann.phastcons_score, 1.0)
        self.assertIsNone(ann.phylop_score)

    def test_no_data_is_reported_as_not_found_not_error(self):
        provider = LocalBigWigProvider("phylop")
        fake_proc = mock.Mock(returncode=1, stdout="", stderr="no data returned")
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "subprocess.run", return_value=fake_proc
        ), mock.patch.object(LocalBigWigProvider, "is_available", return_value=True):
            fake_config.conservation.PHYLOP_GRCH38_LOCAL_BIGWIG = "/fake/hg38.phyloP100way.bw"
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            ann = provider.query("chr17", 1, "C", "T", "GRCh38")
        self.assertFalse(ann.found)
        self.assertIsNone(ann.error)

    def test_missing_binary_returns_none_not_error(self):
        provider = LocalBigWigProvider("phylop")
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch.object(
            LocalBigWigProvider, "is_available", return_value=False
        ):
            fake_config.conservation.PHYLOP_GRCH38_LOCAL_BIGWIG = "/fake/hg38.phyloP100way.bw"
            result = provider.query("chr17", 100, "C", "T", "GRCh38")
        self.assertIsNone(result)

    def test_timeout_reported_as_error(self):
        provider = LocalBigWigProvider("phylop")
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="bigWigSummary", timeout=5)
        ), mock.patch.object(LocalBigWigProvider, "is_available", return_value=True):
            fake_config.conservation.PHYLOP_GRCH38_LOCAL_BIGWIG = "/fake/hg38.phyloP100way.bw"
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            ann = provider.query("chr17", 100, "C", "T", "GRCh38")
        self.assertFalse(ann.found)
        self.assertIsNotNone(ann.error)

    def test_parses_gerp_score_from_successful_bigwigsummary_call(self):
        # GERP's local-bigwig fallback (`GERP_GRCH38_LOCAL_BIGWIG`) shares
        # this exact class/code path with phylop/phastcons -- covered
        # here explicitly since it was previously untested (only
        # exercised, if at all, via a real deployer-provisioned file).
        provider = LocalBigWigProvider("gerp")
        fake_proc = mock.Mock(returncode=0, stdout="5.01\n", stderr="")
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "subprocess.run", return_value=fake_proc
        ) as mock_run, mock.patch.object(LocalBigWigProvider, "is_available", return_value=True):
            fake_config.conservation.GERP_GRCH38_LOCAL_BIGWIG = "/fake/hg38.gerp.bw"
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            ann = provider.query("chr17", 7674858, "C", "T", "GRCh38")
        self.assertTrue(mock_run.called)
        self.assertTrue(ann.found)
        self.assertAlmostEqual(ann.gerp_score, 5.01)
        self.assertIsNone(ann.phylop_score)
        self.assertIsNone(ann.phastcons_score)

    def test_subprocess_argv_is_list_form_not_shell_string(self):
        # Windows-specific regression guard: this must stay a list
        # passed to subprocess.run (Python's own argv-list quoting,
        # via list2cmdline on Windows), never a shell=True string built
        # by hand -- a hand-built string would corrupt/mis-tokenize a
        # Windows-style local bigwig path containing both a drive
        # letter+backslashes and a space (e.g. under
        # "C:\\Program Files\\..."), which is a realistic path shape
        # on Windows that a POSIX-only deployer would never hit.
        provider = LocalBigWigProvider("gerp")
        windows_path = r"C:\Users\Test User\bigwig data\hg38.gerp.bw"
        fake_proc = mock.Mock(returncode=0, stdout="2.5\n", stderr="")
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "subprocess.run", return_value=fake_proc
        ) as mock_run, mock.patch.object(LocalBigWigProvider, "is_available", return_value=True):
            fake_config.conservation.GERP_GRCH38_LOCAL_BIGWIG = windows_path
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            provider.query("chr17", 7674858, "C", "T", "GRCh38")

        self.assertTrue(mock_run.called)
        args, kwargs = mock_run.call_args
        argv = args[0]
        self.assertIsInstance(argv, list, "must be a list (argv-form), not a shell command string")
        self.assertEqual(
            argv,
            ["bigWigSummary", windows_path, "chr17", "7674857", "7674858", "1"],
        )
        self.assertNotIn("shell", kwargs)  # default False -- no shell=True, so no manual quoting is needed at all

    def test_missing_binary_after_all_raises_filenotfound_handled_gracefully(self):
        # Covers the real Windows failure mode: shutil.which() only
        # checks `<PATHEXT>`-suffixed candidates on Windows (verified
        # separately -- an extension-less POSIX-style `bigWigSummary`,
        # the exact form UCSC's own kent-tools ship for Linux/macOS
        # with no official Windows build, is invisible to it even when
        # genuinely present on PATH). `is_available()` can therefore
        # say True (e.g. a real `bigWigSummary.exe` was found) while
        # the subsequent actual `subprocess.run` still raises
        # FileNotFoundError (e.g. removed between the check and the
        # call, or a stale/broken PATH entry) -- this must degrade to
        # a skipped local lookup, not an unhandled crash.
        provider = LocalBigWigProvider("gerp")
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "subprocess.run", side_effect=FileNotFoundError("bigWigSummary")
        ), mock.patch.object(LocalBigWigProvider, "is_available", return_value=True):
            fake_config.conservation.GERP_GRCH38_LOCAL_BIGWIG = "/fake/hg38.gerp.bw"
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            result = provider.query("chr17", 100, "C", "T", "GRCh38")
        self.assertIsNone(result)


class TestUCSCApiProviderMocked(unittest.TestCase):
    def test_successful_phylop_response_parsed(self):
        provider = UCSCApiProvider("phylop", endpoint="https://example-not-real.invalid/getData/track")
        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "track": "phyloP100way",
            "phyloP100way": [{"chrom": "chr17", "start": 7674857, "end": 7674858, "value": 7.7619}],
        }
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "requests.get", return_value=fake_response
        ) as mock_get:
            fake_config.conservation.ENABLE_UCSC_API_FALLBACK = True
            fake_config.conservation.OFFLINE_MODE = False
            fake_config.conservation.MAX_RETRIES = 3
            fake_config.conservation.RETRY_BACKOFF_SECS = 0.01
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            ann = provider.query("chr17", 7674858, "C", "T", "GRCh38")
        self.assertTrue(mock_get.called)
        self.assertTrue(ann.found)
        self.assertAlmostEqual(ann.phylop_score, 7.7619)

    def test_successful_phastcons_response_parsed(self):
        provider = UCSCApiProvider("phastcons", endpoint="https://example-not-real.invalid/getData/track")
        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "track": "phastCons100way",
            "phastCons100way": [{"chrom": "chr17", "start": 7674857, "end": 7674858, "value": 1.0}],
        }
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "requests.get", return_value=fake_response
        ) as mock_get:
            fake_config.conservation.ENABLE_UCSC_API_FALLBACK = True
            fake_config.conservation.OFFLINE_MODE = False
            fake_config.conservation.MAX_RETRIES = 3
            fake_config.conservation.RETRY_BACKOFF_SECS = 0.01
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            ann = provider.query("chr17", 7674858, "C", "T", "GRCh38")
        self.assertTrue(mock_get.called)
        self.assertTrue(ann.found)
        self.assertAlmostEqual(ann.phastcons_score, 1.0)
        called_params = mock_get.call_args.kwargs["params"]
        self.assertEqual(called_params["track"], "phastCons100way")

    def test_phylop_hg19_uses_the_all_suffixed_track_name(self):
        provider = UCSCApiProvider("phylop", endpoint="https://example-not-real.invalid/getData/track")
        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"track": "phyloP100wayAll", "phyloP100wayAll": [{"value": 4.4}]}
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "requests.get", return_value=fake_response
        ) as mock_get:
            fake_config.conservation.ENABLE_UCSC_API_FALLBACK = True
            fake_config.conservation.OFFLINE_MODE = False
            fake_config.conservation.MAX_RETRIES = 1
            fake_config.conservation.RETRY_BACKOFF_SECS = 0.01
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            provider.query("chr17", 100, "C", "T", "GRCh37")
        called_params = mock_get.call_args.kwargs["params"]
        self.assertEqual(called_params["genome"], "hg19")
        self.assertEqual(called_params["track"], "phyloP100wayAll")

    def test_phastcons_uses_same_track_name_on_both_builds(self):
        provider = UCSCApiProvider("phastcons", endpoint="https://example-not-real.invalid/getData/track")
        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"track": "phastCons100way", "phastCons100way": [{"value": 1.0}]}
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "requests.get", return_value=fake_response
        ) as mock_get:
            fake_config.conservation.ENABLE_UCSC_API_FALLBACK = True
            fake_config.conservation.OFFLINE_MODE = False
            fake_config.conservation.MAX_RETRIES = 1
            fake_config.conservation.RETRY_BACKOFF_SECS = 0.01
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            provider.query("chr17", 100, "C", "T", "GRCh37")
        self.assertEqual(mock_get.call_args.kwargs["params"]["track"], "phastCons100way")

    def test_empty_values_is_not_found(self):
        provider = UCSCApiProvider("phylop", endpoint="https://example-not-real.invalid/getData/track")
        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"track": "phyloP100way", "phyloP100way": []}
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "requests.get", return_value=fake_response
        ):
            fake_config.conservation.ENABLE_UCSC_API_FALLBACK = True
            fake_config.conservation.OFFLINE_MODE = False
            fake_config.conservation.MAX_RETRIES = 1
            fake_config.conservation.RETRY_BACKOFF_SECS = 0.01
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            ann = provider.query("chr17", 100, "C", "T", "GRCh38")
        self.assertFalse(ann.found)
        self.assertIsNone(ann.error)

    def test_ucsc_error_payload_is_reported_as_error(self):
        provider = UCSCApiProvider("phylop", endpoint="https://example-not-real.invalid/getData/track")
        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"error": "can not find track=bogus"}
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "requests.get", return_value=fake_response
        ):
            fake_config.conservation.ENABLE_UCSC_API_FALLBACK = True
            fake_config.conservation.OFFLINE_MODE = False
            fake_config.conservation.MAX_RETRIES = 1
            fake_config.conservation.RETRY_BACKOFF_SECS = 0.01
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            ann = provider.query("chr17", 100, "C", "T", "GRCh38")
        self.assertFalse(ann.found)
        self.assertIn("bogus", ann.error)

    def test_offline_mode_short_circuits_without_network_call(self):
        provider = UCSCApiProvider("phylop", endpoint="https://example-not-real.invalid/getData/track")
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "requests.get"
        ) as mock_get:
            fake_config.conservation.ENABLE_UCSC_API_FALLBACK = True
            fake_config.conservation.OFFLINE_MODE = True
            result = provider.query("chr17", 100, "C", "T", "GRCh38")
        mock_get.assert_not_called()
        self.assertIsNone(result)


class TestMyVariantGerpProviderMocked(unittest.TestCase):
    """GERP++ has no UCSC/Ensembl REST source (verified -- see the
    class's own docstring); MyVariant.info's dbNSFP-derived score is
    used instead. Response shape mocked here matches a REAL verified
    live response exactly: `dbnsfp.gerp` nests the score one level
    deeper than the field name alone suggests, under a `"91_mammals"`
    sub-key -- confirmed against two independent live variants before
    this shape was hardcoded (see MyVariantGerpProvider._extract_score's
    own docstring)."""

    def test_successful_response_parsed_from_real_verified_shape(self):
        provider = MyVariantGerpProvider(endpoint="https://example-not-real.invalid/v1/variant")
        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "_id": "chr17:g.7674858C>T",
            "dbnsfp": {"gerp": {"91_mammals": {"rankscore": 0.90056, "score": 5.01}}},
        }
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "requests.get", return_value=fake_response
        ) as mock_get:
            fake_config.conservation.ENABLE_MYVARIANT_API_FALLBACK = True
            fake_config.conservation.OFFLINE_MODE = False
            fake_config.conservation.MAX_RETRIES = 3
            fake_config.conservation.RETRY_BACKOFF_SECS = 0.01
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            ann = provider.query("chr17", 7674858, "C", "T", "GRCh38")
        self.assertTrue(mock_get.called)
        self.assertTrue(ann.found)
        self.assertAlmostEqual(ann.gerp_score, 5.01)

    def test_grch38_passes_assembly_param_grch37_does_not(self):
        provider = MyVariantGerpProvider(endpoint="https://example-not-real.invalid/v1/variant")
        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"dbnsfp": {"gerp": {"91_mammals": {"score": 1.0}}}}
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "requests.get", return_value=fake_response
        ) as mock_get:
            fake_config.conservation.ENABLE_MYVARIANT_API_FALLBACK = True
            fake_config.conservation.OFFLINE_MODE = False
            fake_config.conservation.MAX_RETRIES = 1
            fake_config.conservation.RETRY_BACKOFF_SECS = 0.01
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            provider.query("chr17", 100, "C", "T", "GRCh38")
            self.assertEqual(mock_get.call_args.kwargs["params"].get("assembly"), "hg38")
            provider.query("chr17", 100, "C", "T", "GRCh37")
            self.assertNotIn("assembly", mock_get.call_args.kwargs["params"])

    def test_non_snv_returns_none_not_error(self):
        provider = MyVariantGerpProvider()
        for ref, alt in [("CA", "C"), ("C", "CA"), ("N", "A")]:
            with self.subTest(ref=ref, alt=alt):
                self.assertIsNone(provider.query("chr17", 100, ref, alt, "GRCh38"))

    def test_404_is_not_found_not_error(self):
        provider = MyVariantGerpProvider(endpoint="https://example-not-real.invalid/v1/variant")
        fake_response = mock.Mock()
        fake_response.status_code = 404
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "requests.get", return_value=fake_response
        ):
            fake_config.conservation.ENABLE_MYVARIANT_API_FALLBACK = True
            fake_config.conservation.OFFLINE_MODE = False
            fake_config.conservation.MAX_RETRIES = 1
            fake_config.conservation.RETRY_BACKOFF_SECS = 0.01
            fake_config.conservation.QUERY_TIMEOUT_SECS = 5
            ann = provider.query("chr17", 100, "C", "T", "GRCh38")
        self.assertFalse(ann.found)
        self.assertIsNone(ann.error)

    def test_extract_score_handles_list_shaped_gerp_field(self):
        score = MyVariantGerpProvider._extract_score([{"91_mammals": {"score": 2.5}}])
        self.assertEqual(score, 2.5)

    def test_extract_score_returns_none_for_missing_data(self):
        self.assertIsNone(MyVariantGerpProvider._extract_score(None))
        self.assertIsNone(MyVariantGerpProvider._extract_score({}))
        self.assertIsNone(MyVariantGerpProvider._extract_score({"91_mammals": {}}))

    def test_offline_mode_short_circuits_without_network_call(self):
        provider = MyVariantGerpProvider(endpoint="https://example-not-real.invalid/v1/variant")
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config, mock.patch(
            "requests.get"
        ) as mock_get:
            fake_config.conservation.ENABLE_MYVARIANT_API_FALLBACK = True
            fake_config.conservation.OFFLINE_MODE = True
            result = provider.query("chr17", 100, "C", "T", "GRCh38")
        mock_get.assert_not_called()
        self.assertIsNone(result)


class TestSingleScoreProvider(unittest.TestCase):
    def test_local_result_short_circuits_api(self):
        local = mock.Mock()
        local.name = "local_bigwig"
        local.query.return_value = ConservationAnnotation(
            chrom="chr17", pos=1, ref="C", alt="T", build="GRCh38", source="local_bigwig", found=True, phylop_score=5.0,
        )
        api = mock.Mock()
        sub = _SingleScoreProvider("phylop", local_provider=local, api_provider=api)
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config:
            fake_config.conservation.OFFLINE_MODE = False
            result = sub.query("chr17", 1, "C", "T", "GRCh38")
        self.assertEqual(result.source, "local_bigwig")
        api.query.assert_not_called()

    def test_falls_through_to_api_when_local_returns_none(self):
        local = mock.Mock()
        local.query.return_value = None
        api = mock.Mock()
        api.name = "ucsc_api"
        api.query.return_value = ConservationAnnotation(
            chrom="chr17", pos=1, ref="C", alt="T", build="GRCh38", source="ucsc_api", found=True, phylop_score=3.0,
        )
        sub = _SingleScoreProvider("phylop", local_provider=local, api_provider=api)
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config:
            fake_config.conservation.OFFLINE_MODE = False
            result = sub.query("chr17", 1, "C", "T", "GRCh38")
        self.assertEqual(result.source, "ucsc_api")

    def test_provider_exception_is_caught_not_raised(self):
        local = mock.Mock()
        local.name = "local_bigwig"
        local.query.side_effect = RuntimeError("boom")
        sub = _SingleScoreProvider("phylop", local_provider=local, api_provider=mock.Mock())
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config:
            fake_config.conservation.OFFLINE_MODE = False
            result = sub.query("chr17", 1, "C", "T", "GRCh38")
        self.assertIsNotNone(result.error)
        self.assertFalse(result.found)


class TestCompositeConservationProviderDefaultWiring(unittest.TestCase):
    def test_gerp_uses_myvariant_not_ucsc(self):
        """GERP++ has no UCSC track (see MyVariantGerpProvider's own
        docstring) -- the default wiring must route it to
        MyVariantGerpProvider, not the generic UCSCApiProvider every
        other score type uses."""
        with mock.patch("pipeline.conservation.provider.CONFIG") as fake_config:
            fake_config.conservation.BIGWIGSUMMARY_BINARY = "bigWigSummary"
            sub_providers = CompositeConservationProvider._build_default_sub_providers()
        self.assertIsInstance(sub_providers["gerp"].api_provider, MyVariantGerpProvider)
        self.assertIsInstance(sub_providers["phylop"].api_provider, UCSCApiProvider)
        self.assertIsInstance(sub_providers["phastcons"].api_provider, UCSCApiProvider)


class TestCompositeConservationProvider(unittest.TestCase):
    def _sub(self, score_type, score_field, value, source="ucsc_api"):
        sub = mock.Mock()
        sub.query.return_value = ConservationAnnotation(
            chrom="chr17", pos=1, ref="C", alt="T", build="GRCh38", source=source, found=True,
            **{score_field: value},
        )
        return sub

    def test_merges_all_three_score_types_into_one_annotation(self):
        composite = CompositeConservationProvider(
            sub_providers={
                "phylop": self._sub("phylop", "phylop_score", 7.76),
                "phastcons": self._sub("phastcons", "phastcons_score", 1.0),
                "gerp": self._sub("gerp", "gerp_score", 5.01, source="myvariant_gerp"),
            }
        )
        result = composite.query("chr17", 1, "C", "T", "GRCh38")
        self.assertTrue(result.found)
        self.assertAlmostEqual(result.phylop_score, 7.76)
        self.assertAlmostEqual(result.phastcons_score, 1.0)
        self.assertAlmostEqual(result.gerp_score, 5.01)
        self.assertIn("phylop:ucsc_api", result.source)
        self.assertIn("phastcons:ucsc_api", result.source)
        self.assertIn("gerp:myvariant_gerp", result.source)

    def test_partial_success_still_reports_found_true(self):
        """One score type erroring must not hide the other's real result
        (mirrors EnsembleManager's own 0/1/2-model partial-success routing)."""
        failing_sub = mock.Mock()
        failing_sub.query.return_value = ConservationAnnotation.from_error(
            "chr17", 1, "C", "T", "GRCh38", "boom"
        )
        composite = CompositeConservationProvider(
            sub_providers={
                "phylop": self._sub("phylop", "phylop_score", 7.76),
                "phastcons": failing_sub,
            }
        )
        result = composite.query("chr17", 1, "C", "T", "GRCh38")
        self.assertTrue(result.found)
        self.assertAlmostEqual(result.phylop_score, 7.76)
        self.assertIsNone(result.phastcons_score)

    def test_all_score_types_failing_reports_error(self):
        failing = mock.Mock()
        failing.query.return_value = ConservationAnnotation.from_error("chr17", 1, "C", "T", "GRCh38", "boom")
        composite = CompositeConservationProvider(sub_providers={"phylop": failing, "phastcons": failing})
        result = composite.query("chr17", 1, "C", "T", "GRCh38")
        self.assertFalse(result.found)
        self.assertIsNotNone(result.error)

    def test_neither_score_type_found_reports_not_found_no_error(self):
        not_found_sub = mock.Mock()
        not_found_sub.query.return_value = ConservationAnnotation.not_found("chr17", 1, "C", "T", "GRCh38", "ucsc_api")
        composite = CompositeConservationProvider(sub_providers={"phylop": not_found_sub, "phastcons": not_found_sub})
        result = composite.query("chr17", 1, "C", "T", "GRCh38")
        self.assertFalse(result.found)
        self.assertIsNone(result.error)


class TestConservationLookup(unittest.TestCase):
    def test_disabled_returns_skipped(self):
        lookup = ConservationLookup(provider=mock.Mock(), cache=None)
        with mock.patch("pipeline.conservation.lookup.CONFIG") as fake_config:
            fake_config.conservation.ENABLED = False
            result = lookup.query_variant(_variant())
        self.assertTrue(result["skipped"])

    def test_caches_successful_result_and_reuses_it(self):
        provider = mock.Mock()
        provider.query.return_value = ConservationAnnotation(
            chrom="chr17", pos=7674858, ref="C", alt="T", build="GRCh38", source="ucsc_api", found=True,
            phylop_score=7.76, phastcons_score=1.0,
        )
        cache = ConservationCache(max_size=10, ttl_seconds=None)
        lookup = ConservationLookup(provider=provider, cache=cache)
        with mock.patch("pipeline.conservation.lookup.CONFIG") as fake_config:
            fake_config.conservation.ENABLED = True
            first = lookup.query_variant(_variant())
            second = lookup.query_variant(_variant())
        self.assertEqual(provider.query.call_count, 1)  # second call was a cache hit
        self.assertEqual(second["source"], "cache")
        self.assertAlmostEqual(first["phylop_score"], 7.76)
        self.assertAlmostEqual(first["phastcons_score"], 1.0)

    def test_two_different_alt_alleles_share_one_cache_entry(self):
        """Conservation is position-only -- two ALTs at the same
        position must not trigger two provider calls (see
        pipeline/conservation/models.py's own docstring)."""
        provider = mock.Mock()
        provider.query.return_value = ConservationAnnotation(
            chrom="chr17", pos=100, ref="C", alt="T", build="GRCh38", source="ucsc_api", found=True, phylop_score=1.0,
        )
        cache = ConservationCache(max_size=10, ttl_seconds=None)
        lookup = ConservationLookup(provider=provider, cache=cache)
        with mock.patch("pipeline.conservation.lookup.CONFIG") as fake_config:
            fake_config.conservation.ENABLED = True
            lookup.query_variant(_variant(pos=100, ref="C", alt="T"))
            lookup.query_variant(_variant(pos=100, ref="C", alt="G"))
        self.assertEqual(provider.query.call_count, 1)


class TestConservationACMGExtension(unittest.TestCase):
    def _cfg(
        self,
        phylop_conserved=2.0, phylop_not_conserved=0.0,
        phastcons_conserved=0.8, phastcons_not_conserved=0.2,
        gerp_conserved=2.0, gerp_not_conserved=0.0,
    ):
        cfg = mock.Mock()
        cfg.conservation.PHYLOP_CONSERVED_THRESHOLD = phylop_conserved
        cfg.conservation.PHYLOP_NOT_CONSERVED_THRESHOLD = phylop_not_conserved
        cfg.conservation.PHASTCONS_CONSERVED_THRESHOLD = phastcons_conserved
        cfg.conservation.PHASTCONS_NOT_CONSERVED_THRESHOLD = phastcons_not_conserved
        cfg.conservation.GERP_CONSERVED_THRESHOLD = gerp_conserved
        cfg.conservation.GERP_NOT_CONSERVED_THRESHOLD = gerp_not_conserved
        return cfg

    def test_conservation_signal_none_when_missing_or_skipped(self):
        args = ("phylop_score", 2.0, 0.0)
        self.assertEqual(ACMGRuleEngine._conservation_signal(None, *args), (None, None))
        self.assertEqual(ACMGRuleEngine._conservation_signal({"skipped": True}, *args), (None, None))
        self.assertEqual(ACMGRuleEngine._conservation_signal({"found": False}, *args), (None, None))
        self.assertEqual(ACMGRuleEngine._conservation_signal({"found": True, "error": "boom"}, *args), (None, None))

    def test_conservation_signal_classifies_conserved_not_conserved_ambiguous(self):
        args = ("phylop_score", 2.0, 0.0)
        self.assertEqual(ACMGRuleEngine._conservation_signal({"found": True, "phylop_score": 5.0}, *args)[0], "conserved")
        self.assertEqual(ACMGRuleEngine._conservation_signal({"found": True, "phylop_score": -1.0}, *args)[0], "not_conserved")
        self.assertEqual(ACMGRuleEngine._conservation_signal({"found": True, "phylop_score": 1.0}, *args)[0], "ambiguous")

    def test_conservation_signal_works_for_phastcons_scale(self):
        args = ("phastcons_score", 0.8, 0.2)
        self.assertEqual(ACMGRuleEngine._conservation_signal({"found": True, "phastcons_score": 1.0}, *args)[0], "conserved")
        self.assertEqual(ACMGRuleEngine._conservation_signal({"found": True, "phastcons_score": 0.0}, *args)[0], "not_conserved")
        self.assertEqual(ACMGRuleEngine._conservation_signal({"found": True, "phastcons_score": 0.5}, *args)[0], "ambiguous")

    def test_pp3_triggers_on_conserved_phylop_alone(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", self._cfg()):
            result = ACMGRuleEngine._pp3(None, None, None, {"found": True, "phylop_score": 7.76})
        self.assertEqual(result.status, "triggered")
        self.assertIn("PhyloP", result.evidence_sources)

    def test_pp3_triggers_on_conserved_phastcons_alone(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", self._cfg()):
            result = ACMGRuleEngine._pp3(None, None, None, {"found": True, "phastcons_score": 1.0})
        self.assertEqual(result.status, "triggered")
        self.assertIn("PhastCons", result.evidence_sources)

    def test_pp3_triggers_on_conserved_gerp_alone(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", self._cfg()):
            result = ACMGRuleEngine._pp3(None, None, None, {"found": True, "gerp_score": 5.01})
        self.assertEqual(result.status, "triggered")
        self.assertIn("GERP++", result.evidence_sources)

    def test_pp3_both_conserved_reports_higher_confidence_than_one_alone(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", self._cfg()):
            both = ACMGRuleEngine._pp3(None, None, None, {"found": True, "phylop_score": 7.76, "phastcons_score": 1.0})
            one = ACMGRuleEngine._pp3(None, None, None, {"found": True, "phylop_score": 7.76})
        self.assertEqual(both.confidence, "Moderate")
        self.assertEqual(one.confidence, "Low")
        self.assertEqual(set(both.evidence_sources), {"PhyloP", "PhastCons"})

    def test_pp3_all_three_scores_conserved_agree(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", self._cfg()):
            result = ACMGRuleEngine._pp3(
                None, None, None,
                {"found": True, "phylop_score": 7.76, "phastcons_score": 1.0, "gerp_score": 5.01},
            )
        self.assertEqual(result.status, "triggered")
        self.assertEqual(set(result.evidence_sources), {"PhyloP", "PhastCons", "GERP++"})

    def test_pp3_not_triggered_when_only_ambiguous_scores(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", self._cfg()):
            result = ACMGRuleEngine._pp3(None, None, None, {"found": True, "phylop_score": 1.0, "phastcons_score": 0.5})
        self.assertEqual(result.status, "not_triggered")

    def test_pp3_not_evaluated_when_no_predictor_at_all(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", self._cfg()):
            result = ACMGRuleEngine._pp3(None, None, None, None)
        self.assertEqual(result.status, "not_evaluated")

    def test_bp4_triggers_on_not_conserved_phylop_alone(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", self._cfg()):
            result = ACMGRuleEngine._bp4(None, None, None, {"found": True, "phylop_score": -2.67})
        self.assertEqual(result.status, "triggered")
        self.assertIn("PhyloP", result.evidence_sources)

    def test_bp4_triggers_on_not_conserved_phastcons_alone(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", self._cfg()):
            result = ACMGRuleEngine._bp4(None, None, None, {"found": True, "phastcons_score": 0.0})
        self.assertEqual(result.status, "triggered")
        self.assertIn("PhastCons", result.evidence_sources)

    def test_bp4_triggers_on_not_conserved_gerp_alone(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", self._cfg()):
            result = ACMGRuleEngine._bp4(None, None, None, {"found": True, "gerp_score": -3.0})
        self.assertEqual(result.status, "triggered")
        self.assertIn("GERP++", result.evidence_sources)

    def test_bp4_not_triggered_when_only_ambiguous_scores(self):
        with mock.patch("pipeline.acmg_rules.CONFIG", self._cfg()):
            result = ACMGRuleEngine._bp4(None, None, None, {"found": True, "phylop_score": 1.0, "phastcons_score": 0.5})
        self.assertEqual(result.status, "not_triggered")

    def test_conflicting_alphamissense_and_conservation_triggers_neither_pp3_nor_bp4(self):
        """Reversed by the PP3/BP4 mutual-exclusivity fix (see
        `ACMGRuleEngine._pp3_bp4`'s docstring): a damaging AlphaMissense
        call plus a not-conserved PhyloP score used to still trigger PP3
        unconditionally (this test previously asserted exactly that,
        treating PP3/BP4 as fully independent criteria that never needed
        to agree). That let a report show both "computational evidence
        supports a deleterious effect" (PP3) and "suggests no
        deleterious effect" (BP4) for the same variant whenever two
        different predictors disagreed. Now, self-contradictory
        computational evidence triggers neither -- both report
        not_triggered with the opposing signal surfaced in
        `conflicting_evidence`."""
        am_result = {"skipped": False, "found": True, "am_class": "likely_pathogenic", "am_pathogenicity": 0.95}
        cons_result = {"found": True, "phylop_score": -2.67}
        with mock.patch("pipeline.acmg_rules.CONFIG", self._cfg()):
            pp3 = ACMGRuleEngine._pp3(am_result, None, None, cons_result)
            bp4 = ACMGRuleEngine._bp4(am_result, None, None, cons_result)
        self.assertEqual(pp3.status, "not_triggered")
        self.assertEqual(bp4.status, "not_triggered")
        self.assertIn("AlphaMissense", pp3.evidence_sources)
        self.assertIn("PhyloP", pp3.evidence_sources)
        self.assertTrue(any("PhyloP" in c for c in pp3.conflicting_evidence))
        self.assertTrue(any("AlphaMissense" in c for c in bp4.conflicting_evidence))

    def test_agreeing_alphamissense_and_conservation_triggers_pp3_not_bp4(self):
        """Same two sources as above, but agreeing (both damaging) --
        must still trigger PP3 normally, confirming the conflict check
        only suppresses genuine disagreement, not concordant evidence
        from multiple sources."""
        am_result = {"skipped": False, "found": True, "am_class": "likely_pathogenic", "am_pathogenicity": 0.95}
        cons_result = {"found": True, "phylop_score": 7.76}
        with mock.patch("pipeline.acmg_rules.CONFIG", self._cfg()):
            pp3 = ACMGRuleEngine._pp3(am_result, None, None, cons_result)
            bp4 = ACMGRuleEngine._bp4(am_result, None, None, cons_result)
        self.assertEqual(pp3.status, "triggered")
        self.assertEqual(bp4.status, "not_triggered")
        self.assertIn("AlphaMissense", pp3.evidence_sources)
        self.assertIn("PhyloP", pp3.evidence_sources)


class TestOrchestratorConservationWiring(unittest.TestCase):
    """Same source-inspection shape as
    tests/test_clingen_integration.py::test_orchestrator_constructs_clingen_client
    -- confirms `GeperPipeline.__init__` wires up `self.conservation_client`
    and `_process_variant` actually calls the new stage helper."""

    def test_orchestrator_constructs_conservation_client(self):
        import inspect
        import pipeline.orchestrator as orchestrator_module

        init_source = inspect.getsource(orchestrator_module.GeperPipeline.__init__)
        self.assertIn("self.conservation_client", init_source)

    def test_process_variant_calls_conservation_stage(self):
        import inspect
        import pipeline.orchestrator as orchestrator_module

        process_source = inspect.getsource(orchestrator_module.GeperPipeline._process_variant)
        self.assertIn("_run_conservation_stage", process_source)
        self.assertIn("conservation_result=conservation_result", process_source)


class TestConservationInOrchestratorResultShape(unittest.TestCase):
    """Confirms build_variant_result's additive-key contract (same
    pattern gnomad_result already follows) holds for conservation_result."""

    def test_defaults_to_skipped_when_not_passed(self):
        from report.json_builder import build_variant_result

        result = build_variant_result(
            variant_dict={}, sequence_context={}, dna_model_results={}, rna_result={}, protein_result={},
            blast_result={}, clinvar_result={}, dbsnp_result={}, interpretation={}, errors=[],
        )
        self.assertEqual(result["conservation"], {"skipped": True, "found": False})

    def test_passthrough_when_provided(self):
        from report.json_builder import build_variant_result

        cons = {"skipped": False, "found": True, "phylop_score": 7.76, "phastcons_score": 1.0}
        result = build_variant_result(
            variant_dict={}, sequence_context={}, dna_model_results={}, rna_result={}, protein_result={},
            blast_result={}, clinvar_result={}, dbsnp_result={}, interpretation={}, errors=[],
            conservation_result=cons,
        )
        self.assertEqual(result["conservation"], cons)


if __name__ == "__main__":
    unittest.main()
