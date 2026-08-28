"""
Card T3-F4: HGVS must appear alongside the coordinate string in both
full-report headings (full Markdown, full PDF), using the exact same
fallback chain (`hgvs_c` -> `hgvs_g` -> locus) the short PDF's own
per-finding heading already used -- see
`report/clinical_report_builder.py::_variant_hgvs_or_locus`, now the
single source three renderers call rather than each carrying its own
copy of this fallback order.

This file covers what `tests/test_summary_short.py::TestFieldExtraction`
already covers for the fallback chain itself (kept there, now pointed at
the shared helper), plus what that file does NOT cover: that the full
Markdown and full PDF headings actually use it too, and that all three
renderers are wired to the SAME function object rather than three
independently-maintained inline copies that could silently drift apart
again -- the exact failure mode this card exists to close.

Fixture recipe (`_build_variant_result`) mirrors
`tests/test_report_consistency.py::_build_variant_result_with_serialized_interpretation`
-- build a real `InterpretationResult`, serialize it, then call
`report/json_builder.py::build_variant_result` the same way
`pipeline/orchestrator.py` does, so `candidate_interpretation` is a
genuinely complete clinical-report dict rather than a hand-trimmed stub
that happens to satisfy today's Markdown renderer's required keys.
"""

import unittest

import report.report_generator as report_generator_module
import report.summary as summary_module
import report.summary_short as summary_short_module
from pipeline.interpretation_result import build_interpretation_result
from report.clinical_report_builder import _variant_hgvs_or_locus
from report.json_builder import build_variant_result
from report.report_generator import ReportGenerator
from report.summary import generate_pdf
from report.summary_short import generate_short_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False


def _legacy_interpretation():
    return {
        "summary": "s",
        "confidence": "Low",
        "legacy_pre_acmg_significance_score": 0,
        "supporting_evidence": [],
        "acmg_evaluation": {
            "classification": "Uncertain Significance",
            "triggered_criteria": [],
            "not_triggered_criteria": [],
            "not_evaluated_criteria": [],
            "combining_rule_trace": [],
        },
    }


def _build_variant_result(variant_dict, normalization_result=None):
    interpretation = _legacy_interpretation()
    result_obj = build_interpretation_result(variant_dict=variant_dict, interpretation=interpretation)
    interpretation["interpretation_result"] = result_obj.to_dict()
    return build_variant_result(
        variant_dict=variant_dict,
        sequence_context={},
        dna_model_results={},
        rna_result={},
        protein_result={},
        blast_result={},
        clinvar_result={},
        dbsnp_result={},
        interpretation=interpretation,
        errors=[],
        normalization_result=normalization_result,
    )


def _document(variant_result):
    return {
        "geper_version": "test",
        "generated_at": "2026-08-28T00:00:00+00:00",
        "input_vcf": "test.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["SAMPLE01"],
        "variant_count": 1,
        "variants": [variant_result],
    }


_VARIANT_DICT = {
    "chrom": "17",
    "pos": 43106534,
    "ref": "C",
    "alt": "A",
    "variant_type": "SNV",
    "id": ".",
    "filter": "PASS",
}


class TestSharedHelperFallbackChain(unittest.TestCase):
    """The fallback rule itself, at its one real definition site."""

    def test_hgvs_c_preferred(self):
        vr = _build_variant_result(
            _VARIANT_DICT, normalization_result={"hgvs_c": "NM_1.2:c.5A>T", "hgvs_g": "NC_1.2:g.99A>T"}
        )
        self.assertEqual(_variant_hgvs_or_locus(vr), "NM_1.2:c.5A>T")

    def test_falls_back_to_hgvs_g_when_hgvs_c_absent(self):
        vr = _build_variant_result(_VARIANT_DICT, normalization_result={"hgvs_c": None, "hgvs_g": "NC_1.2:g.99A>T"})
        self.assertEqual(_variant_hgvs_or_locus(vr), "NC_1.2:g.99A>T")

    def test_falls_back_to_locus_when_neither_available(self):
        variant_dict = dict(_VARIANT_DICT, chrom="17", pos=100, ref="A", alt="T")
        vr = _build_variant_result(variant_dict, normalization_result={})
        self.assertEqual(_variant_hgvs_or_locus(vr), "17:100 A>T")


class TestFullMarkdownHeadingCarriesHgvs(unittest.TestCase):
    def test_hgvs_c_appears_alongside_coordinates_not_instead_of_them(self):
        vr = _build_variant_result(_VARIANT_DICT, normalization_result={"hgvs_c": "NM_000546.6:c.742C>T"})
        markdown = ReportGenerator().generate(_document(vr))
        self.assertIn("NM_000546.6:c.742C>T", markdown)
        # Addition, not a rename -- the ruling was explicit that the
        # coordinate string must stay.
        self.assertIn("17:43106534 C>A", markdown)

    def test_falls_back_to_locus_in_heading_when_no_hgvs(self):
        variant_dict = dict(_VARIANT_DICT, chrom="17", pos=100, ref="A", alt="T")
        vr = _build_variant_result(variant_dict, normalization_result={})
        markdown = ReportGenerator().generate(_document(vr))
        # Accepted, documented consequence of one shared fallback rule:
        # the parenthetical repeats the coordinate string verbatim
        # rather than a renderer-specific special case suppressing it.
        self.assertIn("## Variant 1: 17:100 A>T (17:100 A>T)", markdown)


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestFullPdfHeadingAndBookmarkCarryHgvs(unittest.TestCase):
    def _render_and_extract(self, doc):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(doc, out)
            reader = PdfReader(out)
            text = "\n".join(page.extract_text() for page in reader.pages)
            outline_titles = [entry.title for entry in reader.outline if hasattr(entry, "title")]
            return text, outline_titles

    def test_hgvs_c_appears_in_heading_alongside_coordinates(self):
        vr = _build_variant_result(_VARIANT_DICT, normalization_result={"hgvs_c": "NM_000546.6:c.742C>T"})
        text, _outline = self._render_and_extract(_document(vr))
        self.assertIn("NM_000546.6:c.742C>T", text)
        self.assertIn("17:43106534 C>A", text)

    def test_bookmark_carries_the_same_hgvs_as_the_heading(self):
        vr = _build_variant_result(_VARIANT_DICT, normalization_result={"hgvs_c": "NM_000546.6:c.742C>T"})
        _text, outline_titles = self._render_and_extract(_document(vr))
        self.assertTrue(
            any("NM_000546.6:c.742C>T" in title for title in outline_titles),
            f"expected the PDF outline/bookmark to carry the same HGVS as the heading; got {outline_titles!r}",
        )


class TestOneSourceThreeCallers(unittest.TestCase):
    """
    The two-direction probe: break the ONE shared helper and confirm all
    three renderers change together. If any renderer instead had its own
    independently-maintained copy of the fallback chain, patching the
    shared symbol here would leave that renderer's output unaffected --
    which is exactly the silent-drift failure mode this card exists to
    prevent (the same shape commit b2a6083 removed elsewhere in this
    codebase for a different pair of duplicated functions).
    """

    def test_all_three_renderers_share_the_same_function_object(self):
        self.assertIs(report_generator_module._variant_hgvs_or_locus, _variant_hgvs_or_locus)
        self.assertIs(summary_module._variant_hgvs_or_locus, _variant_hgvs_or_locus)
        self.assertIs(summary_short_module._variant_hgvs_or_locus, _variant_hgvs_or_locus)

    def test_breaking_the_shared_helper_changes_all_three_renderers_together(self):
        sentinel = "SENTINEL-SHARED-HELPER-BROKEN"
        vr = _build_variant_result(_VARIANT_DICT, normalization_result={"hgvs_c": "NM_000546.6:c.742C>T"})
        doc = _document(vr)

        import unittest.mock as mock

        with (
            mock.patch.object(report_generator_module, "_variant_hgvs_or_locus", return_value=sentinel),
            mock.patch.object(summary_module, "_variant_hgvs_or_locus", return_value=sentinel),
            mock.patch.object(summary_short_module, "_variant_hgvs_or_locus", return_value=sentinel),
        ):
            markdown = ReportGenerator().generate(doc)
            self.assertIn(sentinel, markdown)
            self.assertNotIn("NM_000546.6:c.742C>T", markdown)

            if _PYPDF_AVAILABLE:
                import os
                import tempfile

                with tempfile.TemporaryDirectory() as tmp:
                    out = os.path.join(tmp, "report.pdf")
                    generate_pdf(doc, out)
                    reader = PdfReader(out)
                    pdf_text = "\n".join(page.extract_text() for page in reader.pages)
                    self.assertIn(sentinel, pdf_text)
                    self.assertNotIn("NM_000546.6:c.742C>T", pdf_text)

                    out_short = os.path.join(tmp, "short.pdf")
                    generate_short_pdf(doc, out_short)
                    reader_short = PdfReader(out_short)
                    short_text = "\n".join(page.extract_text() for page in reader_short.pages)
                    self.assertIn(sentinel, short_text)
                    self.assertNotIn("NM_000546.6:c.742C>T", short_text)
