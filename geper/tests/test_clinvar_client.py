"""
Tests for `database/clinvar_client.py::ClinVarClient`'s allele-aware
record selection.

Ground truth: BRCA1 c.1233T>G (p.Asp411Glu), genomic 17:43094298 A>C
(GRCh38). Live-verified (2026-07-31): ClinVar's positional search at
this locus returns three distinct variants --
  - VCV000619783: c.1233T>C (p.Asp411=) -- different allele, "Conflicting
    classifications of pathogenicity"
  - VCV000054169: c.1232_1233del (p.Asp411fs) -- an unrelated 2bp
    deletion, "Pathogenic, reviewed by expert panel"
  - VCV000041804: c.1233T>G (p.Asp411Glu) -- the actual queried
    variant, "Benign, reviewed by expert panel"
The real `canonical_spdi`/`variation_loc` shapes below are copied
verbatim from the live esummary response for these three records (only
irrelevant fields trimmed), not synthesized -- so this test is a
regression guard against literally the bug that motivated this fix:
an earlier version of `ClinVarClient` took `records[0]`
unconditionally and attributed the Pathogenic deletion's classification
to this Benign SNV.
"""

import unittest
from unittest import mock

from database.clinvar_client import ClinVarClient, ClinVarMatchStatus
from pipeline.vcf_parser import Variant
from utils.exceptions import ExternalAPIError


def _variant(chrom="17", pos=43094298, ref="A", alt="C") -> Variant:
    return Variant(chrom=chrom, pos=pos, variant_id=".", ref=ref, alt=alt, qual=None, filter_status=None)


def _esearch_response(uids):
    return {"esearchresult": {"idlist": uids}}


def _record(
    uid,
    accession,
    title,
    significance,
    review_status,
    last_evaluated,
    spdi=None,
    variation_loc=None,
):
    entry = {
        "uid": uid,
        "accession": accession,
        "title": title,
        "germline_classification": {
            "description": significance,
            "review_status": review_status,
            "last_evaluated": last_evaluated,
            "trait_set": [{"trait_name": "Hereditary cancer-predisposing syndrome"}],
        },
    }
    if spdi is not None:
        entry["variation_set"] = [
            {
                "canonical_spdi": spdi,
                "variation_loc": variation_loc or [],
            }
        ]
    return entry


def _esummary_response(entries):
    result = {"uids": [e["uid"] for e in entries]}
    for e in entries:
        result[e["uid"]] = e
    return {"result": result}


# Real BRCA1 17:43094298 three-record scenario (see module docstring).
_BRCA1_CONFLICTING_SNV = _record(
    "619783",
    "VCV000619783",
    "NM_007294.4(BRCA1):c.1233T>C (p.Asp411=)",
    "Conflicting classifications of pathogenicity",
    "criteria provided, conflicting classifications",
    "2023/08/04 00:00",
    spdi="NC_000017.11:43094297:A:G",  # different ALT (T>C on coding strand => A>G genomic on the queried A>C's ref base... real value differs, ref stays A)
)
_BRCA1_UNRELATED_DELETION = _record(
    "54169",
    "VCV000054169",
    "NM_007294.4(BRCA1):c.1232_1233del (p.Asp411fs)",
    "Pathogenic",
    "reviewed by expert panel",
    "2016/10/18 00:00",
    spdi="NC_000017.11:43094295:AT:",  # a deletion -- different ref/alt shape entirely
)
_BRCA1_CORRECT_SNV = _record(
    "41804",
    "VCV000041804",
    "NM_007294.4(BRCA1):c.1233T>G (p.Asp411Glu)",
    "Benign",
    "reviewed by expert panel",
    "2024/06/11 00:00",
    spdi="NC_000017.11:43094297:A:C",
    variation_loc=[
        {"assembly_name": "GRCh38", "start": "43094298", "stop": "43094298"},
        {"assembly_name": "GRCh37", "start": "41246315", "stop": "41246315"},
    ],
)


class TestVariantMatch(unittest.TestCase):
    """Unit tests for `_variant_match` in isolation."""

    def test_matches_on_position_and_allele(self):
        result = ClinVarClient._variant_match(_BRCA1_CORRECT_SNV, _variant(), "GRCh38")
        self.assertTrue(result)

    def test_does_not_match_different_allele_same_position(self):
        result = ClinVarClient._variant_match(_BRCA1_CONFLICTING_SNV, _variant(), "GRCh38")
        self.assertFalse(result)

    def test_does_not_match_unrelated_deletion(self):
        result = ClinVarClient._variant_match(_BRCA1_UNRELATED_DELETION, _variant(), "GRCh38")
        self.assertFalse(result)

    def test_none_when_no_variation_set_at_all(self):
        entry = {"uid": "1", "accession": "VCV1", "germline_classification": {}}
        self.assertIsNone(ClinVarClient._variant_match(entry, _variant(), "GRCh38"))

    def test_none_when_variant_is_none(self):
        self.assertIsNone(ClinVarClient._variant_match(_BRCA1_CORRECT_SNV, None, "GRCh38"))

    def test_position_check_rejects_same_ref_alt_at_wrong_position(self):
        """Defense-in-depth check this fix adds: matching ref/alt bases
        alone (the old `_check_ref_alt` behavior) is not sufficient --
        the SPDI/variation_loc position must also agree with the query."""
        entry = _record(
            "999",
            "VCV999",
            "test",
            "Uncertain significance",
            "criteria provided, single submitter",
            "2020/01/01 00:00",
            spdi="NC_000017.11:99999999:A:C",  # same ref/alt, very different position
        )
        self.assertFalse(ClinVarClient._variant_match(entry, _variant(), "GRCh38"))

    def test_grch37_assembly_uses_grch37_variation_loc(self):
        """A record with only a GRCh38 canonical_spdi position but a
        correct GRCh37 variation_loc entry still matches when queried
        under GRCh37 -- exercises the assembly-qualified fallback path,
        not just the SPDI-position arithmetic."""
        entry = _record(
            "2",
            "VCV2",
            "test",
            "Benign",
            "reviewed by expert panel",
            "2024/01/01 00:00",
            spdi="NC_000017.11:99999999:A:C",  # deliberately wrong/unrelated SPDI position
            variation_loc=[{"assembly_name": "GRCh37", "start": "41246315", "stop": "41246315"}],
        )
        result = ClinVarClient._variant_match(
            entry,
            _variant(chrom="17", pos=41246315, ref="A", alt="C"),
            "GRCh37",
        )
        self.assertTrue(result)


class TestIndelBareSpdiMatch(unittest.TestCase):
    """
    Regression tests for the I1 fix (`ClinVarClient._variant_match` now
    reduces both sides to SPDI's bare form via
    `pipeline/variant_normalization.bare_spdi` before comparing).

    Both fixtures are real, live-verified values, not synthesized:
      - VHL c.422dup (VCV000411979), `NC_000003.12:10146593:AA:AAA` --
        confirmed live 2026-08-09 via NCBI esummary. GEPER's own
        normalization stage reduces the VCF record `3:10146594 AA>AAA`
        to `10146594 A>AA` before this method ever sees it (parsimony
        trim only -- no left-alignment happens here, since the base at
        10146593 is 'C', not 'A': confirmed live via Ensembl). The old
        code compared `"AA"/"AAA"` (SPDI, untrimmed) against `"A"/"AA"`
        (query, trimmed) and never matched.
      - BRCA1 c.1232_1233del (VCV000054169), `NC_000017.11:43094297:AT:`
        -- ClinVar's own canonical_spdi is already fully bare (empty
        ALT), while GEPER's VCF-anchored representation at that locus
        is `43094297 CAT>C`. A plain parsimony trim (which must keep
        >=1 anchor base) cannot reduce `"C"` any further, so this case
        needed the anchor-free `bare_spdi` comparison specifically, not
        just trimming.
    """

    def test_vhl_c422dup_matches_after_normalization(self):
        entry = _record(
            "411979",
            "VCV000411979",
            "NM_000551.4(VHL):c.422dup (p.Asn141fs)",
            "Pathogenic",
            "reviewed by expert panel",
            "2023/01/01 00:00",
            spdi="NC_000003.12:10146593:AA:AAA",
        )
        # GEPER's normalization stage has already trimmed the VCF
        # record (3:10146594 AA>AAA) to this form by the time it
        # reaches ClinVarClient -- see pipeline/orchestrator.py's
        # `_run_normalization_stage`.
        query = _variant(chrom="3", pos=10146594, ref="A", alt="AA")
        self.assertTrue(ClinVarClient._variant_match(entry, query, "GRCh38"))

    def test_brca1_deletion_matches_bare_spdi_against_anchored_query(self):
        entry = _record(
            "54169",
            "VCV000054169",
            "NM_007294.4(BRCA1):c.1232_1233del (p.Asp411fs)",
            "Pathogenic",
            "reviewed by expert panel",
            "2016/10/18 00:00",
            spdi="NC_000017.11:43094297:AT:",
        )
        query = _variant(chrom="17", pos=43094297, ref="CAT", alt="C")
        self.assertTrue(ClinVarClient._variant_match(entry, query, "GRCh38"))

    def test_still_rejects_a_genuinely_different_indel_at_same_bare_position(self):
        """Defense-in-depth: bare-form equality must still require the
        SAME deleted/inserted sequence, not just the same interbase
        position -- a different single-base insertion at the identical
        locus must not be conflated with the queried duplication."""
        entry = _record(
            "999999",
            "VCV999999",
            "test",
            "Uncertain significance",
            "criteria provided, single submitter",
            "2020/01/01 00:00",
            spdi="NC_000003.12:10146593:AA:AAG",  # inserts G, not A -- different variant
        )
        query = _variant(chrom="3", pos=10146594, ref="A", alt="AA")
        self.assertFalse(ClinVarClient._variant_match(entry, query, "GRCh38"))


class TestPositionalSearchTermWidensForIndels(unittest.TestCase):
    """
    Regression tests for the I1 retrieval-side fix: for an indel,
    `_positional_search_term` must query a 3-position range (pos-1:
    pos+1), because ClinVar's own `chrpos38` indexing offset relative
    to the VCF anchor position is not consistent -- confirmed live,
    BRCA1 c.1232_1233del (VCV000054169) is indexed at 43094298 (anchor
    +1) while VHL c.422dup (VCV000411979) is indexed at 10146594
    (anchor +0). A single-point query at the anchor position misses
    the deletion case entirely.
    """

    def test_indel_uses_range_query(self):
        term = ClinVarClient()._positional_search_term(_variant(chrom="17", pos=43094297, ref="CAT", alt="C"), "GRCh38")
        self.assertIn("43094296:43094298[chrpos38]", term)

    def test_snv_still_uses_single_point_query(self):
        term = ClinVarClient()._positional_search_term(_variant(), "GRCh38")
        self.assertEqual(term, "17[chr] AND 43094298[chrpos38]")


class TestSelectPrimary(unittest.TestCase):
    def test_picks_most_recently_evaluated(self):
        older = {"accession": "VCV1", "last_evaluated": "2016/10/18 00:00"}
        newer = {"accession": "VCV2", "last_evaluated": "2024/06/11 00:00"}
        self.assertEqual(ClinVarClient._select_primary([older, newer])["accession"], "VCV2")
        self.assertEqual(ClinVarClient._select_primary([newer, older])["accession"], "VCV2")

    def test_missing_date_sorts_last_not_crashes(self):
        dated = {"accession": "VCV1", "last_evaluated": "2020/01/01 00:00"}
        undated = {"accession": "VCV2", "last_evaluated": None}
        self.assertEqual(ClinVarClient._select_primary([dated, undated])["accession"], "VCV1")

    def test_unparseable_date_does_not_crash(self):
        dated = {"accession": "VCV1", "last_evaluated": "2020/01/01 00:00"}
        garbage = {"accession": "VCV2", "last_evaluated": "not-a-date"}
        self.assertEqual(ClinVarClient._select_primary([dated, garbage])["accession"], "VCV1")


class TestQueryVariantIntegration(unittest.TestCase):
    """Full `query_variant` behavior with mocked esearch/esummary,
    reproducing the real BRCA1 3-record scenario."""

    def _mock_responses(self, esearch_payload, esummary_payload):
        return [
            mock.Mock(
                status_code=200,
                json=lambda: esearch_payload,
                raise_for_status=lambda: None,
                text=__import__("json").dumps(esearch_payload),
            ),
            mock.Mock(
                status_code=200,
                json=lambda: esummary_payload,
                raise_for_status=lambda: None,
                text=__import__("json").dumps(esummary_payload),
            ),
        ]

    def test_real_brca1_case_resolves_to_correct_benign_record(self):
        """Regression test for the exact bug: old `records[0]` behavior
        would have attributed VCV000619783 (Conflicting) -- or, in the
        actual observed report, VCV000054169 (Pathogenic, an unrelated
        deletion) -- to this variant. Must now resolve to VCV000041804
        (Benign, expert panel), the actual queried allele."""
        entries = [_BRCA1_CONFLICTING_SNV, _BRCA1_UNRELATED_DELETION, _BRCA1_CORRECT_SNV]
        responses = self._mock_responses(
            _esearch_response([e["uid"] for e in entries]),
            _esummary_response(entries),
        )
        with mock.patch("requests.get", side_effect=responses):
            result = ClinVarClient().query_variant(_variant(), rsid=None, assembly="GRCh38")

        self.assertEqual(result["match_status"], ClinVarMatchStatus.MATCHED.value)
        self.assertTrue(result["found"])
        self.assertEqual(result["record_count"], 3)
        self.assertEqual(result["matched_record_count"], 1)
        self.assertEqual(result["primary_record"]["accession"], "VCV000041804")
        self.assertEqual(result["primary_record"]["clinical_significance"], "Benign")
        # Every co-located record is still present, each correctly labelled.
        by_accession = {r["accession"]: r for r in result["records"]}
        self.assertFalse(by_accession["VCV000619783"]["variant_match"])
        self.assertFalse(by_accession["VCV000054169"]["variant_match"])
        self.assertTrue(by_accession["VCV000041804"]["variant_match"])

    def test_position_only_when_no_record_matches_allele(self):
        entries = [_BRCA1_CONFLICTING_SNV, _BRCA1_UNRELATED_DELETION]  # correct SNV omitted
        responses = self._mock_responses(
            _esearch_response([e["uid"] for e in entries]),
            _esummary_response(entries),
        )
        with mock.patch("requests.get", side_effect=responses):
            result = ClinVarClient().query_variant(_variant(), rsid=None, assembly="GRCh38")

        self.assertEqual(result["match_status"], ClinVarMatchStatus.POSITION_ONLY.value)
        self.assertFalse(result["found"])
        self.assertIsNone(result["primary_record"])
        self.assertEqual(result["record_count"], 2)
        self.assertEqual(result["matched_record_count"], 0)
        # The co-located records are still surfaced for context, not discarded.
        self.assertEqual(len(result["records"]), 2)

    def test_not_found_when_esearch_returns_nothing(self):
        with mock.patch(
            "requests.get",
            return_value=mock.Mock(
                status_code=200,
                json=lambda: _esearch_response([]),
                raise_for_status=lambda: None,
                text=__import__("json").dumps(_esearch_response([])),
            ),
        ):
            result = ClinVarClient().query_variant(_variant(), rsid=None, assembly="GRCh38")

        self.assertEqual(result["match_status"], ClinVarMatchStatus.NOT_FOUND.value)
        self.assertFalse(result["found"])
        self.assertIsNone(result["primary_record"])
        self.assertEqual(result["records"], [])

    def test_tp53_r248w_real_case_resolves_to_expert_panel_record(self):
        """Second live-verified regression case: TP53 c.742C>T
        (p.Arg248Trp), 17:7674221 G>A -- old `records[0]` behavior
        picked VCV002023589 (c.742dup, a different variant, single-
        submitter); must resolve to VCV000012347 (reviewed by expert
        panel, the actual queried allele)."""
        wrong_top = _record(
            "2023589",
            "VCV002023589",
            "NM_000546.6(TP53):c.742dup (p.Arg248fs)",
            "Pathogenic",
            "criteria provided, single submitter",
            "2022/08/12 00:00",
            spdi="NC_000017.11:7674219:C:CC",
        )
        correct = _record(
            "12347",
            "VCV000012347",
            "NM_000546.6(TP53):c.742C>T (p.Arg248Trp)",
            "Pathogenic",
            "reviewed by expert panel",
            "2024/08/05 00:00",
            spdi="NC_000017.11:7674220:G:A",
        )
        entries = [wrong_top, correct]
        responses = self._mock_responses(
            _esearch_response([e["uid"] for e in entries]),
            _esummary_response(entries),
        )
        with mock.patch("requests.get", side_effect=responses):
            result = ClinVarClient().query_variant(
                _variant(chrom="17", pos=7674221, ref="G", alt="A"),
                rsid=None,
                assembly="GRCh38",
            )

        self.assertEqual(result["primary_record"]["accession"], "VCV000012347")
        self.assertEqual(result["primary_record"]["review_status"], "reviewed by expert panel")


class TestNetworkFailureIsSanitized(unittest.TestCase):
    """
    Round 26: `_request_json` used to raise `ExternalAPIError` with the
    full request URL (and, on the retry-exhausted path, the raw
    underlying exception text) folded into its message via an f-string.
    That message reaches `pipeline/orchestrator.py::_run_clinvar_stage`'s
    `{"error": str(exc)}`, then `report/clinical_report_builder.py::
    _clinical_evidence`'s `clinvar_error`, rendered unconditionally by
    `report/report_generator.py` into every Markdown report's Clinical
    Evidence section, and embedded verbatim in geper_results.json's
    `clinvar` key by `report/json_builder.py` -- the same leak class
    rounds 20-24 fixed for SpliceBERT/MMSplice/UniProt/InterPro/ClinGen/
    AlphaFold DB. Full detail still reaches the log; what's raised must
    not.
    """

    def test_retry_exhausted_raises_sanitized_message(self):
        import requests as real_requests

        with (
            mock.patch(
                "requests.get", side_effect=real_requests.ConnectionError("Failed to establish a new connection")
            ),
            mock.patch("database.clinvar_client.time.sleep"),
        ):
            with self.assertRaises(ExternalAPIError) as ctx:
                ClinVarClient().query_variant(_variant(), rsid=None, assembly="GRCh38")

        message = str(ctx.exception)
        self.assertNotIn("http", message)
        self.assertNotIn("eutils.ncbi.nlm.nih.gov", message)
        self.assertNotIn("Failed to establish a new connection", message)
        self.assertEqual(message, "ClinVar request failed after 3 attempts")

    def test_offline_skip_raises_sanitized_message(self):
        with mock.patch("database.clinvar_client.HEALTH") as fake_health:
            fake_health.is_offline.return_value = True
            with self.assertRaises(ExternalAPIError) as ctx:
                ClinVarClient().query_variant(_variant(), rsid=None, assembly="GRCh38")

        message = str(ctx.exception)
        self.assertNotIn("http", message)
        self.assertNotIn("eutils.ncbi.nlm.nih.gov", message)
        self.assertEqual(message, "ClinVar request skipped: ClinVar was confirmed offline at startup.")


class TestWrongRsidDoesNotNarrowCandidateSet(unittest.TestCase):
    """
    Regression test for a second, upstream bug found while verifying
    this fix in production: `database/dbsnp_client.py::DbSNPClient.
    lookup_variant` resolves an rsID via the same kind of unchecked
    `esearch_uids[0]` pick this module's own record selection used to
    have -- live-confirmed for 17:43094298, dbSNP's positional esearch
    returns `['397508848', '80357024']`, and `[0]` (`rs397508848`) is
    the 2bp deletion `c.1232_1233del`'s rsID, not the queried SNV's
    (`rs80357024`). `ClinVarClient` used to search by rsID *instead
    of* position whenever one was supplied, so a wrong rsID didn't
    just mis-rank results -- the correct record never entered the
    candidate set at all, and no amount of correct `_variant_match`
    logic could recover it. `query_variant` now always runs the
    positional search and unions in the rsID search's results, so the
    correct record is present in `records` regardless of which rsID
    dbSNP resolved.
    """

    def test_correct_record_found_even_with_wrong_rsid(self):
        entries = [_BRCA1_CONFLICTING_SNV, _BRCA1_UNRELATED_DELETION, _BRCA1_CORRECT_SNV]
        # Positional esearch returns all 3 real UIDs; the (wrong) rsID
        # esearch returns only the deletion's UID -- exactly the live
        # dbSNP behavior this reproduces.
        responses = [
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=__import__("json").dumps(_esearch_response(["619783", "54169", "41804"])),
            ),
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=__import__("json").dumps(_esearch_response(["54169"])),
            ),
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=__import__("json").dumps(_esummary_response(entries)),
            ),
        ]
        with mock.patch("requests.get", side_effect=responses) as mock_get:
            result = ClinVarClient().query_variant(_variant(), rsid="rs397508848", assembly="GRCh38")

        # positional esearch + rsid esearch + one merged esummary; the
        # 4th call is round 30's submitter efetch (see
        # `ClinVarClient._fetch_submitters`) -- exhausts this test's
        # `responses` list, so that lookup fails and is caught/logged,
        # leaving `submitters` unset rather than affecting this
        # assertion's own concern (candidate-set completeness).
        self.assertEqual(mock_get.call_count, 4)
        self.assertEqual(result["match_status"], ClinVarMatchStatus.MATCHED.value)
        self.assertTrue(result["found"])
        self.assertEqual(result["primary_record"]["accession"], "VCV000041804")
        self.assertEqual(result["primary_record"]["clinical_significance"], "Benign")

    def test_esummary_fetched_once_per_uid_even_if_returned_by_both_searches(self):
        """The rsID search returning a UID the positional search
        already found must not double-fetch or double-list it."""
        entries = [_BRCA1_CORRECT_SNV]
        responses = [
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=__import__("json").dumps(_esearch_response(["41804"])),
            ),
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=__import__("json").dumps(_esearch_response(["41804"])),
            ),  # same UID again
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=__import__("json").dumps(_esummary_response(entries)),
            ),
        ]
        with mock.patch("requests.get", side_effect=responses):
            result = ClinVarClient().query_variant(_variant(), rsid="rs80357024", assembly="GRCh38")

        self.assertEqual(result["record_count"], 1)
        self.assertEqual(len(result["records"]), 1)

    def test_no_rsid_runs_only_the_positional_search(self):
        entries = [_BRCA1_CORRECT_SNV]
        responses = [
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=__import__("json").dumps(_esearch_response(["41804"])),
            ),
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=__import__("json").dumps(_esummary_response(entries)),
            ),
        ]
        with mock.patch("requests.get", side_effect=responses) as mock_get:
            result = ClinVarClient().query_variant(_variant(), rsid=None, assembly="GRCh38")

        # positional esearch + esummary only; the 3rd call is round 30's
        # submitter efetch (see `ClinVarClient._fetch_submitters`) --
        # exhausts this test's `responses` list, so that lookup fails
        # and is caught/logged, leaving `submitters` unset rather than
        # affecting this assertion's own concern (rsid=None doesn't run
        # an rsid-based esearch).
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(result["primary_record"]["accession"], "VCV000041804")


_VCV_XML_ONE_SUBMITTER = """<?xml version="1.0"?>
<ClinVarResult-Set>
  <VariationArchive VariationID="41804" Accession="VCV000041804">
    <ClassifiedRecord>
      <ClinicalAssertionList>
        <ClinicalAssertion ID="1">
          <ClinVarAccession Accession="SCV000123456" SubmitterName="ENIGMA" OrgID="500123"/>
        </ClinicalAssertion>
      </ClinicalAssertionList>
    </ClassifiedRecord>
  </VariationArchive>
</ClinVarResult-Set>"""

_VCV_XML_TWO_SUBMITTERS = """<?xml version="1.0"?>
<ClinVarResult-Set>
  <VariationArchive VariationID="41804" Accession="VCV000041804">
    <ClassifiedRecord>
      <ClinicalAssertionList>
        <ClinicalAssertion ID="1">
          <ClinVarAccession Accession="SCV000123456" SubmitterName="ENIGMA" OrgID="500123"/>
        </ClinicalAssertion>
        <ClinicalAssertion ID="2">
          <ClinVarAccession Accession="SCV000654321" SubmitterName="Ambry Genetics" OrgID="500456"/>
        </ClinicalAssertion>
      </ClinicalAssertionList>
    </ClassifiedRecord>
  </VariationArchive>
</ClinVarResult-Set>"""


class TestSubmitterCapture(unittest.TestCase):
    """
    Round 30: submitting organisation(s) captured on every matched
    ClinVar record (retrospective-study leakage control -- see
    `ROUND_CANDIDATES.md`'s Round 30 entry). Covers the second, XML
    `efetch` request `ClinVarClient._fetch_submitters` makes (submitter
    identity is not present in the `esummary` JSON `_esummary` already
    parses).
    """

    def _responses(self, esearch_payload, esummary_payload, vcv_xml):
        return [
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=__import__("json").dumps(esearch_payload),
            ),
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=__import__("json").dumps(esummary_payload),
            ),
            mock.Mock(status_code=200, raise_for_status=lambda: None, text=vcv_xml),
        ]

    def test_single_submitter_captured_on_matched_record(self):
        entries = [_BRCA1_CORRECT_SNV]
        responses = self._responses(_esearch_response(["41804"]), _esummary_response(entries), _VCV_XML_ONE_SUBMITTER)
        with mock.patch("requests.get", side_effect=responses):
            result = ClinVarClient().query_variant(_variant(), rsid=None, assembly="GRCh38")

        self.assertEqual(
            result["primary_record"]["submitters"],
            [{"name": "ENIGMA", "org_id": "500123", "scv": "SCV000123456"}],
        )

    def test_multiple_submitters_all_captured_not_collapsed(self):
        entries = [_BRCA1_CORRECT_SNV]
        responses = self._responses(_esearch_response(["41804"]), _esummary_response(entries), _VCV_XML_TWO_SUBMITTERS)
        with mock.patch("requests.get", side_effect=responses):
            result = ClinVarClient().query_variant(_variant(), rsid=None, assembly="GRCh38")

        names = {s["name"] for s in result["primary_record"]["submitters"]}
        self.assertEqual(names, {"ENIGMA", "Ambry Genetics"})

    def test_submitter_lookup_failure_leaves_submitters_unset_not_fatal(self):
        """A broken/timed-out submitter efetch must not affect the
        classification-relevant match result computed before it."""
        entries = [_BRCA1_CORRECT_SNV]
        responses = [
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=__import__("json").dumps(_esearch_response(["41804"])),
            ),
            mock.Mock(
                status_code=200,
                raise_for_status=lambda: None,
                text=__import__("json").dumps(_esummary_response(entries)),
            ),
            mock.Mock(status_code=500, raise_for_status=mock.Mock(side_effect=Exception("efetch down"))),
        ]
        with mock.patch("requests.get", side_effect=responses):
            result = ClinVarClient().query_variant(_variant(), rsid=None, assembly="GRCh38")

        self.assertTrue(result["found"])
        self.assertEqual(result["primary_record"]["accession"], "VCV000041804")
        self.assertIsNone(result["primary_record"]["submitters"])

    def test_no_submitter_name_attribute_yields_empty_list_not_crash(self):
        entries = [_BRCA1_CORRECT_SNV]
        xml_no_name = """<?xml version="1.0"?>
<ClinVarResult-Set>
  <VariationArchive VariationID="41804">
    <ClassifiedRecord>
      <ClinicalAssertionList>
        <ClinicalAssertion ID="1">
          <ClinVarAccession Accession="SCV000999999"/>
        </ClinicalAssertion>
      </ClinicalAssertionList>
    </ClassifiedRecord>
  </VariationArchive>
</ClinVarResult-Set>"""
        responses = self._responses(_esearch_response(["41804"]), _esummary_response(entries), xml_no_name)
        with mock.patch("requests.get", side_effect=responses):
            result = ClinVarClient().query_variant(_variant(), rsid=None, assembly="GRCh38")

        self.assertEqual(result["primary_record"]["submitters"], [])


if __name__ == "__main__":
    unittest.main()
