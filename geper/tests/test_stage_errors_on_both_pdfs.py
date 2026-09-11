"""
Card HUMAN-TEXT-the-dbSNP-stage-error-sentence-on-both-PDFs (angela's draft
9765c41, wording APPROVED by the human 2026-09-11: "Approved both. They name
the stage, the failure, and nothing more -- the correct scope for a stage
error."). Short-PDF marker RE-RULED the same day: the approved "⚠" was
measured drawing a black square (no font these PDFs use has U+26A0), so the
human replaced it with the word "Warning:" -- "a black square reads as a
rendering fault and invites distrust of the whole document, and embedding a
font is not worth the licence obligation." See tests/test_pdf_glyph_coverage.py
for the sweep that followed.

Before: a per-variant stage failure (`variant_result["errors"]`, e.g.
"dbSNP stage failed: Connection timed out" -- orchestrator.py writes
f"dbSNP stage failed: {detail}") reached the JSON and the Markdown report's
"### ⚠ Stage Warnings / Errors" section, and NEITHER PDF. A clinician
holding a PDF could not see that an evidence stage had failed for that
variant.

Pinned, verbatim, as approved:
  * full PDF, directly under the "Finding N" heading: "Stage Warnings /
    Errors:" then one "• <message>" line per error;
  * short PDF, directly under the result strip: "Warning: <message>".
The message is whatever the failing stage recorded -- the same text the
Markdown prints. NEGATIVE CONTROL: a variant with no stage error renders
neither line.

Checked twice over: at the flowable level (for "directly under", and for
the exact approved strings) and in real PDFs read back with pypdf (for
what a reader actually sees).

EXTRACTION FACT, measured 2026-09-11, that shapes the rendered check:
  * "•": ReportLab writes the bullet as byte 0x7F under its own font
    encoding, which pypdf extracts as "\\x7f". It is the same glyph every
    other bullet in this PDF is drawn with ("• UniProt: ..." extracts the
    same way), so the rendered check compares against that sibling bullet
    rather than hard-coding pypdf's quirk.
"""

import os
import tempfile
import unittest

from pypdf import PdfReader

from report.report_generator import ReportGenerator
from report.summary import _build_stylesheet, _build_variant_section, generate_pdf
from report.summary_short import _build_short_stylesheet, _build_variant_block, generate_short_pdf
from tests.test_disclaimer_consistency import _document

STAGE_ERROR = "dbSNP stage failed: Connection timed out"
FULL_HEADING = "Stage Warnings / Errors:"
FULL_LINE = f"• {STAGE_ERROR}"
SHORT_LINE = f"Warning: {STAGE_ERROR}"


def _doc_with_error_on_first_variant():
    # Two findings: the first carries the stage error, the second is the
    # in-document negative control.
    document = _document(2)
    document["variants"][0]["errors"] = [STAGE_ERROR]
    return document


def _plain(flowable):
    get = getattr(flowable, "getPlainText", None)
    return get() if get else None


def _pdf_lines(render, document):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "out.pdf")
        render(document, path)
        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    return [line.strip() for line in text.splitlines()]


class TestFullPdfFlowables(unittest.TestCase):
    def test_heading_then_bullet_directly_under_finding_heading(self):
        variant_result = _doc_with_error_on_first_variant()["variants"][0]
        texts = [_plain(f) for f in _build_variant_section(1, variant_result, _build_stylesheet())]
        heading_at = next(i for i, t in enumerate(texts) if t and t.startswith("Finding 1:"))
        self.assertEqual(texts[heading_at + 1], FULL_HEADING)
        self.assertEqual(texts[heading_at + 2], FULL_LINE)

    def test_no_error_renders_neither_line(self):
        variant_result = _document(1)["variants"][0]
        self.assertNotIn("errors", variant_result)
        texts = [t for t in (_plain(f) for f in _build_variant_section(1, variant_result, _build_stylesheet())) if t]
        self.assertFalse([t for t in texts if "Stage Warnings" in t or "stage failed" in t], texts)


class TestShortPdfFlowables(unittest.TestCase):
    def test_line_directly_under_result_strip(self):
        from reportlab.platypus import Table

        variant_result = _doc_with_error_on_first_variant()["variants"][0]
        flow = _build_variant_block(1, variant_result, _build_short_stylesheet())
        strip_at = next(i for i, f in enumerate(flow) if isinstance(f, Table))
        after = [t for t in (_plain(f) for f in flow[strip_at + 1 :]) if t is not None]
        self.assertEqual(after[0], SHORT_LINE)

    def test_no_error_renders_no_line(self):
        variant_result = _document(1)["variants"][0]
        texts = [
            t for t in (_plain(f) for f in _build_variant_block(1, variant_result, _build_short_stylesheet())) if t
        ]
        self.assertFalse([t for t in texts if "Warning:" in t or "stage failed" in t], texts)


class TestRenderedPdfs(unittest.TestCase):
    def test_full_pdf_shows_heading_and_bullet_under_finding_1_only(self):
        lines = _pdf_lines(generate_pdf, _doc_with_error_on_first_variant())
        finding_1 = next(i for i, line in enumerate(lines) if line.startswith("Finding 1:"))
        self.assertEqual(lines[finding_1 + 1], FULL_HEADING)
        # Same bullet glyph as the report's other bullets (see module docstring).
        sibling_bullet = next(line for line in lines if line.endswith("UniProt: no entry resolved."))[0]
        self.assertEqual(lines[finding_1 + 2], f"{sibling_bullet} {STAGE_ERROR}")
        # Finding 2 carries no error: exactly one heading and one bullet in the whole PDF.
        self.assertEqual(lines.count(FULL_HEADING), 1)
        self.assertEqual(sum(STAGE_ERROR in line for line in lines), 1)

    def test_short_pdf_shows_warning_line_for_finding_1_only(self):
        lines = _pdf_lines(generate_short_pdf, _doc_with_error_on_first_variant())
        hits = [line for line in lines if STAGE_ERROR in line]
        # Exactly the ruled line, as its own line, once (Finding 2 has no error).
        self.assertEqual(hits, [SHORT_LINE])

    def test_no_stage_error_anywhere_renders_neither_line_on_either_pdf(self):
        for render in (generate_pdf, generate_short_pdf):
            with self.subTest(render=render.__name__):
                text = "\n".join(_pdf_lines(render, _document(2)))
                self.assertNotIn("Stage Warnings / Errors", text)
                self.assertNotIn("stage failed", text)


class TestSameMessageAsMarkdown(unittest.TestCase):
    """The PDFs reuse the Markdown's exact error text -- no second wording."""

    def test_markdown_carries_the_identical_message(self):
        md = ReportGenerator().generate(_doc_with_error_on_first_variant())
        self.assertIn(f"- {STAGE_ERROR}", md.splitlines())


if __name__ == "__main__":
    unittest.main()
