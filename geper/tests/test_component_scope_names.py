"""
Card DEFECT-two-component-trees-each-self-identify-as-the-WHOLE-product-
under-a-ratified-combined-name -- RULED Option B, 2026-09-11.

The human's words: "the codename in brackets is worth the extra words:
'...the Bij AI variant-interpretation component (GEPER)'". Before this,
every geper surface said it was "produced by the GEPER engine" and both
PDF titles ended "(GEPER engine)" -- a component naming itself as if it
were the whole product.

Pinned VERBATIM here, because it is ratified clinician-facing text and a
reworded sentence is a different claim:

  * the shared scope line (report/summary.py::SCOPE_LINE), rendered by the
    full PDF, the short PDF and the Markdown report;
  * both PDF document titles (the PDF /Title metadata).

Written against RENDERED OUTPUT for the surfaces -- real PDFs read back
with pypdf, the real Markdown string -- and against the SOURCE of the
shipped report package for "no old phrase survives", because a phrase
left in a renderer branch this fixture does not reach would pass every
render test and still ship.
"""

import os
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader

import report as _report_pkg
from report.report_generator import ReportGenerator
from report.summary import SCOPE_LINE, generate_pdf
from report.summary_short import generate_short_pdf
from tests.test_disclaimer_consistency import _document

NEW_SCOPE_LINE = (
    "Variant interpretation from a supplied VCF — produced by the Bij AI "
    "variant-interpretation component (GEPER). Sequencing, alignment and variant "
    "calling are performed upstream; any run-level QC shown here is supplied by "
    "that run and is not computed by this report."
)
NEW_FULL_TITLE = (
    "Bij AI Clinical Genomic Analysis Report — variant interpretation from a "
    "supplied VCF — Bij AI variant-interpretation component (GEPER)"
)
NEW_SHORT_TITLE = (
    "Bij AI Clinical Genomic Summary Report — variant interpretation from a "
    "supplied VCF — Bij AI variant-interpretation component (GEPER)"
)
# The superseded wording. "GEPER engine" covers both the scope line
# ("produced by the GEPER engine") and the titles ("(GEPER engine)").
OLD_PHRASES = ("GEPER engine",)


def _collapse(text: str) -> str:
    # PDF extraction breaks lines where the layout wrapped them; the claim
    # is about the words, not where the line happened to break.
    return " ".join(text.split())


def _render_pdf(render, document):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "out.pdf")
        render(document, path)
        reader = PdfReader(path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        title = reader.metadata.title if reader.metadata else None
    return text, title


class TestScopeLineIsTheRatifiedSentence(unittest.TestCase):
    def test_constant_is_verbatim(self):
        self.assertEqual(SCOPE_LINE, NEW_SCOPE_LINE)


class TestEverySurfaceRendersTheNewName(unittest.TestCase):
    def test_full_pdf_renders_scope_line_and_title(self):
        text, title = _render_pdf(generate_pdf, _document(1))
        self.assertIn(NEW_SCOPE_LINE, _collapse(text))
        self.assertEqual(title, NEW_FULL_TITLE)
        for old in OLD_PHRASES:
            self.assertNotIn(old, text)
            self.assertNotIn(old, title or "")

    def test_short_pdf_renders_scope_line_and_title(self):
        text, title = _render_pdf(generate_short_pdf, _document(1))
        self.assertIn(NEW_SCOPE_LINE, _collapse(text))
        self.assertEqual(title, NEW_SHORT_TITLE)
        for old in OLD_PHRASES:
            self.assertNotIn(old, text)
            self.assertNotIn(old, title or "")

    def test_markdown_renders_scope_line(self):
        md = ReportGenerator().generate(_document(1))
        self.assertIn(f"*{NEW_SCOPE_LINE}*", md)
        for old in OLD_PHRASES:
            self.assertNotIn(old, md)


class TestNoOldPhraseSurvivesInShippedReportCode(unittest.TestCase):
    """Source scan of the whole shipped report package, not just the
    branches the fixture above happens to reach."""

    def test_report_package_source_carries_no_old_phrase(self):
        report_dir = Path(_report_pkg.__file__).resolve().parent
        sources = sorted(report_dir.rglob("*.py"))
        self.assertTrue(sources, f"no report sources found under {report_dir}")
        hits = []
        for path in sources:
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for old in OLD_PHRASES:
                    if old in line:
                        hits.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(hits, [], "superseded component name still in shipped report code")


if __name__ == "__main__":
    unittest.main()
