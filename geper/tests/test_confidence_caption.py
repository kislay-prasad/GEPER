"""
Tests for the Confidence clarifying caption -- a display-only addition
next to every place `ConfidenceEngine.score()`'s output (evidence
*completeness*, not classification *certainty*) is shown to a reader,
in the full PDF (`report/summary.py`), short PDF
(`report/summary_short.py`), and Markdown report
(`report/report_generator.py`). See those modules for the underlying
`clinical_report["confidence"]` dict this caption sits next to --
nothing about its value, computation, or JSON key changes here.

Real, small PDFs are generated with ReportLab (lightweight, no model
weights -- safe to run locally) and inspected with pypdf, following the
same pattern `tests/test_clinician_summary.py` and
`tests/test_summary_short.py` already established. Two variant counts
(1 and 6) are exercised for both PDF formats to confirm the caption
doesn't silently truncate or push existing content (Finding sections,
sign-off block) off the page.
"""

import os
import tempfile
import unittest

from report.clinical_report_builder import build_clinical_report
from report.report_generator import ReportGenerator
from report.summary import generate_pdf
from report.summary_short import generate_short_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False

_CAPTION_TEXT = "Evidence Completeness reflects how much evidence Bij AI could gather for this variant"


def _clinical(classification="Pathogenic", confidence_label="High", confidence_score=90, pending=False):
    return {
        "executive_summary": f"Test executive summary ({classification}).",
        "acmg_classification": {"classification": classification, "triggered_criteria": []},
        "confidence": {"pending": pending, "label": confidence_label, "score": confidence_score},
        "supporting_evidence": [],
        "limitations": [],
        "evidence_sources": [],
        "conflict_resolution": {"severity": None},
    }


def _variant_result(idx, clinical=None):
    return {
        "variant": {"chrom": str(idx), "pos": 100 * idx, "ref": "A", "alt": "T"},
        "interpretation_result": {"gene_symbol": f"GENE{idx}"},
        "candidate_interpretation": _clinical() if clinical is None else clinical,
    }


def _document(n_variants):
    return {
        "geper_version": "test",
        "generated_at": "2026-08-08T00:00:00+00:00",
        "input_vcf": "test.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["SAMPLE01"],
        "variant_count": n_variants,
        "provenance": [],
        "variants": [_variant_result(i) for i in range(1, n_variants + 1)],
    }


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestFullReportCaption(unittest.TestCase):
    def _generate(self, n_variants):
        document = _document(n_variants)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "full.pdf")
            generate_pdf(document, out)
            return "\n".join(p.extract_text() for p in PdfReader(out).pages)

    def test_caption_appears_near_clinician_summary_table(self):
        text = self._generate(3)
        self.assertIn(_CAPTION_TEXT, text)
        # Directly follows the clinician summary table, before the
        # Reviewer Attention section -- not buried far from the score.
        summary_idx = text.index("Clinician Summary")
        caption_idx = text.index(_CAPTION_TEXT)
        attention_idx = text.index("Reviewer Attention")
        self.assertLess(summary_idx, caption_idx)
        self.assertLess(caption_idx, attention_idx)

    def test_caption_appears_once_per_finding_with_a_completed_score(self):
        text = self._generate(3)
        # One clinician-summary-table caption + one per Finding (3
        # variants, each with a completed, non-pending confidence).
        self.assertEqual(text.count(_CAPTION_TEXT), 4)

    def test_no_per_finding_caption_when_confidence_is_pending(self):
        clinical = _clinical(pending=True)
        clinical["confidence"]["label"] = None
        document = _document(1)
        document["variants"][0]["candidate_interpretation"] = clinical
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "full.pdf")
            generate_pdf(document, out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        # No per-finding Confidence line was rendered at all (pending),
        # so no per-finding caption either -- only the table-level one
        # from the (empty in this single-pending-variant case, still
        # rendered) clinician summary table survives.
        self.assertEqual(text.count(_CAPTION_TEXT), 1)

    def test_layout_intact_at_two_variant_counts(self):
        """The caption must not truncate or dislodge existing content --
        every Finding section and the sign-off block must still be
        present at both a small and a larger variant count."""
        for n in (1, 6):
            text = self._generate(n)
            for i in range(1, n + 1):
                self.assertIn(f"Finding {i}:", text)
            self.assertIn("Clinical Scientist", text)
            self.assertIn(_CAPTION_TEXT, text)


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestShortReportCaption(unittest.TestCase):
    def _generate(self, n_variants):
        document = _document(n_variants)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(document, out)
            return "\n".join(p.extract_text() for p in PdfReader(out).pages)

    def test_caption_appears_once_near_result_section(self):
        # The short report's compact per-variant strip has no room for a
        # per-row caption -- one caption directly under the "Result"
        # heading, before any variant strip, covers the whole Confidence
        # column instead of repeating per variant.
        text = self._generate(3)
        self.assertEqual(text.count(_CAPTION_TEXT), 1)
        result_idx = text.index("Result")
        caption_idx = text.index(_CAPTION_TEXT)
        first_finding_idx = text.index("Finding 1:")
        self.assertLess(result_idx, caption_idx)
        self.assertLess(caption_idx, first_finding_idx)

    def test_no_caption_when_no_variants(self):
        document = _document(0)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(document, out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self.assertNotIn(_CAPTION_TEXT, text)

    def test_layout_intact_at_two_variant_counts(self):
        for n in (1, 6):
            text = self._generate(n)
            for i in range(1, n + 1):
                self.assertIn(f"Finding {i}:", text)
            self.assertIn("Clinical Scientist", text)
            self.assertIn(_CAPTION_TEXT, text)


def _ir(confidence_pending=False, confidence_score=90, confidence_label="High", **overrides):
    base = {
        "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
        "gene_symbol": "BRCA1",
        "acmg_classification": "Pathogenic",
        "triggered_rules": [],
        "not_triggered_rules": [],
        "not_evaluated_rules": [],
        "combining_rule_trace": [],
        "supporting_evidence": [],
        "conflicting_evidence": [],
        "ai_consensus": [],
        "ai_context_models": [],
        "confidence_pending": confidence_pending,
        "confidence_score": confidence_score,
        "confidence_label": confidence_label,
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


def _markdown_document(n_variants):
    variants = []
    for i in range(1, n_variants + 1):
        variant = {"chrom": str(i), "pos": 100 * i, "ref": "A", "alt": "T"}
        cr = build_clinical_report(_ir(variant=variant), variant_dict=variant)
        variants.append(
            {
                "variant": variant,
                "interpretation": {},
                "candidate_interpretation": cr,
                "ai_model_status": {},
                "errors": [],
            }
        )
    return {
        "geper_version": "test",
        "generated_at": "2026-08-08T00:00:00+00:00",
        "input_vcf": "test.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["SAMPLE01"],
        "variant_count": n_variants,
        "variants": variants,
    }


class TestMarkdownCaption(unittest.TestCase):
    def test_caption_appears_in_confidence_score_section(self):
        document = _markdown_document(1)
        md = ReportGenerator().generate(document)
        self.assertIn("### 3. Evidence Completeness", md)
        section_start = md.index("### 3. Evidence Completeness")
        section_end = md.index("### 4. Priority Score")
        section = md[section_start:section_end]
        self.assertIn(_CAPTION_TEXT, section)

    def test_caption_appears_once_per_variant_not_duplicated(self):
        document = _markdown_document(2)
        md = ReportGenerator().generate(document)
        self.assertEqual(md.count(_CAPTION_TEXT), 2)

    def test_caption_present_even_when_confidence_pending(self):
        document = _markdown_document(1)
        variant = document["variants"][0]["variant"]
        document["variants"][0]["candidate_interpretation"] = build_clinical_report(
            _ir(confidence_pending=True, confidence_score=None, confidence_label=None, variant=variant),
            variant_dict=variant,
        )
        md = ReportGenerator().generate(document)
        self.assertIn(_CAPTION_TEXT, md)
        self.assertIn("Confidence scoring did not complete", md)


if __name__ == "__main__":
    unittest.main()
