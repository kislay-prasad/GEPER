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


class TestIndelBareSpdiMatch(unittest.TestCase):
    """
    Regression tests for the I1 fix: `_variant_match` now reduces both
    sides to SPDI's bare form (`pipeline/variant_normalization.
    bare_spdi`) before comparing, and `lookup_variant` widens the
    position search to a 3-position range for indels. Real, live-
    verified fixture: VHL c.422dup, rs1553619976, confirmed live
    2026-08-09 -- dbSNP's own `POSITION` field indexes this rsID at
    10146593, one less than the variant's VCF/anchor position 10146594.
    """

    _VHL_DUP = _entry("1553619976", "NC_000003.12:10146593:AA:AAA", genes=("VHL",), clinical_significance="pathogenic")

    def test_matches_after_bare_spdi_reduction(self):
        query = _variant(chrom="3", pos=10146594, ref="A", alt="AA")
        self.assertTrue(DbSNPClient._variant_match(self._VHL_DUP, query))

    def test_lookup_variant_widens_search_for_indels_and_resolves_real_rsid(self):
        """`lookup_variant`'s esearch term must cover position 10146593
        (dbSNP's own indexed position for this rsID), not just the VCF
        anchor position 10146594, or the correct candidate never even
        reaches `_variant_match`."""
        query = _variant(chrom="3", pos=10146594, ref="A", alt="AA", variant_id=".")
        responses = [
            _mock_response(_esearch_response(["1553619976"])),
            _mock_response(_esummary_response([self._VHL_DUP])),
        ]
        with mock.patch("requests.get", side_effect=responses) as mocked_get:
            with mock.patch.object(
                DbSNPClient,
                "_fetch_variation_detail",
                return_value={"rsid": "rs1553619976", "genes": ["VHL"]},
            ):
                result = DbSNPClient().lookup_variant(query, assembly="GRCh38")

        self.assertEqual(result["match_status"], DbSNPMatchStatus.MATCHED.value)
        self.assertEqual(result["rsid"], "rs1553619976")
        esearch_call = mocked_get.call_args_list[0]
        term = esearch_call.kwargs["params"]["term"]
        self.assertIn("10146593:10146595", term)  # pos-1:pos+1 range, covers dbSNP's own indexed position

    def test_snv_still_uses_single_point_query_not_a_range(self):
        query = _variant(chrom="17", pos=43094298, ref="A", alt="C")
        responses = [_mock_response(_esearch_response([]))]
        with mock.patch("requests.get", side_effect=responses) as mocked_get:
            DbSNPClient().lookup_variant(query, assembly="GRCh38")
        term = mocked_get.call_args_list[0].kwargs["params"]["term"]
        self.assertIn("43094298[POSITION]", term)
        self.assertNotIn(":", term.split("AND")[1])


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
            with mock.patch.object(
                DbSNPClient,
                "_fetch_variation_detail",
                return_value={"rsid": "rs80357024", "genes": ["BRCA1"], "dbsnp_build": "157"},
            ):
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
