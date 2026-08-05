"""
Tests for the mandatory ICMR-style AI-disclosure footer
(`report/summary.py::_icmr_ai_disclosure_footer_text` /
`_NumberedCanvas`), required on every page of both the full clinical
PDF (`report/summary.py::generate_pdf`) and its short-form companion
(`report/summary_short.py::generate_short_pdf`) -- both share
`_NumberedCanvas`, so one fix covers both output formats.

Real, small PDFs are generated with ReportLab (lightweight, no model
weights -- safe to run locally) and inspected with pypdf, following the
same pattern `tests/test_clinician_summary.py`/`tests/test_summary_pdf_logo.py`
already established.
"""

import os
import tempfile
import unittest

from report.summary import _ICMR_DRAFT_FOOTER_TEXT, _icmr_ai_disclosure_footer_text, generate_pdf
from report.summary_short import generate_short_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False


def _clinical(classification="Uncertain significance"):
    return {
        "executive_summary": "Padding finding " + ("lorem ipsum dolor sit amet. " * 20),
        "acmg_classification": {"classification": classification, "triggered_criteria": []},
        "confidence": {"pending": True},
        "supporting_evidence": [f"Evidence line {j}" for j in range(6)],
        "limitations": [f"Limitation line {j}" for j in range(4)],
        "evidence_sources": [],
        "conflict_resolution": {"severity": None},
    }


def _document(n_variants: int, gene_prefix: str = "GENE") -> dict:
    return {
        "geper_version": "test",
        "generated_at": "2026-08-06T00:00:00+00:00",
        "input_vcf": "icmr_footer_test.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["SAMPLE01"],
        "variant_count": n_variants,
        "variants": [
            {
                "variant": {"chrom": str(i + 1), "pos": 1000 + i, "ref": "C", "alt": "A"},
                "interpretation_result": {"gene_symbol": f"{gene_prefix}{i + 1}"},
                "clinical_report": _clinical(),
            }
            for i in range(n_variants)
        ],
    }


def _all_page_texts(path: str):
    reader = PdfReader(path)
    return [page.extract_text() for page in reader.pages]


class TestFooterTextFunction(unittest.TestCase):
    def test_no_physician_returns_draft_text(self):
        self.assertEqual(_icmr_ai_disclosure_footer_text({"physician": None}), _ICMR_DRAFT_FOOTER_TEXT)

    def test_empty_patient_dict_returns_draft_text(self):
        self.assertEqual(_icmr_ai_disclosure_footer_text({}), _ICMR_DRAFT_FOOTER_TEXT)

    def test_none_patient_returns_draft_text(self):
        self.assertEqual(_icmr_ai_disclosure_footer_text(None), _ICMR_DRAFT_FOOTER_TEXT)

    def test_physician_present_produces_reviewed_text(self):
        text = _icmr_ai_disclosure_footer_text({"physician": "Dr. A. Sharma, MD"})
        self.assertIn("AI-assisted genomic interpretation", text)
        self.assertIn("Dr. A. Sharma, MD", text)
        self.assertIn("This is not a standalone diagnosis.", text)
        self.assertNotIn("DRAFT", text)

    def test_reviewed_text_never_fabricates_bracket_placeholders(self):
        # GEPER has no registration-number/hospital-name field -- the
        # rendered text must never contain literal bracket placeholders
        # that could be mistaken for real data.
        text = _icmr_ai_disclosure_footer_text({"physician": "Dr. A. Sharma, MD"})
        self.assertNotIn("[", text)
        self.assertNotIn("]", text)


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestFullReportFooterOnEveryPage(unittest.TestCase):
    def test_draft_footer_on_every_page_without_patient_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_document(15), out)  # 15 variants -> forces a multi-page PDF
            texts = _all_page_texts(out)
            self.assertGreaterEqual(len(texts), 2, "test setup should have produced a multi-page PDF")
            for i, text in enumerate(texts):
                self.assertIn("DRAFT", text, f"page {i} missing DRAFT footer")
                self.assertIn("Awaiting clinical review", text, f"page {i} missing DRAFT footer")

    def test_reviewed_footer_on_every_page_with_patient_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(
                _document(15), out, patient_meta={"patient_name": "Jane Doe", "physician": "Dr. A. Sharma, MD"}
            )
            texts = _all_page_texts(out)
            self.assertGreaterEqual(len(texts), 2, "test setup should have produced a multi-page PDF")
            for i, text in enumerate(texts):
                self.assertIn("AI-assisted genomic interpretation", text, f"page {i} missing reviewed footer")
                self.assertIn("Dr. A. Sharma", text, f"page {i} missing reviewed footer")
                self.assertIn("not a standalone diagnosis", text, f"page {i} missing reviewed footer")
                self.assertNotIn("DRAFT", text, f"page {i} incorrectly shows DRAFT footer despite patient_meta")

    def test_patient_meta_with_no_usable_name_still_gets_draft_footer(self):
        # No usable patient_name -> _parse_patient_meta falls back to
        # the de-identified default (physician is dropped too, per its
        # documented "treat exactly like absent" behavior) -- footer
        # must still be the DRAFT warning, not the reviewed text.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_document(2), out, patient_meta={"physician": "Dr. A. Sharma, MD"})
            texts = _all_page_texts(out)
            for text in texts:
                self.assertIn("DRAFT", text)

    def test_corrupt_patient_meta_file_still_gets_draft_footer(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = os.path.join(tmp, "corrupt.json")
            with open(bad_path, "w", encoding="utf-8") as fh:
                fh.write("{not valid json")
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_document(2), out, patient_meta=bad_path)
            for text in _all_page_texts(out):
                self.assertIn("DRAFT", text)


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestShortReportFooterOnEveryPage(unittest.TestCase):
    def test_draft_footer_on_every_page_without_patient_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(_document(8), out)  # matches test_summary_short.py's own "force 2 pages" count
            texts = _all_page_texts(out)
            self.assertGreaterEqual(len(texts), 2, "test setup should have produced a multi-page PDF")
            for i, text in enumerate(texts):
                self.assertIn("DRAFT", text, f"page {i} missing DRAFT footer")
                self.assertIn("Awaiting clinical review", text, f"page {i} missing DRAFT footer")

    def test_reviewed_footer_on_every_page_with_patient_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(
                _document(8), out, patient_meta={"patient_name": "Jane Doe", "physician": "Dr. A. Sharma, MD"}
            )
            texts = _all_page_texts(out)
            self.assertGreaterEqual(len(texts), 2, "test setup should have produced a multi-page PDF")
            for i, text in enumerate(texts):
                self.assertIn("AI-assisted genomic interpretation", text, f"page {i} missing reviewed footer")
                self.assertIn("Dr. A. Sharma", text, f"page {i} missing reviewed footer")
                self.assertNotIn("DRAFT", text, f"page {i} incorrectly shows DRAFT footer despite patient_meta")

    def test_single_variant_calling_convention_still_gets_footer(self):
        # generate_short_pdf also accepts one variant_result dict
        # directly (no top-level "variants" key) -- confirm the footer
        # still renders under that calling convention too.
        single = _document(1)["variants"][0]
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(single, out)
            texts = _all_page_texts(out)
            self.assertTrue(texts)
            for text in texts:
                self.assertIn("DRAFT", text)


if __name__ == "__main__":
    unittest.main()
