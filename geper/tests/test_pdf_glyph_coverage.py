"""
Every character either geper PDF emits must be one its fonts can DRAW.

Ruled by the human 2026-09-11, after the approved short-PDF stage-error
line ("⚠ dbSNP stage failed: ...") was measured drawing a black square in
place of the sign: "a black square reads as a rendering fault and invites
distrust of the whole document, and embedding a font is not worth the
licence obligation." So: plain-text equivalents, and a sweep.

WHAT "CAN DRAW" MEANS HERE -- ReportLab's own rule, not a guess. Both PDFs
(report/summary.py, report/summary_short.py) use only the standard Type 1
Helvetica family; no TTF is registered. When ReportLab writes a string in a
standard Type 1 font it walks `[font] + font.substitutionFonts`
(Symbol, then ZapfDingbats) and encodes each character with the first font
whose encoding has it (`pdfmetrics.unicode2T1`). A character none of them
encodes is written as ZapfDingbats "n" -- the black square. So a character
is drawable iff one of those three encodings accepts it. Measured
2026-09-11 by rendering each candidate and reading the content stream:
"–" "—" "“" "”" "•" in Helvetica itself, "−" "≤" "≥" "→" in Symbol, "✓"
"✔" "✗" in ZapfDingbats (their real glyphs), and "⚠" as ZapfDingbats "n".

TWO CHECKS, because each alone has a blind spot:
  * SOURCE: every string literal (docstrings excluded) in the two renderers
    and the geper modules they import -- covers branches no fixture reaches.
  * RENDER: both PDFs rendered from a fixture that reaches the stage-error
    lines, with ReportLab's encoder observed -- covers text that arrives
    through document data rather than a literal.
"""

import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import reportlab.pdfgen.textobject as _textobject
from reportlab.pdfbase import pdfmetrics

from report.summary import generate_pdf
from report.summary_short import generate_short_pdf
from tests.test_stage_errors_on_both_pdfs import _doc_with_error_on_first_variant

_GEPER_ROOT = Path(__file__).resolve().parents[1]
_RENDERERS = ("report/summary.py", "report/summary_short.py")
# Not imported by the renderers, but its text reaches the full PDF through
# document data: summary.py renders each `priority["explanation"]` reason,
# and those reasons are built here ("✓ ...", "✗ ...").
_DATA_PRODUCERS = ("pipeline/prioritization_engine.py",)
_FONTS = ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique")


def _undrawable(text, font_name="Helvetica"):
    font = pdfmetrics.getFont(font_name)
    bad = []
    for ch in text:
        for f in [font] + list(font.substitutionFonts):
            try:
                ch.encode(f.encName)
                break
            except UnicodeEncodeError:
                continue
        else:
            bad.append(ch)
    return bad


def _imported_geper_modules(rel_path):
    tree = ast.parse((_GEPER_ROOT / rel_path).read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        for name in names:
            candidate = _GEPER_ROOT / (name.replace(".", "/") + ".py")
            if candidate.is_file():
                out.add(candidate.relative_to(_GEPER_ROOT).as_posix())
    return out


def _string_literals(rel_path):
    tree = ast.parse((_GEPER_ROOT / rel_path).read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and body:
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


class TestHelperIsReportLabsRule(unittest.TestCase):
    """Controls for the helper itself, so the checks below cannot pass by
    a helper that flags nothing."""

    def test_flags_the_measured_black_square(self):
        self.assertEqual(_undrawable("⚠ x"), ["⚠"])

    def test_passes_the_measured_drawable_characters(self):
        for font_name in _FONTS:
            with self.subTest(font=font_name):
                self.assertEqual(_undrawable("–—“”•−≤≥→✓✔✗ plain ASCII", font_name), [])


class TestSourceLiteralsAreDrawable(unittest.TestCase):
    def test_no_literal_in_the_pdf_renderers_or_their_helpers_is_undrawable(self):
        modules = set(_RENDERERS) | set(_DATA_PRODUCERS)
        for renderer in _RENDERERS:
            modules |= _imported_geper_modules(renderer)
        self.assertIn("report/clinical_report_builder.py", modules)  # the import walk found the helpers
        hits = []
        for rel in sorted(modules):
            for lineno, value in _string_literals(rel):
                for font_name in _FONTS:
                    bad = _undrawable(value, font_name)
                    if bad:
                        hits.append(f"{rel}:{lineno}: {''.join(sorted(set(bad)))!r} ({font_name})")
                        break
        self.assertEqual(hits, [], "characters these PDFs' fonts would draw as a black square")


class TestRenderedTextIsDrawable(unittest.TestCase):
    def _render_and_observe(self, render):
        seen = []
        real = _textobject.pdfmetrics_unicode2T1

        def observing(text, fonts):
            if isinstance(text, str):
                bad = _undrawable(text, fonts[0].fontName)
                if bad:
                    seen.append((text, bad))
            return real(text, fonts)

        with mock.patch.object(_textobject, "pdfmetrics_unicode2T1", observing):
            with tempfile.TemporaryDirectory() as tmp:
                render(_doc_with_error_on_first_variant(), os.path.join(tmp, "out.pdf"))
        return seen

    def test_observer_actually_sees_drawn_text(self):
        calls = []
        real = _textobject.pdfmetrics_unicode2T1
        with mock.patch.object(_textobject, "pdfmetrics_unicode2T1", lambda t, f: calls.append(t) or real(t, f)):
            with tempfile.TemporaryDirectory() as tmp:
                generate_short_pdf(_doc_with_error_on_first_variant(), os.path.join(tmp, "out.pdf"))
        self.assertTrue(any("dbSNP stage failed" in t for t in calls if isinstance(t, str)))

    def test_full_pdf_draws_no_undrawable_character(self):
        self.assertEqual(self._render_and_observe(generate_pdf), [])

    def test_short_pdf_draws_no_undrawable_character(self):
        self.assertEqual(self._render_and_observe(generate_short_pdf), [])


if __name__ == "__main__":
    unittest.main()
