"""
Regression test for the human decision (2026-08-20): when SpliceFormer
evidence blocks BP7 from triggering, the clinical report must disclose,
right where BP7's evidence is stated (not buried in the Limitations
section), that (1) the model is SpliceFormer, (2) its status is
"uncalibrated", (3) it is "not clinically validated". Kelly is
implementing the underlying fix in pipeline/acmg_rules.py::ACMGRuleEngine
._bp7 (the only place SpliceFormer's evidence text is currently produced
-- see that method's own docstring: "Both are uncalibrated"). This test
does NOT hand-author the disclosure text itself: it calls the real,
current `_bp7` with a mocked (not live) SpliceFormer plugin result --
"(or mock if needed)" per the dispatch -- so the disclosure text comes
from production code, and feeds that real text through the real report
renderers (report/summary.py::generate_pdf, report/report_generator.py
::ReportGenerator), matching the pattern tests/test_confidence_caption.py
already established for "is this text present, and not buried" checks.

Expected to be RED until Kelly's fix lands (the current BP7 evidence
string is just "SpliceFormer predicts a 'large_effect' effect
(score=X)." -- no disclosure wording yet); GREEN once it's added,
without this test needing to change, since it asserts on the real
CriterionResult text rather than a fixture I construct by hand.
"""

import os
import tempfile
import unittest

from pipeline.acmg_rules import ACMGRuleEngine
from report.clinical_report_builder import build_clinical_report
from report.report_generator import ReportGenerator
from report.summary import generate_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False

_DISCLOSURE_SUBSTRINGS = ("SpliceFormer", "uncalibrated", "not clinically validated")

# Shape matches pipeline/models/spliceformer_plugin.py::SpliceFormerPlugin
# .predict()'s real output (score/classification/confidence/details/meta)
# -- mocked here rather than run live, per the dispatch's "(or mock if
# needed)" allowance; a "large_effect" classification is what forces
# BP7's plugin_damaging branch (the one that currently builds the
# undisclosed "SpliceFormer predicts a 'large_effect' effect..." text).
_DAMAGING_SPLICEFORMER_RESULT = {
    "score": 0.91,
    "classification": "large_effect",
    "confidence": 0.91,
    "details": {
        "strand": "+",
        "scored_window_nt": 5000,
        "calibration_status": (
            "uncalibrated -- raw SpliceFormer acceptor/donor probability-delta "
            "summary, not validated against clinical ground truth"
        ),
    },
    "meta": {"model": "spliceformer", "version": "spliceformer;ref=v1.0.0", "device": "cpu"},
}


def _bp7_conflicting_text():
    """Real ACMGRuleEngine._bp7 call (no mocking of acmg_rules.py itself),
    forcing a synonymous variant whose only splice evidence is a
    damaging SpliceFormer call -- the exact scenario the human decision
    covers ('when evidence comes from SpliceFormer')."""
    result = ACMGRuleEngine._bp7(
        is_synonymous=True,
        mmsplice_result=None,
        spliceformer_result=_DAMAGING_SPLICEFORMER_RESULT,
    )
    assert result.status == "not_triggered", (
        "test setup assumption broken: a large_effect SpliceFormer call should "
        "block BP7 from triggering -- fix the fixture, not this assertion"
    )
    return " ".join(result.conflicting_evidence)


_VARIANT_DICT = {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"}


def _ir(conflicting_text):
    """Minimal interpretation_result dict -- mirrors
    tests/test_confidence_caption.py::_ir's shape (the keys
    report/clinical_report_builder.py::build_clinical_report actually
    reads), with `conflicting_evidence` carrying the real BP7 text."""
    return {
        "variant": _VARIANT_DICT,
        "gene_symbol": "TESTGENE",
        "acmg_classification": "Uncertain Significance",
        "triggered_rules": [],
        "not_triggered_rules": [],
        "not_evaluated_rules": [],
        "combining_rule_trace": [],
        "supporting_evidence": [],
        "conflicting_evidence": [conflicting_text],
        "ai_consensus": [],
        "ai_context_models": [],
        "confidence_pending": False,
        "confidence_score": 50,
        "confidence_label": "Moderate",
        "priority_pending": True,
        "priority_explanation": [],
        "conflict_list": [],
        "conflict_score": 0.0,
        "recommendations": [],
        "evidence_sources": ["transcript_cds", "SpliceFormer"],
        "ai_model_errors": [],
    }


def _document(clinical_report):
    return {
        "geper_version": "test",
        "generated_at": "2026-08-08T00:00:00+00:00",
        "input_vcf": "test.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["SAMPLE01"],
        "variant_count": 1,
        "provenance": [],
        "variants": [
            {
                "variant": _VARIANT_DICT,
                "gene_symbol": "TESTGENE",
                "interpretation": {},
                "clinical_report": clinical_report,
                "ai_model_status": {},
                "errors": [],
            }
        ],
    }


class TestBP7SpliceFormerDisclosure(unittest.TestCase):
    def setUp(self):
        self.conflicting_text = _bp7_conflicting_text()
        self.clinical_report = build_clinical_report(_ir(self.conflicting_text), variant_dict=_VARIANT_DICT)
        self.assertIsNotNone(self.clinical_report, "build_clinical_report returned None for a valid ir")
        self.document = _document(self.clinical_report)

    def test_bp7_criterion_result_itself_carries_the_disclosure(self):
        """The disclosure must originate in ACMGRuleEngine._bp7's own
        evidence text, not be bolted on later by a report-layer special
        case -- otherwise a Markdown-only or PDF-only renderer could
        silently drop it."""
        for substring in _DISCLOSURE_SUBSTRINGS:
            self.assertIn(
                substring,
                self.conflicting_text,
                f"BP7's SpliceFormer evidence text is missing required disclosure: {substring!r}. "
                f"Got: {self.conflicting_text!r}",
            )

    def test_markdown_report_discloses_at_the_conflicting_evidence_section(self):
        text = ReportGenerator().generate(self.document)
        for substring in _DISCLOSURE_SUBSTRINGS:
            self.assertIn(substring, text)
        # Not buried: it must appear in/at "6. Conflicting Evidence"
        # (where BP7's evidence is actually stated), not only reachable
        # via "15. Limitations" further down.
        conflicting_idx = text.index("Conflicting Evidence")
        limitations_idx = text.index("Limitations")
        disclosure_idx = text.index("uncalibrated")
        self.assertLess(conflicting_idx, disclosure_idx)
        self.assertLess(disclosure_idx, limitations_idx)

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_pdf_report_discloses_at_the_conflicting_evidence_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "full.pdf")
            generate_pdf(self.document, out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        for substring in _DISCLOSURE_SUBSTRINGS:
            self.assertIn(substring, text)
        conflicting_idx = text.index("Conflicting Evidence")
        disclosure_idx = text.index("uncalibrated")
        self.assertLess(conflicting_idx, disclosure_idx)
        # Same "not buried in limitations" property: if a Limitations
        # section is present at all, the disclosure must precede it.
        if "Limitations" in text:
            self.assertLess(disclosure_idx, text.index("Limitations"))


if __name__ == "__main__":
    unittest.main()
