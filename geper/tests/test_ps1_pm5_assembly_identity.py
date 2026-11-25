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
from utils.exceptions import ExternalAPIError
from report import export_lims
from report.json_builder import build_variant_result
from report.models import compute_content_hash, content_hash_version_of, verify_content_hash
from review import signoff as signoff_module
from review import signoff_status
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
GRCH38_SPELLINGS = ("GRCh38", "hg38", "b38")


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


_UNSET = object()


def evaluate(rule, case_key, uid, build_name, assembly=_UNSET, own_uids=None, matches=None):
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
        assembly=build_name if assembly is _UNSET else assembly,
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
                self.assertIn(
                    "no genome assembly was provided" if value is None else "not a supported build", result["reason"]
                )

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


# approved assembly policy (Phase 0 audit): the complete matrix
APPROVED_GRCH37 = ("GRCh37", "GRCH37", "hg19", "b37")
APPROVED_GRCH38 = ("GRCh38", "hg38", "b38")
REJECTED_SPELLINGS = (
    "37",
    "38",
    "GRCh38.p13",
    "GRCh38.p14",
    "GRCh37.p13",
    "GRCh-37",
    "grch_37",
    "GRCh36",
    "",
    "hg18",
    "foo",
)


class TestApprovedAssemblySpellingMatrix(unittest.TestCase):
    """The approved policy as one matrix: accepted spellings, rejected spellings, and the missing assembly (None)."""

    def test_every_approved_spelling_resolves_to_its_build(self):
        for spelling in APPROVED_GRCH37:
            self.assertEqual(canonical_build(spelling), "GRCh37", spelling)
        for spelling in APPROVED_GRCH38:
            self.assertEqual(canonical_build(spelling), "GRCh38", spelling)

    def test_b38_is_a_grch38_alias_and_nothing_else_was_added(self):
        self.assertEqual(canonical_build("b38"), "GRCh38")
        # the complete alias table, pinned: a new spelling must be added here deliberately
        from pipeline.ps1_pm5.utils import _BUILD_ALIASES

        self.assertEqual(
            _BUILD_ALIASES,
            {
                "grch37": "GRCh37",
                "hg19": "GRCh37",
                "b37": "GRCh37",
                "grch38": "GRCh38",
                "hg38": "GRCh38",
                "b38": "GRCh38",
            },
        )

    def test_every_rejected_spelling_and_every_non_string_is_unresolved(self):
        for value in REJECTED_SPELLINGS + (" ", None, 37, 38.0, ["GRCh38"], b"GRCh38"):
            self.assertIsNone(canonical_build(value), repr(value))

    def test_the_documented_case_and_whitespace_tolerance_is_unchanged(self):
        """Kept from the original C3 implementation; narrowing it to the literal list is an open policy decision."""
        for value, expected in (("grch37", "GRCh37"), ("HG19", "GRCh37"), (" B38 ", "GRCh38"), ("\thg38\n", "GRCh38")):
            self.assertEqual(canonical_build(value), expected, repr(value))

    def test_approved_spellings_run_the_lookup_in_the_right_build_and_rejected_ones_never_reach_ncbi(self):
        for value in APPROVED_GRCH37 + APPROVED_GRCH38 + REJECTED_SPELLINGS + (None,):
            lookup = ClinVarCodonLookup(cache=ClinVarCodonCache(max_size=10, ttl_seconds=60))
            terms = []
            with (
                mock.patch.object(lookup, "_esearch", side_effect=lambda term: terms.append(term) or []),
                mock.patch.object(lookup, "_fetch_submitters", return_value={}),
            ):
                result = lookup.query_codon(_FakeTranscript(), 653, assembly=value)
            with self.subTest(value=value):
                if value in APPROVED_GRCH37:
                    self.assertEqual((result["assembly"], terms), ("GRCh37", ["5[chr] AND 149981550[chrpos37]"]))
                elif value in APPROVED_GRCH38:
                    self.assertEqual((result["assembly"], terms), ("GRCh38", ["5[chr] AND 149981550[chrpos38]"]))
                else:
                    self.assertEqual(terms, [])  # no Entrez request, in particular no silent GRCh38 search
                    self.assertTrue(result["assembly_unresolved"])
                    self.assertIsNone(result["assembly"])
                    self.assertEqual(result["matches"], [])

    def test_ps1_and_pm5_are_not_evaluated_for_every_rejected_spelling_and_for_none(self):
        detail, _ = query_detail(SLC26A2, OWN_SLC26A2)
        pos, ref, alt = own_query(SLC26A2, OWN_SLC26A2)["GRCh38"]
        variant = {"chrom": "5", "pos": pos, "ref": ref, "alt": alt}
        for value in REJECTED_SPELLINGS + (None,):
            lookup = ClinVarCodonLookup(cache=ClinVarCodonCache(max_size=10, ttl_seconds=60))
            with mock.patch.object(lookup, "_esearch") as esearch:
                codon_result = lookup.query_codon(_FakeTranscript(), 653, assembly=value)
            esearch.assert_not_called()
            for rule in (ACMGRuleEngine._ps1, ACMGRuleEngine._pm5):
                with mock.patch.object(
                    ACMGRuleEngine, "_ps1_pm5_query_context", staticmethod(lambda vd, tr: (None, pos, ref, alt, detail))
                ):
                    result = rule(variant_dict=variant, transcript_result={}, clinvar_codon_result=codon_result)
                with self.subTest(value=value, rule=result.code):
                    self.assertEqual(result.status, "not_evaluated")
                    self.assertEqual(result.supporting_evidence, [])

    def test_a_missing_assembly_is_reported_as_missing_and_never_becomes_grch38(self):
        lookup = ClinVarCodonLookup(cache=ClinVarCodonCache(max_size=10, ttl_seconds=60))
        with mock.patch.object(lookup, "_esearch") as esearch:
            missing = lookup.query_codon(_FakeTranscript(), 653, assembly=None)
            unsupported = lookup.query_codon(_FakeTranscript(), 653, assembly="GRCh38.p14")
        esearch.assert_not_called()
        self.assertIsNone(missing["assembly"])
        self.assertIsNone(missing["assembly_input"])
        self.assertIn("no genome assembly was provided", missing["reason"])
        self.assertEqual(unsupported["assembly_input"], "GRCh38.p14")
        self.assertIn("not a supported build", unsupported["reason"])
        detail, _ = query_detail(SLC26A2, OWN_SLC26A2)
        pos, ref, alt = own_query(SLC26A2, OWN_SLC26A2)["GRCh38"]
        with mock.patch.object(
            ACMGRuleEngine, "_ps1_pm5_query_context", staticmethod(lambda vd, tr: (None, pos, ref, alt, detail))
        ):
            result = ACMGRuleEngine._ps1(
                variant_dict={"chrom": "5", "pos": pos, "ref": ref, "alt": alt},
                transcript_result={},
                clinvar_codon_result=missing,
            )
        self.assertEqual(result.status, "not_evaluated")
        self.assertIn("no genome assembly was provided", result.rationale)
        self.assertNotIn("GRCh38", result.rationale)  # the build is never named: nothing was assumed

    def test_the_decision_layer_never_judges_identity_in_an_unresolved_build(self):
        for value in REJECTED_SPELLINGS + (None,):
            ps1 = evaluate("PS1", SLC26A2, OWN_SLC26A2, "GRCh37", assembly=value, own_uids=None)
            with self.subTest(value=value):
                self.assertFalse(ps1.applies)
                self.assertEqual(uids(ps1.matched_anchors), [])
                self.assertTrue(ps1.excluded_records)
                self.assertTrue(
                    all(e["reason_code"] == D.REASON_UNVERIFIED_ASSEMBLY for e in ps1.excluded_records),
                )

    def test_b38_behaves_exactly_like_grch38_in_the_decision_layer(self):
        for rule in ("PS1", "PM5"):
            a = evaluate(rule, TP53, OWN_TP53["c.711G>T"], "GRCh38", assembly="b38")
            b = evaluate(rule, TP53, OWN_TP53["c.711G>T"], "GRCh38", assembly="GRCh38")
            self.assertEqual(a.to_dict(), b.to_dict(), rule)


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


class TestStaleCodonCacheEntriesAreNeverTrusted(unittest.TestCase):
    """
    Cache compatibility (ClinVar audit C3). An entry is served only if it was written by the current code for the
    requested build; anything else is a miss, the lookup is repeated, and no stale record can become an anchor. A
    refetch that fails yields an explicit error (PS1/PM5 not evaluated), never the stale data and never a GRCh38
    fallback. Existing cache files are only ever appended to.
    """

    class _Tx:
        transcript_id = "NM_000546.6"
        chrom = "17"

        def genomic_positions_for_codon(self, codon):
            return [7577570]

    STALE_UID = "STALE000001"

    def _stale_match(self, build_name, with_positions=True):
        match = copy.deepcopy(codon_matches(TP53, 237)[0])
        match.update(uid=self.STALE_UID, accession="VCV" + self.STALE_UID, clinical_significance="Pathogenic")
        match.update(star_rating=3, is_conflicting=False)
        if with_positions:
            match["pos_by_build"] = {build_name: match["pos"]}
        else:
            match.pop("pos_by_build", None)
        return match

    def _entry(self, build_name, **overrides):
        entry = {
            "skipped": False,
            "found": True,
            "source": "ncbi_eutils",
            "transcript_id": "NM_000546.6",
            "codon_number": 237,
            "assembly": build_name,
            "match_schema": MATCH_SCHEMA,
            "matches": [self._stale_match(build_name)],
            "error": None,
            "incomplete": False,
            "incomplete_reason": None,
        }
        entry.update(overrides)
        return entry

    def _fresh_matches(self):
        out = [clinvar_codon_match_from_esummary(u, e) for u, e in CASES[TP53]["entries"].items()]
        return [m for m in out if m is not None and m.codon_number == 237]

    def _query(self, cache, build_name, **patch):
        lookup = ClinVarCodonLookup(cache=cache)
        esearch = patch.get("esearch") or mock.Mock(return_value=["634770"])
        with (
            mock.patch.object(lookup, "_esearch", esearch),
            mock.patch.object(lookup, "_esummary", return_value=self._fresh_matches()),
            mock.patch.object(lookup, "_fetch_submitters", return_value={}),
        ):
            result = lookup.query_codon(self._Tx(), 237, assembly=build_name)
        return result, esearch

    def _assert_refetched_and_stale_unused(self, result, esearch, build_name):
        esearch.assert_called()
        self.assertEqual(result["source"], "ncbi_eutils")
        self.assertEqual(result["assembly"], build_name)
        self.assertEqual(result["match_schema"], MATCH_SCHEMA)
        self.assertNotIn(self.STALE_UID, [m["uid"] for m in result["matches"]])
        self.assertTrue(result["matches"])
        self.assertTrue(all(isinstance(m.get("pos_by_build"), dict) for m in result["matches"]))

    def _key(self, build_name):
        return f"codon:v{MATCH_SCHEMA}:{build_name}:NM_000546.6:237"

    def test_a_valid_entry_for_the_requested_build_is_served_without_a_request(self):
        for build_name in ("GRCh37", "GRCh38"):
            cache = ClinVarCodonCache(max_size=10, ttl_seconds=60)
            cache.put(self._key(build_name), self._entry(build_name))
            result, esearch = self._query(cache, build_name)
            with self.subTest(build=build_name):
                esearch.assert_not_called()
                self.assertEqual(result["source"], "cache")
                self.assertEqual([m["uid"] for m in result["matches"]], [self.STALE_UID])

    def test_an_entry_from_before_c3_without_identity_metadata_is_a_miss(self):
        for build_name, old_key in (("GRCh37", "codon:GRCh37:NM_000546.6:237"), ("GRCh38", "codon:NM_000546.6:237")):
            cache = ClinVarCodonCache(max_size=10, ttl_seconds=60)
            old = {"skipped": False, "found": True, "source": "ncbi_eutils", "matches": [self._stale_match("GRCh38")]}
            for m in old["matches"]:
                m.pop("pos_by_build")
            cache.put(old_key, old)
            # the same stale shape even if it somehow sat under the current key: no schema marker, no assembly
            cache.put(self._key(build_name), dict(old))
            result, esearch = self._query(cache, build_name)
            with self.subTest(build=build_name):
                self._assert_refetched_and_stale_unused(result, esearch, build_name)

    def test_a_grch37_entry_is_never_used_for_a_grch38_request_and_vice_versa(self):
        for requested, other in (("GRCh38", "GRCh37"), ("GRCh37", "GRCh38")):
            # (a) the other build's entry in its own slot: the request's key is simply absent
            cache = ClinVarCodonCache(max_size=10, ttl_seconds=60)
            cache.put(self._key(other), self._entry(other))
            result, esearch = self._query(cache, requested)
            with self.subTest(requested=requested, case="other build's slot"):
                self._assert_refetched_and_stale_unused(result, esearch, requested)
            # (b) the other build's content sitting under THIS request's key (a mislabelled or copied entry)
            cache = ClinVarCodonCache(max_size=10, ttl_seconds=60)
            cache.put(self._key(requested), self._entry(other))
            result, esearch = self._query(cache, requested)
            with self.subTest(requested=requested, case="mislabelled entry"):
                self._assert_refetched_and_stale_unused(result, esearch, requested)

    def test_an_entry_with_another_or_missing_match_schema_is_a_miss(self):
        for bad in (MATCH_SCHEMA - 1, MATCH_SCHEMA + 1, None, "2", str(MATCH_SCHEMA), 2.5, True):
            if bad == MATCH_SCHEMA:  # 2 == 2.0 would pass; not a case here
                continue
            cache = ClinVarCodonCache(max_size=10, ttl_seconds=60)
            cache.put(self._key("GRCh37"), self._entry("GRCh37", match_schema=bad))
            result, esearch = self._query(cache, "GRCh37")
            with self.subTest(match_schema=bad):
                self._assert_refetched_and_stale_unused(result, esearch, "GRCh37")
        cache = ClinVarCodonCache(max_size=10, ttl_seconds=60)
        entry = self._entry("GRCh37")
        del entry["match_schema"]
        cache.put(self._key("GRCh37"), entry)
        result, esearch = self._query(cache, "GRCh37")
        self._assert_refetched_and_stale_unused(result, esearch, "GRCh37")

    def test_an_entry_with_missing_or_incomplete_pos_by_build_is_a_miss(self):
        bad_match_sets = {
            "key absent": [self._stale_match("GRCh37", with_positions=False)],
            "None": [dict(self._stale_match("GRCh37"), pos_by_build=None)],
            "list": [dict(self._stale_match("GRCh37"), pos_by_build=[7577570])],
            "one record lacks it": [self._stale_match("GRCh37"), self._stale_match("GRCh37", with_positions=False)],
            "match not a dict": [self._stale_match("GRCh37"), "VCV000000001"],
            "matches None": None,
            "matches a dict": {"uid": "x"},
        }
        for label, matches in bad_match_sets.items():
            cache = ClinVarCodonCache(max_size=10, ttl_seconds=60)
            cache.put(self._key("GRCh37"), self._entry("GRCh37", matches=matches))
            result, esearch = self._query(cache, "GRCh37")
            with self.subTest(case=label):
                self._assert_refetched_and_stale_unused(result, esearch, "GRCh37")

    def test_an_entry_with_empty_but_present_pos_by_build_is_valid_and_judged_unverified_not_trusted(self):
        """A real record ClinVar placed in no build has `pos_by_build == {}`; that is a legitimate stored shape."""
        cache = ClinVarCodonCache(max_size=10, ttl_seconds=60)
        match = self._stale_match("GRCh37")
        match["pos_by_build"] = {}
        cache.put(self._key("GRCh37"), self._entry("GRCh37", matches=[match]))
        result, esearch = self._query(cache, "GRCh37")
        esearch.assert_not_called()
        self.assertEqual(result["source"], "cache")
        verdict = identify_record(result["matches"][0], "GRCh37", 7577570, "C", "A")
        self.assertEqual(verdict.verdict, D.IDENTITY_UNVERIFIED)  # never an anchor

    def test_an_unusable_entry_never_yields_an_anchor_even_when_the_refetch_fails(self):
        for label, entry in (
            ("old shape", {"skipped": False, "found": True, "matches": [self._stale_match("GRCh37", False)]}),
            ("other build", self._entry("GRCh38")),
            ("other schema", self._entry("GRCh37", match_schema=1)),
        ):
            cache = ClinVarCodonCache(max_size=10, ttl_seconds=60)
            cache.put(self._key("GRCh37"), entry)
            failing = mock.Mock(side_effect=ExternalAPIError("simulated outage"))
            result, esearch = self._query(cache, "GRCh37", esearch=failing)
            with self.subTest(case=label):
                esearch.assert_called()
                self.assertEqual(result["matches"], [])
                self.assertIn("simulated outage", result["error"])
                self.assertEqual(result["assembly"], "GRCh37")  # never silently GRCh38
                detail, _ = query_detail(TP53, OWN_TP53["c.711G>T"])
                pos, ref, alt = own_query(TP53, OWN_TP53["c.711G>T"])["GRCh37"]
                for rule in (ACMGRuleEngine._ps1, ACMGRuleEngine._pm5):
                    with mock.patch.object(
                        ACMGRuleEngine,
                        "_ps1_pm5_query_context",
                        staticmethod(lambda vd, tr: (None, pos, ref, alt, detail)),
                    ):
                        outcome = rule(
                            variant_dict={"chrom": "17", "pos": pos, "ref": ref, "alt": alt},
                            transcript_result={},
                            clinvar_codon_result=result,
                        )
                    self.assertEqual(outcome.status, "not_evaluated")
                    self.assertNotIn(self.STALE_UID, " ".join(outcome.supporting_evidence))

    def test_an_unresolved_assembly_never_reads_the_cache_at_all(self):
        cache = ClinVarCodonCache(max_size=10, ttl_seconds=60)
        for build_name in ("GRCh37", "GRCh38"):
            cache.put(self._key(build_name), self._entry(build_name))
        for value in (None, "", "37", "GRCh38.p14"):
            hits_before = cache.stats()["hits"]
            result, esearch = self._query(cache, value)
            with self.subTest(value=value):
                esearch.assert_not_called()
                self.assertTrue(result["assembly_unresolved"])
                self.assertEqual(result["matches"], [])
                self.assertEqual(cache.stats()["hits"], hits_before)

    def test_existing_cache_files_are_never_rewritten_or_deleted_only_appended_to(self):
        old_line = json.dumps({"key": "codon:GRCh37:NM_000546.6:237", "value": {"found": True, "matches": []}})
        mislabelled = json.dumps({"key": self._key("GRCh37"), "value": self._entry("GRCh38")})
        no_positions = json.dumps(
            {
                "key": "codon:v2:GRCh38:NM_000546.6:237",
                "value": self._entry("GRCh38", matches=[self._stale_match("GRCh38", False)]),
            }
        )
        original = ("\n".join([old_line, mislabelled, "not json at all", no_positions]) + "\n").encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ps1_pm5_cache.jsonl")
            with open(path, "wb") as fh:
                fh.write(original)
            before_mtime = os.stat(path).st_mtime_ns
            for build_name in ("GRCh37", "GRCh38"):
                cache = ClinVarCodonCache(max_size=10, ttl_seconds=60, disk_path=path)
                result, esearch = self._query(cache, build_name)
                self._assert_refetched_and_stale_unused(result, esearch, build_name)
            with open(path, "rb") as fh:
                after = fh.read()
            self.assertTrue(after.startswith(original))  # every original byte is still there, unchanged
            appended = [json.loads(line) for line in after[len(original) :].decode("utf-8").splitlines() if line]
            self.assertTrue(appended)
            self.assertTrue(all(a["value"]["match_schema"] == MATCH_SCHEMA for a in appended))
            self.assertGreaterEqual(os.stat(path).st_mtime_ns, before_mtime)
            # a second process reading the file now finds the fresh entries and serves them without a request
            cache = ClinVarCodonCache(max_size=10, ttl_seconds=60, disk_path=path)
            again, esearch = self._query(cache, "GRCh37")
            esearch.assert_not_called()
            self.assertEqual(again["source"], "cache")
            self.assertNotIn(self.STALE_UID, [m["uid"] for m in again["matches"]])


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


# ---------------------------------------------------------------------------------------------------
# 10. sign-off: what a signed report's content hash does and does not detect for PS1/PM5 evidence
# ---------------------------------------------------------------------------------------------------

with open(os.path.join(_FIXTURES, "c3_signed_documents_ps1_pm5.json"), "r", encoding="utf-8") as _fh:
    SIGNED = json.load(_fh)
with open(os.path.join(_FIXTURES, "c2_content_hash_legacy_documents.json"), "r", encoding="utf-8") as _fh:
    LEGACY_DOCS = json.load(_fh)

# SHA-256 of the two frozen fixtures: they hold the historical digests, and editing either would silently redefine
# what "an existing signed report still verifies" means. A deliberate fixture change must update these.
FIXTURE_SHA256 = {
    "c2_content_hash_legacy_documents.json": "4d9095d8037397a606177b45ada7da85751f24add512cb864d13f5f1f1ca0129",
    "c3_signed_documents_ps1_pm5.json": "049bd648613d05f5dc84e6aa3ef6bcb4670f87b74fd1f042dfc3ea680f702c78",
}


def _signed(document, version):
    """A document sealed the way `review/signoff.py::approve()` seals it (version 2 declares itself; version 1 does not)."""
    out = copy.deepcopy(document)
    if version == 2:
        out["content_hash_version"] = 2
    out["content_hash"] = compute_content_hash(out, version)
    return out


def _clinical_report(document):
    """The object `report/models.py` reads the evidence trail from (candidate_interpretation, else clinical_report)."""
    variant = document["variants"][0]
    return variant.get("candidate_interpretation") or variant.get("clinical_report")


def _tp53_codon(extra=None, drop=(), edit=None):
    base = codon_result_for(TP53, 237, "GRCh37")
    base["matches"] = [m for m in base["matches"] if m["uid"] not in drop] + list(extra or [])
    for m in base["matches"]:
        if edit and m["uid"] in edit:
            m.update(edit[m["uid"]])
    return base


class TestSignedReportHashAndPs1Pm5Evidence(unittest.TestCase):
    OWN = OWN_TP53["c.711G>T"]

    def doc(self, codon_result, case=TP53, uid=None):
        return full_chain(case, uid or self.OWN, "GRCh37", codon_result)[:2]

    def assertSealsDiffer(self, doc_a, doc_b, msg=""):
        for version in (1, 2):
            a, b = _signed(doc_a, version), _signed(doc_b, version)
            self.assertNotEqual(a["content_hash"], b["content_hash"], f"{msg} (hash version {version})")
            # a report signed on one document does not verify against the other's content
            self.assertTrue(verify_content_hash(a, a["content_hash"]))
            self.assertFalse(verify_content_hash({**b, "content_hash": a["content_hash"]}, a["content_hash"]))

    def _extra_record(self, uid, accession, **fields):
        """A same-amino-acid TP53 codon-237 record, copied from a real one, at the real G>A position (another allele)."""
        record = copy.deepcopy(next(m for m in codon_matches(TP53, 237) if m["uid"] == OWN_TP53["c.711G>A"]))
        record.update(uid=uid, accession=accession, **fields)
        return record

    # ---- 1. supporting anchor ------------------------------------------------------------------------------------

    def test_changing_a_supporting_anchor_changes_the_hash(self):
        _, base = self.doc(_tp53_codon())
        anchors = [m["uid"] for m in codon_matches(TP53, 237) if m["uid"] != self.OWN]
        self.assertEqual(len(anchors), 2)
        variants = {
            "an anchor is removed": _tp53_codon(drop=(anchors[0],)),
            "an anchor's accession differs": _tp53_codon(edit={anchors[0]: {"accession": "VCV000999999"}}),
            "an anchor's classification differs": _tp53_codon(
                edit={anchors[0]: {"clinical_significance": "Likely pathogenic"}}
            ),
            "an anchor's review status differs": _tp53_codon(
                edit={anchors[0]: {"review_status": "no assertion criteria provided"}}
            ),
            "a further anchor is added": _tp53_codon(extra=[self._extra_record("5555551", "VCV005555551")]),
        }
        for label, codon in variants.items():
            _, other = self.doc(codon)
            self.assertNotEqual(
                _clinical_report(base)["supporting_evidence"], _clinical_report(other)["supporting_evidence"], label
            )
            self.assertSealsDiffer(base, other, label)

    def test_editing_the_supporting_evidence_of_a_signed_report_breaks_its_seal(self):
        _, document = self.doc(_tp53_codon())
        for version in (1, 2):
            signed = _signed(document, version)
            self.assertTrue(verify_content_hash(signed, signed["content_hash"]))
            tampered = copy.deepcopy(signed)
            evidence = _clinical_report(tampered)["supporting_evidence"]
            index = next(i for i, e in enumerate(evidence) if "VCV001757269" in e)
            evidence[index] = evidence[index].replace("VCV001757269", "VCV001757270")
            self.assertFalse(verify_content_hash(tampered, signed["content_hash"]), f"hash version {version}")
            tampered["review_status"] = "reviewed"
            with self.assertRaises(signoff_module.SignoffError):
                signoff_module.require_reviewed(tampered, "test consumer")

    # ---- 2. conflicting anchor -----------------------------------------------------------------------------------

    def test_changing_a_conflicting_anchor_changes_the_hash(self):
        low = lambda uid: self._extra_record(uid, "VCV00" + uid, star_rating=0)  # noqa: E731  seen but below the bar
        _, none_rejected = self.doc(_tp53_codon())
        _, one_rejected = self.doc(_tp53_codon(extra=[low("5555552")]))
        _, two_rejected = self.doc(_tp53_codon(extra=[low("5555552"), low("5555553")]))
        self.assertEqual(_clinical_report(none_rejected)["conflicting_evidence"], [])
        self.assertEqual(len(_clinical_report(one_rejected)["conflicting_evidence"]), 1)
        self.assertNotEqual(
            _clinical_report(one_rejected)["conflicting_evidence"],
            _clinical_report(two_rejected)["conflicting_evidence"],
        )
        self.assertSealsDiffer(none_rejected, one_rejected, "a rejected record appears")
        self.assertSealsDiffer(one_rejected, two_rejected, "the number of rejected records changes")
        # an anchor turning conflicting moves from supporting to conflicting evidence
        anchor = next(m["uid"] for m in codon_matches(TP53, 237) if m["uid"] != self.OWN)
        _, turned = self.doc(_tp53_codon(edit={anchor: {"is_conflicting": True}}))
        self.assertSealsDiffer(none_rejected, turned, "an anchor becomes conflicting")

    # ---- 3. triggered criterion ----------------------------------------------------------------------------------

    def test_changing_the_triggered_criterion_changes_the_hash_and_editing_it_breaks_the_seal(self):
        _, triggered = self.doc(_tp53_codon())  # PS1 triggered
        _, not_triggered = self.doc(_tp53_codon(drop=tuple(m["uid"] for m in codon_matches(TP53, 237))))
        self.assertEqual(
            [c["code"] for c in _clinical_report(triggered)["acmg_classification"]["triggered_criteria"]], ["PS1"]
        )
        self.assertSealsDiffer(triggered, not_triggered, "PS1 triggered vs not triggered")
        for version in (1, 2):
            signed = _signed(triggered, version)
            for field, replacement in (("code", "PM5"), ("strength", "supporting"), ("direction", "benign")):
                tampered = copy.deepcopy(signed)
                _clinical_report(tampered)["acmg_classification"]["triggered_criteria"][0][field] = replacement
                self.assertFalse(verify_content_hash(tampered, signed["content_hash"]), f"{field} v{version}")
            removed = copy.deepcopy(signed)
            _clinical_report(removed)["acmg_classification"]["triggered_criteria"] = []
            self.assertFalse(verify_content_hash(removed, signed["content_hash"]), f"removed v{version}")

    # ---- 4/5. excluded records -----------------------------------------------------------------------------------

    def _ghost(self, codon_result, uid, accession, *, strip_positions=False, ref=None):
        """A same-position, same-ALT copy of the query's own record, under another UID (so the UID safeguard cannot apply)."""
        record = copy.deepcopy(next(m for m in codon_result["matches"] if m["uid"] == OWN_SLC26A2))
        record.update(uid=uid, accession=accession)
        if strip_positions:
            record.pop("pos_by_build")
        if ref:
            record["ref"] = ref
        codon_result["matches"].append(record)

    def test_changing_an_excluded_records_accession_changes_the_hash(self):
        base = codon_result_for(SLC26A2, 653, "GRCh37")
        a, b = copy.deepcopy(base), copy.deepcopy(base)
        self._ghost(a, "7777771", "VCV007777771", strip_positions=True)
        self._ghost(b, "7777772", "VCV007777772", strip_positions=True)
        vr_a, doc_a = full_chain(SLC26A2, OWN_SLC26A2, "GRCh37", a)[:2]
        vr_b, doc_b = full_chain(SLC26A2, OWN_SLC26A2, "GRCh37", b)[:2]
        for vr in (vr_a, vr_b):
            self.assertEqual(rule(vr, "PS1")["status"], "not_triggered")
        self.assertEqual([e["accession"] for e in rule(vr_a, "PS1")["details"]["excluded_records"]][-1], "VCV007777771")
        self.assertEqual([e["accession"] for e in rule(vr_b, "PS1")["details"]["excluded_records"]][-1], "VCV007777772")
        self.assertSealsDiffer(doc_a, doc_b, "the unverifiable record has another accession")

    def test_changing_an_exclusion_from_identity_unverified_to_ref_mismatch_is_detected(self):
        base = codon_result_for(SLC26A2, 653, "GRCh37")
        unverified, mismatch = copy.deepcopy(base), copy.deepcopy(base)
        self._ghost(unverified, "7777773", "VCV007777773", strip_positions=True)  # no per-build positions: unverifiable
        self._ghost(mismatch, "7777773", "VCV007777773", ref="N")  # same position and ALT, other REF
        vr_u, doc_u = full_chain(SLC26A2, OWN_SLC26A2, "GRCh37", unverified)[:2]
        vr_m, doc_m = full_chain(SLC26A2, OWN_SLC26A2, "GRCh37", mismatch)[:2]
        reason = lambda vr: [e["reason_code"] for e in rule(vr, "PS1")["details"]["excluded_records"]][-1]  # noqa: E731
        self.assertEqual(reason(vr_u), D.REASON_UNVERIFIED_LEGACY_SHAPE)
        self.assertEqual(reason(vr_m), D.REASON_REF_MISMATCH)
        self.assertEqual(rule(vr_u, "PS1")["status"], rule(vr_m, "PS1")["status"])  # the PS1 status itself is the same
        self.assertSealsDiffer(doc_u, doc_m, "unverified -> ref_mismatch")

    def test_an_exclusion_that_changes_no_decision_and_no_reviewer_text_is_not_a_clinical_change(self):
        """Own-record exclusion by UID vs by coordinates: the same record, the same evidence, only the internal reason differs."""
        _, doc_a = full_chain(SLC26A2, OWN_SLC26A2, "GRCh37", codon_result_for(SLC26A2, 653, "GRCh37"))[:2]
        by_uid = copy.deepcopy(doc_a)
        by_coordinates = copy.deepcopy(doc_a)
        for document, reason in ((by_uid, D.REASON_OWN_VARIANT_LEVEL_UID), (by_coordinates, D.REASON_OWN_COORDINATES)):
            ps1 = next(
                r for r in document["variants"][0]["interpretation_result"]["not_triggered_rules"] if r["code"] == "PS1"
            )
            ps1["details"]["excluded_records"][0]["reason_code"] = reason
        for version in (1, 2):
            self.assertEqual(compute_content_hash(by_uid, version), compute_content_hash(by_coordinates, version))

    # ---- what the hash does NOT see (documented, characterised) --------------------------------------------------

    def test_known_limitation_structured_exclusion_details_alone_are_not_in_the_hash(self):
        """
        CHARACTERISATION, not a requirement. The hash reads the evidence trail a reviewer is shown (triggered criteria,
        supporting/conflicting evidence) -- the decision-relevant consequence of every exclusion is in it (tests above).
        The structured `details` of a criterion (`excluded_records`, `caveats_checked`, `unchecked_caveats`) are NOT read
        by `report/models.py::_extract_clinical_content`, so editing them alone, or swapping one rejected record for
        another of the same count, leaves a signed report's seal intact. Closing this needs a new hash version
        (documented in PS1_PM5_ASSEMBLY_IDENTITY.md); when that is approved this test must change with it.
        """
        base = codon_result_for(SLC26A2, 653, "GRCh37")
        self._ghost(base, "7777774", "VCV007777774", strip_positions=True)
        vr, document = full_chain(SLC26A2, OWN_SLC26A2, "GRCh37", base)[:2]
        for version in (1, 2):
            signed = _signed(document, version)
            tampered = copy.deepcopy(signed)
            ps1 = next(
                r for r in tampered["variants"][0]["interpretation_result"]["not_triggered_rules"] if r["code"] == "PS1"
            )
            for entry in ps1["details"]["excluded_records"]:
                entry.update(accession="VCV000000001", uid="1", reason_code="anything", verdict="own", title="other")
            ps1["details"]["caveats_checked"] = ["edited"]
            ps1["details"]["unchecked_caveats"] = ["edited"]
            self.assertTrue(verify_content_hash(tampered, signed["content_hash"]), f"v{version}")
        # two different rejected records, same count: identical hash
        low_a, low_b = copy.deepcopy(_tp53_codon()), copy.deepcopy(_tp53_codon())
        low_a["matches"].append(self._extra_record("5555561", "VCV005555561", star_rating=0, title="record A"))
        low_b["matches"].append(self._extra_record("5555562", "VCV005555562", star_rating=0, title="record B"))
        (_, doc_a), (_, doc_b) = self.doc(low_a), self.doc(low_b)
        for version in (1, 2):
            self.assertEqual(compute_content_hash(doc_a, version), compute_content_hash(doc_b, version))

    # ---- 6. determinism -------------------------------------------------------------------------------------------

    def test_hashes_are_deterministic(self):
        _, first = self.doc(_tp53_codon())
        _, second = self.doc(_tp53_codon())
        shuffled = json.loads(json.dumps(first, sort_keys=True))  # same content, other key order
        later = copy.deepcopy(first)
        later.update(generated_at="2027-01-01T00:00:00+00:00", input_vcf="/elsewhere.vcf", reviewed_by="Someone Else")
        for version in (1, 2):
            digest = compute_content_hash(first, version)
            self.assertEqual(digest, compute_content_hash(second, version))
            self.assertEqual(digest, compute_content_hash(shuffled, version))
            self.assertEqual(digest, compute_content_hash(later, version))  # run metadata is not clinical content
            self.assertEqual(digest, compute_content_hash(first, version))
            self.assertEqual(len(digest), 64)
        self.assertNotEqual(compute_content_hash(first, 1), compute_content_hash(first, 2))  # versions never collide

    # ---- 7/8. historical digests and signed reports ---------------------------------------------------------------

    def test_the_two_fixtures_are_byte_for_byte_what_they_were(self):
        import hashlib

        for name, expected in FIXTURE_SHA256.items():
            with open(os.path.join(_FIXTURES, name), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), expected, name)

    def test_existing_version_1_and_version_2_digests_still_verify(self):
        # (a) the three C2 legacy documents, v1 as produced by unmodified master and v2 as produced by C2's code
        for name, digests in SIGNED["legacy_fixture_digests_v1_v2"].items():
            document = LEGACY_DOCS["documents"][name]
            self.assertEqual(compute_content_hash(document, 1), LEGACY_DOCS["master_digests"][name], name)
            self.assertEqual(compute_content_hash(document, 1), digests["1"], name)
            self.assertEqual(compute_content_hash(document, 2), digests["2"], name)
        # (b) PS1/PM5-bearing documents "signed" under each version by the C2 hash code (a copy of the git blob)
        for name, entry in SIGNED["signed_documents"].items():
            document = entry["document"]
            v1 = {**copy.deepcopy(document), "content_hash": entry["content_hash_v1"]}
            v2 = {**copy.deepcopy(document), "content_hash_version": 2, "content_hash": entry["content_hash_v2"]}
            with self.subTest(document=name):
                self.assertEqual(content_hash_version_of(v1), 1)  # declares nothing: the legacy version
                self.assertEqual(content_hash_version_of(v2), 2)
                self.assertTrue(verify_content_hash(v1, entry["content_hash_v1"]))
                self.assertTrue(verify_content_hash(v2, entry["content_hash_v2"]))
                self.assertEqual(compute_content_hash(v1), entry["content_hash_v1"])
                self.assertEqual(compute_content_hash(v2), entry["content_hash_v2"])
                self.assertNotEqual(entry["content_hash_v1"], entry["content_hash_v2"])
                # a seal never verifies under the other version's rules
                self.assertFalse(verify_content_hash({**v1, "content_hash_version": 2}, entry["content_hash_v1"]))
                signoff_module.require_reviewed({**v1, "review_status": "reviewed"}, "test consumer")
                signoff_module.require_reviewed({**v2, "review_status": "reviewed"}, "test consumer")

    def test_the_c3_code_still_produces_the_documents_that_were_signed(self):
        """Rebuilding the PS1/PM5 documents with the current code gives the same hashed content as the frozen signed ones."""
        for name, case, uid, codon, build, strip in (
            ("tp53_grch37_ps1_triggered_own_excluded", TP53, OWN_TP53["c.711G>T"], 237, "GRCh37", False),
            ("slc26a2_grch37_ps1_not_triggered_own_excluded_pm5_triggered", SLC26A2, OWN_SLC26A2, 653, "GRCh37", False),
            ("slc26a2_grch37_identity_unverified_conflicting_evidence", SLC26A2, OWN_SLC26A2, 653, "GRCh37", True),
            ("slc26a2_grch38_ps1_not_triggered_own_excluded_pm5_triggered", SLC26A2, OWN_SLC26A2, 653, "GRCh38", False),
        ):
            _, rebuilt = full_chain(case, uid, build, codon_result_for(case, codon, build, strip_positions=strip))[:2]
            entry = SIGNED["signed_documents"][name]
            with self.subTest(document=name):
                self.assertEqual(compute_content_hash(rebuilt, 1), entry["content_hash_v1"])
                self.assertEqual(compute_content_hash(rebuilt, 2), entry["content_hash_v2"])

    def test_verifying_a_signed_report_never_rewrites_it_and_the_status_check_reads_it_as_consistent(self):
        entry = SIGNED["signed_documents"]["tp53_grch37_ps1_triggered_own_excluded"]
        for version, field in ((1, "content_hash_v1"), (2, "content_hash_v2")):
            document = {**copy.deepcopy(entry["document"]), "content_hash": entry[field], "review_status": "reviewed"}
            if version == 2:
                document["content_hash_version"] = 2
            before = json.dumps(document, sort_keys=True)
            self.assertTrue(verify_content_hash(document, document["content_hash"]))
            signoff_module.require_reviewed(document, "test consumer")
            with tempfile.TemporaryDirectory() as tmp:
                with open(os.path.join(tmp, signoff_module.RESULTS_FILENAME), "w", encoding="utf-8") as fh:
                    json.dump(document, fh)
                self.assertEqual(signoff_status.inspect_local(tmp).document_hash, "self_consistent")
            self.assertEqual(json.dumps(document, sort_keys=True), before, f"version {version}")
            self.assertEqual(document.get("content_hash_version"), 2 if version == 2 else None)

    def test_an_unknown_declared_version_never_verifies(self):
        entry = SIGNED["signed_documents"]["tp53_grch37_ps1_triggered_own_excluded"]
        for bad in (0, 3, "2", 2.0, True):
            document = {**copy.deepcopy(entry["document"]), "content_hash_version": bad}
            self.assertFalse(verify_content_hash(document, entry["content_hash_v2"]), repr(bad))


if __name__ == "__main__":
    unittest.main()
