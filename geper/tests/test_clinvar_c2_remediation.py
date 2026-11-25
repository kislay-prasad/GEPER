"""
Tests for the ClinVar audit C2 remediation round (approved after the downstream audit):

* D1  `evidence_sources` no longer lists ClinVar when only context-only records were retrieved; the
      retrieved-but-not-evidence indication is a separate field (`context_data_sources`);
* D2  the record-type policy never raises on malformed external values (non-text descriptions/review
      statuses, unhashable or missing record types);
* D3  the "0 other, non-matching" wording is gone;
* D4  non-matching co-located records stay visible next to a matched record, apart from matched/context;
* D5  the test gaps the audit's mutation run found (the `clinvar: None` hash guard, date ordering, both
      star-allele spellings, the overflow/summary branches);
* unknown/missing record types stay context only, with a structured warning naming accession and type;
* explicit reviewer attention for conflicting eligible records (flags, Markdown, both PDFs, JSON, LIMS JSON);
* the versioned content hash: version 1 is the legacy hash, byte for byte (digests produced by UNMODIFIED
  origin/master are frozen in `fixtures/c2_content_hash_legacy_documents.json`); version 2 additionally
  attests the ClinVar record attribution; new approvals are sealed under version 2.

Real frozen ClinVar candidate sets (`fixtures/c2_clinvar_record_types.json`) drive the real
`ClinVarClient.query_variant`; derived cases say so in their names.
"""

import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from pypdf import PdfReader

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.interpretation import InterpretationEngine
from pipeline.ps1_pm5.utils import clinvar_codon_match_from_esummary
from report import export_lims
from report.clinical_report_builder import _clinical_evidence, _variant_reviewer_flags
from report.models import (
    CONTENT_HASH_VERSION,
    LEGACY_CONTENT_HASH_VERSION,
    compute_content_hash,
    content_hash_version_of,
    verify_content_hash,
)
from report.report_generator import ReportGenerator
from report.summary_short import generate_short_pdf
from review import signoff as signoff_module
from review import signoff_status
from tests import (
    test_clinvar_record_types as _base,
)  # module import: its TestCase classes must not be re-collected here
from tests.test_clinvar_record_types import CASES, _markdown, accessions, run_client
from tests.test_signoff import _make_document
from utils.clinvar_records import (
    ROLE_STAR_ALLELE_RECORD,
    ROLE_UNRECOGNIZED_RECORD_TYPE,
    ROLE_VARIANT_RECORD_UNCLASSIFIED,
    as_text,
    classifications_conflict,
    classify_record_role,
    conflict_clause,
    has_individual_classification,
    is_single_allele_record,
    order_records,
    record_type_warning,
    record_type_warning_message,
    review_rank,
    reviewer_attention_text,
    summarize_context,
    unrecognized_record_reason,
)
from utils.exceptions import SignoffError

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
with open(os.path.join(_FIXTURES, "c2_content_hash_legacy_documents.json"), "r", encoding="utf-8") as _fh:
    LEGACY_HASH_FIXTURE = json.load(_fh)

TP53 = CASES["TP53_M237I"]
ABCB11 = CASES["ABCB11_c2636TG"]
STAR23 = CASES["CYP2C19_star23"]
NO_OWN = CASES["ABCB11_no_own_classification"]


def conflict_result():
    """DERIVED: the real TP53 M237I record plus a clone of it classified Benign (two eligible records disagree)."""
    entries = _base.TestDerivedCases()._clone(TP53, "VCV000634770", "VCV9999999", description="Benign")
    return run_client(TP53, entries)[0]


def not_found_result():
    return run_client(TP53, uids=[])[0]


def _interpret(clinvar_result):
    return InterpretationEngine().interpret(
        {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
        [],
        clinvar_result,
        {},
        {},
        {},
        clingen_result={"gene_resolution_status": "resolved"},
    )["interpretation_result"]


# ---------------------------------------------------------------------------------------------------
# D1  evidence_sources vs context data
# ---------------------------------------------------------------------------------------------------


class TestEvidenceSourcesSemantics(unittest.TestCase):
    def test_context_only_records_are_not_an_evidence_source(self):
        for case in (STAR23, NO_OWN, CASES["CYP2C19_c1004GA"], CASES["CYP2C19_diplotype_only"]):
            ir = _interpret(run_client(case)[0])
            self.assertNotIn("ClinVar", ir["evidence_sources"], case["key"])
            self.assertEqual(ir["context_data_sources"], ["ClinVar"], case["key"])

    def test_an_eligible_record_is_an_evidence_source_and_not_listed_as_context_data(self):
        for case in (TP53, ABCB11):
            ir = _interpret(run_client(case)[0])
            self.assertIn("ClinVar", ir["evidence_sources"], case["key"])
            self.assertEqual(ir["context_data_sources"], [], case["key"])

    def test_conflicting_eligible_records_are_an_evidence_source(self):
        ir = _interpret(conflict_result())
        self.assertIn("ClinVar", ir["evidence_sources"])
        self.assertEqual(ir["context_data_sources"], [])

    def test_no_clinvar_record_lists_nothing(self):
        ir = _interpret(not_found_result())
        self.assertNotIn("ClinVar", ir["evidence_sources"])
        self.assertEqual(ir["context_data_sources"], [])

    def test_a_result_stored_before_the_policy_keeps_the_old_rule(self):
        legacy = {
            "match_status": "position_only",
            "found": False,
            "records": [{"uid": "1", "accession": "VCV1", "variant_match": False, "clinical_significance": "Benign"}],
            "matched_records": [],
            "primary_record": None,
        }
        ir = _interpret(legacy)
        self.assertIn("ClinVar", ir["evidence_sources"])
        self.assertEqual(ir["context_data_sources"], [])

    def test_scored_fields_of_a_context_only_variant_equal_a_not_found_variant(self):
        keys = (
            "acmg_classification",
            "acmg_net_points",
            "acmg_pathogenic_points",
            "acmg_benign_points",
            "confidence_score",
            "confidence_label",
            "priority_score",
            "priority_category",
            "conflict_score",
            "conflict_severity",
            "legacy_pre_acmg_significance_score",
        )
        reference = _interpret(not_found_result())
        for case in (STAR23, NO_OWN, CASES["CYP2C19_c1004GA"]):
            ir = _interpret(run_client(case)[0])
            for key in keys:
                self.assertEqual(ir.get(key), reference.get(key), f"{case['key']}: {key}")

    def test_the_full_markdown_report_and_both_pdfs_label_context_data_apart_from_evidence(self):
        helper = _base.TestPdfAndFullReport()
        result = run_client(STAR23)[0]
        document = helper._document(result)
        clinical = document["variants"][0]["candidate_interpretation"]
        # the helper's synthetic interpretation has no evidence_sources of its own: set the real fields
        clinical["evidence_sources"] = []
        clinical["context_data_sources"] = ["ClinVar"]
        text = helper._pdf_text(document)
        self.assertIn("Evidence Sources: none", text)
        self.assertIn("Context data retrieved (not evidence about this variant): ClinVar", text)
        lines = ReportGenerator._render_explainability(
            {"decision_summary": "d", "reasoning_chain": [], "evidence_contributed": []}, ["ClinVar"]
        )
        self.assertIn("**Evidence sources that contributed:** none", lines)
        self.assertIn("**Context data retrieved (not evidence about this variant):** ClinVar", lines)
        without_context = ReportGenerator._render_explainability(
            {"decision_summary": "d", "reasoning_chain": [], "evidence_contributed": []}, []
        )
        self.assertFalse(any("Context data retrieved" in line for line in without_context))

    def test_explainability_says_why_clinvar_did_not_contribute(self):
        ir = _interpret(run_client(STAR23)[0])
        reasons = {e["source"]: e["reason"] for e in ir["explainability"]["evidence_not_contributed"]}
        self.assertIn("context only", reasons["ClinVar"])
        self.assertNotIn("ClinVar", ir["explainability"]["evidence_contributed"])

    def test_lims_json_finding_carries_context_data_sources_and_the_csv_still_lists_evidence_only(self):
        self.assertIn("context_data_sources", export_lims.LIMSFinding.model_fields)
        self.assertNotIn("context_data_sources", export_lims._CSV_COLUMNS)


# ---------------------------------------------------------------------------------------------------
# D2  hardening against malformed external fields
# ---------------------------------------------------------------------------------------------------

_NOT_TEXT = (123, 1.5, True, ["x"], {"a": 1}, ("t",))


class TestMalformedExternalFields(unittest.TestCase):
    def test_as_text(self):
        self.assertEqual(as_text("x"), "x")
        for value in (None, *_NOT_TEXT):
            self.assertIsNone(as_text(value))

    def test_role_function_never_raises_and_never_makes_a_malformed_record_eligible(self):
        for bad in (None, *_NOT_TEXT):
            self.assertEqual(classify_record_role(bad, 1, "Pathogenic"), ROLE_UNRECOGNIZED_RECORD_TYPE, bad)
            self.assertEqual(
                classify_record_role("single nucleotide variant", 1, bad), ROLE_VARIANT_RECORD_UNCLASSIFIED, bad
            )
            self.assertFalse(has_individual_classification(bad))
            self.assertFalse(is_single_allele_record(bad, 1))
            self.assertEqual(review_rank(bad), 0)

    def test_ordering_conflict_and_summary_helpers_tolerate_non_text_fields(self):
        records = [
            {"accession": "VCV2", "review_status": 3, "last_evaluated": ["x"], "clinical_significance": {"a": 1}},
            {"accession": "VCV1", "review_status": None, "last_evaluated": 5, "clinical_significance": 7},
            {"accession": "VCV3", "review_status": "reviewed by expert panel", "last_evaluated": True},
        ]
        self.assertEqual(accessions(order_records(records)), ["VCV3", "VCV1", "VCV2"])
        self.assertFalse(classifications_conflict(records))
        self.assertEqual(summarize_context([{"record_type": ["x"]}, {"record_type": None}]), {"unrecognised type": 2})
        self.assertIn(
            "unspecified",
            conflict_clause({"individual_records_conflict": True, "matched_records": [{"clinical_significance": 5}]}),
        )

    def test_client_survives_malformed_values_in_every_field_the_policy_reads(self):
        for field, bad_values in (
            ("obj_type", (None, "", 123, ["x"], {"a": 1}, True)),
            ("description", (123, ["x"], {"a": 1}, True)),
            ("review_status", (123, ["x"], {"a": 1}, True)),
        ):
            for bad in bad_values:
                entries = copy.deepcopy(TP53["entries"])
                target = next(e for e in entries.values() if e["accession"] == "VCV000634770")
                if field == "obj_type":
                    target["obj_type"] = bad
                else:
                    target["germline_classification"][field] = bad
                with self.subTest(field=field, value=bad):
                    result, _ = run_client(TP53, entries)  # must not raise
                    record = next(r for r in result["records"] if r["accession"] == "VCV000634770")
                    self.assertIsInstance(record["clinical_significance"], (str, type(None)), (field, bad))
                    self.assertIsInstance(record["review_status"], (str, type(None)), (field, bad))
                    if field == "description":
                        self.assertIsNone(record["clinical_significance"])
                    if field == "review_status":
                        # the record stays an eligible individual-variant record; only its status is unknown
                        self.assertTrue(record["eligible_for_variant_classification"])
                        self.assertIsNone(record["review_status"])
                    else:
                        self.assertFalse(record["eligible_for_variant_classification"])
                        self.assertIsNone(result["primary_record"])
                        self.assertEqual(result["match_status"], "position_only")
                        self.assertEqual(result["context_record_count"], 1)
                    json.dumps(result)  # JSON-safe: nothing malformed leaks into the stored result

    def test_downstream_renderers_accept_a_context_record_that_came_from_a_malformed_field(self):
        for field, bad in (
            ("obj_type", ["x"]),
            ("description", 123),
            ("description", ["x"]),
            ("review_status", {"a": 1}),
        ):
            entries = copy.deepcopy(TP53["entries"])
            target = next(e for e in entries.values() if e["accession"] == "VCV000634770")
            if field == "obj_type":
                target["obj_type"] = bad
            else:
                target["germline_classification"][field] = bad
            with self.subTest(field=field, value=bad):
                result, _ = run_client(TP53, entries)
                _markdown(result)
                _interpret(result)
                # the LIMS models are typed (Optional[str]): a leaked non-text value would raise here
                export_lims._clinical_database({"clinvar": result, "clingen": {}})
                _clinical_evidence({"clinvar": result, "clingen": {}})

    def test_ps1_pm5_guard_tolerates_unhashable_and_missing_record_types(self):
        entry = copy.deepcopy(next(e for e in TP53["entries"].values() if e["accession"] == "VCV000634770"))
        self.assertIsNotNone(clinvar_codon_match_from_esummary(entry["uid"], entry))  # control: the entry is usable
        for bad in (None, ["x"], {"a": 1}, 5, ""):
            entry["obj_type"] = bad
            self.assertIsNone(clinvar_codon_match_from_esummary(entry["uid"], entry), bad)


# ---------------------------------------------------------------------------------------------------
# D3  not-found wording
# ---------------------------------------------------------------------------------------------------


class TestNotFoundWording(unittest.TestCase):
    def test_context_only_wording_never_claims_zero_other_variants(self):
        for case in (STAR23, CASES["CYP2C19_c1004GA"], CASES["CYP2C19_diplotype_only"]):
            result = run_client(case)[0]
            self.assertEqual(result["record_count"], result["context_record_count"])
            texts = [
                ACMGRuleEngine._clinvar_not_found_reason(result),
                ACMGRuleEngine._clinvar_crossref(result)["note"],
                " ".join(_interpret(result)["supporting_evidence"] + _interpret(result)["conflicting_evidence"]),
            ]
            for text in texts:
                self.assertNotIn("0 other", text, case["key"])
            self.assertIn("no individual-variant ClinVar record with a classification", texts[0])
            self.assertIn("No individual-variant ClinVar record with a classification", texts[1])

    def test_a_real_count_of_other_variants_is_still_stated(self):
        result = run_client(NO_OWN)[0]
        others = result["record_count"] - result["context_record_count"]
        self.assertGreater(others, 0)
        self.assertIn(f"{others} other ClinVar-catalogued", ACMGRuleEngine._clinvar_not_found_reason(result))
        self.assertIn(f"{others} other, non-matching", ACMGRuleEngine._clinvar_crossref(result)["note"])


# ---------------------------------------------------------------------------------------------------
# D4  non-matching records stay visible, apart from matched and context records
# ---------------------------------------------------------------------------------------------------


class TestNonMatchingRecordsRemainVisible(unittest.TestCase):
    def test_matched_record_context_and_other_variants_are_three_separate_blocks(self):
        result = run_client(CASES["NBAS_c619CT"])[0]
        self.assertTrue(result["matched_records"] and result["context_records"])
        non_matching = [r for r in result["records"] if r["variant_match"] is not True]
        self.assertTrue(non_matching)
        text = _markdown(result)
        lines = text.splitlines()
        self.assertEqual(len([line for line in lines if "**this variant**" in line]), 1)
        header = next(i for i, line in enumerate(lines) if line.startswith("_Other variant(s) catalogued"))
        self.assertIn("NOT this variant and not evidence about it", lines[header])
        block = [line for line in lines[header + 1 :] if line.startswith("- ")]
        self.assertEqual(len(block), min(3, len(non_matching)))
        for line in block:
            self.assertIn("different variant at this position", line)
            self.assertNotIn("**this variant**", line)
        context_header = next(i for i, line in enumerate(lines) if line.startswith("_Context only"))
        self.assertLess(
            next(i for i, line in enumerate(lines) if "**this variant**" in line), context_header
        )  # matched, then context, then other variants
        self.assertLess(context_header, header)
        for accession in (r["accession"] for r in non_matching[:3]):
            self.assertIn(accession, text)

    def test_other_variants_with_an_unconfirmed_match_are_labelled_as_such(self):
        result = copy.deepcopy(run_client(CASES["NBAS_c619CT"])[0])
        for record in result["records"]:
            if record["variant_match"] is False:
                record["variant_match"] = None
        text = _markdown(result)
        self.assertIn("match unconfirmed", text)
        self.assertNotIn("different variant at this position", text)

    def test_more_than_three_other_variants_get_an_overflow_line(self):
        result = copy.deepcopy(run_client(CASES["BRCA1_c68_69del"])[0])
        others = [r for r in result["records"] if r["variant_match"] is not True]
        self.assertGreater(len(others), 3)
        text = _markdown(result)
        self.assertIn(f"… and {len(others) - 3} more non-matching record(s) in the JSON report.", text)

    def test_the_position_only_case_still_lists_them(self):
        text = _markdown(run_client(NO_OWN)[0])
        self.assertIn("_Other variant(s) catalogued at this same genomic position", text)
        self.assertIn("different variant at this position", text)

    def test_summary_bullets_state_the_count_without_calling_them_evidence(self):
        evidence = _clinical_evidence({"clinvar": run_client(CASES["NBAS_c619CT"])[0], "clingen": {}})
        self.assertGreater(evidence["clinvar_other_variants_count"], 0)
        document = _base.TestPdfAndFullReport()._document(run_client(CASES["NBAS_c619CT"])[0])
        md = ReportGenerator().generate(document)
        self.assertIn("**ClinVar other variants at this position (not evidence about this variant):**", md)
        pdf = _base.TestPdfAndFullReport()._pdf_text(document)
        self.assertIn("ClinVar other variants at this position (not evidence about this variant)", pdf)

    def test_a_legacy_result_renders_exactly_as_before(self):
        legacy = {
            "match_status": "position_only",
            "found": False,
            "records": [
                {
                    "uid": "1",
                    "accession": "VCV1",
                    "variant_match": False,
                    "clinical_significance": "Benign",
                    "review_status": "x",
                    "condition": ["c"],
                }
            ],
            "matched_records": [],
            "primary_record": None,
        }
        text = "\n".join(ReportGenerator._render_clinvar(legacy))
        self.assertIn("NOT this variant:_", text)
        self.assertNotIn("not evidence about it", text)


# ---------------------------------------------------------------------------------------------------
# D5  closing the audit's test gaps
# ---------------------------------------------------------------------------------------------------


class TestAuditTestGaps(unittest.TestCase):
    def test_both_star_allele_spellings_are_context_only_star_allele_records(self):
        for spelling in ("Haplotype, single variant", "Haplotype defined by a single variant"):
            self.assertEqual(classify_record_role(spelling, 1, "drug response"), ROLE_STAR_ALLELE_RECORD, spelling)
            self.assertFalse(is_single_allele_record(spelling, 1), spelling)
            self.assertIsNone(unrecognized_record_reason(spelling, 1), spelling)

    def test_both_star_allele_spellings_through_the_client_stay_context_only(self):
        for spelling in ("Haplotype, single variant", "Haplotype defined by a single variant"):
            entries = copy.deepcopy(TP53["entries"])
            next(e for e in entries.values() if e["accession"] == "VCV000634770")["obj_type"] = spelling
            result, _ = run_client(TP53, entries)
            record = next(r for r in result["records"] if r["accession"] == "VCV000634770")
            self.assertEqual(record["record_role"], ROLE_STAR_ALLELE_RECORD, spelling)
            self.assertIsNone(result["primary_record"], spelling)
            self.assertEqual(result["record_type_warnings"], [], spelling)  # a KNOWN type: no unknown-type warning

    def test_date_ordering_newer_evaluation_first_when_review_rank_ties(self):
        records = [
            {
                "accession": "VCV1",
                "review_status": "criteria provided, single submitter",
                "last_evaluated": "2020/01/01 00:00",
            },
            {
                "accession": "VCV2",
                "review_status": "criteria provided, single submitter",
                "last_evaluated": "2024/06/01 00:00",
            },
            {
                "accession": "VCV3",
                "review_status": "criteria provided, single submitter",
                "last_evaluated": "2022/03/05 00:00",
            },
            {
                "accession": "VCV4",
                "review_status": "criteria provided, single submitter",
                "last_evaluated": "not a date",
            },
        ]
        self.assertEqual(accessions(order_records(records)), ["VCV2", "VCV3", "VCV1", "VCV4"])
        self.assertEqual(accessions(order_records(reversed(records))), ["VCV2", "VCV3", "VCV1", "VCV4"])

    def test_review_rank_outranks_date_and_accession_breaks_a_full_tie(self):
        records = [
            {
                "accession": "VCVB",
                "review_status": "criteria provided, single submitter",
                "last_evaluated": "2030/01/01 00:00",
            },
            {"accession": "VCVA", "review_status": "reviewed by expert panel", "last_evaluated": "2001/01/01 00:00"},
            {
                "accession": "VCVD",
                "review_status": "criteria provided, single submitter",
                "last_evaluated": "2030/01/01 00:00",
            },
        ]
        self.assertEqual(accessions(order_records(records)), ["VCVA", "VCVB", "VCVD"])

    def test_the_clinvar_none_content_hash_guard(self):
        document = {"variants": [{"variant": {"chrom": "1", "pos": 1, "ref": "A", "alt": "T"}, "clinvar": None}]}
        absent = {"variants": [{"variant": {"chrom": "1", "pos": 1, "ref": "A", "alt": "T"}}]}
        empty = {"variants": [{"variant": {"chrom": "1", "pos": 1, "ref": "A", "alt": "T"}, "clinvar": {}}]}
        for version in (LEGACY_CONTENT_HASH_VERSION, CONTENT_HASH_VERSION):
            digest = compute_content_hash(document, version)  # raised TypeError on master
            self.assertEqual(len(digest), 64)
            self.assertEqual(digest, compute_content_hash(empty, version))
        self.assertEqual(compute_content_hash(document, 1), compute_content_hash(absent, 1))

    def test_the_null_primary_content_hash_guard(self):
        for result in (not_found_result(), run_client(STAR23)[0], conflict_result()):
            self.assertIsNone(result["primary_record"])
            document = {"variants": [{"variant": {"chrom": "1", "pos": 1, "ref": "A", "alt": "T"}, "clinvar": result}]}
            self.assertEqual(len(compute_content_hash(document, 1)), 64)
            self.assertEqual(len(compute_content_hash(document, 2)), 64)

    def test_more_than_three_eligible_records_get_an_overflow_line_and_conflict_wording(self):
        case = TP53
        entries = copy.deepcopy(case["entries"])
        src = next(e for e in entries.values() if e["accession"] == "VCV000634770")
        for n, text in enumerate(("Benign", "Likely benign", "Uncertain significance", "Pathogenic"), start=1):
            clone = copy.deepcopy(src)
            clone["uid"] = f"88{n}"
            clone["accession"] = f"VCV880000{n}"
            clone["germline_classification"]["description"] = text
            entries[clone["uid"]] = clone
        result, _ = run_client(case, entries)
        self.assertEqual(len(result["matched_records"]), 5)
        self.assertTrue(result["individual_records_conflict"])
        self.assertIsNone(result["primary_record"])
        text = _markdown(result)
        self.assertIn("… and 2 more individual-variant record(s) in the JSON report.", text)

    def test_no_conflict_clause_and_no_attention_text_without_a_conflict(self):
        self.assertEqual(conflict_clause(run_client(TP53)[0]), "")
        self.assertIsNone(reviewer_attention_text(run_client(TP53)[0]))
        self.assertIsNone(reviewer_attention_text(None))

    def test_context_only_summary_bullet_in_markdown_and_pdf(self):
        helper = _base.TestPdfAndFullReport()
        document = helper._document(run_client(STAR23)[0])
        md = ReportGenerator().generate(document)
        self.assertIn(
            "- **ClinVar:** no individual-variant record with a classification found for this exact variant.", md
        )
        self.assertIn(
            "no individual-variant record with a classification found for this exact variant",
            helper._pdf_text(document),
        )


# ---------------------------------------------------------------------------------------------------
# Unknown / missing record types: context only + structured warning, never fatal
# ---------------------------------------------------------------------------------------------------


class TestUnknownRecordTypeWarning(unittest.TestCase):
    def _result_with_type(self, value, case=TP53, accession="VCV000634770"):
        entries = copy.deepcopy(case["entries"])
        target = next(e for e in entries.values() if e["accession"] == accession)
        if value is KeyError:
            target.pop("obj_type", None)
        else:
            target["obj_type"] = value
        return run_client(case, entries)[0]

    def test_unknown_type_is_context_only_with_a_warning_naming_accession_and_type(self):
        with self.assertLogs("geper.database.clinvar_client", level="WARNING") as logs:
            result = self._result_with_type("Brand new record type")
        self.assertIsNone(result["primary_record"])
        self.assertEqual(result["match_status"], "position_only")
        self.assertEqual(
            result["record_type_warnings"],
            [
                {
                    "accession": "VCV000634770",
                    "uid": next(e["uid"] for e in TP53["entries"].values() if e["accession"] == "VCV000634770"),
                    "record_type": "Brand new record type",
                    "component_count": 1,
                    "reason": "unrecognized_record_type",
                    "treated_as": "context_only",
                }
            ],
        )
        self.assertTrue(any("VCV000634770" in m and "Brand new record type" in m for m in logs.output))
        self.assertEqual(result["context_records"][0]["record_role"], ROLE_UNRECOGNIZED_RECORD_TYPE)

    def test_missing_and_non_text_types_warn_with_their_own_reason(self):
        for value, reason in (
            (KeyError, "missing_record_type"),
            (None, "missing_record_type"),
            ("", "missing_record_type"),
            (["x"], "non_text_record_type"),
        ):
            with self.subTest(value=value):
                result = self._result_with_type(value)
                self.assertEqual([w["reason"] for w in result["record_type_warnings"]], [reason])
                self.assertIsNone(result["record_type_warnings"][0]["record_type"])
                self.assertIsNone(result["primary_record"])

    def test_a_known_type_with_the_wrong_component_count_warns(self):
        entries = copy.deepcopy(TP53["entries"])
        target = next(e for e in entries.values() if e["accession"] == "VCV000634770")
        target["variation_set"] = target["variation_set"] * 2
        result, _ = run_client(TP53, entries)
        self.assertEqual([w["reason"] for w in result["record_type_warnings"]], ["unexpected_component_count"])
        self.assertIsNone(result["primary_record"])

    def test_recognised_types_and_non_matching_records_raise_no_warning(self):
        for case in CASES.values():
            self.assertEqual(run_client(case)[0]["record_type_warnings"], [], case["key"])
        self.assertEqual(not_found_result()["record_type_warnings"], [])
        non_matching = {
            "accession": "VCV1",
            "uid": "1",
            "record_type": "weird",
            "component_count": 1,
            "variant_match": False,
        }
        self.assertIsNone(record_type_warning(non_matching))

    def test_every_branch_of_the_reason_function(self):
        self.assertEqual(unrecognized_record_reason(None, 1), "missing_record_type")
        self.assertEqual(unrecognized_record_reason("  ", 1), "missing_record_type")
        self.assertEqual(unrecognized_record_reason(["x"], 1), "non_text_record_type")
        self.assertEqual(unrecognized_record_reason(5, 1), "non_text_record_type")
        self.assertEqual(unrecognized_record_reason("Weird", 1), "unrecognized_record_type")
        self.assertEqual(unrecognized_record_reason("Variation", 1), "unrecognized_record_type")
        self.assertEqual(unrecognized_record_reason("Deletion", 2), "unexpected_component_count")
        self.assertEqual(unrecognized_record_reason("Deletion", None), "unexpected_component_count")
        for known in ("Deletion", "Diplotype", "Haplotype, single variant"):
            self.assertIsNone(unrecognized_record_reason(known, 1))
        self.assertIsNone(unrecognized_record_reason("Diplotype", 7))  # multi-variant types may have any count

    def test_warning_text(self):
        warning = {"accession": "VCV7", "record_type": "Weird", "reason": "unrecognized_record_type"}
        text = record_type_warning_message(warning)
        self.assertIn("VCV7", text)
        self.assertIn("'Weird'", text)
        self.assertIn("context only", text)

    def test_the_pipeline_stage_surfaces_the_warning_and_does_not_fail(self):
        from pipeline.orchestrator import GeperPipeline

        result = self._result_with_type("Brand new record type")
        pipeline = GeperPipeline.__new__(GeperPipeline)
        pipeline.clinvar_client = mock.Mock(query_variant=mock.Mock(return_value=result))
        pipeline.sequence_context_gen = mock.Mock(assembly="GRCh38")
        pipeline._timer = lambda name: mock.MagicMock()
        errors = []
        out = GeperPipeline._run_clinvar_stage(pipeline, mock.Mock(), {}, errors)
        self.assertIs(out, result)
        self.assertEqual(len(errors), 1)
        self.assertIn("VCV000634770", errors[0])
        self.assertIn("Brand new record type", errors[0])

    def test_warning_reaches_the_reports_and_lims_json(self):
        result = self._result_with_type("Brand new record type")
        evidence = _clinical_evidence({"clinvar": result, "clingen": {}})
        self.assertEqual(evidence["clinvar_record_type_warnings"][0]["accession"], "VCV000634770")
        document = _base.TestPdfAndFullReport()._document(result)
        self.assertIn("**ClinVar record type warning:**", ReportGenerator().generate(document))
        self.assertIn("ClinVar record type warning", _base.TestPdfAndFullReport()._pdf_text(document))
        db = export_lims._clinical_database({"clinvar": result, "clingen": {}})
        self.assertEqual(db.clinvar_record_type_warnings[0].accession, "VCV000634770")
        self.assertEqual(db.clinvar_record_type_warnings[0].record_type, "Brand new record type")
        self.assertEqual(db.clinvar_record_type_warnings[0].treated_as, "context_only")

    def test_unknown_types_never_change_a_classification(self):
        reference = _interpret(not_found_result())
        ir = _interpret(self._result_with_type("Brand new record type"))
        for key in ("acmg_classification", "acmg_net_points", "confidence_score", "priority_score", "conflict_score"):
            self.assertEqual(ir.get(key), reference.get(key), key)


# ---------------------------------------------------------------------------------------------------
# Reviewer attention for conflicting eligible submissions
# ---------------------------------------------------------------------------------------------------


class TestReviewerAttentionForConflictingEligibleRecords(unittest.TestCase):
    def setUp(self):
        self.result = conflict_result()
        self.helper = _base.TestPdfAndFullReport()
        self.document = self.helper._document(self.result)

    def test_text_names_the_classifications_and_says_none_is_used(self):
        text = reviewer_attention_text(self.result)
        self.assertIn("2 individual-variant records", text)
        self.assertIn("Benign", text)
        self.assertIn("Pathogenic/Likely pathogenic", text)
        self.assertIn("none is used as a single classification", text)

    def test_shared_reviewer_flags_include_it_and_only_for_a_conflict(self):
        clinical = self.document["variants"][0]["candidate_interpretation"]
        flags = _variant_reviewer_flags({"clinvar": self.result}, clinical)
        self.assertTrue(any("conflicting individual-variant records" in f for f in flags), flags)
        for result in (run_client(TP53)[0], run_client(STAR23)[0], not_found_result()):
            clinical_ok = _base.TestPdfAndFullReport()._document(result)["variants"][0]["candidate_interpretation"]
            self.assertFalse(
                any(
                    "conflicting individual-variant" in f
                    for f in _variant_reviewer_flags({"clinvar": result}, clinical_ok)
                )
            )

    def test_flags_tolerate_a_malformed_clinvar_block_or_evidence_block(self):
        for raw in ("text", 5, ["x"], None):
            clinical = {"clinical_evidence": raw}
            self.assertEqual(_variant_reviewer_flags({"clinvar": raw}, clinical), [])

    def test_full_markdown_has_the_attention_section_entry_and_the_section_12_line(self):
        md = ReportGenerator().generate(self.document)
        section = md.split("## Reviewer Attention", 1)[1].split("\n## ", 1)[0]
        self.assertIn("conflicting individual-variant records", section)
        self.assertIn("**⚠ ClinVar reviewer attention:**", md)

    def test_full_pdf_and_short_pdf_carry_it(self):
        full = self.helper._pdf_text(self.document)
        self.assertIn("ClinVar reviewer attention", full)
        self.assertIn("conflicting individual-variant records", full)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "short.pdf")
            generate_short_pdf(self.document, path)
            short = " ".join((p.extract_text() or "") for p in PdfReader(path).pages).replace("\n", " ")
        self.assertIn("Reviewer attention:", short)
        self.assertIn("conflicting individual-variant records", short)

    def test_structured_json_and_lims_json_carry_it(self):
        evidence = self.document["variants"][0]["candidate_interpretation"]["clinical_evidence"]
        self.assertEqual(evidence["clinvar_reviewer_attention"], reviewer_attention_text(self.result))
        db = export_lims._clinical_database({"clinvar": self.result, "clingen": {}})
        self.assertEqual(db.clinvar_reviewer_attention, reviewer_attention_text(self.result))
        self.assertTrue(db.clinvar_individual_records_conflict)
        self.assertIsNone(db.clinvar_significance)  # still no single classification
        json.dumps(db.model_dump())

    def test_no_conflict_means_no_attention_anywhere(self):
        for result in (run_client(TP53)[0], run_client(STAR23)[0], not_found_result()):
            self.assertNotIn(
                "clinvar_reviewer_attention",
                _clinical_evidence({"clinvar": result, "clingen": {}}),
                result["match_status"],
            )
            self.assertIsNone(
                export_lims._clinical_database({"clinvar": result, "clingen": {}}).clinvar_reviewer_attention
            )

    def test_it_adds_no_scoring_input(self):
        reference = {
            k: v
            for k, v in _interpret(self.result).items()
            if k.startswith(("acmg_", "confidence_", "priority_", "conflict_"))
        }
        with_unknown = copy.deepcopy(self.result)
        with_unknown["individual_records_conflict"] = False  # the flag alone is the only difference in scoring input
        again = {
            k: v
            for k, v in _interpret(self.result).items()
            if k.startswith(("acmg_", "confidence_", "priority_", "conflict_"))
        }
        self.assertEqual(reference, again)
        self.assertEqual(reference["conflict_score"], 0.0)  # no new conflict-list entry / score from the flag
        self.assertEqual(reference["acmg_classification"], _interpret(not_found_result())["acmg_classification"])

    def test_lims_csv_header_is_still_unchanged(self):
        self.assertEqual(export_lims._CSV_COLUMNS, _base._CSV_COLUMNS_ON_MASTER)


# ---------------------------------------------------------------------------------------------------
# Versioned content hash
# ---------------------------------------------------------------------------------------------------

_V = {"chrom": "17", "pos": 7674252, "ref": "C", "alt": "A"}


def hash_document(clinvar, **candidate):
    clinical = {
        "acmg_classification": {
            "classification": "Uncertain Significance",
            "triggered_criteria": [],
            "not_evaluated_count": 1,
        },
        "evidence_sources": [],
        "context_data_sources": [],
    }
    clinical.update(candidate)
    return {
        "assembly": "GRCh38",
        "variants": [
            {
                "variant": dict(_V),
                "clinvar": copy.deepcopy(clinvar),
                "interpretation_result": {"gene_symbol": "TP53", "acmg_classification": "Uncertain Significance"},
                "candidate_interpretation": clinical,
            }
        ],
    }


def v2(document):
    return compute_content_hash(document, 2)


def v1(document):
    return compute_content_hash(document, 1)


class TestLegacyHashCompatibility(unittest.TestCase):
    def test_documents_with_no_version_hash_to_exactly_what_unmodified_master_produced(self):
        for name, document in LEGACY_HASH_FIXTURE["documents"].items():
            expected = LEGACY_HASH_FIXTURE["master_digests"][name]
            self.assertEqual(compute_content_hash(document), expected, name)
            self.assertEqual(compute_content_hash(document, LEGACY_CONTENT_HASH_VERSION), expected, name)
            self.assertTrue(verify_content_hash(document, expected), name)
            self.assertEqual(content_hash_version_of(document), LEGACY_CONTENT_HASH_VERSION, name)

    def test_declaring_version_one_is_the_same_as_declaring_nothing(self):
        for name, document in LEGACY_HASH_FIXTURE["documents"].items():
            declared = dict(document, content_hash_version=1)
            self.assertEqual(compute_content_hash(declared), LEGACY_HASH_FIXTURE["master_digests"][name], name)

    def test_a_legacy_hash_does_not_see_clinvar_context_but_version_two_does(self):
        document = copy.deepcopy(LEGACY_HASH_FIXTURE["documents"]["matched_with_primary"])
        before1, before2 = v1(document), v2(document)
        document["variants"][0]["clinvar"]["records"][1]["clinical_significance"] = "Benign"  # a non-matching record
        self.assertEqual(v1(document), before1)
        self.assertNotEqual(v2(document), before2)

    def test_the_two_versions_never_collide_on_the_same_document(self):
        for document in LEGACY_HASH_FIXTURE["documents"].values():
            self.assertNotEqual(v1(document), v2(document))

    def test_a_legacy_approved_report_still_passes_the_export_gate_and_status_check(self):
        document = copy.deepcopy(LEGACY_HASH_FIXTURE["documents"]["matched_with_primary"])
        document["review_status"] = "reviewed"
        document["content_hash"] = LEGACY_HASH_FIXTURE["master_digests"]["matched_with_primary"]
        signoff_module.require_reviewed(document, "test consumer")  # raises SignoffError on a mismatch
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, signoff_module.RESULTS_FILENAME), "w", encoding="utf-8") as fh:
                json.dump(document, fh)
            self.assertEqual(signoff_status.inspect_local(tmp).document_hash, "self_consistent")

    def test_an_unknown_or_malformed_declared_version_never_verifies(self):
        document = copy.deepcopy(LEGACY_HASH_FIXTURE["documents"]["matched_with_primary"])
        digest = LEGACY_HASH_FIXTURE["master_digests"]["matched_with_primary"]
        for bad in (0, 3, 99, "2", 2.0, True, [2]):
            document["content_hash_version"] = bad
            self.assertIsNone(content_hash_version_of(document), bad)
            self.assertFalse(verify_content_hash(document, digest), bad)
            with self.assertRaises(ValueError):
                compute_content_hash(document)
        self.assertFalse(verify_content_hash(document, ""))


class TestVersionTwoDistinguishesTheClinVarEvidence(unittest.TestCase):
    def setUp(self):
        self.not_found = not_found_result()
        self.context_only = run_client(STAR23)[0]
        self.one_eligible = run_client(TP53)[0]
        self.conflict = conflict_result()

    def test_the_four_situations_hash_differently_where_the_legacy_hash_could_not_tell_them_apart(self):
        situations = (self.not_found, self.context_only, self.one_eligible, self.conflict)
        legacy = {v1(hash_document(r)) for r in situations}
        current = {v2(hash_document(r)) for r in situations}
        self.assertEqual(len(current), 4)
        # the legacy hash had collapsed every null-primary situation into one digest
        self.assertEqual(len({v1(hash_document(r)) for r in (self.not_found, self.context_only, self.conflict)}), 1)
        self.assertLess(len(legacy), 4)

    def test_no_clinvar_record_versus_a_failed_query(self):
        failed = {"query": None, "found": False, "error": "ClinVar request failed"}
        self.assertNotEqual(v2(hash_document(self.not_found)), v2(hash_document(failed)))
        self.assertNotEqual(v2(hash_document(failed)), v2(hash_document({})))

    def test_deterministic_and_independent_of_record_order(self):
        for result in (self.context_only, self.conflict):
            document = hash_document(result)
            digest = v2(document)
            self.assertEqual(digest, v2(copy.deepcopy(document)))
            shuffled = copy.deepcopy(document)
            clinvar = shuffled["variants"][0]["clinvar"]
            for key in ("records", "matched_records", "context_records"):
                clinvar[key] = list(reversed(clinvar[key]))
            self.assertEqual(digest, v2(shuffled))

    def test_a_change_to_a_context_record_invalidates_the_hash(self):
        base = hash_document(self.context_only)
        digest = v2(base)
        for field, value in (
            ("clinical_significance", "Benign"),
            ("accession", "VCV0000000"),
            ("record_type", "Haplotype"),
            ("review_status", "no assertion criteria provided"),
            ("title", "something else"),
        ):
            changed = copy.deepcopy(base)
            changed["variants"][0]["clinvar"]["context_records"][0][field] = value
            self.assertNotEqual(v2(changed), digest, field)
        removed = copy.deepcopy(base)
        removed["variants"][0]["clinvar"]["context_records"].pop()
        self.assertNotEqual(v2(removed), digest)
        added = copy.deepcopy(base)
        added["variants"][0]["clinvar"]["context_records"].append({"accession": "VCV1", "uid": "1"})
        self.assertNotEqual(v2(added), digest)

    def test_a_change_to_an_eligible_records_identity_or_classification_invalidates_the_hash(self):
        base = hash_document(self.one_eligible)
        digest = v2(base)
        for field, value in (
            ("accession", "VCV0000001"),
            ("uid", "1"),
            ("clinical_significance", "Benign"),
            ("review_status", "no assertion criteria provided"),
            ("last_evaluated", "2001/01/01 00:00"),
            ("condition", ["another condition"]),
        ):
            changed = copy.deepcopy(base)
            changed["variants"][0]["clinvar"]["matched_records"][0][field] = value
            self.assertNotEqual(v2(changed), digest, field)

    def test_conflicting_eligible_submissions_are_attested(self):
        base = hash_document(self.conflict)
        digest = v2(base)
        flipped = copy.deepcopy(base)
        flipped["variants"][0]["clinvar"]["individual_records_conflict"] = False
        self.assertNotEqual(v2(flipped), digest)
        reclassified = copy.deepcopy(base)
        reclassified["variants"][0]["clinvar"]["matched_records"][0]["clinical_significance"] = "Likely benign"
        self.assertNotEqual(v2(reclassified), digest)
        dropped = copy.deepcopy(base)
        dropped["variants"][0]["clinvar"]["matched_records"].pop()
        self.assertNotEqual(v2(dropped), digest)
        promoted = copy.deepcopy(base)
        promoted["variants"][0]["clinvar"]["primary_record"] = copy.deepcopy(
            promoted["variants"][0]["clinvar"]["matched_records"][0]
        )
        self.assertNotEqual(v2(promoted), digest)

    def test_which_eligible_record_is_the_primary_one_is_attested_even_when_the_classification_is_the_same(self):
        entries = _base.TestDerivedCases()._clone(TP53, "VCV000634770", "VCV9999999")  # agrees with the original
        result = run_client(TP53, entries)[0]
        self.assertEqual(len(result["matched_records"]), 2)
        self.assertFalse(result["individual_records_conflict"])
        base = hash_document(result)
        swapped = copy.deepcopy(base)
        clinvar = swapped["variants"][0]["clinvar"]
        other = next(r for r in clinvar["matched_records"] if r["accession"] != clinvar["primary_record"]["accession"])
        clinvar["primary_record"] = copy.deepcopy(other)
        self.assertEqual(v1(swapped), v1(base))  # the legacy content cannot tell the two apart ...
        self.assertNotEqual(v2(swapped), v2(base))  # ... version 2 can

    def test_the_evidence_policy_version_is_attested(self):
        base = hash_document(self.one_eligible)
        changed = copy.deepcopy(base)
        changed["variants"][0]["clinvar"]["evidence_policy"] = "clinvar-single-allele-v2"
        self.assertNotEqual(v2(changed), v2(base))
        self.assertEqual(v1(changed), v1(base))  # the legacy hash never saw the policy
        legacy_shape = copy.deepcopy(base)
        del legacy_shape["variants"][0]["clinvar"]["evidence_policy"]
        self.assertNotEqual(v2(legacy_shape), v2(base))

    def test_non_matching_records_warnings_and_context_data_sources_are_attested(self):
        base = hash_document(run_client(CASES["NBAS_c619CT"])[0])
        digest = v2(base)
        other = copy.deepcopy(base)
        next(r for r in other["variants"][0]["clinvar"]["records"] if r["variant_match"] is False)[
            "clinical_significance"
        ] = "Benign"
        self.assertNotEqual(v2(other), digest)
        warned = copy.deepcopy(base)
        warned["variants"][0]["clinvar"]["record_type_warnings"] = [
            {"accession": "VCV1", "uid": "1", "reason": "unrecognized_record_type"}
        ]
        self.assertNotEqual(v2(warned), digest)
        sources = copy.deepcopy(base)
        sources["variants"][0]["candidate_interpretation"]["context_data_sources"] = ["ClinVar"]
        self.assertNotEqual(v2(sources), digest)

    def test_malformed_clinvar_blocks_still_hash(self):
        for clinvar in (
            None,
            {},
            {"primary_record": None},
            {"records": "x", "matched_records": [None, 3]},
            {"context_records": [1, "a"]},
        ):
            self.assertEqual(
                len(v2(hash_document(clinvar)) if clinvar is not None else v2({"variants": [{"clinvar": None}]})), 64
            )


class TestApprovalIsSealedUnderTheCurrentVersion(unittest.TestCase):
    def _approved_run(self, clinvar_result):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        output_dir = os.path.join(tmp, "run")
        os.makedirs(output_dir)
        document = _make_document()
        document["variants"][0]["clinvar"] = clinvar_result
        document["variants"][0]["candidate_interpretation"]["clinical_evidence"] = _clinical_evidence(
            {"clinvar": clinvar_result, "clingen": {}}
        )
        with open(os.path.join(output_dir, signoff_module.RESULTS_FILENAME), "w", encoding="utf-8") as fh:
            json.dump(document, fh)
        manifest = signoff_module.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
        with open(os.path.join(output_dir, signoff_module.RESULTS_FILENAME), "r", encoding="utf-8") as fh:
            return output_dir, manifest, json.load(fh)

    def test_approve_records_the_version_in_the_document_and_binds_it_through_the_manifest(self):
        for result in (not_found_result(), run_client(STAR23)[0], run_client(TP53)[0], conflict_result()):
            output_dir, manifest, document = self._approved_run(result)
            self.assertEqual(document["content_hash_version"], CONTENT_HASH_VERSION)
            # the manifest key set is a fixed contract (pinned by test_signoff_publication_crash); it binds the
            # document -- version included -- through the results file's SHA-256
            self.assertNotIn("content_hash_version", manifest)
            self.assertEqual(
                manifest["results_json_sha256"],
                signoff_module._sha256_file(os.path.join(output_dir, signoff_module.RESULTS_FILENAME)),
            )
            self.assertEqual(manifest["content_hash"], document["content_hash"])
            self.assertTrue(verify_content_hash(document, document["content_hash"]))
            self.assertEqual(document["content_hash"], compute_content_hash(document, 2))
            signoff_module.require_reviewed(document, "test consumer")

    def test_status_check_reads_an_approved_run_as_self_consistent(self):
        output_dir, _, _ = self._approved_run(conflict_result())
        self.assertEqual(signoff_status.inspect_local(output_dir).document_hash, "self_consistent")

    def test_changing_reviewer_visible_clinvar_information_after_approval_is_refused(self):
        _, _, document = self._approved_run(run_client(STAR23)[0])
        stored = document["content_hash"]
        tampered = copy.deepcopy(document)
        tampered["variants"][0]["clinvar"]["context_records"][0]["clinical_significance"] = "Benign"
        self.assertFalse(verify_content_hash(tampered, stored))
        with self.assertRaises(SignoffError):
            signoff_module.require_reviewed(tampered, "test consumer")
        conflicted = copy.deepcopy(document)
        conflicted["variants"][0]["clinvar"]["individual_records_conflict"] = True
        self.assertFalse(verify_content_hash(conflicted, stored))

    def test_downgrading_the_declared_version_cannot_make_a_tampered_report_verify(self):
        _, _, document = self._approved_run(conflict_result())
        stored = document["content_hash"]
        tampered = copy.deepcopy(document)
        tampered["variants"][0]["clinvar"]["matched_records"][0]["clinical_significance"] = "Benign"
        tampered["content_hash_version"] = 1
        self.assertFalse(verify_content_hash(tampered, stored))
        removed = copy.deepcopy(document)
        del removed["content_hash_version"]
        self.assertFalse(verify_content_hash(removed, stored))  # a v2 digest is not a legacy digest

    def test_reapproving_a_legacy_report_is_an_explicit_new_seal_and_does_not_rewrite_the_old_one(self):
        legacy = copy.deepcopy(LEGACY_HASH_FIXTURE["documents"]["matched_with_primary"])
        legacy_digest = LEGACY_HASH_FIXTURE["master_digests"]["matched_with_primary"]
        resealed = copy.deepcopy(legacy)
        resealed["content_hash_version"] = CONTENT_HASH_VERSION
        resealed["content_hash"] = compute_content_hash(resealed)
        self.assertNotEqual(resealed["content_hash"], legacy_digest)
        self.assertTrue(verify_content_hash(legacy, legacy_digest))  # the old seal is untouched and still verifies
        self.assertEqual(compute_content_hash(legacy), legacy_digest)


if __name__ == "__main__":
    unittest.main()
