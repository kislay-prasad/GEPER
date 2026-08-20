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

    def test_physician_alone_without_document_returns_draft_text(self):
        # Corrected contract (card review-status-two-sources-of-truth):
        # `patient["physician"]` is attribution only. Without a
        # `document` to supply `review_status`, the fail-safe direction
        # is always DRAFT -- a physician string alone must never be
        # able to assert "reviewed" by itself.
        text = _icmr_ai_disclosure_footer_text({"physician": "Dr. A. Sharma, MD"})
        self.assertEqual(text, _ICMR_DRAFT_FOOTER_TEXT)

    def test_reviewed_document_produces_reviewed_text(self):
        document = {"review_status": "reviewed", "reviewed_by": "Dr. A. Sharma, MD"}
        text = _icmr_ai_disclosure_footer_text({"physician": "Dr. A. Sharma, MD"}, document)
        self.assertIn("AI-assisted genomic interpretation", text)
        self.assertIn("Dr. A. Sharma, MD", text)
        self.assertIn("This is not a standalone diagnosis.", text)
        self.assertNotIn("DRAFT", text)

    def test_reviewed_document_falls_back_to_patient_physician_when_reviewed_by_missing(self):
        # `document["reviewed_by"]` is the primary name source once
        # review_status="reviewed" establishes a sign-off exists;
        # `patient["physician"]` is only a fallback for the NAME at
        # that point, never for the review state itself.
        document = {"review_status": "reviewed"}
        text = _icmr_ai_disclosure_footer_text({"physician": "Dr. A. Sharma, MD"}, document)
        self.assertIn("Dr. A. Sharma, MD", text)
        self.assertNotIn("DRAFT", text)

    def test_reviewed_document_without_any_reviewer_name_omits_name(self):
        # Signed off, but no name recorded anywhere -- must not invent
        # one, and must not fall back to DRAFT either (that would deny
        # a sign-off that genuinely happened).
        document = {"review_status": "reviewed"}
        text = _icmr_ai_disclosure_footer_text({}, document)
        self.assertIn("reviewed and signed off", text)
        self.assertNotIn("DRAFT", text)

    def test_reviewed_text_never_fabricates_bracket_placeholders(self):
        # GEPER has no registration-number/hospital-name field -- the
        # rendered text must never contain literal bracket placeholders
        # that could be mistaken for real data. Needs an actual
        # reviewed document -- physician alone no longer reaches the
        # reviewed-text branch at all under the corrected contract.
        document = {"review_status": "reviewed", "reviewed_by": "Dr. A. Sharma, MD"}
        text = _icmr_ai_disclosure_footer_text({"physician": "Dr. A. Sharma, MD"}, document)
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

    def test_reviewed_footer_on_every_page_after_signoff(self):
        # Corrected contract: the reviewed footer requires
        # document["review_status"] == "reviewed" (a real sign-off
        # record) -- patient_meta alone can no longer produce it.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            document = _document(15)
            document["review_status"] = "reviewed"
            document["reviewed_by"] = "Dr. A. Sharma, MD"
            generate_pdf(document, out, patient_meta={"patient_name": "Jane Doe", "physician": "Dr. A. Sharma, MD"})
            texts = _all_page_texts(out)
            self.assertGreaterEqual(len(texts), 2, "test setup should have produced a multi-page PDF")
            for i, text in enumerate(texts):
                self.assertIn("AI-assisted genomic interpretation", text, f"page {i} missing reviewed footer")
                self.assertIn("Dr. A. Sharma", text, f"page {i} missing reviewed footer")
                self.assertIn("not a standalone diagnosis", text, f"page {i} missing reviewed footer")
                self.assertNotIn("DRAFT", text, f"page {i} incorrectly shows DRAFT footer despite sign-off")

    def test_patient_meta_alone_no_longer_produces_reviewed_footer(self):
        # THE vulnerability this contract fixes: physician/patient_meta
        # alone, with no document["review_status"] sign-off, must NOT
        # produce a reviewed-looking footer -- DRAFT must stay.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(
                _document(15), out, patient_meta={"patient_name": "Jane Doe", "physician": "Dr. A. Sharma, MD"}
            )
            texts = _all_page_texts(out)
            self.assertGreaterEqual(len(texts), 2, "test setup should have produced a multi-page PDF")
            for i, text in enumerate(texts):
                self.assertIn("DRAFT", text, f"page {i} incorrectly shows reviewed footer from patient_meta alone")
                # "Dr. A. Sharma" alone legitimately appears in the
                # "Referring Physician" identity block on every page --
                # that's attribution, not a review claim (see
                # `_icmr_ai_disclosure_footer_text`'s docstring). Only
                # the FOOTER SENTENCE itself must not claim review.
                self.assertNotIn(
                    "reviewed by Dr. A. Sharma", text, f"page {i} footer incorrectly claims review with no sign-off"
                )

    def test_no_usable_name_and_no_physician_still_gets_draft_footer(self):
        # An empty/insufficient patient_meta (no patient_name, no
        # physician either) must still produce the DRAFT footer -- this
        # is the genuine "nothing to review yet" state.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_document(2), out, patient_meta={})
            texts = _all_page_texts(out)
            for text in texts:
                self.assertIn("DRAFT", text)

    def test_deidentified_run_with_signoff_gets_reviewed_footer(self):
        # `physician` is intentionally decoupled from `patient_name`
        # (see `_parse_patient_meta`'s docstring): a de-identified/
        # research sample -- GEPER's stated default -- can still be
        # reviewed and signed off by a named clinician without a
        # patient name ever being attached. This is the exact
        # behavior `geper/review/signoff.py`'s `approve` command
        # depends on -- approve() always writes review_status=
        # "reviewed" AND supplies patient_meta={"physician": ...}
        # together (never physician alone), so that's what's exercised
        # here under the corrected contract.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            document = _document(2)
            document["review_status"] = "reviewed"
            document["reviewed_by"] = "Dr. A. Sharma, MD"
            generate_pdf(document, out, patient_meta={"physician": "Dr. A. Sharma, MD"})
            texts = _all_page_texts(out)
            for i, text in enumerate(texts):
                self.assertIn("AI-assisted genomic interpretation", text, f"page {i} missing reviewed footer")
                self.assertIn("Dr. A. Sharma", text, f"page {i} missing reviewed footer")
                self.assertNotIn("DRAFT", text, f"page {i} incorrectly shows DRAFT footer despite sign-off")

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

    def test_reviewed_footer_on_every_page_after_signoff(self):
        # Corrected contract: the reviewed footer requires
        # document["review_status"] == "reviewed" (a real sign-off
        # record) -- patient_meta alone can no longer produce it.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            document = _document(8)
            document["review_status"] = "reviewed"
            document["reviewed_by"] = "Dr. A. Sharma, MD"
            generate_short_pdf(
                document, out, patient_meta={"patient_name": "Jane Doe", "physician": "Dr. A. Sharma, MD"}
            )
            texts = _all_page_texts(out)
            self.assertGreaterEqual(len(texts), 2, "test setup should have produced a multi-page PDF")
            for i, text in enumerate(texts):
                self.assertIn("AI-assisted genomic interpretation", text, f"page {i} missing reviewed footer")
                self.assertIn("Dr. A. Sharma", text, f"page {i} missing reviewed footer")
                self.assertNotIn("DRAFT", text, f"page {i} incorrectly shows DRAFT footer despite sign-off")

    def test_patient_meta_alone_no_longer_produces_reviewed_footer(self):
        # THE vulnerability this contract fixes: physician/patient_meta
        # alone, with no document["review_status"] sign-off, must NOT
        # produce a reviewed-looking footer -- DRAFT must stay.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(
                _document(8), out, patient_meta={"patient_name": "Jane Doe", "physician": "Dr. A. Sharma, MD"}
            )
            texts = _all_page_texts(out)
            self.assertGreaterEqual(len(texts), 2, "test setup should have produced a multi-page PDF")
            for i, text in enumerate(texts):
                self.assertIn("DRAFT", text, f"page {i} incorrectly shows reviewed footer from patient_meta alone")
                # "Dr. A. Sharma" alone legitimately appears in the
                # "Referring Physician" identity block on every page --
                # that's attribution, not a review claim (see
                # `_icmr_ai_disclosure_footer_text`'s docstring). Only
                # the FOOTER SENTENCE itself must not claim review.
                self.assertNotIn(
                    "reviewed by Dr. A. Sharma", text, f"page {i} footer incorrectly claims review with no sign-off"
                )

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
