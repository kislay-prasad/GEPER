"""
Tests for the report "References" section (`report/clinical_report_builder.py
::_references`/`_REFERENCES`) -- specifically the 2026-08-08 addition of
HPO and Orphanet citations, per `DATA_SOURCE_LICENSE_AUDIT.md`'s finding
that both are CC-BY-4.0 (attribution required) and were previously
missing from every report GEPER generates.

HPO is wired through the existing conditional mechanism (present only
when `evidence_sources` contains "HPO" -- i.e. whenever PP4 actually
evaluated, see `pipeline/acmg_rules.py::ACMGRuleEngine._pp4`). Orphanet
is cited unconditionally instead: GEPER's pipeline fetches and caches
Orphanet's data every run (`pipeline/orphanet/bootstrap.py`), but that
data is not wired into any ACMG criterion or otherwise surfaced
per-variant, so it can never earn a place via the conditional mechanism
-- see `_ORPHANET_REFERENCE`'s own comment in `clinical_report_builder.py`
for the full reasoning behind that decision.

PDF tests generate real, small PDFs with ReportLab (lightweight, no
model weights -- safe to run locally), following the same pattern
`tests/test_clinician_summary.py`/`tests/test_indian_population_frequency.py`
already established.
"""

import os
import tempfile
import unittest

from report.clinical_report_builder import _ALPHAFOLD_REFERENCE, _ORPHANET_REFERENCE, _references, build_clinical_report
from report.report_generator import ReportGenerator
from report.summary import generate_pdf
from report.summary_short import generate_short_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False


def _ir(evidence_sources=None, **overrides):
    base = {
        "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
        "gene_symbol": "BRCA1",
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
        "priority_pending": True,
        "priority_explanation": [],
        "conflict_list": [],
        "conflict_score": 0.0,
        "recommendations": [],
        "evidence_sources": evidence_sources or [],
        "ai_model_errors": [],
    }
    base.update(overrides)
    return base


class TestReferencesUnit(unittest.TestCase):
    def test_orphanet_always_present_even_with_no_evidence_sources(self):
        # Was `[_ORPHANET_REFERENCE]` alone until commit 3927c82
        # (2026-08-22): AlphaFold DB is CC-BY-4.0 like Orphanet, was
        # present-but-unreachable via the same `evidence_sources` gate
        # (it doesn't contribute ACMG evidence, only structural context,
        # so it never legitimately appears there), and got the identical
        # unconditional-citation fix. `_references()` appends Orphanet
        # then AlphaFold, in that order -- see `_references()`'s own
        # source. Count intentionally +1 from before that commit, not a
        # regression.
        self.assertEqual(_references(_ir(evidence_sources=[])), [_ORPHANET_REFERENCE, _ALPHAFOLD_REFERENCE])

    def test_orphanet_always_present_alongside_other_sources(self):
        # 2 conditional entries (ClinVar, gnomAD) + Orphanet + AlphaFold
        # (both unconditional, see the comment above) = 4, not 3 as
        # before commit 3927c82.
        refs = _references(_ir(evidence_sources=["ClinVar", "gnomAD"]))
        self.assertIn(_ORPHANET_REFERENCE, refs)
        self.assertIn(_ALPHAFOLD_REFERENCE, refs)
        self.assertEqual(len(refs), 4)

    def test_hpo_present_when_evidence_sources_contains_hpo(self):
        refs = _references(_ir(evidence_sources=["HPO"]))
        self.assertTrue(any(r.startswith("Human Phenotype Ontology") for r in refs))
        self.assertIn(_ORPHANET_REFERENCE, refs)

    def test_hpo_absent_when_not_in_evidence_sources(self):
        refs = _references(_ir(evidence_sources=["ClinVar"]))
        self.assertFalse(any(r.startswith("Human Phenotype Ontology") for r in refs))

    def test_orphanet_citation_credits_inserm(self):
        # Matches Orphanet's own recommended citation format
        # (orphadata.com/legal-notice/, confirmed live 2026-08-08).
        self.assertIn("INSERM", _ORPHANET_REFERENCE)
        self.assertIn("orphadata.com", _ORPHANET_REFERENCE)

    def test_hpo_citation_matches_own_recommended_format(self):
        # HPO's own citation guidance recommends the most recent NAR
        # article (obophenotype.github.io/human-phenotype-ontology/
        # community/cite/, confirmed live 2026-08-08).
        refs = _references(_ir(evidence_sources=["HPO"]))
        hpo_ref = next(r for r in refs if r.startswith("Human Phenotype Ontology"))
        self.assertIn("Nucleic Acids Research", hpo_ref)


class TestBuildClinicalReportIncludesReferences(unittest.TestCase):
    def test_references_key_present_and_includes_orphanet(self):
        cr = build_clinical_report(_ir(evidence_sources=["ClinVar"]), raw_evidence={})
        self.assertIn(_ORPHANET_REFERENCE, cr["references"])


def _document(evidence_sources):
    cr = build_clinical_report(_ir(evidence_sources=evidence_sources), raw_evidence={})
    return {
        "geper_version": "t",
        "generated_at": "2026-01-01T00:00:00Z",
        "input_vcf": "x.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["S1"],
        "variant_count": 1,
        "variants": [
            {
                "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
                "interpretation": {},
                "candidate_interpretation": cr,
                "ai_model_status": {},
                "errors": [],
            }
        ],
    }


class TestMarkdownReferencesRendering(unittest.TestCase):
    def test_orphanet_appears_unconditionally_in_markdown(self):
        doc = _document(evidence_sources=["ClinVar"])
        md = ReportGenerator().generate(doc)
        self.assertIn("### 16. References", md)
        self.assertIn("Orphanet/Orphadata", md)
        self.assertIn("INSERM", md)

    def test_hpo_appears_in_markdown_when_pp4_evaluated(self):
        doc = _document(evidence_sources=["HPO"])
        md = ReportGenerator().generate(doc)
        self.assertIn("Human Phenotype Ontology (HPO)", md)
        self.assertIn("Nucleic Acids Research", md)
        # Orphanet still present alongside it.
        self.assertIn("Orphanet/Orphadata", md)

    def test_hpo_absent_from_markdown_when_not_evaluated(self):
        doc = _document(evidence_sources=["ClinVar"])
        md = ReportGenerator().generate(doc)
        self.assertNotIn("Human Phenotype Ontology", md)


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestFullPdfReferencesRendering(unittest.TestCase):
    """Real ReportLab PDF generation + pypdf text extraction, per this
    feature's stated verification requirement."""

    def test_orphanet_and_hpo_appear_in_full_pdf(self):
        document = _document(evidence_sources=["HPO", "ClinVar"])
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.pdf")
            generate_pdf(document, out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self.assertIn("References:", text)
        self.assertIn("Orphanet/Orphadata", text)
        self.assertIn("Human Phenotype Ontology", text)

    def test_orphanet_appears_in_full_pdf_even_with_no_other_evidence_sources(self):
        document = _document(evidence_sources=[])
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.pdf")
            generate_pdf(document, out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self.assertIn("Orphanet/Orphadata", text)


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestShortPdfNowRendersReferences(unittest.TestCase):
    """
    SUPERSEDES TestShortPdfDeliberatelyOmitsReferences. Ruling on
    AM-04-attribution-condition-unresolved, item 2 (human via god,
    2026-09-11): "YES, THE SHORT PDF NEEDS A REFERENCES SECTION. A
    sign-off document with zero source attribution is wrong independent
    of what CC BY 4.0 requires... a clinician signing should be able to
    see what the conclusion rests on." The short PDF now renders the
    same References list the full PDF and Markdown already render
    (`_references()`, gated on `evidence_sources` for conditional
    entries, unconditional for Orphanet/AlphaFold) -- confirming that
    holds, rather than assuming it does.
    """

    def test_orphanet_and_hpo_appear_in_short_pdf(self):
        document = _document(evidence_sources=["HPO", "ClinVar"])
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r_short.pdf")
            generate_short_pdf(document, out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self.assertIn("References:", text)
        self.assertIn("Orphanet/Orphadata", text)
        self.assertIn("Human Phenotype Ontology", text)

    def test_hpo_absent_from_short_pdf_when_not_evaluated(self):
        document = _document(evidence_sources=["ClinVar"])
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r_short_no_hpo.pdf")
            generate_short_pdf(document, out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self.assertIn("References:", text)
        self.assertIn("Orphanet/Orphadata", text)
        self.assertNotIn("Human Phenotype Ontology", text)


if __name__ == "__main__":
    unittest.main()
