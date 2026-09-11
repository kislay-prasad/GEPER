"""
DEFECT-BOTH-PDFs-Report-Generated-ALWAYS-SHOWS-THE-RENDER-MOMENT-never-the-documents-own-
generated_at-and-Markdown-gets-it-right.

Both PDF renderers (`report/summary.py`, `report/summary_short.py`) printed "Report Generated"
as `format_ist(datetime.now(timezone.utc))`, unconditionally -- the document's own persisted
`generated_at` was never consulted at all. Markdown (`report/report_generator.py:275`) already
reads it correctly and falls back to the literal marker "Not available" when it is genuinely
missing. Ruled 2026-09-11 (#17): BOTH (a) read the persisted `generated_at` with the same
"Not available" marker, AND (b) relabel the render-moment value to say what it actually measures
("PDF Rendered", not "Report Generated") -- these are two different facts, not alternatives.

#20 folds into this card: `_derive_run_id`'s fallback (`report/summary.py:489`) fabricated
`datetime.now()` when `generated_at` was absent, so re-rendering the SAME already-written
document with no `generated_at` produced a DIFFERENT Run ID each time -- the same fabrication
family as the date field. Fixed to a fixed marker instead of a fabricated timestamp.

Real PDFs, extracted via pypdf, on the CI-parity interpreter -- not asserted from reading the
source alone.
"""

import os
import re
import tempfile
import unittest
from datetime import datetime, timezone

from report.summary import _derive_run_id, generate_pdf
from report.summary_short import generate_short_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False


def _minimal_document(generated_at=None):
    doc = {
        "geper_version": "test",
        "input_vcf": "test.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["SAMPLE01"],
        "variant_count": 1,
        "variants": [
            {
                "variant": {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"},
                "candidate_interpretation": {
                    "executive_summary": "Test summary for generated_at/PDF-rendered test.",
                    "acmg_classification": {"classification": "Pathogenic", "triggered_criteria": []},
                    "confidence": {"pending": False, "label": "High"},
                    "supporting_evidence": [],
                    "limitations": [],
                },
            }
        ],
    }
    if generated_at is not None:
        doc["generated_at"] = generated_at
    return doc


def _extract_pdf_text(path: str) -> str:
    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestFullPdfReportGeneratedReadsThePersistedValue(unittest.TestCase):
    def test_old_document_shows_its_own_date_not_todays(self):
        """A document generated 2025-01-01 and re-rendered TODAY must still say 2025-01-01
        next to 'Report Generated' -- the persisted value, not the render moment."""
        doc = _minimal_document(generated_at="2025-01-01T00:00:00+00:00")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "full.pdf")
            generate_pdf(doc, out)
            text = _extract_pdf_text(out)
        self.assertIn("2025", text, "the persisted generated_at year must appear somewhere in the PDF")
        today_year = str(datetime.now(timezone.utc).year)
        # "Report Generated" must be paired with 2025, not with today's year, unless they
        # happen to coincide (not the case for this fixture: 2025 vs whatever today's run year is).
        m = re.search(r"Report Generated\s*[:\n]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}\s+\w+\s+[0-9]{4})", text)
        self.assertIsNotNone(m, f"could not find a 'Report Generated' date in the PDF text: {text!r}")
        self.assertIn("2025", m.group(0))
        if today_year != "2025":
            self.assertNotIn(today_year, m.group(0))

    def test_document_with_no_generated_at_shows_the_marker_not_todays_date(self):
        """No persisted generated_at at all -- must render the same 'Not available' marker
        Markdown already uses, never a fabricated 'now'."""
        doc = _minimal_document(generated_at=None)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "full.pdf")
            generate_pdf(doc, out)
            text = _extract_pdf_text(out)
        m = re.search(r"Report Generated\s*[:\n]?\s*(.+)", text)
        self.assertIsNotNone(m)
        self.assertIn("Not available", text)

    def test_pdf_rendered_field_shows_todays_date_honestly_labelled(self):
        """The render-moment fact still belongs on the page -- just under its own honest
        label, not borrowing 'Report Generated'."""
        doc = _minimal_document(generated_at="2025-01-01T00:00:00+00:00")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "full.pdf")
            generate_pdf(doc, out)
            text = _extract_pdf_text(out)
        self.assertIn("PDF Rendered", text)
        today_year = str(datetime.now(timezone.utc).year)
        m = re.search(r"PDF Rendered\s*[:\n]?\s*(.+)", text)
        self.assertIsNotNone(m)
        self.assertIn(today_year, m.group(0))


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestShortPdfReportGeneratedReadsThePersistedValue(unittest.TestCase):
    def test_old_document_shows_its_own_date_not_todays(self):
        doc = _minimal_document(generated_at="2025-01-01T00:00:00+00:00")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(doc, out)
            text = _extract_pdf_text(out)
        m = re.search(r"Report Generated\s*[:\n]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}\s+\w+\s+[0-9]{4})", text)
        self.assertIsNotNone(m, f"could not find a 'Report Generated' date in the PDF text: {text!r}")
        self.assertIn("2025", m.group(0))
        today_year = str(datetime.now(timezone.utc).year)
        if today_year != "2025":
            self.assertNotIn(today_year, m.group(0))

    def test_document_with_no_generated_at_shows_the_marker_not_todays_date(self):
        doc = _minimal_document(generated_at=None)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(doc, out)
            text = _extract_pdf_text(out)
        self.assertIn("Not available", text)

    def test_pdf_rendered_field_shows_todays_date_honestly_labelled(self):
        doc = _minimal_document(generated_at="2025-01-01T00:00:00+00:00")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(doc, out)
            text = _extract_pdf_text(out)
        self.assertIn("PDF Rendered", text)
        today_year = str(datetime.now(timezone.utc).year)
        m = re.search(r"PDF Rendered\s*[:\n]?\s*(.+)", text)
        self.assertIsNotNone(m)
        self.assertIn(today_year, m.group(0))


class TestDeriveRunIdNoLongerFabricatesFromNow(unittest.TestCase):
    """#20, folded into #17: same fabrication family as the date field."""

    def test_missing_generated_at_returns_a_fixed_marker_not_a_timestamp(self):
        """Before the fix: `_derive_run_id({}, None)` embedded `datetime.now()`, so calling
        it twice for the SAME already-written document (no generated_at) produced two
        DIFFERENT run ids. After the fix: deterministic marker, same both times."""
        first = _derive_run_id({}, None)
        second = _derive_run_id({}, None)
        self.assertEqual(first, second, "a missing generated_at must not produce a different run id each call")
        self.assertFalse(
            re.search(r"\d{8}T?\d{4,6}", first),
            f"run id looks timestamp-shaped (fabricated from now()), not a fixed marker: {first!r}",
        )

    def test_present_generated_at_is_unaffected(self):
        """Control: the fix must not touch the already-correct path."""
        self.assertEqual(
            _derive_run_id({"generated_at": "2026-08-31T10:00:00+00:00"}, None),
            "BIJ-RUN-20260831T10000",
        )

    def test_explicit_run_id_still_passes_through_unchanged(self):
        """Control: unrelated to this fix at all."""
        self.assertEqual(_derive_run_id({}, "LAB-2026-00042"), "LAB-2026-00042")


if __name__ == "__main__":
    unittest.main()
