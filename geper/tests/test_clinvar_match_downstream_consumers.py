"""
Tests that every downstream consumer of `ClinVarClient.query_variant`'s
output reads `primary_record`/`match_status`, not a bare `records[0]`
-- covering the consumers `tests/test_clinvar_client.py` doesn't reach
directly: `pipeline/confidence_engine.py`,
`pipeline/conflict_resolution_engine.py`,
`pipeline/prioritization_engine.py`, `pipeline/acmg_rules.py`'s
`_bp6`/`_clinvar_crossref`, and `report/clinical_report_builder.py`.

Each test constructs a `clinvar_result` dict in the shape
`database/clinvar_client.py::ClinVarClient.query_variant` actually
returns (not the pre-fix `{"records": [...]}` shape), with a
`MATCHED` case (one allele-matched record among several co-located
ones) and a `POSITION_ONLY` case (co-located records exist, none
match) -- the exact two states that used to be conflated into "use
records[0] either way".
"""

import unittest

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.confidence_engine import ConfidenceEngine
from pipeline.conflict_resolution_engine import ConflictResolutionEngine
from pipeline.prioritization_engine import PrioritizationEngine
from report.clinical_report_builder import _clinical_evidence


# The exact wrong-record-would-be-picked shape: three co-located BRCA1
# records, only the last one an allele match (mirrors the real
# 17:43094298 case -- see database/clinvar_client.py's module docstring).
_WRONG_TOP = {
    "accession": "VCV000054169", "clinical_significance": "Pathogenic",
    "review_status": "reviewed by expert panel", "variant_match": False,
}
_CORRECT_MATCH = {
    "accession": "VCV000041804", "clinical_significance": "Benign",
    "review_status": "reviewed by expert panel", "last_evaluated": "2024/06/11 00:00",
    "variant_match": True,
}
_ANOTHER_NON_MATCH = {
    "accession": "VCV000619783", "clinical_significance": "Conflicting classifications of pathogenicity",
    "review_status": "criteria provided, conflicting classifications", "variant_match": False,
}

_MATCHED_RESULT = {
    "found": True,
    "match_status": "matched",
    "record_count": 3,
    "matched_record_count": 1,
    "records": [_ANOTHER_NON_MATCH, _WRONG_TOP, _CORRECT_MATCH],
    "matched_records": [_CORRECT_MATCH],
    "primary_record": _CORRECT_MATCH,
}

_POSITION_ONLY_RESULT = {
    "found": False,
    "match_status": "position_only",
    "record_count": 2,
    "matched_record_count": 0,
    "records": [_ANOTHER_NON_MATCH, _WRONG_TOP],
    "matched_records": [],
    "primary_record": None,
}

_NOT_FOUND_RESULT = {
    "found": False, "match_status": "not_found", "record_count": 0,
    "matched_record_count": 0, "records": [], "matched_records": [], "primary_record": None,
}


class TestConfidenceEngine(unittest.TestCase):
    def test_matched_uses_primary_record_not_wrong_top(self):
        cat = ConfidenceEngine._clinical_quality(_MATCHED_RESULT, None, 3.0)
        self.assertIn("ClinVar", cat.sources_checked)
        # Benign/expert-panel primary_record -> high review-status quality tier.
        self.assertEqual(cat.quality, 1.0)
        self.assertNotIn("VCV000054169", cat.rationale)

    def test_position_only_does_not_score_the_unrelated_record(self):
        cat = ConfidenceEngine._clinical_quality(_POSITION_ONLY_RESULT, None, 3.0)
        self.assertNotIn("ClinVar", cat.sources_checked)
        self.assertEqual(cat.presence, 0.0)
        self.assertIn("No ClinVar record found for this exact variant", cat.rationale)

    def test_not_found_reports_plain_no_record(self):
        cat = ConfidenceEngine._clinical_quality(_NOT_FOUND_RESULT, None, 3.0)
        self.assertNotIn("ClinVar", cat.sources_checked)
        self.assertIn("No ClinVar record found for this variant.", cat.rationale)


class TestConflictResolutionEngine(unittest.TestCase):
    def test_clinical_conflict_uses_primary_record(self):
        clingen_result = {"found": True, "gene_symbol": "BRCA1", "clinical_validity_summary": "Disputed"}
        # primary_record is Benign -> "pathogenic" not in sig -> no conflict item, regardless of the
        # unrelated co-located Pathogenic record that would have triggered one under the old bug.
        item = ConflictResolutionEngine._clinical_conflict(_MATCHED_RESULT, clingen_result)
        self.assertIsNone(item)

    def test_position_only_never_reads_the_unrelated_pathogenic_record(self):
        clingen_result = {"found": True, "gene_symbol": "BRCA1", "clinical_validity_summary": "Disputed"}
        item = ConflictResolutionEngine._clinical_conflict(_POSITION_ONLY_RESULT, clingen_result)
        self.assertIsNone(item)

    def test_population_conflict_reads_primary_record_significance(self):
        gnomad_result = {"found": True, "global_af": 0.02}
        item = ConflictResolutionEngine._population_conflict("Uncertain Significance", _MATCHED_RESULT, gnomad_result)
        # primary_record is Benign, not pathogenic -> clinvar_pathogenic False;
        # acmg_classification also not pathogenic-leaning -> no conflict item.
        self.assertIsNone(item)


class TestPrioritizationEngine(unittest.TestCase):
    def test_matched_uses_primary_record(self):
        reasons = []
        factor = PrioritizationEngine._clinical_factor(_MATCHED_RESULT, None, 2.0, reasons)
        self.assertIn("Benign", factor.rationale)
        self.assertNotIn("Pathogenic", factor.rationale)

    def test_position_only_does_not_leak_unrelated_pathogenic_note(self):
        reasons = []
        factor = PrioritizationEngine._clinical_factor(_POSITION_ONLY_RESULT, None, 2.0, reasons)
        self.assertNotIn("Pathogenic", factor.rationale)
        self.assertIn("No ClinVar record found for this exact variant", factor.rationale)


class TestAcmgRulesBp6AndCrossref(unittest.TestCase):
    def test_bp6_triggers_on_correctly_matched_benign_record(self):
        result = ACMGRuleEngine._bp6(_MATCHED_RESULT)
        self.assertEqual(result.status, "triggered")
        self.assertIn("VCV000041804", result.rationale)
        self.assertNotIn("VCV000054169", result.rationale)

    def test_bp6_position_only_is_not_evaluated_with_context_in_rationale(self):
        result = ACMGRuleEngine._bp6(_POSITION_ONLY_RESULT)
        self.assertEqual(result.status, "not_evaluated")
        self.assertIn("2 other", result.rationale)

    def test_bp6_not_found_is_not_evaluated(self):
        result = ACMGRuleEngine._bp6(_NOT_FOUND_RESULT)
        self.assertEqual(result.status, "not_evaluated")

    def test_clinvar_crossref_matched(self):
        crossref = ACMGRuleEngine._clinvar_crossref(_MATCHED_RESULT)
        self.assertTrue(crossref["available"])
        self.assertEqual(crossref["accession"], "VCV000041804")
        self.assertEqual(crossref["clinical_significance"], "Benign")

    def test_clinvar_crossref_position_only_surfaces_co_located_context(self):
        crossref = ACMGRuleEngine._clinvar_crossref(_POSITION_ONLY_RESULT)
        self.assertFalse(crossref["available"])
        self.assertEqual(len(crossref["co_located_other_variants"]), 2)
        accessions = {v["accession"] for v in crossref["co_located_other_variants"]}
        self.assertEqual(accessions, {"VCV000054169", "VCV000619783"})


class TestClinicalReportBuilder(unittest.TestCase):
    def test_matched_surfaces_correct_record(self):
        out = _clinical_evidence({"clinvar": _MATCHED_RESULT, "clingen": {}})
        self.assertTrue(out["clinvar_available"])
        self.assertEqual(out["clinvar"]["clinical_significance"], "Benign")

    def test_position_only_reports_co_located_count_not_a_classification(self):
        out = _clinical_evidence({"clinvar": _POSITION_ONLY_RESULT, "clingen": {}})
        self.assertFalse(out["clinvar_available"])
        self.assertNotIn("clinvar", out)
        self.assertEqual(out["clinvar_co_located_count"], 2)


if __name__ == "__main__":
    unittest.main()
