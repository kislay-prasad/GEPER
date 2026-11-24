"""
ClinVar lookup completeness and GRCh37 indel matching -- Priority 2 remediation.

Two defects reproduced against live ClinVar (2026-11-24) before the fix:

1. TRUNCATED SEARCH. `ClinVarClient._esearch` asked for retmax=10 and read
   only `idlist`, never `count`. The +/-1 bp indel window at BRCA1
   c.68_69del (GRCh38 17:43124027 ACT>A; VCV000017662, Pathogenic, reviewed
   by expert panel) holds 20 records; the matching one was 20th and was never
   fetched, so the variant was reported POSITION_ONLY -- "No ClinVar record
   found for this exact variant" -- on both builds, and the expert-panel
   disagreement check (gated on match_status == "matched") could not fire.

2. GRCh37 INDELS. ClinVar's `canonical_spdi` is on GRCh38. For a GRCh37
   query the SPDI comparison ran in the wrong coordinate frame, and the
   raw-allele fallback only works for SNVs, so even when the correct record
   WAS fetched, GRCh37 17:41276044 ACT>A returned variant_match=False.

The record shapes below are taken from the live esummary responses
(irrelevant fields trimmed). The mock NCBI server honours the requested
`retmax` and reports the true `count`, as the real API does -- so the
truncation test fails on the old code (retmax=10) for the same reason the
live lookup did.

A third, related gap is pinned too: PS1/PM5 drew a NEGATIVE conclusion
("not_triggered") from a ClinVar codon lookup that had partly failed, and a
totally failed lookup was described as "no ClinVar record ... was found".
"""

import unittest
from unittest import mock

from database.clinvar_client import ClinVarClient, ClinVarMatchStatus, self_build_offset
from pipeline.acmg_rules import ACMGRuleEngine, CriterionResult
from pipeline.ps1_pm5.lookup import ClinVarCodonLookup
from pipeline.vcf_parser import Variant
from utils.exceptions import ExternalAPIError
from utils.ncbi_eutils import ESEARCH_MAX_RECORDS, ESUMMARY_BATCH_SIZE, complete_esearch_idlist


def _variant(pos, ref, alt, chrom="17"):
    return Variant(chrom=chrom, pos=pos, variant_id=".", ref=ref, alt=alt, qual=None, filter_status=None)


def _entry(uid, accession, title, significance, review, spdi, loc38, loc37):
    variation_loc = []
    if loc38 is not None:
        variation_loc.append({"assembly_name": "GRCh38", "chr": "17", "start": str(loc38)})
    if loc37 is not None:
        variation_loc.append({"assembly_name": "GRCh37", "chr": "17", "start": str(loc37)})
    return {
        "uid": uid,
        "accession": accession,
        "title": title,
        "germline_classification": {
            "description": significance,
            "review_status": review,
            "last_evaluated": "2024/01/01 00:00",
            "trait_set": [],
        },
        "variation_set": [{"canonical_spdi": spdi, "variation_loc": variation_loc}],
    }


# Live: VCV000017662, BRCA1 c.68_69del (185delAG founder variant).
_BRCA1_C68 = _entry(
    "17662",
    "VCV000017662",
    "NM_007294.4(BRCA1):c.68_69del (p.Glu23fs)",
    "Pathogenic",
    "reviewed by expert panel",
    "NC_000017.11:43124027:CTCT:CT",
    43124028,
    41276045,
)
# Live: VCV000054169, BRCA1 c.1232_1233del; VCV000041804, c.1233T>G.
_BRCA1_C1232DEL = _entry(
    "54169",
    "VCV000054169",
    "NM_007294.4(BRCA1):c.1232_1233del (p.Asp411fs)",
    "Pathogenic",
    "reviewed by expert panel",
    "NC_000017.11:43094297:AT:",
    43094298,
    41246315,
)
_BRCA1_C1233TG = _entry(
    "41804",
    "VCV000041804",
    "NM_007294.4(BRCA1):c.1233T>G (p.Asp411Glu)",
    "Benign",
    "reviewed by expert panel",
    "NC_000017.11:43094297:A:C",
    43094298,
    41246315,
)


def _filler(i):
    # A different, non-matching variant in the same window.
    return _entry(
        str(900000 + i),
        f"VCV00090{i:04d}",
        f"filler {i}",
        "Uncertain significance",
        "criteria provided, single submitter",
        f"NC_000017.11:{43124100 + i}:G:A",
        43124101 + i,
        41276118 + i,
    )


class _FakeEutils:
    """Honours `retmax` and reports the true `count`, like NCBI."""

    def __init__(self, entries):
        self.entries = {e["uid"]: e for e in entries}
        self.order = [e["uid"] for e in entries]
        self.esummary_batches = []

    def __call__(self, url, params):
        if url.endswith("esearch.fcgi"):
            retmax = int(params.get("retmax", 20))
            return {"esearchresult": {"count": str(len(self.order)), "idlist": self.order[:retmax]}}
        if url.endswith("esummary.fcgi"):
            uids = params["id"].split(",")
            self.esummary_batches.append(len(uids))
            result = {"uids": uids}
            result.update({u: self.entries[u] for u in uids})
            return {"result": result}
        raise AssertionError(f"unexpected url {url}")


class TestEsearchCompleteness(unittest.TestCase):
    def test_complete_list_is_returned(self):
        self.assertEqual(
            complete_esearch_idlist({"esearchresult": {"count": "2", "idlist": ["1", "2"]}}, source="t"), ["1", "2"]
        )

    def test_truncated_list_raises_instead_of_passing_as_complete(self):
        with self.assertRaises(ExternalAPIError) as ctx:
            complete_esearch_idlist({"esearchresult": {"count": "20", "idlist": ["1"] * 10}}, source="t")
        self.assertIn("incomplete", str(ctx.exception))

    def test_missing_or_unparseable_count_trusts_the_list(self):
        for payload in ({"esearchresult": {"idlist": ["1"]}}, {"esearchresult": {"count": "n/a", "idlist": ["1"]}}):
            with self.subTest(payload=payload):
                self.assertEqual(complete_esearch_idlist(payload, source="t"), ["1"])


class TestBrca1C68DelIsFoundOnBothBuilds(unittest.TestCase):
    def _query(self, pos, assembly):
        # 19 non-matching records first, the real one 20th -- the live order.
        fake = _FakeEutils([_filler(i) for i in range(19)] + [_BRCA1_C68])
        client = ClinVarClient()
        with (
            mock.patch.object(client, "_request_json", side_effect=fake),
            mock.patch.object(client, "_fetch_submitters", return_value={}),
        ):
            return client.query_variant(_variant(pos, "ACT", "A"), assembly=assembly), fake

    def test_grch38(self):
        result, _ = self._query(43124027, "GRCh38")
        self.assertEqual(result["match_status"], ClinVarMatchStatus.MATCHED.value)
        self.assertEqual(result["record_count"], 20)
        self.assertEqual(result["primary_record"]["accession"], "VCV000017662")

    def test_grch37(self):
        result, _ = self._query(41276044, "GRCh37")
        self.assertEqual(result["match_status"], ClinVarMatchStatus.MATCHED.value)
        self.assertEqual(result["primary_record"]["accession"], "VCV000017662")

    def test_a_search_larger_than_the_limit_fails_loudly(self):
        fake = _FakeEutils([_filler(i) for i in range(ESEARCH_MAX_RECORDS + 1)])
        client = ClinVarClient()
        with mock.patch.object(client, "_request_json", side_effect=fake):
            with self.assertRaises(ExternalAPIError):
                client.query_variant(_variant(43124027, "ACT", "A"), assembly="GRCh38")

    def test_esummary_is_batched(self):
        fake = _FakeEutils([_filler(i) for i in range(450)])
        client = ClinVarClient()
        with (
            mock.patch.object(client, "_request_json", side_effect=fake),
            mock.patch.object(client, "_fetch_submitters", return_value={}),
        ):
            result = client.query_variant(_variant(43124027, "ACT", "A"), assembly="GRCh38")
        self.assertEqual(result["record_count"], 450)
        self.assertTrue(all(n <= ESUMMARY_BATCH_SIZE for n in fake.esummary_batches), fake.esummary_batches)


class TestGrch37AlleleMatching(unittest.TestCase):
    def _match(self, entry, pos, ref, alt, assembly="GRCh37"):
        return ClinVarClient._variant_match(entry, _variant(pos, ref, alt), assembly)

    def test_grch37_deletions_match_their_own_record(self):
        self.assertTrue(self._match(_BRCA1_C68, 41276044, "ACT", "A"))
        self.assertTrue(self._match(_BRCA1_C1232DEL, 41246314, "CAT", "C"))

    def test_grch37_snv_still_matches_and_other_alleles_do_not(self):
        self.assertTrue(self._match(_BRCA1_C1233TG, 41246315, "A", "C"))
        self.assertFalse(self._match(_BRCA1_C1233TG, 41246315, "A", "G"))
        self.assertFalse(self._match(_BRCA1_C1232DEL, 41246314, "CATA", "C"))
        self.assertFalse(self._match(_BRCA1_C68, 41276044, "ACTCT", "A"))

    def test_no_query_build_location_means_no_translated_match(self):
        no37 = _entry("1", "VCV1", "t", "Pathogenic", "x", "NC_000017.11:43124027:CTCT:CT", 43124028, None)
        self.assertFalse(self._match(no37, 41276044, "ACT", "A"))

    def test_grch38_behaviour_unchanged(self):
        self.assertTrue(self._match(_BRCA1_C68, 43124027, "ACT", "A", assembly="GRCh38"))
        self.assertTrue(self._match(_BRCA1_C1232DEL, 43094297, "CAT", "C", assembly="GRCh38"))

    def test_self_build_offset(self):
        loc = _BRCA1_C68["variation_set"][0]["variation_loc"]
        self.assertEqual(self_build_offset(loc, "GRCh37"), 43124028 - 41276045)
        self.assertIsNone(self_build_offset([{"assembly_name": "GRCh38", "start": "1"}], "GRCh37"))
        self.assertIsNone(
            self_build_offset(
                [{"assembly_name": "GRCh38", "start": "x"}, {"assembly_name": "GRCh37", "start": "1"}], "GRCh37"
            )
        )


class _Transcript:
    transcript_id = "NM_TEST.1"
    chrom = "17"

    @staticmethod
    def genomic_positions_for_codon(codon_number):
        return [100, 101, 102]


class TestCodonLookupReportsPartialFailure(unittest.TestCase):
    def _lookup(self, side_effects):
        lookup = ClinVarCodonLookup.__new__(ClinVarCodonLookup)
        lookup.cache = None
        calls = iter(side_effects)

        def esearch(term):
            effect = next(calls)
            if isinstance(effect, Exception):
                raise effect
            return effect

        with (
            mock.patch.object(lookup, "_esearch", side_effect=esearch),
            mock.patch.object(lookup, "_esummary", return_value=[mock.Mock(uid="1", to_dict=lambda: {"uid": "1"})]),
            mock.patch.object(lookup, "_fetch_submitters", return_value={}),
            mock.patch("pipeline.ps1_pm5.lookup.dedupe_by_uid", side_effect=lambda m: m),
            mock.patch("pipeline.ps1_pm5.lookup.dataclasses.replace", side_effect=lambda m, **k: m),
        ):
            return lookup.query_codon(_Transcript(), 175, "GRCh38")

    def test_partial_failure_is_marked_incomplete(self):
        result = self._lookup([["1"], ExternalAPIError("incomplete search"), []])
        self.assertIsNone(result["error"])
        self.assertTrue(result["incomplete"])
        self.assertIn("incomplete search", result["incomplete_reason"])

    def test_total_failure_is_an_error_not_incomplete(self):
        result = self._lookup([ExternalAPIError("down")] * 3)
        self.assertIsNotNone(result["error"])
        self.assertFalse(result["incomplete"])

    def test_clean_lookup_is_complete(self):
        result = self._lookup([["1"], [], []])
        self.assertIsNone(result["error"])
        self.assertFalse(result["incomplete"])


class _Query:
    def __init__(self, category):
        self.category = category


class TestPs1Pm5NeverConcludesAbsenceFromAFailedLookup(unittest.TestCase):
    def _result(self, status, code="PM5"):
        return CriterionResult(code, "pathogenic", "moderate", status, "original rationale")

    def test_total_failure_is_not_evaluated_and_says_the_lookup_failed(self):
        out = ACMGRuleEngine._ps1_pm5_lookup_gap(
            self._result("not_evaluated"), _Query("missense"), {"error": "NCBI down", "matches": []}
        )
        self.assertEqual(out.status, "not_evaluated")
        self.assertIn("lookup failed", out.rationale)
        self.assertNotIn("no ClinVar record", out.rationale)

    def test_partial_failure_cannot_conclude_not_triggered(self):
        out = ACMGRuleEngine._ps1_pm5_lookup_gap(
            self._result("not_triggered"),
            _Query("missense"),
            {"error": None, "incomplete": True, "incomplete_reason": "position 101 failed", "matches": [{}]},
        )
        self.assertEqual(out.status, "not_evaluated")
        self.assertIn("incomplete", out.rationale)

    def test_a_positive_finding_from_retrieved_records_is_kept(self):
        triggered = self._result("triggered")
        out = ACMGRuleEngine._ps1_pm5_lookup_gap(
            triggered, _Query("missense"), {"error": None, "incomplete": True, "matches": [{}]}
        )
        self.assertIs(out, triggered)

    def test_non_missense_and_complete_lookups_are_unchanged(self):
        not_applicable = self._result("not_triggered")
        self.assertIs(
            ACMGRuleEngine._ps1_pm5_lookup_gap(not_applicable, _Query("nonsense"), {"error": "down"}), not_applicable
        )
        self.assertIs(ACMGRuleEngine._ps1_pm5_lookup_gap(not_applicable, None, {"error": "down"}), not_applicable)
        clean = self._result("not_triggered")
        self.assertIs(
            ACMGRuleEngine._ps1_pm5_lookup_gap(clean, _Query("missense"), {"error": None, "incomplete": False}), clean
        )


# ---------------------------------------------------------------------------
# PR #51 round 2 (F6, F7)
# ---------------------------------------------------------------------------


class TestF6NcbiErrorPayloadsAreFailuresNotEmptyResults(unittest.TestCase):
    # NCBI reports some failures as a normal JSON body. Each of these has no
    # `count` and an empty/absent `idlist`, and used to read as "not found".
    _ERROR_BODIES = {
        "rate-limit body (top-level error)": {"error": "API rate limit exceeded", "api-key": "x", "count": "1"},
        "invalid query (esearchresult.ERROR)": {"esearchresult": {"ERROR": "Invalid query"}},
        "top-level ERROR": {"ERROR": "service unavailable"},
        "no result set": {},
        "result set is not an object": {"esearchresult": "oops"},
        "payload is not an object": ["unexpected"],
        "payload is None": None,
    }

    def test_each_error_body_raises(self):
        for label, payload in self._ERROR_BODIES.items():
            with self.subTest(body=label):
                with self.assertRaises(ExternalAPIError):
                    complete_esearch_idlist(payload, source="t")

    def test_messages_carry_no_query_text_or_url(self):
        for label, payload in self._ERROR_BODIES.items():
            with self.subTest(body=label):
                try:
                    complete_esearch_idlist(payload, source="ClinVarClient")
                except ExternalAPIError as exc:
                    message = str(exc).lower()
                    self.assertNotIn("http", message)
                    self.assertNotIn("invalid query", message)
                    self.assertNotIn("rate limit exceeded", message)

    def test_a_genuine_zero_hit_search_is_still_not_found(self):
        # NCBI sends errorlist.phrasesnotfound with count "0" for a term that
        # simply matches nothing. That is a real "not found", not a failure.
        zero = {"esearchresult": {"count": "0", "idlist": [], "errorlist": {"phrasesnotfound": ["1[chrpos38]"]}}}
        self.assertEqual(complete_esearch_idlist(zero, source="t"), [])
        warned = {"esearchresult": {"count": "0", "idlist": [], "warninglist": {"outputmessages": ["x"]}}}
        self.assertEqual(complete_esearch_idlist(warned, source="t"), [])

    def test_valid_shapes_are_unchanged(self):
        self.assertEqual(complete_esearch_idlist({"esearchresult": {"idlist": ["1", "2"]}}, source="t"), ["1", "2"])
        self.assertEqual(
            complete_esearch_idlist({"esearchresult": {"count": "2", "idlist": ["1", "2"]}}, source="t"), ["1", "2"]
        )
        with self.assertRaises(ExternalAPIError):  # truncation still detected
            complete_esearch_idlist({"esearchresult": {"count": "5", "idlist": ["1"]}}, source="t")

    def test_clinvar_client_reports_a_failed_lookup_not_not_found(self):
        client = ClinVarClient()
        with mock.patch.object(client, "_request_json", return_value={"error": "API rate limit exceeded"}):
            with self.assertRaises(ExternalAPIError):
                client.query_variant(_variant(43124027, "ACT", "A"), assembly="GRCh38")

    def test_codon_lookup_surfaces_the_error_instead_of_an_empty_result(self):
        lookup = ClinVarCodonLookup.__new__(ClinVarCodonLookup)
        lookup.cache = None
        with mock.patch.object(lookup, "_request_json", return_value={"esearchresult": {"ERROR": "Invalid query"}}):
            result = lookup.query_codon(_Transcript(), 175, "GRCh38")
        self.assertIsNotNone(result["error"])
        self.assertFalse(result["found"])


class _RecordingCache:
    def __init__(self):
        self.puts = []

    def get(self, key):
        return None

    def put(self, key, value):
        self.puts.append((key, value))


class TestF7IncompleteCodonLookupsAreNotCached(unittest.TestCase):
    def _run(self, side_effects):
        lookup = ClinVarCodonLookup.__new__(ClinVarCodonLookup)
        lookup.cache = _RecordingCache()
        calls = iter(side_effects)

        def esearch(term):
            effect = next(calls)
            if isinstance(effect, Exception):
                raise effect
            return effect

        with (
            mock.patch.object(lookup, "_esearch", side_effect=esearch),
            mock.patch.object(lookup, "_esummary", return_value=[mock.Mock(uid="1", to_dict=lambda: {"uid": "1"})]),
            mock.patch.object(lookup, "_fetch_submitters", return_value={}),
            mock.patch("pipeline.ps1_pm5.lookup.dedupe_by_uid", side_effect=lambda m: m),
            mock.patch("pipeline.ps1_pm5.lookup.dataclasses.replace", side_effect=lambda m, **k: m),
        ):
            result = lookup.query_codon(_Transcript(), 175, "GRCh38")
        return result, lookup.cache.puts

    def test_a_complete_result_is_cached(self):
        result, puts = self._run([["1"], [], []])
        self.assertFalse(result["incomplete"])
        self.assertEqual(len(puts), 1)

    def test_an_incomplete_result_is_returned_but_not_cached(self):
        result, puts = self._run([["1"], ExternalAPIError("incomplete search"), []])
        self.assertTrue(result["incomplete"])
        self.assertEqual(puts, [], "an incomplete lookup must not be cached, or PS1/PM5 stay degraded for the TTL")

    def test_a_total_failure_is_still_not_cached(self):
        result, puts = self._run([ExternalAPIError("down")] * 3)
        self.assertIsNotNone(result["error"])
        self.assertEqual(puts, [])

    def test_a_recovered_lookup_replaces_the_incomplete_one(self):
        # First call incomplete (not cached); the retry is complete and IS cached.
        _, first = self._run([["1"], ExternalAPIError("blip"), []])
        _, second = self._run([["1"], [], []])
        self.assertEqual((len(first), len(second)), (0, 1))


if __name__ == "__main__":
    unittest.main()
