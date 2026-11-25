"""
Regression tests for ClinVar audit C3: PS1/PM5 self-exclusion compared a GRCh38 position (the match's canonical-SPDI
`pos`) with a coordinate in the run's own assembly, so on GRCh37 input a variant's OWN ClinVar record was not excluded
and could be cited as its own PS1 anchor.

Ground truth, no invented coordinates: every ClinVar record below is a real esummary entry frozen in
`fixtures/c2_clinvar_record_types.json`, and the GRCh37 position of each query is ClinVar's OWN `variation_loc` start
for the very same record (never a conversion done here). `fixtures/c3_ps1_pm5_grch38_golden.json` holds the PS1/PM5
output of the UNMODIFIED code (origin/master 26864ae) for 108 GRCh38 queries and must keep matching.

The full engine cannot be run on GRCh37 offline (it needs the GRCh37 Ensembl transcript); the query's amino-acid
consequence is therefore taken from the ClinVar title (`p.Cys653Ser`) and the engine's query-context step is stubbed.
"""

import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from pypdf import PdfReader

from database.clinvar_client import ClinVarClient
from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.interpretation import InterpretationEngine
from pipeline.ps1_pm5 import decision as D
from pipeline.ps1_pm5.cache import ClinVarCodonCache
from pipeline.ps1_pm5.decision import PS1PM5Evaluator, identify_record
from pipeline.ps1_pm5.lookup import MATCH_SCHEMA, ClinVarCodonLookup
from pipeline.ps1_pm5.models import ClinVarCodonMatch
from pipeline.ps1_pm5.utils import canonical_build, clinvar_codon_match_from_esummary, parse_protein_change
from pipeline.pvs1.utils import CodingConsequenceDetail
from pipeline.vcf_parser import Variant
from report import export_lims
from report.json_builder import build_variant_result
from report.models import compute_content_hash
from report.report_generator import ReportGenerator
from report.summary import generate_pdf
from tests import test_ps1_pm5 as _base  # module import: its TestCase classes must not be re-collected here

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
with open(os.path.join(_FIXTURES, "c2_clinvar_record_types.json"), "r", encoding="utf-8") as _fh:
    CASES = {c["key"]: c for c in json.load(_fh)["cases"]}
with open(os.path.join(_FIXTURES, "c3_ps1_pm5_grch38_golden.json"), "r", encoding="utf-8") as _fh:
    GOLDEN = json.load(_fh)["rows"]

SLC26A2 = "SLC26A2_c1957TA"
TP53 = "TP53_M237I"
BRCA1_C1233 = "BRCA1_c1233TG"
BRCA1_C68 = "BRCA1_c68_69del"
OWN_SLC26A2 = "4098"  # p.Cys653Ser, P/LP, 2-star: the ONLY same-amino-acid record at its codon
OWN_TP53 = {"c.711G>T": "634770", "c.711G>C": "1757269", "c.711G>A": "142714"}  # three alleles, one position
GRCH37_SPELLINGS = ("GRCh37", "GRCH37", "hg19", "b37")
GRCH38_SPELLINGS = ("GRCh38", "hg38")


# ---------------------------------------------------------------------------------------------------
# real-fixture helpers
# ---------------------------------------------------------------------------------------------------


def entry(case_key, uid):
    return CASES[case_key]["entries"][uid]


def own_query(case_key, uid):
    """{build: (pos, ref, alt)} of one real record: GRCh38 from its canonical SPDI, GRCh37 from ClinVar's own variation_loc."""
    component = entry(case_key, uid)["variation_set"][0]
    _, spdi_pos, ref, alt = component["canonical_spdi"].split(":", 3)
    locs = {loc["assembly_name"]: int(loc["start"]) for loc in component["variation_loc"]}
    assert locs["GRCh38"] == int(spdi_pos) + 1  # the fixture is internally consistent
    return {"GRCh38": (int(spdi_pos) + 1, ref, alt), "GRCh37": (locs["GRCh37"], ref, alt)}


def codon_matches(case_key, codon):
    """Every real single-allele record with a parseable missense change at `codon`, as the lookup returns them (dicts)."""
    out = []
    for uid, e in CASES[case_key]["entries"].items():
        m = clinvar_codon_match_from_esummary(uid, e)
        if m is not None and m.codon_number == codon:
            out.append(m.to_dict())
    return out


def query_detail(case_key, uid):
    parsed = parse_protein_change(entry(case_key, uid)["title"])
    return CodingConsequenceDetail("missense", parsed["codon_number"], parsed["ref_aa"], parsed["alt_aa"]), parsed[
        "codon_number"
    ]


def evaluate(rule, case_key, uid, build_name, assembly=None, own_uids=None, matches=None):
    """PS1 or PM5 for the real record `uid` taken as the query variant, in `build_name`, through the real evaluator."""
    detail, codon = query_detail(case_key, uid)
    pos, ref, alt = own_query(case_key, uid)[build_name]
    ev = PS1PM5Evaluator()
    fn = ev.evaluate_ps1 if rule == "PS1" else ev.evaluate_pm5
    return fn(
        detail,
        matches if matches is not None else codon_matches(case_key, codon),
        None,
        pos,
        ref,
        alt,
        assembly=assembly or build_name,
        own_uids=own_uids,
    )


def uids(records):
    return sorted(r["uid"] for r in records)


# ---------------------------------------------------------------------------------------------------
# 1. assembly aliases
# ---------------------------------------------------------------------------------------------------


class _FakeTranscript:
    transcript_id = "NM_TEST.1"
    chrom = "5"

    def genomic_positions_for_codon(self, codon):
        return [149981550]


class TestAssemblyAliases(unittest.TestCase):
    def test_supported_spellings_are_canonicalised(self):
        for spelling in GRCH37_SPELLINGS:
            self.assertEqual(canonical_build(spelling), "GRCh37", spelling)
        for spelling in GRCH38_SPELLINGS:
            self.assertEqual(canonical_build(spelling), "GRCh38", spelling)
        self.assertEqual(canonical_build("  hg19 "), "GRCh37")
        self.assertEqual(canonical_build("HG38"), "GRCh38")

    def test_unknown_or_unsupported_values_are_unresolved_never_grch38(self):
        for value in (None, "", " ", "37", "38", "GRCh36", "hg18", "GRCh38.p14", "GRCh37.p13", "foo", 37, ["GRCh38"]):
            self.assertIsNone(canonical_build(value), repr(value))

    def test_the_lookup_uses_the_right_entrez_field_cache_key_and_reports_the_build(self):
        for spelling, field, build in [(s, "chrpos37", "GRCh37") for s in GRCH37_SPELLINGS] + [
            (s, "chrpos38", "GRCh38") for s in GRCH38_SPELLINGS
        ]:
            cache = ClinVarCodonCache(max_size=10, ttl_seconds=60)
            lookup = ClinVarCodonLookup(cache=cache)
            terms = []
            with (
                mock.patch.object(lookup, "_esearch", side_effect=lambda term: terms.append(term) or []),
                mock.patch.object(lookup, "_fetch_submitters", return_value={}),
            ):
                result = lookup.query_codon(_FakeTranscript(), 653, assembly=spelling)
            with self.subTest(spelling=spelling):
                self.assertEqual(terms, [f"5[chr] AND 149981550[{field}]"])
                self.assertEqual(result["assembly"], build)
                self.assertEqual(result["match_schema"], MATCH_SCHEMA)
                self.assertFalse(result.get("assembly_unresolved"))

    def test_spellings_of_one_build_share_one_cache_entry_and_the_builds_never_mix(self):
        cache = ClinVarCodonCache(max_size=10, ttl_seconds=60)
        lookup = ClinVarCodonLookup(cache=cache)
        calls = []
        with (
            mock.patch.object(lookup, "_esearch", side_effect=lambda term: calls.append(term) or []),
            mock.patch.object(lookup, "_fetch_submitters", return_value={}),
        ):
            lookup.query_codon(_FakeTranscript(), 653, assembly="GRCh37")
            again = lookup.query_codon(_FakeTranscript(), 653, assembly="hg19")
            other = lookup.query_codon(_FakeTranscript(), 653, assembly="GRCh38")
        self.assertEqual(again["source"], "cache")
        self.assertEqual(len(calls), 2)  # one GRCh37 search, one GRCh38 search
        self.assertEqual(other["assembly"], "GRCh38")

    def test_unresolved_assembly_does_not_run_the_lookup_and_says_so(self):
        for value in (None, "", "37", "GRCh38.p14", "GRCh36"):
            lookup = ClinVarCodonLookup(cache=ClinVarCodonCache(max_size=10, ttl_seconds=60))
            with mock.patch.object(lookup, "_esearch") as esearch:
                result = lookup.query_codon(_FakeTranscript(), 653, assembly=value)
            with self.subTest(value=value):
                esearch.assert_not_called()
                self.assertTrue(result["assembly_unresolved"])
                self.assertIsNone(result["assembly"])
                self.assertEqual(result["matches"], [])
                self.assertIn("not a supported build", result["reason"])

    def test_pm5_and_ps1_become_not_evaluated_with_an_explicit_reason_when_the_assembly_is_unresolved(self):
        codon_result = {
            "skipped": False,
            "found": False,
            "matches": [],
            "assembly": None,
            "assembly_unresolved": True,
            "assembly_input": "GRCh38.p14",
        }
        detail, _ = query_detail(SLC26A2, OWN_SLC26A2)
        pos, ref, alt = own_query(SLC26A2, OWN_SLC26A2)["GRCh38"]
        variant = {"chrom": "5", "pos": pos, "ref": ref, "alt": alt}
        for rule in (ACMGRuleEngine._ps1, ACMGRuleEngine._pm5):
            with mock.patch.object(
                ACMGRuleEngine, "_ps1_pm5_query_context", staticmethod(lambda vd, tr: (None, pos, ref, alt, detail))
            ):
                result = rule(variant_dict=variant, transcript_result={}, clinvar_codon_result=codon_result)
            self.assertEqual(result.status, "not_evaluated")
            self.assertIn("could not be resolved to GRCh37 or GRCh38", result.rationale)
            self.assertIn("'GRCh38.p14'", result.rationale)


# ---------------------------------------------------------------------------------------------------
# 2. same variant, same assembly / other assembly
# ---------------------------------------------------------------------------------------------------


class TestSameVariantAcrossAssemblies(unittest.TestCase):
    def test_slc26a2_grch38_baseline_the_own_record_is_excluded(self):
        ps1 = evaluate("PS1", SLC26A2, OWN_SLC26A2, "GRCh38")
        self.assertFalse(ps1.applies)
        self.assertEqual(uids(ps1.matched_anchors), [])
        self.assertEqual([e["uid"] for e in ps1.excluded_records], [OWN_SLC26A2])
        self.assertEqual(ps1.excluded_records[0]["reason_code"], D.REASON_OWN_COORDINATES)

    def test_slc26a2_grch37_gives_the_same_answer_it_used_to_cite_its_own_record(self):
        for spelling in GRCH37_SPELLINGS:
            ps1 = evaluate("PS1", SLC26A2, OWN_SLC26A2, "GRCh37", assembly=spelling)
            with self.subTest(spelling=spelling):
                self.assertFalse(ps1.applies)
                self.assertEqual(uids(ps1.matched_anchors), [])
                self.assertEqual([e["uid"] for e in ps1.excluded_records], [OWN_SLC26A2])
                self.assertNotIn("VCV000004098", " ".join(ps1.supporting_evidence))

    def test_slc26a2_pm5_is_unchanged_in_both_builds(self):
        for b in ("GRCh38", "GRCh37"):
            pm5 = evaluate("PM5", SLC26A2, OWN_SLC26A2, b)
            self.assertTrue(pm5.applies)
            self.assertEqual(
                uids(pm5.matched_anchors), ["1994865"]
            )  # Cys653Gly, LP 2-star: a genuine other-amino-acid anchor

    def test_the_old_comparison_would_have_failed_this_exact_case(self):
        """Documents the defect: the match's `pos` is GRCh38, the GRCh37 query's is not (ClinVar's own coordinates)."""
        match = codon_matches(SLC26A2, 653)[1]
        q38, q37 = own_query(SLC26A2, OWN_SLC26A2)["GRCh38"], own_query(SLC26A2, OWN_SLC26A2)["GRCh37"]
        self.assertEqual(match["pos"], q38[0])
        self.assertNotEqual(match["pos"], q37[0])
        self.assertEqual(match["pos_by_build"], {"GRCh37": q37[0], "GRCh38": q38[0]})

    def test_tp53_three_alleles_own_excluded_other_alleles_retained_in_both_builds(self):
        for label, own in OWN_TP53.items():
            others = sorted(set(OWN_TP53.values()) - {own})
            for build_name in ("GRCh38", "GRCh37"):
                ps1 = evaluate("PS1", TP53, own, build_name)
                with self.subTest(allele=label, build=build_name):
                    self.assertTrue(ps1.applies)
                    self.assertEqual(uids(ps1.matched_anchors), others)  # the two OTHER alleles, same position
                    self.assertEqual([e["uid"] for e in ps1.excluded_records], [own])
                    self.assertNotIn(own, uids(ps1.rejected_anchors))

    def test_a_different_allele_at_the_same_position_is_a_valid_anchor_even_when_ref_and_position_match(self):
        match = codon_matches(TP53, 237)[0]
        pos, ref, alt = own_query(TP53, "634770")["GRCh37"]
        other_alt = next(m for m in codon_matches(TP53, 237) if m["alt"] != alt)
        for build_name, qpos in (("GRCh37", pos), ("GRCh38", own_query(TP53, "634770")["GRCh38"][0])):
            self.assertEqual(identify_record(other_alt, build_name, qpos, ref, alt).verdict, D.IDENTITY_DIFFERENT)
        self.assertEqual(
            identify_record(match, "GRCh37", pos, ref, alt).verdict in (D.IDENTITY_OWN, D.IDENTITY_DIFFERENT), True
        )

    def test_a_nearby_variant_in_the_same_codon_is_not_excluded(self):
        # BRCA1 codon 24: records at 43124026 (GRCh37 41276043) and 43124027 (41276044). Query: c.70T>C (43124027 A>G).
        for build_name in ("GRCh38", "GRCh37"):
            pm5 = evaluate("PM5", BRCA1_C68, "55674", build_name)
            with self.subTest(build=build_name):
                self.assertEqual(
                    uids(pm5.matched_anchors), ["55678", "638952", "649351"]
                )  # adjacent-position and same-position others
                self.assertEqual(
                    [e["uid"] for e in pm5.excluded_records], []
                )  # own record has the SAME amino acid: PM5 never sees it
                ps1 = evaluate("PS1", BRCA1_C68, "55674", build_name)
                self.assertFalse(ps1.applies)
                self.assertEqual([e["uid"] for e in ps1.excluded_records], ["55674"])

    def test_a_benign_own_record_is_in_neither_the_accepted_nor_the_rejected_list_in_either_build(self):
        for build_name in ("GRCh38", "GRCh37"):
            ps1 = evaluate("PS1", BRCA1_C1233, "41804", build_name)
            with self.subTest(build=build_name):
                self.assertEqual(uids(ps1.matched_anchors), [])
                self.assertEqual(uids(ps1.rejected_anchors), [])  # master on GRCh37 put the Benign own record here
                self.assertEqual([e["uid"] for e in ps1.excluded_records], ["41804"])
                self.assertNotIn("VCV000041804", " ".join(ps1.caveats_checked[:0] + ps1.conflicting_evidence))

    def test_every_real_anchor_capable_record_behaves_identically_across_builds(self):
        """Property test over the fixtures: own excluded in both builds; every other record's role identical in both."""
        checked = 0
        for case_key, case in CASES.items():
            for uid, e in case["entries"].items():
                m = clinvar_codon_match_from_esummary(uid, e)
                if m is None or "GRCh37" not in m.pos_by_build or len(e["variation_set"]) != 1:
                    continue
                for rule in ("PS1", "PM5"):
                    results = {b: evaluate(rule, case_key, uid, b) for b in ("GRCh38", "GRCh37")}
                    roles = {
                        b: (
                            uids(r.matched_anchors),
                            uids(r.rejected_anchors),
                            sorted((x["uid"], x["reason_code"]) for x in r.excluded_records),
                        )
                        for b, r in results.items()
                    }
                    with self.subTest(case=case_key, uid=uid, rule=rule):
                        self.assertEqual(roles["GRCh38"], roles["GRCh37"])
                        for b in roles:
                            self.assertNotIn(uid, roles[b][0] + roles[b][1])
                        self.assertEqual(results["GRCh38"].applies, results["GRCh37"].applies)
                        checked += 1
        self.assertGreater(checked, 20)


# ---------------------------------------------------------------------------------------------------
# 3. the identity decision itself
# ---------------------------------------------------------------------------------------------------


def make_match(**changes):
    base = {
        "uid": "1",
        "accession": "VCV000000001",
        "title": "t",
        "pos": 1000,
        "ref": "A",
        "alt": "T",
        "protein_change": "K1E",
        "pos_by_build": {"GRCh37": 500, "GRCh38": 1000},
    }
    base.update(changes)
    return base


class TestIdentityDecision(unittest.TestCase):
    def test_same_position_and_alleles_in_each_build(self):
        self.assertEqual(identify_record(make_match(), "GRCh38", 1000, "A", "T").verdict, D.IDENTITY_OWN)
        self.assertEqual(identify_record(make_match(), "GRCh37", 500, "A", "T").verdict, D.IDENTITY_OWN)

    def test_a_grch37_coordinate_is_never_compared_with_the_grch38_position(self):
        # query 1000 in GRCh37 coordinates is NOT the record whose GRCh38 position is 1000 (its GRCh37 position is 500)
        self.assertEqual(identify_record(make_match(), "GRCh37", 1000, "A", "T").verdict, D.IDENTITY_DIFFERENT)

    def test_alleles_are_compared_case_insensitively_and_a_different_alt_is_a_different_variant(self):
        self.assertEqual(identify_record(make_match(), "GRCh38", 1000, "a", "t").verdict, D.IDENTITY_OWN)
        self.assertEqual(identify_record(make_match(), "GRCh38", 1000, "A", "G").verdict, D.IDENTITY_DIFFERENT)

    def test_ref_mismatch_never_lets_the_record_through_and_is_flagged(self):
        for build_name, pos in (("GRCh38", 1000), ("GRCh37", 500)):
            identity = identify_record(make_match(), build_name, pos, "C", "T")
            self.assertEqual(identity.verdict, D.IDENTITY_REF_MISMATCH)
            self.assertEqual(identity.reason_code, D.REASON_REF_MISMATCH)
            self.assertTrue(identity.excludes)

    def test_ref_mismatch_is_reported_in_the_evaluation_for_review(self):
        match = make_match(
            protein_change="C653S",
            clinical_significance="Pathogenic",
            review_status="reviewed by expert panel",
            star_rating=3,
            is_conflicting=False,
        )
        detail = CodingConsequenceDetail("missense", 653, "C", "S")
        ps1 = PS1PM5Evaluator().evaluate_ps1(detail, [match], None, 1000, "C", "T", assembly="GRCh38")
        self.assertFalse(ps1.applies)
        self.assertEqual(ps1.matched_anchors, [])
        self.assertEqual(ps1.excluded_records[0]["reason_code"], D.REASON_REF_MISMATCH)
        self.assertTrue(any("different reference allele" in c and "needs review" in c for c in ps1.unchecked_caveats))
        self.assertTrue(any("needs review" in c for c in ps1.conflicting_evidence))
        self.assertIn("identity could not be verified or needs review", ps1.rationale)

    def test_variant_level_uid_is_an_additional_safeguard_and_wins_even_without_comparable_coordinates(self):
        # coordinates alone would call this a different variant (and an unresolved build cannot compare at all)
        for build_name in ("GRCh37", "GRCh38", None):
            identity = identify_record(make_match(), build_name, 424242, "G", "C", own_uids=["1"])
            self.assertEqual((identity.verdict, identity.reason_code), (D.IDENTITY_OWN, D.REASON_OWN_VARIANT_LEVEL_UID))
        self.assertEqual(
            identify_record(make_match(), "GRCh38", 424242, "G", "C", own_uids=["2"]).verdict, D.IDENTITY_DIFFERENT
        )
        self.assertEqual(
            identify_record(make_match(uid=1), "GRCh38", 424242, "G", "C", own_uids=["1"]).verdict, D.IDENTITY_OWN
        )

    def test_unverifiable_identity_is_unverified_with_a_structured_reason(self):
        cases = [
            (make_match(), None, 500, D.REASON_UNVERIFIED_ASSEMBLY),
            (make_match(), "GRCh37", None, D.REASON_UNVERIFIED_QUERY),
            (make_match(pos_by_build={"GRCh38": 1000}), "GRCh37", 500, D.REASON_UNVERIFIED_NO_POSITION),
            (make_match(pos_by_build={}), "GRCh37", 500, D.REASON_UNVERIFIED_NO_POSITION),
            (make_match(pos_by_build={"GRCh37": "500"}), "GRCh37", 500, D.REASON_UNVERIFIED_NO_POSITION),
            (make_match(pos_by_build={"GRCh37": True}), "GRCh37", 1, D.REASON_UNVERIFIED_NO_POSITION),
        ]
        legacy = make_match()
        del legacy["pos_by_build"]
        cases.append((legacy, "GRCh37", 500, D.REASON_UNVERIFIED_LEGACY_SHAPE))
        for match, build_name, pos, reason in cases:
            identity = identify_record(match, build_name, pos, "A", "T")
            with self.subTest(reason=reason, build=build_name):
                self.assertEqual((identity.verdict, identity.reason_code), (D.IDENTITY_UNVERIFIED, reason))

    def test_a_match_stored_before_pos_by_build_existed_still_works_on_grch38(self):
        legacy = make_match()
        del legacy["pos_by_build"]
        self.assertEqual(identify_record(legacy, "GRCh38", 1000, "A", "T").verdict, D.IDENTITY_OWN)
        self.assertEqual(identify_record(legacy, "GRCh38", 1001, "A", "T").verdict, D.IDENTITY_DIFFERENT)


class TestUnresolvedAndUnverifiedEvidenceIsIneligibleAndExplained(unittest.TestCase):
    def _ps1(self, assembly, matches=None):
        detail, codon = query_detail(SLC26A2, OWN_SLC26A2)
        pos, ref, alt = own_query(SLC26A2, OWN_SLC26A2)["GRCh37"]
        return PS1PM5Evaluator().evaluate_ps1(
            detail, matches or codon_matches(SLC26A2, codon), None, pos, ref, alt, assembly=assembly
        )

    def test_missing_or_unsupported_assembly_makes_every_record_ineligible_and_says_why(self):
        for assembly in (None, "", "GRCh38.p14", "foo"):
            ps1 = self._ps1(assembly)
            with self.subTest(assembly=assembly):
                self.assertFalse(ps1.applies)
                self.assertEqual(ps1.matched_anchors, [])
                self.assertEqual({e["reason_code"] for e in ps1.excluded_records}, {D.REASON_UNVERIFIED_ASSEMBLY})
                self.assertTrue(
                    any("genome assembly of this run could not be resolved" in c for c in ps1.unchecked_caveats)
                )

    def test_old_cache_shaped_matches_in_a_grch37_run_are_not_silently_verified(self):
        legacy = codon_matches(SLC26A2, 653)
        for m in legacy:
            del m["pos_by_build"]
        ps1 = self._ps1("GRCh37", legacy)
        self.assertFalse(ps1.applies)
        self.assertEqual(ps1.matched_anchors, [])
        self.assertEqual({e["reason_code"] for e in ps1.excluded_records}, {D.REASON_UNVERIFIED_LEGACY_SHAPE})
        self.assertTrue(any("no per-assembly positions" in c for c in ps1.unchecked_caveats))

    def test_a_record_without_a_position_in_the_runs_build_is_ineligible_not_assumed_distinct(self):
        incomplete = codon_matches(SLC26A2, 653)
        for m in incomplete:
            m["pos_by_build"] = {"GRCh38": m["pos"]}
        ps1 = self._ps1("GRCh37", incomplete)
        self.assertEqual(ps1.matched_anchors, [])
        self.assertEqual({e["reason_code"] for e in ps1.excluded_records}, {D.REASON_UNVERIFIED_NO_POSITION})
        # ... and the same records are fully usable in GRCh38, where `pos` is the position in that build
        detail, codon = query_detail(SLC26A2, OWN_SLC26A2)
        pos, ref, alt = own_query(SLC26A2, OWN_SLC26A2)["GRCh38"]
        pm5 = PS1PM5Evaluator().evaluate_pm5(detail, incomplete, None, pos, ref, alt, assembly="GRCh38")
        self.assertTrue(pm5.applies)

    def test_missing_assembly_key_in_a_codon_result_is_unresolved_not_grch38(self):
        detail, codon = query_detail(SLC26A2, OWN_SLC26A2)
        pos, ref, alt = own_query(SLC26A2, OWN_SLC26A2)["GRCh38"]
        variant = {"chrom": "5", "pos": pos, "ref": ref, "alt": alt}
        result = {"skipped": False, "found": True, "matches": codon_matches(SLC26A2, codon)}  # no "assembly" key
        with mock.patch.object(
            ACMGRuleEngine, "_ps1_pm5_query_context", staticmethod(lambda vd, tr: (None, pos, ref, alt, detail))
        ):
            pm5 = ACMGRuleEngine._pm5(variant_dict=variant, transcript_result={}, clinvar_codon_result=result)
        self.assertFalse(pm5.details["applies"])
        self.assertEqual({e["reason_code"] for e in pm5.details["excluded_records"]}, {D.REASON_UNVERIFIED_ASSEMBLY})


class TestEngineAdapterPassesTheRunsBuildAndNeedsNoVariantLevelResult(unittest.TestCase):
    """`ACMGRuleEngine._ps1/_pm5`: coordinates alone must identify the own record when the variant-level lookup is absent."""

    def _run(self, rule, clinvar_result):
        detail, codon = query_detail(SLC26A2, OWN_SLC26A2)
        pos, ref, alt = own_query(SLC26A2, OWN_SLC26A2)["GRCh37"]
        variant = {"chrom": "5", "pos": pos, "ref": ref, "alt": alt}
        codon_result = codon_result_for(SLC26A2, codon, "GRCh37")
        with mock.patch.object(
            ACMGRuleEngine, "_ps1_pm5_query_context", staticmethod(lambda vd, tr: (None, pos, ref, alt, detail))
        ):
            return rule(
                variant_dict=variant,
                transcript_result={},
                clinvar_codon_result=codon_result,
                clinvar_result=clinvar_result,
            )

    def test_grch37_own_record_is_excluded_by_coordinates_when_there_is_no_variant_level_result(self):
        for clinvar_result in (
            None,
            {},
            {"query": None, "found": False, "error": "ClinVar stage failed"},
            {"records": []},
        ):
            ps1 = self._run(ACMGRuleEngine._ps1, clinvar_result)
            with self.subTest(clinvar_result=clinvar_result):
                self.assertEqual(ps1.status, "not_triggered")
                self.assertEqual(
                    [e["reason_code"] for e in ps1.details["excluded_records"]], [D.REASON_OWN_COORDINATES]
                )
                self.assertEqual(self._run(ACMGRuleEngine._pm5, clinvar_result).status, "triggered")

    def test_only_exact_variant_level_matches_count_as_the_variants_own_uids(self):
        records = [
            {"uid": "1", "variant_match": True},
            {"uid": "2", "variant_match": False},
            {"uid": "3", "variant_match": None},
            {"uid": "4"},
            "junk",
        ]
        self.assertEqual(ACMGRuleEngine._own_clinvar_uids({"records": records}), ["1"])
        self.assertEqual(ACMGRuleEngine._own_clinvar_uids(None), [])
        self.assertEqual(ACMGRuleEngine._own_clinvar_uids({"records": None}), [])


class TestIncompleteClinVarCoordinates(unittest.TestCase):
    def _match(self, mutate):
        e = copy.deepcopy(entry(SLC26A2, OWN_SLC26A2))
        mutate(e["variation_set"][0])
        return clinvar_codon_match_from_esummary(OWN_SLC26A2, e)

    def test_intact_record_keeps_both_builds_and_pos_keeps_its_grch38_meaning(self):
        m = self._match(lambda v: None)
        self.assertEqual(m.pos, 149981550)
        self.assertEqual(m.pos_by_build, {"GRCh37": 149361113, "GRCh38": 149981550})
        self.assertEqual(m.to_dict()["pos_by_build"], {"GRCh37": 149361113, "GRCh38": 149981550})

    def test_missing_or_partial_variation_loc_never_raises_and_never_invents_a_position(self):
        self.assertEqual(self._match(lambda v: v.pop("variation_loc")).pos_by_build, {})
        self.assertEqual(self._match(lambda v: v.__setitem__("variation_loc", None)).pos_by_build, {})
        self.assertEqual(self._match(lambda v: v.__setitem__("variation_loc", "x")).pos_by_build, {})
        only38 = self._match(
            lambda v: v.__setitem__(
                "variation_loc", [loc for loc in v["variation_loc"] if loc["assembly_name"] == "GRCh38"]
            )
        )
        self.assertEqual(only38.pos_by_build, {"GRCh38": 149981550})
        junk = self._match(
            lambda v: v.__setitem__(
                "variation_loc",
                [
                    None,
                    "x",
                    {"assembly_name": "GRCh37", "start": "abc"},
                    {"assembly_name": "GRCh37"},
                    {"assembly_name": "NCBI36", "start": "5"},
                    {"assembly_name": "GRCh38", "start": "149981550"},
                ],
            )
        )
        self.assertEqual(junk.pos_by_build, {"GRCh38": 149981550})

    def test_a_grch38_start_that_contradicts_the_spdi_position_distrusts_every_build(self):
        m = self._match(lambda v: v["variation_loc"].__setitem__(0, {"assembly_name": "GRCh38", "start": "149981551"}))
        self.assertEqual(m.pos_by_build, {})

    def test_unusable_spdi_still_drops_the_record(self):
        for spdi in ("garbage", "NC_000005.10:abc:T:A", "NC_000005.10:149981549:T:", None):
            self.assertIsNone(self._match(lambda v, s=spdi: v.__setitem__("canonical_spdi", s)), spdi)

    def test_incomplete_record_is_ineligible_in_grch37_and_does_not_raise(self):
        m = self._match(lambda v: v.pop("variation_loc")).to_dict()
        self.assertEqual(identify_record(m, "GRCh37", 149361113, "T", "A").reason_code, D.REASON_UNVERIFIED_NO_POSITION)
        self.assertEqual(identify_record(m, "GRCh38", 149981550, "T", "A").verdict, D.IDENTITY_OWN)

    def test_the_unused_is_same_variant_as_helper_is_gone(self):
        self.assertFalse(hasattr(ClinVarCodonMatch, "is_same_variant_as"))


# ---------------------------------------------------------------------------------------------------
# 4. caching: identity is per query variant and never baked into the shared cached codon result
# ---------------------------------------------------------------------------------------------------


class TestIdentityIsIndependentOfTheCodonCache(unittest.TestCase):
    def _lookup(self, cache):
        lookup = ClinVarCodonLookup(cache=cache)
        all_matches = [clinvar_codon_match_from_esummary(u, e) for u, e in CASES[TP53]["entries"].items()]
        all_matches = [m for m in all_matches if m is not None and m.codon_number == 237]
        patches = (
            mock.patch.object(lookup, "_esearch", return_value=["634770"]),
            mock.patch.object(lookup, "_esummary", return_value=all_matches),
            mock.patch.object(lookup, "_fetch_submitters", return_value={}),
        )
        return lookup, patches

    class _Tx:
        transcript_id = "NM_000546.6"
        chrom = "17"

        def genomic_positions_for_codon(self, codon):
            return [7577570]  # GRCh37 position of codon 237's first base (ClinVar's own loc of these records)

    def test_two_variants_sharing_one_cached_codon_each_exclude_only_their_own_record(self):
        cache = ClinVarCodonCache(max_size=10, ttl_seconds=60)
        lookup, patches = self._lookup(cache)
        with patches[0], patches[1], patches[2]:
            first = lookup.query_codon(self._Tx(), 237, assembly="GRCh37")
            second = lookup.query_codon(self._Tx(), 237, assembly="hg19")
        self.assertEqual(first["source"], "ncbi_eutils")
        self.assertEqual(second["source"], "cache")
        snapshot = copy.deepcopy(second)
        outcomes = {}
        for label, own in OWN_TP53.items():
            detail, _ = query_detail(TP53, own)
            pos, ref, alt = own_query(TP53, own)["GRCh37"]
            ps1 = PS1PM5Evaluator().evaluate_ps1(
                detail, second["matches"], None, pos, ref, alt, assembly=second["assembly"]
            )
            outcomes[label] = ([e["uid"] for e in ps1.excluded_records], uids(ps1.matched_anchors))
        self.assertEqual(outcomes["c.711G>T"], (["634770"], ["142714", "1757269"]))
        self.assertEqual(outcomes["c.711G>C"], (["1757269"], ["142714", "634770"]))
        self.assertEqual(outcomes["c.711G>A"], (["142714"], ["1757269", "634770"]))
        self.assertEqual(second, snapshot)  # evaluating never touched the shared result ...
        self.assertTrue(
            all("excluded" not in m and "is_own" not in m for m in second["matches"])
        )  # ... and it carries no per-variant verdict

    def test_a_cache_entry_with_the_old_match_shape_is_never_reused(self):
        for build_name, old_key in (
            ("GRCh37", "codon:GRCh37:NM_000546.6:237"),
            ("GRCh38", "codon:GRCh38:NM_000546.6:237"),
        ):
            cache = ClinVarCodonCache(max_size=10, ttl_seconds=60)
            stale = {
                "skipped": False,
                "found": True,
                "source": "ncbi_eutils",
                "matches": [{"uid": "634770", "pos": 7674252, "ref": "C", "alt": "A", "protein_change": "M237I"}],
            }
            cache.put(old_key, dict(stale))  # the key format used before C3
            lookup, patches = self._lookup(cache)
            with patches[0] as esearch, patches[1], patches[2]:
                result = lookup.query_codon(self._Tx(), 237, assembly=build_name)
            with self.subTest(build=build_name):
                esearch.assert_called()  # a fresh lookup, not the stale entry
                self.assertEqual(result["source"], "ncbi_eutils")
                self.assertTrue(all("pos_by_build" in m for m in result["matches"]))

    def test_an_entry_under_the_current_key_but_without_the_schema_marker_is_ignored(self):
        cache = ClinVarCodonCache(max_size=10, ttl_seconds=60)
        cache.put(
            f"codon:v{MATCH_SCHEMA}:GRCh37:NM_000546.6:237",
            {"skipped": False, "found": True, "matches": [], "assembly": "GRCh37"},
        )
        lookup, patches = self._lookup(cache)
        with patches[0] as esearch, patches[1], patches[2]:
            result = lookup.query_codon(self._Tx(), 237, assembly="GRCh37")
        esearch.assert_called()
        self.assertTrue(result["matches"])


# ---------------------------------------------------------------------------------------------------
# 5. indels, C2 record roles
# ---------------------------------------------------------------------------------------------------


class TestIndelAndRecordRolePolicyUnchanged(unittest.TestCase):
    def test_indel_queries_stay_gated_exactly_as_before_in_both_builds(self):
        out = {}
        for build_name, pos in (
            ("GRCh38", 43124027),
            ("GRCh37", 41276044),
        ):  # BRCA1 c.68_69del ACT>A, ClinVar's own coordinates
            for rule in ("evaluate_ps1", "evaluate_pm5"):
                r = getattr(PS1PM5Evaluator(), rule)(None, [{"uid": "x"}], None, pos, "ACT", "A", assembly=build_name)
                self.assertFalse(r.applies)
                self.assertIn("not a single-nucleotide substitution", r.rationale)
                self.assertEqual(r.excluded_records, [])
                out[(build_name, rule)] = r.rationale
        self.assertEqual(out[("GRCh38", "evaluate_ps1")], out[("GRCh37", "evaluate_ps1")])

    def test_context_only_records_are_still_never_anchors(self):
        """C2: haplotype/diplotype/unrecognised records never reach the codon matches, whatever the assembly."""
        case = CASES["CYP2C19_c1004GA"]
        for uid, e in case["entries"].items():
            if e["obj_type"] in ("Diplotype", "Haplotype", "Haplotype, single variant"):
                self.assertIsNone(clinvar_codon_match_from_esummary(uid, e), uid)
        bad = copy.deepcopy(entry(SLC26A2, OWN_SLC26A2))
        bad["obj_type"] = "Brand new record type"
        self.assertIsNone(clinvar_codon_match_from_esummary(OWN_SLC26A2, bad))


# ---------------------------------------------------------------------------------------------------
# 6. GRCh38 behaviour is unchanged: 108 queries x PS1/PM5 against the unmodified code's output
# ---------------------------------------------------------------------------------------------------


class TestGrch38GoldenBehaviourIsPreserved(unittest.TestCase):
    def test_every_golden_row_matches_the_unmodified_code(self):
        self.assertEqual(len(GOLDEN), 108)
        triggered = 0
        for row in GOLDEN:
            codon_result = _base.clinvar_codon_result(row["codon"])
            variant = _base.variant(row["pos"], row["ref"], row["alt"])
            for code, fn in (("PS1", ACMGRuleEngine._ps1), ("PM5", ACMGRuleEngine._pm5)):
                r = fn(
                    variant_dict=variant, transcript_result=_base.transcript_result(), clinvar_codon_result=codon_result
                )
                d = r.details or {}
                now = {
                    "status": r.status,
                    "direction": r.direction,
                    "strength": r.strength,
                    "rationale": r.rationale,
                    "supporting_evidence": r.supporting_evidence,
                    "conflicting_evidence": r.conflicting_evidence,
                    "confidence": r.confidence,
                    "applies": d.get("applies"),
                    "matched": sorted(m["uid"] for m in d.get("matched_anchors", [])),
                    "rejected": sorted(m["uid"] for m in d.get("rejected_anchors", [])),
                    "decision_path": d.get("decision_path"),
                }
                with self.subTest(codon=row["codon"], pos=row["pos"], alt=row["alt"], code=code):
                    self.assertEqual(now, row[code])
                triggered += r.status == "triggered"
        self.assertEqual(triggered, 23)


# ---------------------------------------------------------------------------------------------------
# 7. end to end: no self-citation in supporting evidence, reports or LIMS JSON
# ---------------------------------------------------------------------------------------------------


def variant_level_clinvar(case_key, uid, build_name):
    """The real variant-level ClinVar lookup for the real record `uid` taken as the query, in `build_name`."""
    case = CASES[case_key]
    pos, ref, alt = own_query(case_key, uid)[build_name]
    ids = list(case["entries"])
    client = ClinVarClient()

    def fake(url, params):
        if url.endswith("esearch.fcgi"):
            return {"esearchresult": {"count": str(len(ids)), "idlist": ids}}
        wanted = params["id"].split(",")
        return {"result": {"uids": wanted, **{u: case["entries"][u] for u in wanted}}}

    with (
        mock.patch.object(client, "_request_json", side_effect=fake),
        mock.patch.object(client, "_fetch_submitters", return_value={}),
    ):
        return client.query_variant(
            Variant(chrom=case["chrom"], pos=pos, variant_id=".", ref=ref, alt=alt, qual=None, filter_status=None),
            assembly=build_name,
        )


def full_chain(case_key, uid, build_name, codon_result):
    """Real variant-level ClinVar -> real InterpretationEngine/ACMG engine -> variant_result -> Markdown, PDF text, LIMS JSON."""
    detail, _ = query_detail(case_key, uid)
    pos, ref, alt = own_query(case_key, uid)[build_name]
    variant = {"chrom": CASES[case_key]["chrom"], "pos": pos, "ref": ref, "alt": alt}
    clinvar = json.loads(json.dumps(variant_level_clinvar(case_key, uid, build_name), default=str))
    with mock.patch.object(
        ACMGRuleEngine, "_ps1_pm5_query_context", staticmethod(lambda vd, tr: (None, pos, ref, alt, detail))
    ):
        interp = InterpretationEngine().interpret(
            variant,
            [],
            copy.deepcopy(clinvar),
            {},
            {},
            {},
            clingen_result={
                "gene_resolution_status": "resolved",
                "found": True,
                "gene_symbol": "GENE1",
                "clinical_validity_summary": "Definitive",
            },
            clinvar_codon_result=copy.deepcopy(codon_result),
        )
    vr = build_variant_result(
        variant,
        {},
        {},
        {},
        {},
        {},
        copy.deepcopy(clinvar),
        {},
        interp,
        [],
        clinvar_codon_result=copy.deepcopy(codon_result),
    )
    document = {
        "geper_version": "t",
        "generated_at": "2026-11-25T00:00:00+00:00",
        "input_vcf": "x.vcf",
        "assembly": build_name,
        "vcf_samples": ["S1"],
        "variant_count": 1,
        "variants": [vr],
        "review_status": "reviewed",
        "reviewed_by": "Dr T",
        "reviewed_at": "2026-11-25T00:00:00+00:00",
    }
    markdown = ReportGenerator().generate(document)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "o.pdf")
        generate_pdf(document, path)
        pdf = " ".join((p.extract_text() or "") for p in PdfReader(path).pages).replace("\n", " ")
        jp = os.path.join(tmp, "l.json")
        export_lims.export_lims_json(document, jp, run_id="R1")
        with open(jp, "r", encoding="utf-8") as fh:
            lims = json.load(fh)
    return vr, document, markdown, pdf, lims


def codon_result_for(case_key, codon, build_name, strip_positions=False):
    matches = codon_matches(case_key, codon)
    if strip_positions:
        for m in matches:
            del m["pos_by_build"]
    return {"skipped": False, "found": True, "assembly": build_name, "match_schema": MATCH_SCHEMA, "matches": matches}


def rule(variant_result, code):
    ir = variant_result["interpretation_result"]
    return next(
        r for r in ir["not_triggered_rules"] + ir["triggered_rules"] + ir["not_evaluated_rules"] if r["code"] == code
    )


def ps1_rule(variant_result):
    return rule(variant_result, "PS1")


class TestEndToEndEvidenceTrail(unittest.TestCase):
    def test_grch37_run_has_no_self_citation_and_the_same_classification_as_grch38(self):
        outputs = {
            b: full_chain(SLC26A2, OWN_SLC26A2, b, codon_result_for(SLC26A2, 653, b)) for b in ("GRCh38", "GRCh37")
        }
        for b, (vr, _, markdown, pdf, lims) in outputs.items():
            with self.subTest(build=b):
                ps1 = ps1_rule(vr)
                self.assertEqual(ps1["status"], "not_triggered")
                self.assertNotIn("VCV000004098", " ".join(ps1.get("supporting_evidence") or []))
                self.assertNotIn("VCV000004098", ps1["rationale"])
                self.assertEqual(ps1["details"]["matched_anchors"], [])
                self.assertEqual([e["accession"] for e in ps1["details"]["excluded_records"]], ["VCV000004098"])
                # the variant-level ClinVar block legitimately names the record; PS1 must not be among the triggered criteria
                self.assertNotIn("PS1", [r["code"] for r in vr["interpretation_result"]["triggered_rules"]])
                self.assertNotIn("PS1", json.dumps(lims["findings"][0]["classification"]))
                for text in (markdown, pdf):
                    self.assertNotIn("PS1 applies", text)
        self.assertEqual(
            outputs["GRCh37"][0]["interpretation_result"]["acmg_classification"],
            outputs["GRCh38"][0]["interpretation_result"]["acmg_classification"],
        )
        self.assertEqual(
            outputs["GRCh37"][0]["interpretation_result"]["acmg_net_points"],
            outputs["GRCh38"][0]["interpretation_result"]["acmg_net_points"],
        )

    def test_the_variant_level_uid_alone_also_excludes_the_own_record_when_coordinates_cannot_decide(self):
        # an old-shaped (position-less) match set in a GRCh37 run: coordinates cannot verify anything, the confirmed UID still can
        vr, *_ = full_chain(
            SLC26A2, OWN_SLC26A2, "GRCh37", codon_result_for(SLC26A2, 653, "GRCh37", strip_positions=True)
        )
        reasons = {e["accession"]: e["reason_code"] for e in rule(vr, "PS1")["details"]["excluded_records"]}
        self.assertEqual(reasons, {"VCV000004098": D.REASON_OWN_VARIANT_LEVEL_UID})
        pm5_reasons = {e["accession"]: e["reason_code"] for e in rule(vr, "PM5")["details"]["excluded_records"]}
        self.assertEqual(
            pm5_reasons, {"VCV001994865": D.REASON_UNVERIFIED_LEGACY_SHAPE}
        )  # another record: unverifiable, ineligible

    def test_an_unverified_record_is_explained_in_markdown_pdf_json_and_lims_and_is_not_an_anchor(self):
        vr, document, markdown, pdf, lims = full_chain(
            SLC26A2, OWN_SLC26A2, "GRCh37", codon_result_for(SLC26A2, 653, "GRCh37", strip_positions=True)
        )
        pm5 = rule(
            vr, "PM5"
        )  # the only record that is unverifiable here is the other-amino-acid one (PM5's anchor in GRCh38)
        self.assertEqual(pm5["status"], "not_triggered")
        self.assertEqual(pm5["details"]["matched_anchors"], [])
        marker = "could not be confirmed as different from this variant"
        self.assertTrue(any(marker in c for c in pm5["details"]["unchecked_caveats"]))
        self.assertIn(marker, json.dumps(vr["interpretation_result"]))
        self.assertIn(marker, markdown)
        self.assertIn(marker, pdf)
        self.assertIn(marker, json.dumps(lims["findings"][0]["conflicting_evidence"]))
        self.assertNotIn(
            "VCV001994865 -- NM_000112.4(SLC26A2):c.1957T>G", " ".join(pm5.get("supporting_evidence") or [])
        )
        for text in (markdown, pdf):
            self.assertNotIn("PM5 applies", text)

    def test_the_lims_csv_header_is_unchanged(self):
        from tests.test_clinvar_record_types import _CSV_COLUMNS_ON_MASTER

        self.assertEqual(export_lims._CSV_COLUMNS, _CSV_COLUMNS_ON_MASTER)

    def test_pm5_still_applies_through_the_full_chain_in_both_builds(self):
        for b in ("GRCh38", "GRCh37"):
            vr, *_ = full_chain(SLC26A2, OWN_SLC26A2, b, codon_result_for(SLC26A2, 653, b))
            pm5 = next(r for r in vr["interpretation_result"]["triggered_rules"] if r["code"] == "PM5")
            self.assertEqual([a["uid"] for a in pm5["details"]["matched_anchors"]], ["1994865"])


# ---------------------------------------------------------------------------------------------------
# 8. content hash / sign-off: the new PS1/PM5 evidence is covered by the existing mechanism
# ---------------------------------------------------------------------------------------------------


class TestContentHashCoversTheNewEvidence(unittest.TestCase):
    def test_a_change_in_the_ps1_evidence_trail_changes_both_hash_versions_and_old_documents_still_verify(self):
        vr_ok, document_ok, *_ = full_chain(SLC26A2, OWN_SLC26A2, "GRCh37", codon_result_for(SLC26A2, 653, "GRCh37"))
        vr_unverified, document_unverified, *_ = full_chain(
            SLC26A2, OWN_SLC26A2, "GRCh37", codon_result_for(SLC26A2, 653, "GRCh37", strip_positions=True)
        )
        for version in (1, 2):
            self.assertNotEqual(
                compute_content_hash(document_ok, version),
                compute_content_hash(document_unverified, version),
                f"version {version}",
            )
        # determinism: the same inputs hash identically (nothing per-run leaks into the hashed content)
        again, document_again, *_ = full_chain(SLC26A2, OWN_SLC26A2, "GRCh37", codon_result_for(SLC26A2, 653, "GRCh37"))
        for version in (1, 2):
            self.assertEqual(compute_content_hash(document_ok, version), compute_content_hash(document_again, version))

    def test_a_document_without_any_c3_field_hashes_as_before(self):
        """Legacy compatibility: the C2 frozen legacy documents (digests produced by unmodified master) are untouched."""
        with open(os.path.join(_FIXTURES, "c2_content_hash_legacy_documents.json"), "r", encoding="utf-8") as fh:
            fixture = json.load(fh)
        for name, document in fixture["documents"].items():
            self.assertEqual(compute_content_hash(document), fixture["master_digests"][name], name)


# ---------------------------------------------------------------------------------------------------
# 9. a decision-relevant PS1/PM5 exclusion changes the content hash (both hash versions)
# ---------------------------------------------------------------------------------------------------


def _hash_document(case_key, uid, codon_result):
    """The hashed document of one GRCh37 run; only `codon_result` varies between the two sides of every pair below."""
    return full_chain(case_key, uid, "GRCh37", codon_result)[:2]


def _as_another_variants_record(codon_result, uid):
    """The same real record, but no longer recognisable as the query's own (other UID, position moved): the pre-C3 outcome."""
    out = copy.deepcopy(codon_result)
    for m in out["matches"]:
        if m["uid"] == uid:
            m["uid"] = "9999999"
            m["pos_by_build"]["GRCh37"] += 1000
    return out


class TestDecisionRelevantExclusionsChangeTheContentHash(unittest.TestCase):
    """
    Each pair differs ONLY in the ClinVar codon result: the variant, the real variant-level ClinVar record, the ACMG
    engine and the report chain are identical. Both content-hash versions must tell the two documents apart.
    """

    def assertHashDiffers(self, doc_a, doc_b):
        for version in (1, 2):
            self.assertNotEqual(
                compute_content_hash(doc_a, version), compute_content_hash(doc_b, version), f"hash version {version}"
            )

    def test_excluding_the_own_record_vs_counting_it_as_a_third_anchor_changes_the_hash(self):
        uid = OWN_TP53["c.711G>T"]
        base = codon_result_for(TP53, 237, "GRCh37")
        vr_a, doc_a = _hash_document(TP53, uid, base)
        vr_b, doc_b = _hash_document(TP53, uid, _as_another_variants_record(base, uid))
        self.assertEqual(len(rule(vr_a, "PS1")["details"]["matched_anchors"]), 2)
        self.assertEqual(len(rule(vr_b, "PS1")["details"]["matched_anchors"]), 3)
        self.assertHashDiffers(doc_a, doc_b)  # carried by the hashed supporting evidence (the anchor list)

    def test_excluding_the_own_record_that_decides_ps1_changes_the_hash(self):
        base = codon_result_for(SLC26A2, 653, "GRCh37")
        vr_a, doc_a = _hash_document(SLC26A2, OWN_SLC26A2, base)
        vr_b, doc_b = _hash_document(SLC26A2, OWN_SLC26A2, _as_another_variants_record(base, OWN_SLC26A2))
        self.assertEqual((rule(vr_a, "PS1")["status"], rule(vr_b, "PS1")["status"]), ("not_triggered", "triggered"))
        self.assertHashDiffers(doc_a, doc_b)  # carried by the hashed triggered criteria

    def test_a_ref_mismatch_flag_changes_the_hash_although_the_ps1_status_is_unchanged(self):
        base = codon_result_for(SLC26A2, 653, "GRCh37")
        sides = []
        for ref in (None, "N"):  # same position and ALT; the UID is unknown to the variant-level lookup in both
            side = copy.deepcopy(base)
            for m in side["matches"]:
                if m["uid"] == OWN_SLC26A2:
                    m["uid"] = "9999999"
                    if ref:
                        m["ref"] = ref
            sides.append(_hash_document(SLC26A2, OWN_SLC26A2, side))
        (vr_a, doc_a), (vr_b, doc_b) = sides
        self.assertEqual(rule(vr_a, "PS1")["status"], rule(vr_b, "PS1")["status"])
        self.assertEqual(
            [e["reason_code"] for e in rule(vr_b, "PS1")["details"]["excluded_records"]], [D.REASON_REF_MISMATCH]
        )
        self.assertHashDiffers(doc_a, doc_b)  # carried by the hashed conflicting evidence

    def test_an_unverifiable_record_changes_the_hash_even_when_ps1_stays_triggered(self):
        uid = OWN_TP53["c.711G>T"]
        base = codon_result_for(TP53, 237, "GRCh37")
        extra = copy.deepcopy(base)
        ghost = copy.deepcopy(next(m for m in extra["matches"] if m["uid"] == OWN_TP53["c.711G>A"]))
        ghost.update(uid="8888888", accession="VCV008888888")
        ghost.pop("pos_by_build")  # a same-amino-acid record whose identity cannot be verified
        extra["matches"].append(ghost)
        vr_a, doc_a = _hash_document(TP53, uid, base)
        vr_b, doc_b = _hash_document(TP53, uid, extra)
        self.assertEqual((rule(vr_a, "PS1")["status"], rule(vr_b, "PS1")["status"]), ("triggered", "triggered"))
        self.assertHashDiffers(doc_a, doc_b)  # carried by the hashed conflicting evidence only

    def test_known_limitation_a_not_evaluated_criterions_exclusion_note_is_not_in_the_hash(self):
        """
        CHARACTERISATION of a documented reporting limitation (see PS1_PM5_ASSEMBLY_IDENTITY.md), not a requirement: when the
        PS1/PM5 lookup failed or was incomplete the criterion is `not_evaluated`, and its identity-exclusion note lives only in
        unhashed criterion fields (`conflicting_evidence` is aggregated from triggered/not-triggered rules only). The decision
        ("not evaluated") is the same either way, so no decision is hidden. A separate auditability task may change this; that
        task must update this test together with a content-hash version decision.
        """
        incomplete = codon_result_for(SLC26A2, 653, "GRCh37")
        incomplete["incomplete"] = True
        incomplete["incomplete_reason"] = "synthetic partial lookup"
        incomplete["matches"] = [m for m in incomplete["matches"] if m["uid"] == OWN_SLC26A2]
        with_unverifiable = copy.deepcopy(incomplete)
        ghost = copy.deepcopy(with_unverifiable["matches"][0])
        ghost.update(uid="7777777", accession="VCV007777777")
        ghost.pop("pos_by_build")
        with_unverifiable["matches"].append(ghost)
        vr_a, doc_a = _hash_document(SLC26A2, OWN_SLC26A2, incomplete)
        vr_b, doc_b = _hash_document(SLC26A2, OWN_SLC26A2, with_unverifiable)
        self.assertEqual((rule(vr_a, "PS1")["status"], rule(vr_b, "PS1")["status"]), ("not_evaluated", "not_evaluated"))
        self.assertEqual(len(rule(vr_a, "PS1")["details"]["excluded_records"]), 1)
        self.assertEqual(len(rule(vr_b, "PS1")["details"]["excluded_records"]), 2)  # the note is in the JSON detail ...
        for version in (1, 2):  # ... but not in either hash version
            self.assertEqual(compute_content_hash(doc_a, version), compute_content_hash(doc_b, version))


if __name__ == "__main__":
    unittest.main()
