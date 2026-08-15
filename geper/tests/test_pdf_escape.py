"""
Regression tests for `report/pdf_escape.py` (round 25) -- the shared
XML-escaping helper for ReportLab `Paragraph` text, and its application
at the clinician-override render sites in `report/summary.py`/
`report/summary_short.py` that round 24 confirmed live.

Round 24 reproduced three distinct failure modes directly against the
installed `reportlab` package, all from a single unescaped value
reaching `Paragraph`:
  1. `"R&D"` silently mangles to `"R&D;"` (an unescaped `&word` is
     treated as an unterminated entity reference and silently closed).
  2. `"<this>"` is silently DELETED WHOLESALE -- no error, no warning.
  3. `"<br>"` (colliding with one of ReportLab's own recognized tag
     names) raises `ValueError` and crashes report generation.

Every test below exercises all three, not just the ampersand case --
the round's own instruction was explicit that a test covering only the
mangle would miss the two worse failure modes. Lightweight ReportLab
`Paragraph` construction only; no model weights, no pipeline import.
"""

import unittest
from xml.sax.saxutils import escape as _xml_escape

from reportlab.platypus import Paragraph

from report.pdf_escape import esc
from report.summary import _build_stylesheet, _build_variant_section
from report.summary_short import _build_short_stylesheet, _build_variant_block

# The exact three adversarial strings round 24 reproduced against the
# installed reportlab package (see this module's docstring / round 24's
# entry in ROUND_CANDIDATES.md).
# Reportlab's mangle only triggers when `&` is immediately followed by
# a word character with no space (parsed as an attempted, unterminated
# entity reference) -- "lab & clinic" (space after `&`) does NOT
# reproduce it; "R&D" (no space) does, matching round 24's own
# reproduction exactly.
_AMPERSAND_CASE = "Confirmed per R&D team review"
_DELETION_CASE = "See <this> finding for detail"
_CRASH_CASE = "Downgraded <br> per new evidence"


def _plain_text(styles, text: str) -> str:
    """Builds a real `Paragraph` from `text` under the given stylesheet
    and returns ReportLab's own rendered plain text -- the actual
    decoded output a PDF page would show, not the raw pre-parse string."""
    return Paragraph(text, styles["BodyText"]).getPlainText()


class TestEscHelper(unittest.TestCase):
    def test_none_becomes_empty_string(self):
        self.assertEqual(esc(None), "")

    def test_non_string_values_are_coerced(self):
        self.assertEqual(esc(42), "42")
        self.assertEqual(esc(3.5), "3.5")

    def test_escapes_ampersand_less_than_greater_than(self):
        self.assertEqual(esc("R&D <b>bold</b> 5>2"), "R&amp;D &lt;b&gt;bold&lt;/b&gt; 5&gt;2")

    def test_matches_xml_sax_saxutils_escape_directly(self):
        # `esc()` is a thin wrapper -- confirms it hasn't drifted from
        # the standard-library escaper it wraps.
        sample = "a & b < c > d"
        self.assertEqual(esc(sample), _xml_escape(sample))


class TestEscPreventsAllThreeFailureModesDirectly(unittest.TestCase):
    """Reproduces round 24's three failure modes against a raw
    `Paragraph`, unescaped vs. escaped, proving `esc()` fixes all three
    -- not just the ampersand case."""

    def test_ampersand_mangle_unescaped_vs_fixed(self):
        # Unescaped: reproduces round 24's mangle ("R&D" -> "R&D;").
        mangled = Paragraph(_AMPERSAND_CASE, _build_stylesheet()["BodyText"]).getPlainText()
        self.assertNotEqual(mangled, _AMPERSAND_CASE, "expected the pre-fix mangle to still reproduce")
        # Escaped: round-trips to the original text, unmangled.
        fixed = Paragraph(esc(_AMPERSAND_CASE), _build_stylesheet()["BodyText"]).getPlainText()
        self.assertEqual(fixed, _AMPERSAND_CASE)

    def test_silent_deletion_unescaped_vs_fixed(self):
        # Unescaped: reproduces round 24's silent deletion of "<this>".
        deleted = Paragraph(_DELETION_CASE, _build_stylesheet()["BodyText"]).getPlainText()
        self.assertNotIn("this", deleted, "expected the pre-fix deletion to still reproduce")
        # Escaped: the bracketed text survives intact.
        fixed = Paragraph(esc(_DELETION_CASE), _build_stylesheet()["BodyText"]).getPlainText()
        self.assertEqual(fixed, _DELETION_CASE)
        self.assertIn("<this>", fixed)

    def test_crash_unescaped_vs_fixed(self):
        # Unescaped: reproduces round 24's ValueError crash.
        with self.assertRaises(ValueError):
            Paragraph(_CRASH_CASE, _build_stylesheet()["BodyText"])
        # Escaped: renders without raising, text preserved.
        fixed = Paragraph(esc(_CRASH_CASE), _build_stylesheet()["BodyText"]).getPlainText()
        self.assertEqual(fixed, _CRASH_CASE)


def _override_variant_result(reason: str, new_classification: str = "Likely Pathogenic", clinician_id: str = "dr.a"):
    return {
        "variant": {"chrom": "17", "pos": 100, "ref": "A", "alt": "T"},
        "interpretation_result": {"gene_symbol": "BRCA1"},
        "clinical_report": {
            "executive_summary": "Variant 17:100 A>T in BRCA1 was classified as **Pathogenic**.",
            "acmg_classification": {
                "classification": "Pathogenic",
                "clinician_override": {
                    "original_classification": "Pathogenic",
                    "new_classification": new_classification,
                    "reason": reason,
                    "clinician_id": clinician_id,
                    "timestamp": "2026-08-15T12:00:00+00:00",
                },
            },
            "confidence": {"pending": True},
        },
    }


class TestIntentionalMarkupSurvivesEscapedValues(unittest.TestCase):
    """
    Round 25's audit found this codebase deliberately emits its own
    ReportLab mini-HTML (`<b>`, `<br/>`, `&nbsp;`) around interpolated
    values -- `esc()` must be applied to the VALUE at its interpolation
    point, never to the composed string as a whole, or GEPER's own
    literal markup would itself be escaped into visible `&lt;b&gt;`
    text. Confirms the fix-pattern actually used throughout
    `report/summary.py`/`report/summary_short.py` keeps both true at
    once: an adversarial value is neutralized AND surrounding
    intentional markup still renders as real bold/line-break
    formatting, not literal text.
    """

    def test_bold_tag_survives_while_adjacent_value_is_escaped(self):
        gene = _CRASH_CASE  # adversarial gene symbol stand-in
        text = f"<b>{esc(gene)}</b> confirmed"
        para = Paragraph(text, _build_stylesheet()["BodyText"])
        # `getPlainText()` strips real markup but passes literal escaped
        # text through unchanged -- if `<b>` had itself been escaped, it
        # would show up verbatim ("&lt;b&gt;") in the plain-text output.
        plain = para.getPlainText()
        self.assertNotIn("&lt;b&gt;", plain)
        self.assertIn(gene, plain)
        # The `<b>` tag is real formatting, not text: confirm the parsed
        # paragraph actually carries a bold sub-fragment' font, not by
        # string-matching the literal tag out of getPlainText() (which
        # never includes it either way).
        self.assertTrue(any("Bold" in (f.fontName or "") for f in para.frags), "expected a real bold fragment")

    def test_nbsp_entity_survives_while_adjacent_value_is_escaped(self):
        text = f"<b>Run:</b> {esc(_AMPERSAND_CASE)} &nbsp;&nbsp; <b>Build:</b> GRCh38"
        plain = Paragraph(text, _build_stylesheet()["BodyText"]).getPlainText()
        self.assertIn(_AMPERSAND_CASE, plain)
        self.assertIn("Run:", plain)
        self.assertIn("Build:", plain)
        self.assertNotIn("&nbsp;", plain)  # decoded to real spacing, not left literal


class TestClinicianOverrideRenderSurvivesAdversarialText(unittest.TestCase):
    """
    `review/signoff.py::override()`'s `reason`/`new_classification`/
    `clinician_id` are free CLI text (round 24's confirmed-live finding)
    rendered at `report/summary.py:1990`-ish (`_build_variant_section`)
    and `report/summary_short.py`'s `_build_variant_block`. Neither
    call site may crash, silently drop the clinician's own words, or
    mangle them, for any of round 24's three reproduced inputs.
    """

    def _flowables_text(self, flow) -> str:
        return " ".join(f.getPlainText() for f in flow if isinstance(f, Paragraph))

    def test_full_report_override_block_ampersand(self):
        flow = _build_variant_section(1, _override_variant_result(_AMPERSAND_CASE), _build_stylesheet())
        self.assertIn(_AMPERSAND_CASE, self._flowables_text(flow))

    def test_full_report_override_block_deletion(self):
        flow = _build_variant_section(1, _override_variant_result(_DELETION_CASE), _build_stylesheet())
        text = self._flowables_text(flow)
        self.assertIn(_DELETION_CASE, text)
        self.assertIn("<this>", text)

    def test_full_report_override_block_crash(self):
        # Must not raise.
        flow = _build_variant_section(1, _override_variant_result(_CRASH_CASE), _build_stylesheet())
        self.assertIn(_CRASH_CASE, self._flowables_text(flow))

    def test_full_report_override_new_classification_and_clinician_id_also_escaped(self):
        vr = _override_variant_result("routine reason", new_classification=_CRASH_CASE, clinician_id=_AMPERSAND_CASE)
        flow = _build_variant_section(1, vr, _build_stylesheet())
        text = self._flowables_text(flow)
        self.assertIn(_CRASH_CASE, text)
        self.assertIn(_AMPERSAND_CASE, text)

    def test_short_report_override_block_all_three(self):
        for case in (_AMPERSAND_CASE, _DELETION_CASE, _CRASH_CASE):
            with self.subTest(case=case):
                flow = _build_variant_block(1, _override_variant_result(case), _build_short_stylesheet())
                text = self._flowables_text(flow)
                self.assertIn(case, text)


if __name__ == "__main__":
    unittest.main()
