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

UPDATE (2026-08-31, calibration_status coupling fix): `_DISCLOSURE_
SUBSTRINGS`'s third element changed from "not clinically validated" to
"not validated against clinical ground truth". `_bp7` used to hand-type
"(uncalibrated; not clinically validated)" next to the score -- fixed
wording, regardless of what either plugin actually said about its own
calibration. It now reads `details.calibration_status` from the real
plugin result instead (see `pipeline/acmg_rules.py::ACMGRuleEngine.
_bp7`), so the disclosed text is exactly whichever sentence the plugin
itself reports. `_DAMAGING_SPLICEFORMER_RESULT` below already carries
SpliceFormer's real sentence, which does not contain the literal phrase
"not clinically validated" -- so this test needed exactly the update its
own docstring said it wouldn't, once the follow-up fix (reading the
field instead of restating it) landed on top of the original disclosure
fix this file was written to protect. See
`TestBP7CalibrationStatusIsReadNotRestated` below for the coupling proof
this earlier version of the file didn't have: a test that changes what a
plugin says about its calibration and confirms BP7's output changes with
it, rather than asserting a fixed phrase that would stay identical
either way.
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

_DISCLOSURE_SUBSTRINGS = ("SpliceFormer", "uncalibrated", "not validated against clinical ground truth")

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
                "candidate_interpretation": clinical_report,
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
        # SpliceFormer's real calibration_status sentence (read from the
        # field rather than the old short hand-typed phrase) is long
        # enough that the PDF's fixed page width can line-wrap inside
        # it -- a rendering artifact, not a missing disclosure. Collapse
        # whitespace before the substring check so a wrap doesn't read
        # as absence; `text` itself stays unwrapped for the
        # position-ordering checks below, which only look for short
        # words/headings the wrap doesn't split.
        flattened = " ".join(text.split())
        for substring in _DISCLOSURE_SUBSTRINGS:
            self.assertIn(substring, flattened)
        conflicting_idx = text.index("Conflicting Evidence")
        disclosure_idx = text.index("uncalibrated")
        self.assertLess(conflicting_idx, disclosure_idx)
        # Same "not buried in limitations" property: if a Limitations
        # section is present at all, the disclosure must precede it.
        if "Limitations" in text:
            self.assertLess(disclosure_idx, text.index("Limitations"))


class TestBP7CalibrationStatusIsReadNotRestated(unittest.TestCase):
    """The coupling proof the substring tests above cannot provide: they
    only confirm certain words appear, which stayed true even when the
    caveat was a hand-typed constant unrelated to what either plugin
    actually reported. These tests confirm the disclosed text tracks
    each plugin's own `details.calibration_status` value -- change what
    a plugin says about its calibration, and BP7's rendered evidence
    changes with it. Verified manually before this fix landed: mutating
    SpliceFormerPlugin's calibration_status string and re-running the
    tests above left them GREEN (the old hand-typed phrase doesn't
    reference the field, so nothing could break); the same mutation
    against the fixed `_bp7` makes the *old* substring check ("not
    clinically validated") go red, because that literal phrase is gone
    from the output -- see the dispatch report for the captured
    before/after text. These tests are the automated, permanent form of
    that same check, using dependency injection instead of a source-file
    mutation so they stay fast and CI-safe.
    """

    _ALT_SPLICEFORMER_RESULT = {
        "score": 0.80,
        "classification": "moderate_effect",
        "confidence": 0.80,
        "details": {
            "calibration_status": "a distinctly different calibration sentence unique to this fixture",
        },
    }

    # Real text from pipeline/models/splicebert_plugin.py's two write
    # sites (:419 early-return, :481 main path) -- deliberately
    # DIFFERENT wording (the main path names two extra caveats the
    # early-return path doesn't: "zero-shot P(ref)-P(alt)" and "not
    # donor/acceptor-specific"). That divergence is a genuine finding
    # from the calibration_status investigation, not a bug this fix
    # corrects (out of scope -- see the dispatch); both fixtures use
    # classification="no_significant_effect" (a real value either path
    # can produce) so both exercise BP7's supporting_evidence branch and
    # are directly comparable.
    _SPLICEBERT_EARLY_RETURN_RESULT = {
        "score": 0.0,
        "classification": "no_significant_effect",
        "confidence": 0.0,
        "details": {
            "calibration_status": (
                "uncalibrated -- raw SpliceBERT masked-marginal "
                "probability-change summary, not validated against "
                "clinical ground truth"
            ),
        },
    }
    _SPLICEBERT_MAIN_PATH_RESULT = {
        "score": 0.1,
        "classification": "no_significant_effect",
        "confidence": 0.1,
        "details": {
            "calibration_status": (
                "uncalibrated -- raw SpliceBERT masked-marginal "
                "probability-change summary (zero-shot P(ref)-P(alt)); "
                "not donor/acceptor-specific, not validated against "
                "clinical ground truth"
            ),
        },
    }

    def test_two_distinct_spliceformer_wordings_produce_two_distinct_caveats(self):
        text_a = " ".join(
            ACMGRuleEngine._bp7(
                is_synonymous=True,
                mmsplice_result=None,
                spliceformer_result=_DAMAGING_SPLICEFORMER_RESULT,
            ).conflicting_evidence
        )
        text_b = " ".join(
            ACMGRuleEngine._bp7(
                is_synonymous=True,
                mmsplice_result=None,
                spliceformer_result=self._ALT_SPLICEFORMER_RESULT,
            ).conflicting_evidence
        )
        self.assertIn(_DAMAGING_SPLICEFORMER_RESULT["details"]["calibration_status"], text_a)
        self.assertIn(self._ALT_SPLICEFORMER_RESULT["details"]["calibration_status"], text_b)
        self.assertNotIn(self._ALT_SPLICEFORMER_RESULT["details"]["calibration_status"], text_a)
        self.assertNotIn(_DAMAGING_SPLICEFORMER_RESULT["details"]["calibration_status"], text_b)

    def test_splicebert_two_write_sites_carry_different_wording_and_both_flow_through(self):
        self.assertNotEqual(
            self._SPLICEBERT_EARLY_RETURN_RESULT["details"]["calibration_status"],
            self._SPLICEBERT_MAIN_PATH_RESULT["details"]["calibration_status"],
        )
        text_early = " ".join(
            ACMGRuleEngine._bp7(
                is_synonymous=True,
                mmsplice_result=None,
                splicebert_result=self._SPLICEBERT_EARLY_RETURN_RESULT,
            ).supporting_evidence
        )
        text_main = " ".join(
            ACMGRuleEngine._bp7(
                is_synonymous=True,
                mmsplice_result=None,
                splicebert_result=self._SPLICEBERT_MAIN_PATH_RESULT,
            ).supporting_evidence
        )
        self.assertIn(self._SPLICEBERT_EARLY_RETURN_RESULT["details"]["calibration_status"], text_early)
        self.assertIn(self._SPLICEBERT_MAIN_PATH_RESULT["details"]["calibration_status"], text_main)
        self.assertNotIn(self._SPLICEBERT_MAIN_PATH_RESULT["details"]["calibration_status"], text_early)
        self.assertNotIn(self._SPLICEBERT_EARLY_RETURN_RESULT["details"]["calibration_status"], text_main)

    def test_missing_calibration_status_gets_an_honest_fallback_not_a_guess(self):
        """No real plugin path produces a result with `classification`
        set but `details.calibration_status` absent today -- this is a
        defensive case for a read that must not assume the key exists.
        The fallback must not re-invent the old hardcoded "(uncalibrated;
        not clinically validated)" phrasing for a plugin that never said
        anything about its own calibration -- that would silently
        rebuild the exact defect this fix removes, one level down, for
        the one case where asserting "uncalibrated" is least justified."""
        result = {
            "score": 0.5,
            "classification": "moderate_effect",
            "confidence": 0.5,
            "details": {},
        }
        text = " ".join(
            ACMGRuleEngine._bp7(
                is_synonymous=True,
                mmsplice_result=None,
                spliceformer_result=result,
            ).conflicting_evidence
        )
        self.assertNotIn("uncalibrated; not clinically validated", text)
        self.assertIn("not reported", text)


if __name__ == "__main__":
    unittest.main()
