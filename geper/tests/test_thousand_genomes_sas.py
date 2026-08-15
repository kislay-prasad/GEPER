"""
Tests for `annotation/thousand_genomes_sas.py` -- the 1000 Genomes SAS
sub-population frequency lookup, the SOLE Indian/South-Asian
population-frequency source as of 2026-08-08 (IndiGenomes was retired
from GEPER's active query path -- see `DATA_SOURCE_LICENSE_AUDIT.md` --
and this module's own module docstring). See
`report/clinical_report_builder.py::_indian_population_frequency` for
the report-layer rendering tested separately in
`tests/test_indian_population_frequency.py`.

`requests.get` is mocked throughout -- no real network calls in the test
suite. The response shapes mocked here (`overlap/region`'s `alleles`
list, `variation/human/{rsid}?pops=1`'s `populations` list with
`population`/`allele`/`allele_count`/`frequency` keys, including the
real, live-confirmed row-order quirk for rs699/ITU -- see
`_fetch_population_frequencies`'s docstring) were captured from real,
live calls to `rest.ensembl.org` during development.
"""

import unittest
from unittest import mock

from annotation import thousand_genomes_sas as m
from pipeline.vcf_parser import Variant


def _variant(chrom="1", pos=230710048, ref="A", alt="G"):
    return Variant(chrom=chrom, pos=pos, variant_id=".", ref=ref, alt=alt, qual=None, filter_status=None)


def _fake_response(payload):
    resp = mock.Mock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _patch_config(**overrides):
    cfg = mock.Mock()
    cfg.ENABLED = True
    cfg.MAX_RETRIES = 3
    cfg.RETRY_BACKOFF_SECS = 0.001
    cfg.QUERY_TIMEOUT_SECS = 5
    for key, value in overrides.items():
        setattr(cfg, key, value)
    patcher = mock.patch("annotation.thousand_genomes_sas.CONFIG")
    fake_config = patcher.start()
    fake_config.thousand_genomes_sas = cfg
    fake_config.api.ENSEMBL_REST_BASE = "https://rest.ensembl.org"
    return patcher


# Real overlap/region response shape (rs699, confirmed live 2026-08-08),
# trimmed to the one matching candidate.
_OVERLAP_REGION_PAYLOAD = [
    {
        "source": "dbSNP",
        "start": 230710048,
        "end": 230710048,
        "alleles": ["A", "G"],
        "id": "rs699",
        "assembly_name": "GRCh38",
    }
]

# Real /variation/human/rs699?pops=1 response shape (trimmed to the
# pooled SAS + all 5 sub-populations), confirmed live -- deliberately
# includes ITU's real row-order quirk (G-allele row listed BEFORE the
# A-allele row, opposite of every other population for the same rsID)
# to exercise the allele-matching fix, not positional row-picking.
_VARIATION_PAYLOAD = {
    "name": "rs699",
    "populations": [
        {"population": "1000GENOMES:phase_3:SAS", "allele": "A", "allele_count": 356, "frequency": 0.3640081799591},
        {"population": "1000GENOMES:phase_3:SAS", "allele": "G", "allele_count": 622, "frequency": 0.6359918200409},
        {"population": "1000GENOMES:phase_3:GIH", "allele": "A", "allele_count": 84, "frequency": 0.407766990291262},
        {"population": "1000GENOMES:phase_3:GIH", "allele": "G", "allele_count": 122, "frequency": 0.592233009708738},
        {"population": "1000GENOMES:phase_3:PJL", "allele": "A", "allele_count": 75, "frequency": 0.390625},
        {"population": "1000GENOMES:phase_3:PJL", "allele": "G", "allele_count": 117, "frequency": 0.609375},
        {"population": "1000GENOMES:phase_3:BEB", "allele": "A", "allele_count": 55, "frequency": 0.319767441860465},
        {"population": "1000GENOMES:phase_3:BEB", "allele": "G", "allele_count": 117, "frequency": 0.680232558139535},
        {"population": "1000GENOMES:phase_3:STU", "allele": "A", "allele_count": 65, "frequency": 0.318627450980392},
        {"population": "1000GENOMES:phase_3:STU", "allele": "G", "allele_count": 139, "frequency": 0.681372549019608},
        # ITU: G-allele row listed FIRST -- the real, live-confirmed
        # opposite ordering from every other population above.
        {"population": "1000GENOMES:phase_3:ITU", "allele": "G", "allele_count": 127, "frequency": 0.622549019607843},
        {"population": "1000GENOMES:phase_3:ITU", "allele": "A", "allele_count": 77, "frequency": 0.377450980392157},
        # A non-phase_3, non-SAS population -- must be ignored entirely.
        {"population": "gnomADe:sas", "allele": "G", "allele_count": 999, "frequency": 0.5},
    ],
}


class TestResolveRsid(unittest.TestCase):
    def test_matches_allele_and_position(self):
        _patch_config()
        with mock.patch("requests.get", return_value=_fake_response(_OVERLAP_REGION_PAYLOAD)):
            rsid = m._resolve_rsid("1", 230710048, "A", "G")
        self.assertEqual(rsid, "rs699")

    def test_ref_mismatch_does_not_match(self):
        _patch_config()
        with mock.patch("requests.get", return_value=_fake_response(_OVERLAP_REGION_PAYLOAD)):
            rsid = m._resolve_rsid("1", 230710048, "C", "G")  # wrong REF
        self.assertIsNone(rsid)

    def test_alt_not_in_alleles_does_not_match(self):
        _patch_config()
        with mock.patch("requests.get", return_value=_fake_response(_OVERLAP_REGION_PAYLOAD)):
            rsid = m._resolve_rsid("1", 230710048, "A", "T")  # ALT never observed here
        self.assertIsNone(rsid)

    def test_no_candidates_returns_none(self):
        _patch_config()
        with mock.patch("requests.get", return_value=_fake_response([])):
            rsid = m._resolve_rsid("1", 1, "A", "T")
        self.assertIsNone(rsid)


class TestFetchPopulationFrequencies(unittest.TestCase):
    def test_picks_the_alt_matching_row_not_a_fixed_position(self):
        """Regression test for the real bug caught during development:
        picking `entries[1]` unconditionally silently returned ITU's
        REF-allele frequency mislabeled as its ALT frequency, since
        ITU's rows come back in the opposite order from every other
        population for the same rsID (confirmed live). Allele-matching
        must get every population's ALT-allele frequency correctly."""
        _patch_config()
        with mock.patch("requests.get", return_value=_fake_response(_VARIATION_PAYLOAD)):
            result = m._fetch_population_frequencies("rs699", "G")
        self.assertAlmostEqual(result["ITU"]["af"], 0.622549019607843)
        self.assertEqual(result["ITU"]["ac"], 127)
        self.assertEqual(result["ITU"]["an"], 204)  # 127 + 77

    def test_all_five_sub_populations_and_pooled_present(self):
        _patch_config()
        with mock.patch("requests.get", return_value=_fake_response(_VARIATION_PAYLOAD)):
            result = m._fetch_population_frequencies("rs699", "G")
        self.assertEqual(set(result.keys()), {"SAS", "GIH", "PJL", "BEB", "STU", "ITU"})

    def test_non_phase3_populations_are_ignored(self):
        _patch_config()
        with mock.patch("requests.get", return_value=_fake_response(_VARIATION_PAYLOAD)):
            result = m._fetch_population_frequencies("rs699", "G")
        # gnomADe:sas must never leak in under the "SAS" key.
        self.assertNotEqual(result["SAS"]["ac"], 999)

    def test_alt_allele_absent_for_a_population_is_omitted_not_fabricated(self):
        payload = {
            "populations": [
                {"population": "1000GENOMES:phase_3:GIH", "allele": "A", "allele_count": 10, "frequency": 1.0},
            ]
        }
        _patch_config()
        with mock.patch("requests.get", return_value=_fake_response(payload)):
            result = m._fetch_population_frequencies("rsFAKE", "G")  # querying for G, only A is present
        self.assertNotIn("GIH", result)


class TestQueryVariantEndToEnd(unittest.TestCase):
    def test_rsid_hint_skips_resolution_call(self):
        _patch_config()
        with (
            mock.patch("annotation.thousand_genomes_sas._resolve_rsid") as fake_resolve,
            mock.patch("requests.get", return_value=_fake_response(_VARIATION_PAYLOAD)),
        ):
            result = m.ThousandGenomesSASLookup().query_variant(_variant(), assembly="GRCh38", rsid_hint="rs699")
        fake_resolve.assert_not_called()
        self.assertTrue(result["found"])
        self.assertEqual(result["rsid"], "rs699")

    def test_without_hint_resolves_then_queries(self):
        _patch_config()
        with mock.patch(
            "requests.get", side_effect=[_fake_response(_OVERLAP_REGION_PAYLOAD), _fake_response(_VARIATION_PAYLOAD)]
        ) as fake_get:
            result = m.ThousandGenomesSASLookup().query_variant(_variant(), assembly="GRCh38")
        self.assertEqual(fake_get.call_count, 2)
        self.assertTrue(result["found"])

    def test_found_result_shape(self):
        _patch_config()
        with mock.patch("requests.get", return_value=_fake_response(_VARIATION_PAYLOAD)):
            result = m.ThousandGenomesSASLookup().query_variant(_variant(), assembly="GRCh38", rsid_hint="rs699")
        self.assertTrue(result["found"])
        self.assertEqual(result["sas_pooled"]["af"], 0.6359918200409)
        self.assertEqual(set(result["sub_populations"].keys()), {"GIH", "PJL", "BEB", "STU", "ITU"})
        self.assertEqual(result["total_sample_size"], 494)
        self.assertIn("Houston", result["population_labels"]["GIH"])
        self.assertEqual(result["sample_sizes"]["GIH"], 106)

    def test_no_rsid_resolved_is_a_clean_not_found(self):
        _patch_config()
        with mock.patch("requests.get", return_value=_fake_response([])):
            result = m.ThousandGenomesSASLookup().query_variant(_variant(), assembly="GRCh38")
        self.assertFalse(result["found"])
        self.assertIsNone(result.get("error"))

    def test_rsid_resolved_but_no_population_data_is_not_found(self):
        _patch_config()
        with mock.patch("requests.get", return_value=_fake_response({"populations": []})):
            result = m.ThousandGenomesSASLookup().query_variant(_variant(), assembly="GRCh38", rsid_hint="rs699")
        self.assertFalse(result["found"])

    def test_query_failure_after_retries_is_reported_not_hidden(self):
        import requests as real_requests

        _patch_config(MAX_RETRIES=1)
        with mock.patch("requests.get", side_effect=real_requests.exceptions.Timeout("boom")):
            result = m.ThousandGenomesSASLookup().query_variant(_variant(), assembly="GRCh38", rsid_hint="rs699")
        self.assertFalse(result["found"])
        self.assertIn("error", result)
        self.assertIsNotNone(result["error"])

    def test_query_failure_error_does_not_leak_the_request_url(self):
        """Round 23: `query_variant`'s `error` used to embed the full
        request URL and raw exception text (`str(exc)` on an
        `ExternalAPIError` built from `_get`'s own f-string) verbatim --
        `report/clinical_report_builder.py::_indian_population_frequency`
        folds this `error` straight into `sas_error`, which
        `report/summary.py`/`report/report_generator.py` render into
        the clinical report unconditionally. Full detail (URL included)
        still reaches the log; what's returned to callers/reports must
        not."""
        import requests as real_requests

        _patch_config(MAX_RETRIES=1)
        with mock.patch("requests.get", side_effect=real_requests.exceptions.Timeout("boom")):
            result = m.ThousandGenomesSASLookup().query_variant(_variant(), assembly="GRCh38", rsid_hint="rs699")
        self.assertNotIn("rest.ensembl.org", result["error"])
        self.assertNotIn("boom", result["error"])
        self.assertEqual(result["error"], "Ensembl request failed after 1 attempts")

    def test_offline_skip_error_does_not_leak_the_request_url(self):
        _patch_config()
        with mock.patch.object(m.HEALTH, "is_offline", return_value=True):
            result = m.ThousandGenomesSASLookup().query_variant(_variant(), assembly="GRCh38", rsid_hint="rs699")
        self.assertFalse(result["found"])
        self.assertNotIn("rest.ensembl.org", result["error"])
        self.assertEqual(result["error"], "Ensembl was confirmed offline at startup; 1000 Genomes SAS lookup skipped.")

    def test_disabled_short_circuits_without_network_call(self):
        _patch_config(ENABLED=False)
        with mock.patch("requests.get") as fake_get:
            result = m.ThousandGenomesSASLookup().query_variant(_variant(), assembly="GRCh38")
        fake_get.assert_not_called()
        self.assertTrue(result["skipped"])
        self.assertIn("disabled", result["reason"])


if __name__ == "__main__":
    unittest.main()
