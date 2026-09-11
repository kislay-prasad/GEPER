"""
Regression coverage for commit 3927c82 (AlphaFold DB CC-BY-4.0 attribution
fix). AlphaFold DB is integrated into GEPER (confidence bands, model
version, structural context) but never contributes ACMG criterion
evidence, so it was previously absent from `evidence_sources` and
therefore never reached `_REFERENCES`'s conditional gate -- present in
the pipeline, never cited in a shipped report. The fix (following the
existing Orphanet precedent) makes the citation unconditional.

Real Markdown and PDF output is generated (ReportLab + pypdf, same
pattern `tests/test_confidence_caption.py` already established) and
inspected for the citation text, rather than asserting on `_references()`
alone -- the earlier Orphanet/HPO citation bug (see `report/summary.py`'s
own comment near its references-rendering call site) was specifically a
case where the builder computed the right list but a renderer never
consumed it, so testing only the builder would not have caught that
class of defect.
"""

import os
import tempfile
import unittest

from report.clinical_report_builder import _ALPHAFOLD_REFERENCE, _references, build_clinical_report
from report.report_generator import ReportGenerator
from report.summary import generate_pdf
from report.summary_short import generate_short_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False

# The four elements AlphaFold's own recommended attribution requires:
# methods paper, source identification, license name, license link.
_REQUIRED_SUBSTRINGS = [
    "Jumper et al. 2021",
    "DeepMind Technologies Limited",
    "CC-BY-4.0",
    "creativecommons.org/licenses/by/4.0",
]


def _normalize(text: str) -> str:
    # ReportLab wraps long lines for page width; pypdf's extract_text()
    # renders each wrapped line break as a literal "\n" rather than a
    # space, which breaks a naive substring match across the wrap point
    # (e.g. "DeepMind\nTechnologies Limited") even though the rendered
    # PDF, read visually, contains the citation intact. Collapsing all
    # whitespace matches how a human reader (or any real compliance
    # check) would actually read the page.
    return " ".join(text.split())


def _assert_attribution_present(testcase: unittest.TestCase, text: str, where: str) -> None:
    normalized = _normalize(text)
    missing = [s for s in _REQUIRED_SUBSTRINGS if s not in normalized]
    testcase.assertEqual(missing, [], f"{where} is missing required AlphaFold attribution substrings: {missing}")


class TestReferencesUnconditional(unittest.TestCase):
    """`_references()` itself: AlphaFold must appear regardless of `evidence_sources`."""

    def test_present_with_empty_evidence_sources(self):
        refs = _references({"evidence_sources": []})
        self.assertIn(_ALPHAFOLD_REFERENCE, refs)

    def test_present_even_when_evidence_sources_never_names_alphafold(self):
        # AlphaFold never contributes ACMG evidence, so "AlphaFold DB"
        # never legitimately appears in evidence_sources -- the citation
        # must not depend on it appearing there.
        refs = _references({"evidence_sources": ["ClinVar", "gnomAD"]})
        self.assertIn(_ALPHAFOLD_REFERENCE, refs)


def _document_with_alphafold_variant():
    ir = {
        "gene_symbol": "BRCA1",
        "classification": "Pathogenic",
        "triggered_criteria": [],
        "not_evaluated_rules": [],
        "confidence_pending": False,
        "confidence_score": 90,
        "confidence_label": "High",
        "priority_pending": False,
        "priority_score": 80,
        "priority_category": "High",
        "evidence_sources": ["ClinVar", "gnomAD"],  # deliberately not "AlphaFold DB"
    }
    raw_evidence = {
        "alphafold": {
            "skipped": False,
            "error": None,
            "found": True,
            "affected_residue_band": "very_high",
            "mean_plddt_band": "very_high",
            "model_version": "AlphaFold DB v4",
        },
    }
    clinical = build_clinical_report(ir, variant_dict={"chrom": "17", "pos": 43106534}, raw_evidence=raw_evidence)
    return {
        "geper_version": "test",
        "generated_at": "2026-08-22T00:00:00+00:00",
        "input_vcf": "test.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["SAMPLE01"],
        "variant_count": 1,
        "provenance": [],
        "variants": [
            {
                "variant": {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"},
                "interpretation_result": {"gene_symbol": "BRCA1"},
                "candidate_interpretation": clinical,
            }
        ],
    }


class TestRenderedReportsCiteAlphaFold(unittest.TestCase):
    def test_markdown_report_cites_alphafold(self):
        text = ReportGenerator().generate(_document_with_alphafold_variant())
        _assert_attribution_present(self, text, "Markdown report (report_generator.py)")

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_full_pdf_cites_alphafold(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "full.pdf")
            generate_pdf(_document_with_alphafold_variant(), out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        _assert_attribution_present(self, text, "Full PDF (summary.py)")

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_short_pdf_now_cites_alphafold(self):
        """
        SUPERSEDES test_short_pdf_has_no_references_section_at_all, per its
        own docstring: "If the short PDF is ever changed to surface
        AlphaFold content directly... this test should be replaced with one
        requiring attribution there too." That change is this one -- ruling
        on AM-04-attribution-condition-unresolved, item 2 (human via god,
        2026-09-11): "YES, THE SHORT PDF NEEDS A REFERENCES SECTION. A
        sign-off document with zero source attribution is wrong independent
        of what CC BY 4.0 requires... a clinician signing should be able to
        see what the conclusion rests on." `report/summary_short.py` now
        renders the same `references` list the full PDF and Markdown
        already render (built by `_references()`, which cites AlphaFold
        unconditionally -- see `_ALPHAFOLD_REFERENCE`'s own comment), so
        the short PDF must carry the same required attribution substrings.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(_document_with_alphafold_variant(), out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        _assert_attribution_present(self, text, "Short PDF (summary_short.py)")


if __name__ == "__main__":
    unittest.main()
