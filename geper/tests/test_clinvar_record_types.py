"""
Regression tests for ClinVar audit C2: haplotype, diplotype and other multi-variant ClinVar records
were attributed to single variants.

Approved evidence policy (see `utils/clinvar_records.py` and `geper/CLINVAR_RECORD_TYPES.md`):

* only an explicitly recognised single-allele record with exactly one component, an allele match and
  an individual-variant classification is eligible evidence;
* haplotype, star-allele, diplotype, genotype, compound-heterozygote, phase-unknown and any
  unrecognised record type are context only, labelled, preserved, and never scored;
* "no classification for the single variant" is not a classification;
* several eligible records that disagree are all kept and none is chosen.

`fixtures/c2_clinvar_record_types.json` freezes REAL ClinVar candidate sets (esearch ids plus esummary
entries slimmed to the fields GEPER reads) for ABCB11, NBAS, SLC26A2, CYP2C19 (three positions), TP53
M237I, BRCA1 c.1233T>G, BRCA1 c.68_69del and a BRCA1 record with conflicting submitters.
`fixtures/c2_legacy_clinvar_results.json` holds results shaped as stored before the policy, with the
outputs the UNMODIFIED code (origin/master b99ca97) produced for them. Cases the live data does not offer
-- a haplotype-only match, unknown record types, conflicting eligible records -- are DERIVED from those real
records and say so in their names.
"""

import copy
import json
import os
import random
import tempfile
import unittest
from unittest import mock

from pypdf import PdfReader

from database.clinvar_client import ClinVarClient, ClinVarMatchStatus
from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.confidence_engine import ConfidenceEngine
from pipeline.conflict_resolution_engine import ConflictResolutionEngine
from pipeline.interpretation import InterpretationEngine
from pipeline.prioritization_engine import PrioritizationEngine
from pipeline.ps1_pm5.models import star_rating
from pipeline.ps1_pm5.utils import clinvar_codon_match_from_esummary
from pipeline.vcf_parser import Variant
from report import export_lims
from report.clinical_report_builder import _clinical_evidence, build_clinical_report
from report.models import compute_content_hash
from report.report_generator import ReportGenerator
from report.summary import generate_pdf
from utils.clinvar_records import (
    EVIDENCE_POLICY_VERSION,
    MULTI_VARIANT_TYPES,
    ROLE_MULTI_VARIANT_RECORD,
    ROLE_STAR_ALLELE_RECORD,
    ROLE_UNRECOGNIZED_RECORD_TYPE,
    ROLE_VARIANT_RECORD,
    ROLE_VARIANT_RECORD_UNCLASSIFIED,
    SINGLE_ALLELE_TYPES,
    STAR_ALLELE_TYPES,
    classify_record_role,
    classifications_conflict,
    has_individual_classification,
    is_eligible,
    is_single_allele_record,
    order_records,
    review_rank,
)
from tests.test_disclaimer_consistency import _ir

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
with open(os.path.join(_FIXTURES, "c2_clinvar_record_types.json"), "r", encoding="utf-8") as _fh:
    CASES = {c["key"]: c for c in json.load(_fh)["cases"]}
with open(os.path.join(_FIXTURES, "c2_legacy_clinvar_results.json"), "r", encoding="utf-8") as _fh:
    LEGACY = json.load(_fh)

# The LIMS CSV header as it was on master before this change. It is a fixed contract and must not change.
_CSV_COLUMNS_ON_MASTER = [
    "sample_id",
    "run_id",
    "finding_number",
    "chromosome",
    "position",
    "reference_allele",
    "alternate_allele",
    "variant_type",
    "genomic_hgvs",
    "coding_hgvs",
    "protein_hgvs",
    "protein_hgvs_source",
    "gene_symbol",
    "acmg_classification",
    "triggered_acmg_criteria",
    "not_evaluated_criteria_count",
    "confidence_score",
    "confidence_label",
    "confidence_pending",
    "priority_score",
    "priority_category",
    "priority_rank",
    "priority_pending",
    "case_rank",
    "case_rank_score",
    "phenotype_match_score",
    "case_prioritization_tier",
    "gnomad_global_af",
    "gnomad_found",
    "dbsnp_rsid",
    "clinvar_significance",
    "clinvar_review_status",
    "clingen_gene_validity",
    "evidence_sources",
    "stage_errors",
    "interpretation_available",
    "run_caveats",
    "variant_allele_fraction",
    "variant_allele_fraction_status",
    "reference_assembly",
]


def _variant(case):
    return Variant(
        chrom=case["chrom"],
        pos=case["pos"],
        variant_id=".",
        ref=case["ref"],
        alt=case["alt"],
        qual=None,
        filter_status=None,
    )


def run_client(case, entries=None, uids=None):
    """The REAL `ClinVarClient.query_variant` against a frozen candidate set; returns (result, submitter-lookup uid lists)."""
    entries = copy.deepcopy(entries if entries is not None else case["entries"])
    ids = list(uids if uids is not None else entries.keys())
    submitter_calls = []
    client = ClinVarClient()

    def fake_request(url, params):
        if url.endswith("esearch.fcgi"):
            return {"esearchresult": {"count": str(len(ids)), "idlist": ids}}
        wanted = params["id"].split(",")
        return {"result": {"uids": wanted, **{u: entries[u] for u in wanted}}}

    def fake_submitters(uids_):
        submitter_calls.append(list(uids_))
        return {}

    with (
        mock.patch.object(client, "_request_json", side_effect=fake_request),
        mock.patch.object(client, "_fetch_submitters", side_effect=fake_submitters),
    ):
        return client.query_variant(_variant(case), assembly=case["assembly"]), submitter_calls


def by_accession(case):
    return {e["accession"]: e for e in case["entries"].values()}


def uid_of(case, accession):
    return by_accession(case)[accession]["uid"]


def without(case, *accessions):
    """The case's entries minus the named records (DERIVED cases)."""
    drop = {uid_of(case, a) for a in accessions}
    return {u: e for u, e in case["entries"].items() if u not in drop}


def only_context(case, keep_accessions):
    """The case's entries with every ELIGIBLE record removed (what ClinVar would hold without the individual record)."""
    result, _ = run_client(case)
    eligible = {r["accession"] for r in result["matched_records"]}
    return without(case, *[a for a in eligible if a not in keep_accessions])


def accessions(records):
    return [r["accession"] for r in records]


# ---------------------------------------------------------------------------------------------------
# 1. The pure record-role function
# ---------------------------------------------------------------------------------------------------


class TestRecordRole(unittest.TestCase):
    def test_every_recognised_single_allele_type_with_one_component_and_a_classification_is_a_variant_record(self):
        for record_type in sorted(SINGLE_ALLELE_TYPES):
            with self.subTest(record_type=record_type):
                self.assertEqual(classify_record_role(record_type, 1, "Pathogenic"), ROLE_VARIANT_RECORD)

    def test_multi_variant_types_are_multi_variant_records_whatever_their_component_count(self):
        for record_type in sorted(MULTI_VARIANT_TYPES):
            for components in (0, 1, 2, 3):
                with self.subTest(record_type=record_type, components=components):
                    self.assertEqual(
                        classify_record_role(record_type, components, "Pathogenic"), ROLE_MULTI_VARIANT_RECORD
                    )

    def test_a_diplotype_with_one_component_is_still_multi_variant(self):
        # CYP2C19*23/*23: ClinVar lists a single component for a homozygous genotype.
        self.assertEqual(classify_record_role("Diplotype", 1, "drug response"), ROLE_MULTI_VARIANT_RECORD)

    def test_star_allele_record_is_its_own_role_and_never_eligible(self):
        for record_type in sorted(STAR_ALLELE_TYPES):
            self.assertEqual(classify_record_role(record_type, 1, "drug response"), ROLE_STAR_ALLELE_RECORD)
        self.assertFalse(is_eligible(ROLE_STAR_ALLELE_RECORD, True))

    def test_unknown_missing_or_generic_record_types_default_to_context_only(self):
        for record_type in (
            None,
            "",
            "Variation",
            "Complex",
            "copy number gain",
            "SomeFutureNcbiType",
            "Haplotype ",
            "snv",
        ):
            with self.subTest(record_type=record_type):
                role = classify_record_role(record_type, 1, "Pathogenic")
                self.assertEqual(role, ROLE_UNRECOGNIZED_RECORD_TYPE)
                self.assertFalse(is_eligible(role, True))

    def test_a_single_allele_type_needs_exactly_one_component(self):
        for components in (None, 0, 2, 5):
            with self.subTest(components=components):
                self.assertEqual(
                    classify_record_role("single nucleotide variant", components, "Pathogenic"),
                    ROLE_UNRECOGNIZED_RECORD_TYPE,
                )

    def test_missing_or_non_classifications_make_the_record_unclassified_not_eligible(self):
        for text in (
            None,
            "",
            "   ",
            "no classification for the single variant",
            "No Classification For The Single Variant ",
        ):
            with self.subTest(text=text):
                self.assertFalse(has_individual_classification(text))
                role = classify_record_role("single nucleotide variant", 1, text)
                self.assertEqual(role, ROLE_VARIANT_RECORD_UNCLASSIFIED)
                self.assertFalse(is_eligible(role, True))

    def test_real_classifications_count(self):
        for text in (
            "Pathogenic",
            "Likely benign",
            "Uncertain significance",
            "Conflicting classifications of pathogenicity",
        ):
            self.assertTrue(has_individual_classification(text))

    def test_eligibility_also_needs_an_allele_match(self):
        self.assertTrue(is_eligible(ROLE_VARIANT_RECORD, True))
        self.assertFalse(is_eligible(ROLE_VARIANT_RECORD, False))
        self.assertFalse(is_eligible(ROLE_VARIANT_RECORD, None))

    def test_single_allele_guard_used_by_ps1_pm5_ignores_the_classification(self):
        self.assertTrue(is_single_allele_record("single nucleotide variant", 1))
        self.assertFalse(is_single_allele_record("Diplotype", 1))
        self.assertFalse(is_single_allele_record(None, 1))
        self.assertFalse(is_single_allele_record("single nucleotide variant", 2))

    def test_review_rank_agrees_with_the_ps1_pm5_star_rating_for_recognised_statuses(self):
        for status in (
            "practice guideline",
            "reviewed by expert panel",
            "criteria provided, multiple submitters, no conflicts",
            "criteria provided, single submitter",
            "criteria provided, conflicting classifications",
            "no assertion criteria provided",
            "no classification for the single variant",
            None,
        ):
            with self.subTest(status=status):
                self.assertEqual(review_rank(status), star_rating(status))


# ---------------------------------------------------------------------------------------------------
# 2. Real frozen cases through the real client
# ---------------------------------------------------------------------------------------------------


class TestAbcb11(unittest.TestCase):
    """c.2636T>G is VUS in its own record; a NEWER haplotype containing it is Likely pathogenic."""

    case = CASES["ABCB11_c2636TG"]

    def test_the_haplotype_no_longer_replaces_the_individual_record(self):
        result, _ = run_client(self.case)
        self.assertEqual(result["match_status"], "matched")
        self.assertEqual(result["primary_record"]["accession"], "VCV003776848")
        self.assertEqual(result["primary_record"]["clinical_significance"], "Uncertain significance")
        self.assertEqual(accessions(result["matched_records"]), ["VCV003776848"])
        self.assertEqual(result["matched_record_count"], 1)

    def test_the_haplotype_is_preserved_as_labelled_context(self):
        result, _ = run_client(self.case)
        self.assertEqual(accessions(result["context_records"]), ["VCV004846108"])
        haplotype = result["context_records"][0]
        self.assertEqual(haplotype["record_type"], "Haplotype")
        self.assertEqual(haplotype["component_count"], 2)
        self.assertEqual(haplotype["record_role"], ROLE_MULTI_VARIANT_RECORD)
        self.assertFalse(haplotype["eligible_for_variant_classification"])
        self.assertTrue(haplotype["variant_match"])  # "contains this allele" keeps its meaning
        self.assertEqual(haplotype["clinical_significance"], "Likely pathogenic")  # preserved, not scored
        self.assertEqual(result["context_record_count"], 1)

    def test_the_policy_version_and_conflict_flags(self):
        result, _ = run_client(self.case)
        self.assertEqual(result["evidence_policy"], EVIDENCE_POLICY_VERSION)
        self.assertFalse(result["individual_records_conflict"])
        self.assertFalse(result["multiple_eligible_records"])

    def test_only_the_individual_record_gets_a_submitter_lookup(self):
        _, calls = run_client(self.case)
        self.assertEqual(calls, [[uid_of(self.case, "VCV003776848")]])

    def test_the_unrelated_co_located_variant_stays_in_records_only(self):
        result, _ = run_client(self.case)
        self.assertEqual(result["record_count"], 3)
        self.assertNotIn("VCV002687802", accessions(result["matched_records"] + result["context_records"]))


class TestAbcb11VariantWithNoClassificationOfItsOwn(unittest.TestCase):
    """The variant's own record says 'no classification for the single variant'; a haplotype says Likely pathogenic."""

    case = CASES["ABCB11_no_own_classification"]

    def test_the_unclassified_variant_is_not_reported_as_likely_pathogenic(self):
        result, _ = run_client(self.case)
        self.assertIsNone(result["primary_record"])
        self.assertEqual(result["matched_records"], [])
        self.assertEqual(result["match_status"], "position_only")
        self.assertFalse(result["found"])

    def test_both_records_are_kept_as_labelled_context(self):
        result, _ = run_client(self.case)
        roles = {r["accession"]: r["record_role"] for r in result["context_records"]}
        self.assertEqual(roles["VCV004847041"], ROLE_VARIANT_RECORD_UNCLASSIFIED)
        self.assertEqual(roles["VCV004846108"], ROLE_MULTI_VARIANT_RECORD)

    def test_no_classification_is_not_read_as_a_pathogenicity_class(self):
        result, _ = run_client(self.case)
        own = next(r for r in result["context_records"] if r["accession"] == "VCV004847041")
        self.assertEqual(own["clinical_significance"], "no classification for the single variant")
        for text in ("benign", "pathogenic"):
            self.assertNotEqual(ConflictResolutionEngine._classification_tier(own["clinical_significance"]), text)


class TestNbas(unittest.TestCase):
    case = CASES["NBAS_c619CT"]

    def test_the_individual_record_is_cited_not_the_haplotype(self):
        result, _ = run_client(self.case)
        self.assertEqual(result["primary_record"]["accession"], "VCV002059937")
        self.assertEqual(accessions(result["context_records"]), ["VCV004851488"])
        self.assertEqual(result["context_records"][0]["record_type"], "Haplotype")


class TestSlc26a2(unittest.TestCase):
    case = CASES["SLC26A2_c1957TA"]

    def test_individual_record_wins_by_policy_not_by_date(self):
        result, _ = run_client(self.case)
        self.assertEqual(result["primary_record"]["accession"], "VCV000004098")
        self.assertEqual(accessions(result["context_records"]), ["VCV004823938"])

    def test_the_outcome_does_not_depend_on_which_record_is_newer(self):
        entries = copy.deepcopy(self.case["entries"])
        own = next(e for e in entries.values() if e["accession"] == "VCV000004098")
        own["germline_classification"]["last_evaluated"] = "2001/01/01 00:00"  # now OLDER than the haplotype
        result, _ = run_client(self.case, entries)
        self.assertEqual(result["primary_record"]["accession"], "VCV000004098")


class TestCyp2c19C1004GA(unittest.TestCase):
    """35 of the 36 records containing c.1004G>A are a haplotype or diplotype; the variant's own record has no classification."""

    case = CASES["CYP2C19_c1004GA"]

    def test_no_haplotype_or_diplotype_becomes_the_primary_record(self):
        result, _ = run_client(self.case)
        self.assertIsNone(result["primary_record"])
        self.assertEqual(result["matched_records"], [])
        self.assertEqual(result["match_status"], "position_only")

    def test_all_36_records_are_kept_as_typed_context(self):
        result, _ = run_client(self.case)
        self.assertEqual(result["context_record_count"], 36)
        by_type = {}
        for r in result["context_records"]:
            by_type[r["record_type"]] = by_type.get(r["record_type"], 0) + 1
        self.assertEqual(by_type, {"Diplotype": 34, "Haplotype": 1, "single nucleotide variant": 1})

    def test_no_drug_response_record_is_presented_as_a_classification(self):
        result, _ = run_client(self.case)
        self.assertNotIn("drug response", [(r or {}).get("clinical_significance") for r in [result["primary_record"]]])

    def test_no_submitter_lookup_for_context_records(self):
        _, calls = run_client(self.case)
        self.assertEqual(
            calls, []
        )  # no eligible record -> `_fetch_submitters` is never even asked about context records


class TestCyp2c19Star23(unittest.TestCase):
    case = CASES["CYP2C19_star23"]

    def test_star_allele_and_diplotypes_are_context_only(self):
        result, _ = run_client(self.case)
        self.assertIsNone(result["primary_record"])
        self.assertEqual(result["match_status"], "position_only")
        roles = {r["record_role"] for r in result["context_records"]}
        self.assertEqual(roles, {ROLE_MULTI_VARIANT_RECORD, ROLE_STAR_ALLELE_RECORD})
        star = [r for r in result["context_records"] if r["record_role"] == ROLE_STAR_ALLELE_RECORD]
        self.assertEqual([r["record_type"] for r in star], ["Haplotype, single variant"])
        self.assertEqual(star[0]["title"], "CYP2C19*23")

    def test_the_one_component_diplotype_is_not_mistaken_for_a_single_variant(self):
        result, _ = run_client(self.case)
        homozygous = next(r for r in result["context_records"] if r["title"] == "CYP2C19*23/*23")
        self.assertEqual(homozygous["component_count"], 1)
        self.assertEqual(homozygous["record_type"], "Diplotype")
        self.assertFalse(homozygous["eligible_for_variant_classification"])


class TestDiplotypeOnly(unittest.TestCase):
    case = CASES["CYP2C19_diplotype_only"]

    def test_real_diplotype_only_match_yields_no_individual_evidence(self):
        result, _ = run_client(self.case)
        self.assertIsNone(result["primary_record"])
        self.assertEqual(result["matched_records"], [])
        self.assertEqual(result["match_status"], "position_only")
        self.assertGreater(result["context_record_count"], 0)
        self.assertTrue(all(not r["eligible_for_variant_classification"] for r in result["records"]))


class TestValidSingleVariantRecords(unittest.TestCase):
    def test_tp53_m237i_pathogenic_likely_pathogenic(self):
        result, _ = run_client(CASES["TP53_M237I"])
        self.assertEqual(result["primary_record"]["accession"], "VCV000634770")
        self.assertEqual(result["primary_record"]["clinical_significance"], "Pathogenic/Likely pathogenic")
        self.assertEqual(result["context_record_count"], 0)
        self.assertEqual(result["match_status"], "matched")

    def test_brca1_c1233tg_expert_panel_benign(self):
        result, _ = run_client(CASES["BRCA1_c1233TG"])
        self.assertEqual(result["primary_record"]["accession"], "VCV000041804")
        self.assertEqual(result["primary_record"]["clinical_significance"], "Benign")
        self.assertEqual(result["primary_record"]["review_status"], "reviewed by expert panel")
        self.assertEqual(result["context_record_count"], 0)

    def test_brca1_c68_69del_expert_panel_pathogenic(self):
        result, _ = run_client(CASES["BRCA1_c68_69del"])
        self.assertEqual(result["primary_record"]["accession"], "VCV000017662")
        self.assertEqual(result["primary_record"]["record_type"], "Microsatellite")
        self.assertEqual(result["primary_record"]["clinical_significance"], "Pathogenic")
        self.assertEqual(result["record_count"], 20)
        self.assertEqual(result["matched_record_count"], 1)

    def test_a_real_record_with_conflicting_submitters_stays_one_eligible_record(self):
        case = CASES["BRCA1_conflicting_submissions"]
        result, _ = run_client(case)
        self.assertEqual(result["match_status"], "matched")
        self.assertTrue(result["primary_record"]["clinical_significance"].startswith("Conflicting"))
        self.assertFalse(result["individual_records_conflict"])  # ClinVar's own aggregate label is one record

    def test_every_real_case_that_matches_cites_a_record_that_is_eligible(self):
        for key, case in CASES.items():
            with self.subTest(case=key):
                result, _ = run_client(case)
                primary = result["primary_record"]
                if primary is not None:
                    self.assertTrue(primary["eligible_for_variant_classification"])
                    self.assertEqual(primary["record_role"], ROLE_VARIANT_RECORD)
                    self.assertIn(primary["record_type"], SINGLE_ALLELE_TYPES)
                    self.assertEqual(primary["component_count"], 1)
                for record in result["matched_records"]:
                    self.assertTrue(record["eligible_for_variant_classification"])
                for record in result["context_records"]:
                    self.assertFalse(record["eligible_for_variant_classification"])


# ---------------------------------------------------------------------------------------------------
# 3. Derived cases (stated as derived): haplotype-only, mixed, conflicting, unknown types, missing classification
# ---------------------------------------------------------------------------------------------------


class TestDerivedCases(unittest.TestCase):
    abcb11 = CASES["ABCB11_c2636TG"]
    tp53 = CASES["TP53_M237I"]

    def test_haplotype_only_derived_from_abcb11_by_removing_the_individual_record(self):
        entries = without(self.abcb11, "VCV003776848")
        result, calls = run_client(self.abcb11, entries)
        self.assertIsNone(result["primary_record"])
        self.assertEqual(result["match_status"], "position_only")
        self.assertEqual(accessions(result["context_records"]), ["VCV004846108"])
        self.assertEqual(calls, [])

    def test_diplotype_only_derived_from_cyp2c19_by_removing_everything_but_diplotypes(self):
        case = CASES["CYP2C19_c1004GA"]
        entries = {u: e for u, e in case["entries"].items() if e["obj_type"] == "Diplotype"}
        result, _ = run_client(case, entries)
        self.assertIsNone(result["primary_record"])
        self.assertEqual(result["context_record_count"], 34)

    def test_mixed_individual_haplotype_and_diplotype_records(self):
        case = CASES["CYP2C19_c1004GA"]
        entries = copy.deepcopy(case["entries"])
        own = next(e for e in entries.values() if e["accession"] == "VCV000634859")
        own["germline_classification"]["description"] = "Likely benign"  # DERIVED: give the own record a classification
        result, _ = run_client(case, entries)
        self.assertEqual(result["primary_record"]["accession"], "VCV000634859")
        self.assertEqual(result["primary_record"]["clinical_significance"], "Likely benign")
        self.assertEqual(result["context_record_count"], 35)
        self.assertEqual({r["record_type"] for r in result["context_records"]}, {"Diplotype", "Haplotype"})

    def _clone(self, case, accession, new_accession, **changes):
        entries = copy.deepcopy(case["entries"])
        src = next(e for e in entries.values() if e["accession"] == accession)
        clone = copy.deepcopy(src)
        clone["uid"] = "99" + src["uid"]
        clone["accession"] = new_accession
        for key, value in changes.items():
            if key in ("description", "review_status", "last_evaluated"):
                clone["germline_classification"][key] = value
            else:
                clone[key] = value
        entries[clone["uid"]] = clone
        return entries

    def test_conflicting_eligible_records_are_all_kept_and_none_is_chosen(self):
        entries = self._clone(
            self.tp53, "VCV000634770", "VCV9999999", description="Likely benign", last_evaluated="2030/01/01 00:00"
        )
        result, calls = run_client(self.tp53, entries)
        self.assertEqual(result["match_status"], "matched")
        self.assertTrue(result["individual_records_conflict"])
        self.assertIsNone(result["primary_record"])  # the NEWEST record (2030) must not win
        self.assertEqual(set(accessions(result["matched_records"])), {"VCV000634770", "VCV9999999"})
        self.assertTrue(result["multiple_eligible_records"])
        self.assertEqual(len(calls[0]), 2)

    def test_agreeing_eligible_records_are_ordered_by_review_status_then_date_not_by_date_alone(self):
        entries = self._clone(
            self.tp53,
            "VCV000634770",
            "VCV9999998",
            review_status="criteria provided, single submitter",
            last_evaluated="2030/01/01 00:00",  # newer but weaker
        )
        result, _ = run_client(self.tp53, entries)
        self.assertFalse(result["individual_records_conflict"])
        self.assertEqual(result["primary_record"]["accession"], "VCV000634770")  # multiple submitters outranks single
        self.assertEqual(accessions(result["matched_records"]), ["VCV000634770", "VCV9999998"])

    def test_display_order_is_deterministic_whatever_the_input_order(self):
        base = self._clone(self.tp53, "VCV000634770", "VCV9999997", review_status="criteria provided, single submitter")
        orders = []
        for seed in range(6):
            keys = list(base)
            random.Random(seed).shuffle(keys)
            result, _ = run_client(self.tp53, base, uids=keys)
            orders.append(accessions(result["matched_records"]))
        self.assertEqual(len({tuple(o) for o in orders}), 1)

    def test_unknown_record_types_default_to_context_only(self):
        for obj_type in ("Variation", "SomeFutureNcbiType", "Complex", "", None):
            with self.subTest(obj_type=obj_type):
                entries = copy.deepcopy(self.tp53["entries"])
                own = next(e for e in entries.values() if e["accession"] == "VCV000634770")
                if obj_type is None:
                    own.pop("obj_type")
                else:
                    own["obj_type"] = obj_type
                result, _ = run_client(self.tp53, entries)
                self.assertIsNone(result["primary_record"])
                self.assertEqual(result["match_status"], "position_only")
                self.assertIn("VCV000634770", accessions(result["context_records"]))
                self.assertEqual(
                    next(r for r in result["context_records"] if r["accession"] == "VCV000634770")["record_role"],
                    ROLE_UNRECOGNIZED_RECORD_TYPE,
                )

    def test_a_single_allele_type_with_two_components_is_context_only(self):
        entries = copy.deepcopy(self.tp53["entries"])
        own = next(e for e in entries.values() if e["accession"] == "VCV000634770")
        own["variation_set"].append(copy.deepcopy(own["variation_set"][0]))
        result, _ = run_client(self.tp53, entries)
        self.assertIsNone(result["primary_record"])
        self.assertEqual(result["context_records"][0]["component_count"], 2)

    def test_missing_individual_classifications_are_never_eligible(self):
        for description in (None, "", "no classification for the single variant"):
            with self.subTest(description=description):
                entries = copy.deepcopy(self.tp53["entries"])
                own = next(e for e in entries.values() if e["accession"] == "VCV000634770")
                own["germline_classification"]["description"] = description
                result, _ = run_client(self.tp53, entries)
                self.assertIsNone(result["primary_record"])
                self.assertEqual(result["match_status"], "position_only")
                self.assertEqual(
                    next(r for r in result["context_records"] if r["accession"] == "VCV000634770")["record_role"],
                    ROLE_VARIANT_RECORD_UNCLASSIFIED,
                )

    def test_no_valid_single_variant_match_at_all_is_not_found_or_position_only(self):
        result, _ = run_client(self.tp53, without(self.tp53, "VCV000634770"))
        self.assertIsNone(result["primary_record"])
        self.assertEqual(result["match_status"], "position_only")
        self.assertEqual(result["context_record_count"], 0)
        empty, _ = run_client(self.tp53, {}, uids=[])
        self.assertEqual(empty["match_status"], ClinVarMatchStatus.NOT_FOUND.value)
        self.assertEqual(empty["evidence_policy"], EVIDENCE_POLICY_VERSION)
        self.assertEqual(empty["context_records"], [])
        self.assertIsNone(empty["primary_record"])

    def test_classifications_conflict_helper(self):
        self.assertFalse(
            classifications_conflict(
                [{"clinical_significance": "Pathogenic"}, {"clinical_significance": "pathogenic "}]
            )
        )
        self.assertTrue(
            classifications_conflict([{"clinical_significance": "Pathogenic"}, {"clinical_significance": "Benign"}])
        )
        self.assertEqual([r["accession"] for r in order_records([{"accession": "B"}, {"accession": "A"}])], ["A", "B"])


class TestInvariantOnRandomMixes(unittest.TestCase):
    def test_primary_is_always_an_eligible_record_or_none(self):
        rng = random.Random(20261124)
        base = CASES["TP53_M237I"]
        template = next(e for e in base["entries"].values() if e["accession"] == "VCV000634770")
        types = sorted(SINGLE_ALLELE_TYPES | MULTI_VARIANT_TYPES | STAR_ALLELE_TYPES) + ["Variation", "Weird", None]
        classes = [
            "Pathogenic",
            "Likely pathogenic",
            "Benign",
            "Uncertain significance",
            "drug response",
            "no classification for the single variant",
            "",
            None,
        ]
        statuses = [
            "practice guideline",
            "reviewed by expert panel",
            "criteria provided, single submitter",
            "no assertion criteria provided",
            None,
        ]
        for trial in range(120):
            entries = {}
            for i in range(rng.randint(1, 6)):
                e = copy.deepcopy(template)
                e["uid"] = str(5000000 + i)
                e["accession"] = f"VCV{5000000 + i:09d}"
                if (t := rng.choice(types)) is None:
                    e.pop("obj_type")
                else:
                    e["obj_type"] = t
                e["germline_classification"].update(
                    {
                        "description": rng.choice(classes),
                        "review_status": rng.choice(statuses),
                        "last_evaluated": f"20{rng.randint(10, 29)}/01/01 00:00",
                    }
                )
                if rng.random() < 0.2:
                    e["variation_set"].append(copy.deepcopy(e["variation_set"][0]))
                entries[e["uid"]] = e
            result, _ = run_client(base, entries)
            primary = result["primary_record"]
            for r in result["matched_records"]:
                self.assertEqual(r["record_role"], ROLE_VARIANT_RECORD)
                self.assertIn(r["record_type"], SINGLE_ALLELE_TYPES)
                self.assertEqual(r["component_count"], 1)
                self.assertTrue(has_individual_classification(r["clinical_significance"]))
            for r in result["context_records"]:
                self.assertFalse(r["eligible_for_variant_classification"])
            if primary is not None:
                self.assertIn(primary["accession"], accessions(result["matched_records"]))
                self.assertFalse(result["individual_records_conflict"])
            else:
                self.assertTrue(not result["matched_records"] or result["individual_records_conflict"])
            self.assertEqual(result["match_status"] == "matched", bool(result["matched_records"]))
            self.assertEqual(result["found"], result["match_status"] == "matched")


# ---------------------------------------------------------------------------------------------------
# 4. Context never influences ACMG, confidence, priority, BP6 or conflict scoring
# ---------------------------------------------------------------------------------------------------

_INTERPRET_KEYS = (
    "acmg_classification",
    "confidence_score",
    "priority_score",
    "priority_category",
    "conflict_severity",
    "conflict_score",
    "interpretation_outcome",
)


def _consumer_snapshot(clinvar_result):
    interp = InterpretationEngine().interpret(
        {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
        [],
        clinvar_result,
        {},
        {},
        {},
        clingen_result={"gene_resolution_status": "resolved"},
    )["interpretation_result"]
    crossref = ACMGRuleEngine._clinvar_crossref(clinvar_result)
    return {
        "interpretation": {k: interp.get(k) for k in _INTERPRET_KEYS},
        "priority_factor": PrioritizationEngine._clinical_factor(clinvar_result, None, 1.0, []).value,
        "confidence_quality": ConfidenceEngine._clinical_quality(clinvar_result, None, 1.0).quality,
        "bp6_status": ACMGRuleEngine._bp6(clinvar_result).status,
        "crossref": (crossref.get("available"), crossref.get("clinical_significance"), crossref.get("review_status")),
        "expert_panel_conflict": bool(
            ConflictResolutionEngine._expert_panel_disagreement_conflict("Uncertain Significance", clinvar_result)
        ),
        "curated_conflict": bool(
            ConflictResolutionEngine._curated_clinvar_disagreement_conflict("Uncertain Significance", clinvar_result)
        ),
        "clinical_conflict": bool(
            ConflictResolutionEngine._clinical_conflict(
                clinvar_result, {"found": True, "clinical_validity_summary": "Disputed", "gene_symbol": "G"}
            )
        ),
        "population_conflict": bool(
            ConflictResolutionEngine._population_conflict(
                "Uncertain Significance", clinvar_result, {"found": True, "global_af": 0.02}
            )
        ),
    }


class TestContextRecordsNeverInfluenceDownstreamEvidence(unittest.TestCase):
    def _assert_same_as_without_context(self, key, keep=()):
        case = CASES[key]
        full, _ = run_client(case)
        stripped_entries = {
            u: e
            for u, e in case["entries"].items()
            if full_roles(full).get(e["accession"]) != "context" or e["accession"] in keep
        }
        stripped, _ = run_client(case, stripped_entries)
        self.assertGreater(full["context_record_count"], 0)
        self.assertEqual(stripped["context_record_count"], 0 if not keep else stripped["context_record_count"])
        self.assertEqual(_consumer_snapshot(full), _consumer_snapshot(stripped))

    def test_abcb11_haplotype_changes_nothing(self):
        self._assert_same_as_without_context("ABCB11_c2636TG")

    def test_nbas_haplotype_changes_nothing(self):
        self._assert_same_as_without_context("NBAS_c619CT")

    def test_slc26a2_haplotype_changes_nothing(self):
        self._assert_same_as_without_context("SLC26A2_c1957TA")

    def test_cyp2c19_diplotypes_and_haplotype_change_nothing(self):
        case = CASES["CYP2C19_c1004GA"]
        full, _ = run_client(case)
        nothing = {}
        stripped, _ = run_client(case, nothing, uids=[])
        # With context records present, the variant must behave exactly like a variant ClinVar has nothing usable for
        # (apart from the wording that names the context): same scores, same conflicts, same classification.
        snap_full, snap_none = (
            _consumer_snapshot(full),
            _consumer_snapshot(
                {
                    "match_status": "position_only",
                    "found": False,
                    "records": [],
                    "matched_records": [],
                    "primary_record": None,
                    "record_count": 0,
                }
            ),
        )
        for key in (
            "interpretation",
            "priority_factor",
            "confidence_quality",
            "bp6_status",
            "expert_panel_conflict",
            "curated_conflict",
            "clinical_conflict",
            "population_conflict",
        ):
            self.assertEqual(snap_full[key], snap_none[key], key)

    def test_the_old_behaviour_would_have_differed(self):
        # Guards the guard: attributing the ABCB11 haplotype as primary (what master did) changes the scores.
        case = CASES["ABCB11_c2636TG"]
        result, _ = run_client(case)
        as_master = dict(result, primary_record=next(r for r in result["context_records"]))
        self.assertNotEqual(
            PrioritizationEngine._clinical_factor(as_master, None, 1.0, []).value,
            PrioritizationEngine._clinical_factor(result, None, 1.0, []).value,
        )
        self.assertTrue(
            ConflictResolutionEngine._curated_clinvar_disagreement_conflict("Uncertain Significance", as_master)
        )
        self.assertFalse(
            ConflictResolutionEngine._curated_clinvar_disagreement_conflict("Uncertain Significance", result)
        )

    def test_conflicting_eligible_records_feed_nothing(self):
        case = CASES["TP53_M237I"]
        entries = TestDerivedCases()._clone(
            case, "VCV000634770", "VCV9999999", description="Benign", last_evaluated="2030/01/01 00:00"
        )
        result, _ = run_client(case, entries)
        self.assertIsNone(result["primary_record"])
        snap = _consumer_snapshot(result)
        none = _consumer_snapshot(
            {
                "match_status": "position_only",
                "found": False,
                "records": [],
                "matched_records": [],
                "primary_record": None,
                "record_count": 0,
            }
        )
        for key in (
            "interpretation",
            "priority_factor",
            "confidence_quality",
            "bp6_status",
            "curated_conflict",
            "expert_panel_conflict",
        ):
            self.assertEqual(snap[key], none[key], key)
        self.assertIn("conflicting classifications", ACMGRuleEngine._clinvar_not_found_reason(result))


def full_roles(result):
    roles = {r["accession"]: "eligible" for r in result["matched_records"]}
    roles.update({r["accession"]: "context" for r in result["context_records"]})
    return roles


# ---------------------------------------------------------------------------------------------------
# 5. PS1/PM5 record-type guard
# ---------------------------------------------------------------------------------------------------


class TestPs1Pm5RecordTypeGuard(unittest.TestCase):
    def _entry_with_parseable_protein_change(self, obj_type, components=1):
        tp53 = next(e for e in CASES["TP53_M237I"]["entries"].values() if e["accession"] == "VCV000634770")
        entry = copy.deepcopy(tp53)
        entry["obj_type"] = obj_type
        entry["variation_set"] = [copy.deepcopy(entry["variation_set"][0]) for _ in range(components)]
        return entry

    def test_an_individual_variant_record_still_builds_a_codon_match(self):
        entry = self._entry_with_parseable_protein_change("single nucleotide variant")
        match = clinvar_codon_match_from_esummary("634770", entry)
        self.assertIsNotNone(match)
        self.assertEqual((match.ref_aa, match.alt_aa, match.codon_number), ("M", "I", 237))

    def test_a_multi_variant_or_star_allele_record_never_builds_a_match_even_with_a_parseable_title(self):
        for obj_type in sorted(MULTI_VARIANT_TYPES | STAR_ALLELE_TYPES):
            for components in (1, 2):
                with self.subTest(obj_type=obj_type, components=components):
                    entry = self._entry_with_parseable_protein_change(obj_type, components)
                    self.assertIsNone(clinvar_codon_match_from_esummary("1", entry))

    def test_unknown_or_missing_record_type_never_builds_a_match(self):
        for obj_type in ("Variation", "Weird", "", None):
            with self.subTest(obj_type=obj_type):
                entry = self._entry_with_parseable_protein_change("single nucleotide variant")
                if obj_type is None:
                    entry.pop("obj_type")
                else:
                    entry["obj_type"] = obj_type
                self.assertIsNone(clinvar_codon_match_from_esummary("1", entry))

    def test_a_single_allele_type_with_two_components_never_builds_a_match(self):
        self.assertIsNone(
            clinvar_codon_match_from_esummary(
                "1", self._entry_with_parseable_protein_change("single nucleotide variant", 2)
            )
        )

    def test_no_real_context_record_becomes_a_codon_anchor(self):
        for key in ("ABCB11_c2636TG", "CYP2C19_c1004GA", "CYP2C19_star23", "NBAS_c619CT", "SLC26A2_c1957TA"):
            case = CASES[key]
            result, _ = run_client(case)
            for record in result["context_records"]:
                entry = case["entries"][record["uid"]]
                with self.subTest(case=key, accession=record["accession"]):
                    if record["record_role"] != ROLE_VARIANT_RECORD_UNCLASSIFIED:
                        self.assertIsNone(clinvar_codon_match_from_esummary(record["uid"], entry))


# ---------------------------------------------------------------------------------------------------
# 6. Reviewer-facing output: Markdown, PDF, JSON, LIMS
# ---------------------------------------------------------------------------------------------------


def _markdown(result):
    return "\n".join(ReportGenerator._render_clinvar(result))


class TestMarkdown(unittest.TestCase):
    def test_abcb11_labels_only_the_individual_record_as_this_variant(self):
        text = _markdown(run_client(CASES["ABCB11_c2636TG"])[0])
        this_variant = [line for line in text.splitlines() if "**this variant**" in line]
        self.assertEqual(len(this_variant), 1)
        self.assertIn("Uncertain significance", this_variant[0])
        self.assertIn("Context only", text)
        self.assertIn("Haplotype", text)
        self.assertIn("Likely pathogenic", text)  # preserved, but only in the context block
        self.assertNotIn("Likely pathogenic", this_variant[0])

    def test_cyp2c19_has_no_this_variant_line_and_summarises_the_context_by_type(self):
        text = _markdown(run_client(CASES["CYP2C19_c1004GA"])[0])
        self.assertNotIn("**this variant**", text)
        self.assertIn("No individual-variant ClinVar record with a classification matches this exact variant", text)
        self.assertIn("Diplotype ×34", text)
        self.assertIn("Context only", text)
        self.assertLess(len([line for line in text.splitlines() if line.startswith("- ")]), 12)  # not 36 lines

    def test_star_allele_record_is_labelled_as_such(self):
        text = _markdown(run_client(CASES["CYP2C19_star23"])[0])
        self.assertIn("Haplotype, single variant", text)
        self.assertNotIn("**this variant**", text)

    def test_conflicting_eligible_records_are_all_shown_and_none_is_selected(self):
        case = CASES["TP53_M237I"]
        entries = TestDerivedCases()._clone(case, "VCV000634770", "VCV9999999", description="Benign")
        text = _markdown(run_client(case, entries)[0])
        self.assertIn("conflicting classifications", text)
        self.assertEqual(
            len([line for line in text.splitlines() if "this variant" in line and line.startswith("- ")]), 2
        )

    def test_clinical_evidence_summary_wording(self):
        for key, expected in (
            ("CYP2C19_c1004GA", "context (not used for classification)"),
            ("ABCB11_c2636TG", "context (not used for classification)"),
        ):
            evidence = _clinical_evidence({"clinvar": run_client(CASES[key])[0], "clingen": {}})
            self.assertTrue(evidence["clinvar_context_total"] > 0)
            self.assertEqual(evidence["clinvar_evidence_policy"], EVIDENCE_POLICY_VERSION)
        evidence = _clinical_evidence({"clinvar": run_client(CASES["CYP2C19_c1004GA"])[0], "clingen": {}})
        self.assertFalse(evidence["clinvar_available"])
        self.assertEqual(evidence["clinvar_context_by_type"]["Diplotype"], 34)
        self.assertLessEqual(len(evidence["clinvar_context_records"]), 5)
        self.assertEqual(evidence["clinvar_co_located_count"], 0)  # context records are not "other variants"


class TestPdfAndFullReport(unittest.TestCase):
    def _document(self, clinvar_result):
        variant_dict = {"chrom": "10", "pos": 94842879, "ref": "G", "alt": "A"}
        report = build_clinical_report(
            _ir("10", 94842879, "CYP2C19"), variant_dict=variant_dict, raw_evidence={"clinvar": clinvar_result}
        )
        return {
            "geper_version": "test",
            "generated_at": "2026-11-24T00:00:00+00:00",
            "input_vcf": "c2.vcf",
            "assembly": "GRCh38",
            "vcf_samples": ["S1"],
            "variant_count": 1,
            "variants": [
                {
                    "variant": variant_dict,
                    "interpretation_result": {"gene_symbol": "CYP2C19"},
                    "candidate_interpretation": report,
                    "clinvar": clinvar_result,
                }
            ],
        }

    def _pdf_text(self, document):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.pdf")
            generate_pdf(document, path)
            return " ".join((page.extract_text() or "") for page in PdfReader(path).pages).replace("\n", " ")

    def test_pdf_labels_context_and_does_not_present_it_as_a_classification(self):
        text = self._pdf_text(self._document(run_client(CASES["CYP2C19_c1004GA"])[0]))
        self.assertIn("ClinVar context (not used for classification)", text)
        self.assertIn("Diplotype x34", text)
        self.assertNotIn("ClinVar: drug response", text)
        self.assertIn("no individual-variant record with a classification", text)

    def test_markdown_report_section_12_and_json_carry_the_same_distinction(self):
        result = run_client(CASES["ABCB11_c2636TG"])[0]
        document = self._document(result)
        md = ReportGenerator().generate(document)
        self.assertIn("**ClinVar context (not used for classification):**", md)
        self.assertIn("**ClinVar:** Uncertain significance", md)
        raw = json.loads(json.dumps(document, default=str))
        self.assertEqual(raw["variants"][0]["clinvar"]["evidence_policy"], EVIDENCE_POLICY_VERSION)
        self.assertEqual(raw["variants"][0]["clinvar"]["context_records"][0]["record_type"], "Haplotype")

    def test_a_conflicting_result_is_rendered_not_hidden(self):
        case = CASES["TP53_M237I"]
        entries = TestDerivedCases()._clone(case, "VCV000634770", "VCV9999999", description="Benign")
        document = self._document(run_client(case, entries)[0])
        self.assertIn("conflicting individual-variant records", ReportGenerator().generate(document))
        self.assertIn("conflicting individual-variant records", self._pdf_text(document))


class TestLimsExport(unittest.TestCase):
    def test_lims_json_carries_the_policy_and_labelled_context_but_no_context_classification_in_the_scored_columns(
        self,
    ):
        result = run_client(CASES["ABCB11_c2636TG"])[0]
        db = export_lims._clinical_database({"clinvar": result, "clingen": {}})
        self.assertEqual(db.clinvar_significance, "Uncertain significance")
        self.assertEqual(db.clinvar_evidence_policy, EVIDENCE_POLICY_VERSION)
        self.assertEqual(db.clinvar_individual_record_count, 1)
        self.assertEqual(db.clinvar_context_record_count, 1)
        self.assertEqual(db.clinvar_context_records[0].record_type, "Haplotype")
        self.assertEqual(db.clinvar_context_records[0].accession, "VCV004846108")
        self.assertFalse(db.clinvar_individual_records_conflict)

    def test_diplotype_only_leaves_the_scored_columns_empty_and_caps_the_context_list(self):
        result = run_client(CASES["CYP2C19_c1004GA"])[0]
        db = export_lims._clinical_database({"clinvar": result, "clingen": {}})
        self.assertIsNone(db.clinvar_significance)
        self.assertIsNone(db.clinvar_review_status)
        self.assertEqual(db.clinvar_context_record_count, 36)
        self.assertEqual(len(db.clinvar_context_records), export_lims.LIMS_CONTEXT_RECORD_LIMIT)

    def test_conflicting_records_are_flagged_not_resolved(self):
        case = CASES["TP53_M237I"]
        entries = TestDerivedCases()._clone(case, "VCV000634770", "VCV9999999", description="Benign")
        db = export_lims._clinical_database({"clinvar": run_client(case, entries)[0], "clingen": {}})
        self.assertIsNone(db.clinvar_significance)
        self.assertTrue(db.clinvar_individual_records_conflict)
        self.assertEqual(db.clinvar_individual_record_count, 2)

    def test_the_lims_csv_header_is_unchanged(self):
        self.assertEqual(export_lims._CSV_COLUMNS, _CSV_COLUMNS_ON_MASTER)


# ---------------------------------------------------------------------------------------------------
# 7. Backward compatibility with results stored before the policy
# ---------------------------------------------------------------------------------------------------


class TestStoredResultsFromBeforeThePolicy(unittest.TestCase):
    def test_markdown_evidence_crossref_and_reasons_are_exactly_what_master_produced(self):
        for name, stored in LEGACY["inputs"].items():
            expected = LEGACY["outputs"][name]
            with self.subTest(result=name):
                self.assertEqual(ReportGenerator._render_clinvar(stored), expected["markdown"])
                self.assertEqual(_clinical_evidence({"clinvar": stored, "clingen": {}}), expected["evidence"])
                self.assertEqual(ACMGRuleEngine._clinvar_crossref(stored), expected["crossref"])
                self.assertEqual(ACMGRuleEngine._clinvar_not_found_reason(stored), expected["not_found_reason"])

    def test_the_content_hash_of_a_document_with_a_primary_record_is_unchanged(self):
        document = {
            "variants": [
                {"variant": {"chrom": "1", "pos": 5, "ref": "A", "alt": "T"}, "clinvar": LEGACY["inputs"]["matched"]}
            ]
        }
        self.assertEqual(compute_content_hash(document), LEGACY["outputs"]["content_hash_matched"])

    def test_a_null_primary_record_no_longer_breaks_the_content_hash(self):
        # Before: `None.get(...)` raised, so sign-off could not hash a document with such a variant. The policy makes a
        # null primary record common (a variant that only sits inside a haplotype), so it must hash.
        for stored in (
            LEGACY["inputs"]["position_only"],
            LEGACY["inputs"]["not_found"],
            run_client(CASES["CYP2C19_c1004GA"])[0],
        ):
            document = {"variants": [{"variant": {"chrom": "1", "pos": 5, "ref": "A", "alt": "T"}, "clinvar": stored}]}
            self.assertEqual(len(compute_content_hash(document)), 64)

    def test_consumers_accept_stored_results_without_any_new_field(self):
        for name, stored in LEGACY["inputs"].items():
            with self.subTest(result=name):
                self.assertNotIn("evidence_policy", stored)
                _consumer_snapshot(stored)  # must not raise

    def test_legacy_matched_result_still_drives_the_scores(self):
        snap = _consumer_snapshot(LEGACY["inputs"]["matched"])
        self.assertEqual(snap["priority_factor"], 1.0)
        self.assertEqual(snap["confidence_quality"], 1.0)


if __name__ == "__main__":
    unittest.main()
