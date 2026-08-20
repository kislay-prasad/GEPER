"""
Tests for the Markdown-caveat-parity / PDF-recommendations-parity
follow-up (card run-caveats-missing-from-markdown-and-json-renderers,
Markdown audit): Markdown gains document-level A2 (ACMG methodology
statement), A1 (offline-sources caveat), A9 (reviewer-attention
flags), and patient_consent -- previously PDF-only -- via three
helpers relocated from report/summary.py into
report/clinical_report_builder.py. The full PDF gains per-variant
recommendations, mirroring Markdown's existing section 14 --
previously Markdown-only.

Written against OBSERVABLE OUTPUT (rendered Markdown/PDF text, and
where content sits relative to per-finding boundaries in the PDF) --
never against Kelly's internals -- except for the "one source, not
four copies" class, which is necessarily different in kind: see that
class's own docstring for why.
"""

import os
import re
import tempfile
import unittest
from unittest import mock

from report.clinical_report_builder import ACMG_METHODOLOGY_STATEMENT, build_clinical_report
from report.report_generator import ReportGenerator
from report.summary import generate_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _ir(chrom: str, pos: int, gene: str, **overrides) -> dict:
    base = {
        "variant": {"chrom": chrom, "pos": pos, "ref": "C", "alt": "A"},
        "gene_symbol": gene,
        "acmg_classification": "Uncertain significance",
        "triggered_rules": [],
        "not_triggered_rules": [],
        "not_evaluated_rules": [],
        "combining_rule_trace": [],
        "supporting_evidence": [],
        "conflicting_evidence": [],
        "ai_consensus": [],
        "ai_context_models": [],
        "confidence_pending": True,
        "confidence_score": None,
        "confidence_label": None,
        "priority_pending": True,
        "priority_explanation": [],
        "conflict_list": [],
        "conflict_score": 0.0,
        "conflict_severity": None,
        "recommendations": [],
        "evidence_sources": [],
        "ai_model_errors": [],
    }
    base.update(overrides)
    return base


def _variant(i: int, **ir_overrides) -> dict:
    chrom, pos, gene = str(i), 1000 + i, f"GENE{i}"
    variant_dict = {"chrom": chrom, "pos": pos, "ref": "C", "alt": "A"}
    ir = _ir(chrom, pos, gene, **ir_overrides)
    return {
        "variant": variant_dict,
        "interpretation_result": {"gene_symbol": gene},
        "clinical_report": build_clinical_report(ir, variant_dict=variant_dict),
    }


def _document(variants=(), **overrides) -> dict:
    document = {
        "geper_version": "test",
        "generated_at": "2026-08-21T00:00:00+00:00",
        "input_vcf": "caveat_parity_test.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["SAMPLE01"],
        "variant_count": len(variants),
        "variants": list(variants),
    }
    document.update(overrides)
    return document


def _all_pdf_text(path: str) -> str:
    reader = PdfReader(path)
    return "\n".join(page.extract_text() for page in reader.pages)


def _mock_offline(*services):
    return mock.patch("utils.service_health.HEALTH.offline_services", return_value=list(services))


class TestMarkdownDocumentLevelCaveats(unittest.TestCase):
    """A2/A1/A9/patient_consent must reach Markdown as DOCUMENT-level
    content -- alongside the existing disclaimer, not nested inside
    any one finding. Where a check can be done with zero variants
    (A2, A1, consent), it deliberately is: a zero-variant document has
    no per-variant Limitations list at all, so any of this content
    showing up there structurally proves it is genuinely document-
    level, not incidentally leaking in via per-variant inclusion."""

    def test_a2_acmg_methodology_at_document_level(self):
        md = ReportGenerator().generate(_document())  # zero variants
        self.assertIn(_normalize(ACMG_METHODOLOGY_STATEMENT), _normalize(md))

    def test_a1_offline_sources_at_document_level(self):
        with _mock_offline("Ensembl"):
            md = ReportGenerator().generate(_document())  # zero variants
        self.assertIn("Ensembl", md)
        self.assertIn("unreachable during this analysis run and were not queried", md)

    def test_a9_reviewer_attention_at_document_level(self):
        variant = _variant(1, conflict_severity="Major")
        md = ReportGenerator().generate(_document([variant]))
        self.assertIn("Conflicting evidence (Major)", md)

    def test_patient_consent_at_document_level(self):
        consent = {"clinical_reporting": True, "research": False, "timestamp": "2026-08-01T09:00:00+00:00"}
        md = ReportGenerator().generate(_document(patient_consent=consent))  # zero variants
        self.assertIn("consent", md.lower())
        self.assertIn("2026-08-01T09:00:00+00:00", md)


class TestPerVariantLimitationsUnmodifiedInMarkdown(unittest.TestCase):
    """Scope boundary (dispatch requirement 4): the document-level
    additions must be ADDITIVE. Per-variant clinical_report[
    "limitations"] must not gain A1/A9/consent entries just because a
    document-level version now also exists -- that would be a second,
    accidental copy, exactly the failure mode this whole card exists
    to close."""

    def test_offline_caveat_and_reviewer_flag_absent_from_per_variant_limitations(self):
        variant = _variant(1, conflict_severity="Major")
        with _mock_offline("Ensembl"):
            # Force the document-level content to actually render so a
            # false pass (content simply never generated at all) can't
            # masquerade as a true one.
            ReportGenerator().generate(_document([variant], patient_consent={"clinical_reporting": True}))

        limitations = variant["clinical_report"]["limitations"]
        joined = _normalize(" ".join(limitations))
        self.assertNotIn("unreachable during this analysis run", joined)
        self.assertNotIn("Conflicting evidence (Major)", joined)
        self.assertNotIn("Consent", joined)


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestRecommendationsPerVariantInFullPdf(unittest.TestCase):
    """Full PDF must render each variant's own `clinical_report[
    "recommendations"]`, attributable to that specific finding --
    mirroring Markdown's existing section 14 (report_generator.py:835,
    `clinical_report.get("recommendations")`)."""

    def test_recommendations_present_and_attributable_per_variant(self):
        rec_1 = "Recommend confirmatory Sanger sequencing for this variant."
        rec_2 = "Recommend segregation testing in affected family members."
        variants = [_variant(1, recommendations=[rec_1]), _variant(2, recommendations=[rec_2])]
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_document(variants), out)
            text = _all_pdf_text(out)

        self.assertIn(rec_1, text)
        self.assertIn(rec_2, text)

        # Attributable: each recommendation must sit within its OWN
        # finding's span, not merely present anywhere in the document
        # (e.g. accidentally attached to the wrong variant, or dumped
        # in one place for all variants). "Finding N" is the PDF's
        # own, already-established per-finding heading convention.
        idx_finding_1 = text.index("Finding 1")
        idx_finding_2 = text.index("Finding 2")
        idx_rec_1 = text.index(rec_1)
        idx_rec_2 = text.index(rec_2)
        self.assertTrue(
            idx_finding_1 < idx_rec_1 < idx_finding_2,
            "variant 1's recommendation should render within variant 1's own finding section",
        )
        self.assertGreater(
            idx_rec_2,
            idx_finding_2,
            "variant 2's recommendation should render within variant 2's own finding section",
        )

    def test_recommendations_section_omitted_when_empty(self):
        # NOT a mirror of Markdown's explicit "no recommendations
        # generated" fallback line (report_generator.py:842) --
        # confirmed with Kelly's actual implementation (summary.py, see
        # its own comment at the Recommendations block) that the PDF's
        # per-finding blocks all follow an omit-when-absent convention
        # (Supporting Evidence, Conflicting Evidence, Limitations
        # behave identically), a deliberate, documented divergence from
        # Markdown's fixed numbered-outline convention, not a gap.
        variant = _variant(1, recommendations=[])
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_document([variant]), out)
            text = _all_pdf_text(out)
        self.assertNotIn("Recommendations", text)


class TestRelocatedHelpersHaveOneSource(unittest.TestCase):
    """Dispatch requirement 3: "a test that would fail if someone
    re-forks [the relocated helpers] into summary.py or
    report_generator.py." Necessarily different in kind from the
    output-content tests above, for the same reason A4's identity
    tests were (see tests/test_disclaimer_consistency.py::
    TestDisclaimerHasOneSourceNotFourCopies's docstring): content
    agreement alone only proves "currently agrees", not "cannot
    re-fork" -- a freshly retyped, coincidentally-identical local copy
    would still pass an output-equality check.

    KNOWN LIMITATION, stated plainly rather than hidden: this checks
    identity ONLY for the case where a renderer holds a same-named
    local reference to the helper (e.g. `from
    report.clinical_report_builder import _offline_sources_caveat_text`
    -- the exact pattern A4's RESEARCH_USE_DISCLAIMER relocation used).
    If a renderer instead calls the relocated helper via fully-
    qualified module access with no local binding at all, there is
    nothing in that renderer's own namespace to check identity
    against, and this test is a harmless no-op for that helper rather
    than a false failure. It still catches the literal regression
    named in the dispatch (a re-forked, same-named local def shadowing
    the import), which is the concrete failure mode this repo has hit
    twice already this session.
    """

    def _assert_single_source(self, name):
        import report.clinical_report_builder as crb
        import report.report_generator as rg
        import report.summary as summ

        canonical = getattr(crb, name, None)
        self.assertIsNotNone(canonical, f"{name} not found on report.clinical_report_builder after relocation")
        for consumer, label in ((summ, "report.summary"), (rg, "report.report_generator")):
            local = getattr(consumer, name, None)
            if local is not None:
                self.assertIs(local, canonical, f"{label}.{name} is a separate object, not the shared one")

    def test_offline_sources_caveat_has_one_source(self):
        self._assert_single_source("_offline_sources_caveat_text")

    def test_reviewer_flags_has_one_source(self):
        self._assert_single_source("_variant_reviewer_flags")

    def test_consent_parsing_has_one_source(self):
        self._assert_single_source("_consent_value_label")


if __name__ == "__main__":
    unittest.main()
