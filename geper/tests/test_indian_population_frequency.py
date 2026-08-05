"""
Tests for the "Indian Population Frequency" report section (India-
deployment feature): `report/clinical_report_builder.py
::_indian_population_frequency` (the shared section data both output
formats render), plus its Markdown (`report/report_generator.py`) and
PDF (`report/summary.py`) renderings.

PDF tests generate real, small PDFs with ReportLab (lightweight, no
model weights -- safe to run locally), following the same pattern
`tests/test_clinician_summary.py` already established.
"""

import os
import tempfile
import unittest
from unittest import mock

from report.clinical_report_builder import _indian_population_frequency, build_clinical_report
from report.report_generator import ReportGenerator
from report.summary import generate_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False


def _ir(**overrides):
    base = {
        "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
        "gene_symbol": "BRCA1",
        "acmg_classification": "Uncertain significance",
        "triggered_rules": [],
        "not_triggered_rules": [],
        "not_evaluated_rules": [],
        "combining_rule_trace": [],
        "supporting_evidence": [],
        "conflicting_evidence": [],
        "ai_consensus": [],
        "ai_context_models": [],
        "confidence_pending": True,
        "priority_pending": True,
        "priority_explanation": [],
        "conflict_list": [],
        "conflict_score": 0.0,
        "recommendations": [],
        "evidence_sources": [],
        "ai_model_errors": [],
    }
    base.update(overrides)
    return base


def _patch_threshold(value=0.01):
    patcher = mock.patch("report.clinical_report_builder.CONFIG")
    fake_config = patcher.start()
    fake_config.indigenomes.COMMON_AF_THRESHOLD = value
    return patcher


class TestIndianPopulationFrequencySection(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)

    def test_gnomad_sas_and_indigenomes_both_present(self):
        _patch_threshold(0.01)
        raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"sas": {"af": 0.02}}}}
        indigenomes = {"skipped": False, "found": True, "af": 0.015, "ac": 30, "an": 2000}
        section = _indian_population_frequency(raw, indigenomes)
        self.assertEqual(section["gnomad_af_sas"], 0.02)
        self.assertEqual(section["indigenomes_af"], 0.015)
        self.assertTrue(section["indigenomes_available"])
        self.assertTrue(section["common_in_indian_population"])

    def test_both_rare_no_common_flag(self):
        _patch_threshold(0.01)
        raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"sas": {"af": 0.0001}}}}
        indigenomes = {"skipped": False, "found": True, "af": 0.0002, "ac": 1, "an": 2000}
        section = _indian_population_frequency(raw, indigenomes)
        self.assertFalse(section["common_in_indian_population"])

    def test_only_indigenomes_common_still_flags(self):
        # gnomAD SAS below threshold, IndiGenomes alone above it --
        # flag must still fire (never requires both sources to agree).
        _patch_threshold(0.01)
        raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"sas": {"af": 0.001}}}}
        indigenomes = {"skipped": False, "found": True, "af": 0.05, "ac": 100, "an": 2000}
        section = _indian_population_frequency(raw, indigenomes)
        self.assertTrue(section["common_in_indian_population"])

    def test_indigenomes_not_found_distinct_from_error(self):
        _patch_threshold(0.01)
        raw = {"gnomad": {}}
        section = _indian_population_frequency(raw, {"skipped": False, "found": False})
        self.assertFalse(section["indigenomes_available"])
        self.assertIsNone(section["indigenomes_error"])
        self.assertIsNone(section["indigenomes_af"])

    def test_indigenomes_error_surfaced(self):
        _patch_threshold(0.01)
        raw = {"gnomad": {}}
        section = _indian_population_frequency(raw, {"skipped": False, "found": False, "error": "timeout"})
        self.assertEqual(section["indigenomes_error"], "timeout")

    def test_indigenomes_skipped_reason_surfaced(self):
        _patch_threshold(0.01)
        raw = {"gnomad": {}}
        section = _indian_population_frequency(
            raw, {"skipped": True, "found": False, "reason": "IndiGenomes is GRCh38-only; ..."}
        )
        self.assertIn("GRCh38-only", section["indigenomes_skipped_reason"])

    def test_indigenomes_result_none_handled(self):
        _patch_threshold(0.01)
        section = _indian_population_frequency({"gnomad": {}}, None)
        self.assertFalse(section["indigenomes_available"])
        self.assertFalse(section["common_in_indian_population"])

    def test_gnomad_missing_sas_entry(self):
        _patch_threshold(0.01)
        raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"eas": {"af": 0.3}}}}
        section = _indian_population_frequency(raw, None)
        self.assertIsNone(section["gnomad_af_sas"])
        self.assertFalse(section["common_in_indian_population"])  # eas doesn't count toward the Indian-population flag


class TestBuildClinicalReportIncludesSection(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)

    def test_section_present_in_full_clinical_report(self):
        _patch_threshold(0.01)
        raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"sas": {"af": 0.02}}}}
        cr = build_clinical_report(
            _ir(), raw_evidence=raw, indigenomes_result={"skipped": False, "found": True, "af": 0.03}
        )
        self.assertIn("indian_population_frequency", cr)
        self.assertTrue(cr["indian_population_frequency"]["common_in_indian_population"])


class TestMarkdownRendering(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)

    def _document(self, indigenomes_result):
        _patch_threshold(0.01)
        raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"sas": {"af": 0.02}}}}
        cr = build_clinical_report(_ir(), raw_evidence=raw, indigenomes_result=indigenomes_result)
        return {
            "geper_version": "t",
            "generated_at": "2026-01-01T00:00:00Z",
            "input_vcf": "x.vcf",
            "assembly": "GRCh38",
            "vcf_samples": [],
            "variant_count": 1,
            "variants": [
                {
                    "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
                    "interpretation": {},
                    "clinical_report": cr,
                    "ai_model_status": {},
                    "errors": [],
                }
            ],
        }

    def test_section_11_present_with_both_sources(self):
        doc = self._document({"skipped": False, "found": True, "af": 0.015, "ac": 30, "an": 2000})
        md = ReportGenerator().generate(doc)
        self.assertIn("### 11. Indian Population Frequency", md)
        self.assertIn("gnomAD (South Asian, SAS)", md)
        self.assertIn("IndiGenomes", md)
        self.assertIn("Common in Indian populations", md)

    def test_subsequent_sections_renumbered_correctly(self):
        doc = self._document({"skipped": False, "found": False})
        md = ReportGenerator().generate(doc)
        self.assertIn("### 12. Clinical Evidence", md)
        self.assertIn("### 13. Sequence Context", md)
        self.assertIn("### 14. Recommendations", md)
        self.assertIn("### 15. Limitations", md)
        self.assertIn("### 16. References", md)
        self.assertIn("### 17. Evidence Sources", md)

    def test_indigenomes_not_found_renders_plainly(self):
        doc = self._document({"skipped": False, "found": False})
        md = ReportGenerator().generate(doc)
        self.assertIn("IndiGenomes:** variant not found", md)


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestPdfRendering(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)

    def test_section_appears_in_generated_pdf(self):
        _patch_threshold(0.01)
        raw = {"gnomad": {"skipped": False, "found": True, "population_breakdown": {"sas": {"af": 0.02}}}}
        cr = build_clinical_report(
            _ir(),
            raw_evidence=raw,
            indigenomes_result={"skipped": False, "found": True, "af": 0.015, "ac": 30, "an": 2000},
        )
        document = {
            "geper_version": "t",
            "generated_at": "2026-01-01T00:00:00Z",
            "input_vcf": "x.vcf",
            "assembly": "GRCh38",
            "vcf_samples": ["S1"],
            "variant_count": 1,
            "variants": [
                {
                    "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
                    "interpretation": {},
                    "clinical_report": cr,
                    "ai_model_status": {},
                    "errors": [],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.pdf")
            generate_pdf(document, out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self.assertIn("Indian Population Frequency", text)
        self.assertIn("IndiGenomes", text)
        self.assertIn("Common in Indian populations", text)

    def test_section_omitted_when_no_data_at_all(self):
        # Neither gnomAD SAS nor IndiGenomes has anything -- the section
        # must render nothing (no empty header noise).
        _patch_threshold(0.01)
        raw = {"gnomad": {"skipped": True}}
        cr = build_clinical_report(_ir(), raw_evidence=raw, indigenomes_result=None)
        document = {
            "geper_version": "t",
            "generated_at": "2026-01-01T00:00:00Z",
            "input_vcf": "x.vcf",
            "assembly": "GRCh38",
            "vcf_samples": ["S1"],
            "variant_count": 1,
            "variants": [
                {
                    "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
                    "interpretation": {},
                    "clinical_report": cr,
                    "ai_model_status": {},
                    "errors": [],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.pdf")
            generate_pdf(document, out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self.assertNotIn("Indian Population Frequency", text)


if __name__ == "__main__":
    unittest.main()
