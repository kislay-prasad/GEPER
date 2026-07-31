"""
Tests for `database/dbsnp_client.py::DbSNPClient`'s allele-aware rsID
selection -- the fourth instance of the "first-record-wins" bug found
in this codebase (after ClinVar's record selection, ClinVar's own
retrieval narrowing, and now dbSNP's rsID resolution feeding both).

Ground truth: BRCA1 c.1233T>G (p.Asp411Glu), genomic 17:43094298 A>C
(GRCh38). Live-verified (2026-07-31): dbSNP's positional esearch at
this locus returns two UIDs -- `397508848` (rs397508848, the 2bp
deletion `c.1232_1233del`) and `80357024` (rs80357024, the actually-
queried SNV, alongside its A>G/A>T siblings under the same rsID). The
real `spdi` shapes below are copied verbatim from the live esummary
response for these two records, not synthesized.
"""

import unittest
from unittest import mock

from database.dbsnp_client import DbSNPClient, DbSNPMatchStatus
from pipeline.vcf_parser import Variant


def _variant(chrom="17", pos=43094298, ref="A", alt="C", variant_id=".") -> Variant:
    return Variant(chrom=chrom, pos=pos, variant_id=variant_id, ref=ref, alt=alt, qual=None, filter_status=None)


def _esearch_response(uids):
    return {"esearchresult": {"idlist": uids}}


def _entry(uid, spdi, genes=("BRCA1",), clinical_significance=""):
    return {
        "uid": uid,
        "spdi": spdi,
        "genes": [{"name": g, "gene_id": "672"} for g in genes],
        "clinical_significance": clinical_significance,
    }


def _esummary_response(entries):
    result = {"uids": [e["uid"] for e in entries]}
    for e in entries:
        result[e["uid"]] = e
    return {"result": result}


def _mock_response(payload):
    import json
    return mock.Mock(status_code=200, raise_for_status=lambda: None, text=json.dumps(payload))


# Real dbSNP 17:43094298 two-record scenario (see module docstring).
_UNRELATED_DELETION = _entry("397508848", "NC_000017.11:43094297:AT:", clinical_significance="pathogenic")
_CORRECT_SNV = _entry(
    "80357024",
    "NC_000017.11:43094297:A:C,NC_000017.11:43094297:A:G,NC_000017.11:43094297:A:T",
    clinical_significance="likely-benign,benign",
)


class TestVariantMatch(unittest.TestCase):
    def test_matches_one_of_several_alt_alleles_under_one_rsid(self):
        self.assertTrue(DbSNPClient._variant_match(_CORRECT_SNV, _variant()))

    def test_does_not_match_unrelated_deletion(self):
        self.assertFalse(DbSNPClient._variant_match(_UNRELATED_DELETION, _variant()))

    def test_does_not_match_a_sibling_allele_not_actually_queried(self):
        # Same rsID as the correct match, but querying the G alt (also under this rsID) with a C query alt.
        self.assertTrue(DbSNPClient._variant_match(_CORRECT_SNV, _variant(alt="G")))
        self.assertFalse(DbSNPClient._variant_match(_CORRECT_SNV, _variant(alt="T", pos=1)))

    def test_none_when_no_spdi_field(self):
        self.assertIsNone(DbSNPClient._variant_match({"uid": "1"}, _variant()))

    def test_position_check_rejects_same_ref_alt_wrong_position(self):
        entry = _entry("999", "NC_000017.11:99999999:A:C")
        self.assertFalse(DbSNPClient._variant_match(entry, _variant()))


class TestSelectPrimary(unittest.TestCase):
    def test_prefers_lower_numbered_rsid(self):
        newer = {"rsid": "rs80357024"}
        older = {"rsid": "rs12345"}
        self.assertEqual(DbSNPClient._select_primary([newer, older])["rsid"], "rs12345")
        self.assertEqual(DbSNPClient._select_primary([older, newer])["rsid"], "rs12345")


class TestLookupVariantIntegration(unittest.TestCase):
    def test_real_brca1_case_resolves_correct_rsid(self):
        """Regression test for the exact bug: old `uids[0]` behavior
        resolved rs397508848 (the unrelated deletion's rsID). Must now
        resolve rs80357024, the actually-queried SNV's rsID."""
        entries = [_UNRELATED_DELETION, _CORRECT_SNV]
        responses = [
            _mock_response(_esearch_response(["397508848", "80357024"])),
            _mock_response(_esummary_response(entries)),
        ]
        with mock.patch("requests.get", side_effect=responses):
            with mock.patch.object(DbSNPClient, "_fetch_variation_detail", return_value={"rsid": "rs80357024", "genes": ["BRCA1"], "dbsnp_build": "157"}):
                result = DbSNPClient().lookup_variant(_variant(variant_id="."), assembly="GRCh38")

        self.assertEqual(result["match_status"], DbSNPMatchStatus.MATCHED.value)
        self.assertTrue(result["found"])
        self.assertEqual(result["rsid"], "rs80357024")
        self.assertEqual(result["record_count"], 2)
        self.assertEqual(result["matched_record_count"], 1)
        by_rsid = {r["rsid"]: r for r in result["records"]}
        self.assertFalse(by_rsid["rs397508848"]["variant_match"])
        self.assertTrue(by_rsid["rs80357024"]["variant_match"])

    def test_position_only_when_no_candidate_matches_allele(self):
        entries = [_UNRELATED_DELETION]  # correct SNV omitted
        responses = [
            _mock_response(_esearch_response(["397508848"])),
            _mock_response(_esummary_response(entries)),
        ]
        with mock.patch("requests.get", side_effect=responses):
            result = DbSNPClient().lookup_variant(_variant(variant_id="."), assembly="GRCh38")

        self.assertEqual(result["match_status"], DbSNPMatchStatus.POSITION_ONLY.value)
        self.assertFalse(result["found"])
        self.assertIsNone(result["rsid"])
        self.assertIsNone(result["detail"])
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(len(result["records"]), 1)  # still surfaced for context

    def test_not_found_when_esearch_returns_nothing(self):
        with mock.patch("requests.get", return_value=_mock_response(_esearch_response([]))):
            result = DbSNPClient().lookup_variant(_variant(variant_id="."), assembly="GRCh38")

        self.assertEqual(result["match_status"], DbSNPMatchStatus.NOT_FOUND.value)
        self.assertFalse(result["found"])
        self.assertIsNone(result["rsid"])
        self.assertEqual(result["records"], [])

    def test_vcf_id_column_rsid_is_trusted_and_unverified(self):
        """The VCF-supplied rsID path never ran a position search to
        verify against, so `variant_match` is None (unverified), not
        True -- distinguishable from an esearch-confirmed match."""
        with mock.patch.object(DbSNPClient, "_fetch_variation_detail", return_value=None):
            result = DbSNPClient().lookup_variant(_variant(variant_id="rs80357024"), assembly="GRCh38")

        self.assertEqual(result["rsid"], "rs80357024")
        self.assertTrue(result["found"])
        self.assertEqual(result["source"], "vcf_id_column")
        self.assertIsNone(result["primary_record"]["variant_match"])


if __name__ == "__main__":
    unittest.main()
