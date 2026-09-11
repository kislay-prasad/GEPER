"""
Tests for the report's source citations (`report/clinical_report_builder.py
::_references`/`_REFERENCES`/`DATA_ATTRIBUTION`).

HISTORY. HPO and Orphanet citations were added 2026-08-08, per
`DATA_SOURCE_LICENSE_AUDIT.md`'s finding that both are CC-BY-4.0
(attribution required) and were previously missing from every report GEPER
generates. HPO is wired through the conditional mechanism (present only when
`evidence_sources` contains "HPO" -- i.e. whenever PP4 actually evaluated, see
`pipeline/acmg_rules.py::ACMGRuleEngine._pp4`). Orphanet -- and, from commit
3927c82, AlphaFold DB -- are attributed unconditionally instead, because
neither ever contributes ACMG criterion evidence and so neither could ever
reach the `evidence_sources` gate (see `_ORPHANET_REFERENCE`'s and
`_ALPHAFOLD_REFERENCE`'s own comments).

THE DECISION THESE TESTS NOW PIN (card HUMAN-DECISION-THE-REFERENCES-LIST-
CONFLATES-..., ruled (A), human via god 2026-09-11): until this change all of
the above went into ONE list headed "References", so a run that used only
HPO + ClinVar listed four sources, two of which it never consulted -- "a
fabricated citation in the exact place a reader goes to check everything
else." The list is now split, on Markdown, full PDF and short PDF, into two
blocks that read as two different claims:

  "Evidence sources used in this run" -- gated on `evidence_sources`, exactly
      as before; only what this run consulted.
  "Data attribution"                   -- the fixed licence notices
      (Orphanet/Orphadata, AlphaFold DB), text unchanged, unconditional.

The tests that pinned the old single "References" heading were REPLACED by
the ones below, not deleted: each old assertion survives as an assertion
about which of the two blocks an entry now lands in.

PDF tests generate real, small PDFs with ReportLab (lightweight, no model
weights -- safe to run locally), following the same pattern
`tests/test_clinician_summary.py`/`tests/test_indian_population_frequency.py`
already established.
"""

import os
import tempfile
import unittest

from report.clinical_report_builder import (
    _ALPHAFOLD_REFERENCE,
    _ORPHANET_REFERENCE,
    _REFERENCES,
    _references,
    build_clinical_report,
)
from report.report_generator import ReportGenerator
from report.summary import generate_pdf
from report.summary_short import generate_short_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False

# Ruled wording (A), verbatim.
EVIDENCE_HEADING = "Evidence sources used in this run"
ATTRIBUTION_HEADING = "Data attribution"


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
    """`_references()` is now the GATED list only -- "what this run used"."""

    def test_no_evidence_sources_means_an_empty_evidence_list(self):
        # REPLACES test_orphanet_always_present_even_with_no_evidence_sources,
        # which pinned `[_ORPHANET_REFERENCE, _ALPHAFOLD_REFERENCE]` here. Those
        # two now live in `data_attribution`, not in the run's evidence list.
        self.assertEqual(_references(_ir(evidence_sources=[])), [])

    def test_hpo_and_clinvar_run_lists_exactly_those_two(self):
        # REPLACES test_orphanet_always_present_alongside_other_sources (which
        # pinned a count of 4 for a 2-source run -- the exact fabricated-
        # citation shape the ruling removes).
        refs = _references(_ir(evidence_sources=["HPO", "ClinVar"]))
        self.assertEqual(refs, [_REFERENCES["HPO"], _REFERENCES["ClinVar"]])
        self.assertNotIn(_ORPHANET_REFERENCE, refs)
        self.assertNotIn(_ALPHAFOLD_REFERENCE, refs)

    def test_hpo_present_when_evidence_sources_contains_hpo(self):
        refs = _references(_ir(evidence_sources=["HPO"]))
        self.assertTrue(any(r.startswith("Human Phenotype Ontology") for r in refs))

    def test_hpo_absent_when_not_in_evidence_sources(self):
        refs = _references(_ir(evidence_sources=["ClinVar"]))
        self.assertFalse(any(r.startswith("Human Phenotype Ontology") for r in refs))

    def test_orphanet_citation_credits_inserm(self):
        # Matches Orphanet's own recommended citation format
        # (orphadata.com/legal-notice/, confirmed live 2026-08-08). Text
        # UNCHANGED by the split.
        self.assertIn("INSERM", _ORPHANET_REFERENCE)
        self.assertIn("orphadata.com", _ORPHANET_REFERENCE)

    def test_hpo_citation_matches_own_recommended_format(self):
        # HPO's own citation guidance recommends the most recent NAR
        # article (obophenotype.github.io/human-phenotype-ontology/
        # community/cite/, confirmed live 2026-08-08).
        refs = _references(_ir(evidence_sources=["HPO"]))
        hpo_ref = next(r for r in refs if r.startswith("Human Phenotype Ontology"))
        self.assertIn("Nucleic Acids Research", hpo_ref)


class TestBuildClinicalReportCarriesBothBlocks(unittest.TestCase):
    def test_references_is_gated_and_data_attribution_is_fixed(self):
        # REPLACES test_references_key_present_and_includes_orphanet.
        cr = build_clinical_report(_ir(evidence_sources=["ClinVar"]), raw_evidence={})
        self.assertEqual(cr["references"], [_REFERENCES["ClinVar"]])
        self.assertEqual(cr["data_attribution"], [_ORPHANET_REFERENCE, _ALPHAFOLD_REFERENCE])

    def test_data_attribution_is_unconditional(self):
        cr = build_clinical_report(_ir(evidence_sources=[]), raw_evidence={})
        self.assertEqual(cr["references"], [])
        self.assertEqual(cr["data_attribution"], [_ORPHANET_REFERENCE, _ALPHAFOLD_REFERENCE])


def _document(evidence_sources, candidate_interpretation=None):
    cr = candidate_interpretation or build_clinical_report(_ir(evidence_sources=evidence_sources), raw_evidence={})
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


def _legacy_document():
    """A geper_results.json written BEFORE the split: `references` still
    carries the unconditional notices mixed in with the gated entries, and
    there is no `data_attribution` key. Re-rendering it (review/signoff.py
    does exactly that) must still put each entry in the right block."""
    cr = build_clinical_report(_ir(evidence_sources=["HPO", "ClinVar"]), raw_evidence={})
    cr["references"] = [_REFERENCES["HPO"], _REFERENCES["ClinVar"], _ORPHANET_REFERENCE, _ALPHAFOLD_REFERENCE]
    del cr["data_attribution"]
    return _document(None, candidate_interpretation=cr)


def _markdown_blocks(md):
    """The bullet lines under each of the two Markdown headings."""
    lines = md.splitlines()
    evidence_at = lines.index(f"### 16. {EVIDENCE_HEADING}")
    attribution_at = lines.index(f"### {ATTRIBUTION_HEADING}")
    next_section = next(i for i in range(attribution_at + 1, len(lines)) if lines[i].startswith("### "))
    evidence = [ln[2:] for ln in lines[evidence_at + 1 : attribution_at] if ln.startswith("- ")]
    attribution = [ln[2:] for ln in lines[attribution_at + 1 : next_section] if ln.startswith("- ")]
    return evidence, attribution, lines[evidence_at + 1 : attribution_at]


class TestMarkdownReferencesRendering(unittest.TestCase):
    def test_two_blocks_replace_the_single_references_heading(self):
        # REPLACES test_orphanet_appears_unconditionally_in_markdown's pin of
        # "### 16. References".
        md = ReportGenerator().generate(_document(evidence_sources=["ClinVar"]))
        self.assertNotIn("### 16. References", md)
        self.assertIn(f"### 16. {EVIDENCE_HEADING}", md)
        self.assertIn(f"### {ATTRIBUTION_HEADING}", md)
        self.assertLess(md.index(EVIDENCE_HEADING), md.index(f"### {ATTRIBUTION_HEADING}"))

    def test_hpo_clinvar_run_lists_exactly_those_two_under_evidence_sources(self):
        md = ReportGenerator().generate(_document(evidence_sources=["HPO", "ClinVar"]))
        evidence, attribution, _ = _markdown_blocks(md)
        self.assertEqual(evidence, [_REFERENCES["HPO"], _REFERENCES["ClinVar"]])
        # Orphanet/AlphaFold still appear -- unconditionally, text unchanged
        # -- but only as attribution, never as a source this run used.
        self.assertEqual(attribution, [_ORPHANET_REFERENCE, _ALPHAFOLD_REFERENCE])

    def test_hpo_absent_from_markdown_when_not_evaluated(self):
        md = ReportGenerator().generate(_document(evidence_sources=["ClinVar"]))
        self.assertNotIn("Human Phenotype Ontology", md)

    def test_run_with_no_evidence_sources_says_so_and_still_attributes(self):
        md = ReportGenerator().generate(_document(evidence_sources=[]))
        evidence, attribution, raw_block = _markdown_blocks(md)
        self.assertEqual(evidence, [])
        self.assertIn("*No evidence sources contributed to this variant's interpretation.*", raw_block)
        self.assertEqual(attribution, [_ORPHANET_REFERENCE, _ALPHAFOLD_REFERENCE])

    def test_legacy_mixed_list_is_split_on_rerender(self):
        evidence, attribution, _ = _markdown_blocks(ReportGenerator().generate(_legacy_document()))
        self.assertEqual(evidence, [_REFERENCES["HPO"], _REFERENCES["ClinVar"]])
        self.assertEqual(attribution, [_ORPHANET_REFERENCE, _ALPHAFOLD_REFERENCE])


def _pdf_text(render, document):
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "r.pdf")
        render(document, out)
        return " ".join("\n".join(p.extract_text() for p in PdfReader(out).pages).split())


class _PdfBlocksContract:
    """Shared by the full and short PDF: same two labelled blocks."""

    render = None

    def _blocks(self, document):
        text = _pdf_text(type(self).render, document)
        self.assertNotIn("References:", text)
        start = text.index(f"{EVIDENCE_HEADING}:")
        split = text.index(f"{ATTRIBUTION_HEADING}:", start)
        return text[start:split], text[split:]

    def _assert_exactly(self, evidence_block, entries):
        # pypdf extracts ReportLab's bullet glyph as "", not "•", so
        # "exactly these entries" is checked by removing the heading and each
        # expected entry and requiring nothing but bullet/whitespace residue.
        residue = evidence_block.replace(f"{EVIDENCE_HEADING}:", "", 1)
        for entry in entries:
            self.assertIn(entry, residue)
            residue = residue.replace(entry, "", 1)
        self.assertEqual(residue.replace("•", "").replace("", "").strip(), "", f"extra text: {residue!r}")

    def test_hpo_clinvar_run_lists_exactly_those_two_under_evidence_sources(self):
        evidence, attribution = self._blocks(_document(evidence_sources=["HPO", "ClinVar"]))
        self._assert_exactly(evidence, [_REFERENCES["HPO"], _REFERENCES["ClinVar"]])
        self.assertNotIn("Orphanet", evidence)
        self.assertNotIn("AlphaFold", evidence)
        self.assertIn("Orphanet/Orphadata", attribution)
        self.assertIn("INSERM", attribution)
        self.assertIn("AlphaFold Protein Structure Database", attribution)

    def test_hpo_absent_when_not_evaluated(self):
        evidence, attribution = self._blocks(_document(evidence_sources=["ClinVar"]))
        self.assertNotIn("Human Phenotype Ontology", evidence + attribution)
        self.assertIn("Orphanet/Orphadata", attribution)

    def test_no_evidence_sources_says_so_and_still_attributes(self):
        evidence, attribution = self._blocks(_document(evidence_sources=[]))
        self._assert_exactly(evidence, ["No evidence sources contributed to this variant's interpretation."])
        self.assertIn("Orphanet/Orphadata", attribution)

    def test_legacy_mixed_list_is_split_on_rerender(self):
        evidence, attribution = self._blocks(_legacy_document())
        self._assert_exactly(evidence, [_REFERENCES["HPO"], _REFERENCES["ClinVar"]])
        self.assertNotIn("Orphanet", evidence)
        self.assertIn("Orphanet/Orphadata", attribution)


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestFullPdfReferencesRendering(_PdfBlocksContract, unittest.TestCase):
    """REPLACES the full-PDF tests that pinned "References:". Real ReportLab
    PDF generation + pypdf text extraction."""

    render = staticmethod(generate_pdf)


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestShortPdfReferencesRendering(_PdfBlocksContract, unittest.TestCase):
    """
    REPLACES TestShortPdfNowRendersReferences (which itself superseded
    TestShortPdfDeliberatelyOmitsReferences). The AM-04 item 2 ruling still
    holds -- "a clinician signing should be able to see what the conclusion
    rests on" -- and ruling (A) now says what that list may contain: only
    what the run used, with the licence notices labelled as attribution.
    """

    render = staticmethod(generate_short_pdf)


if __name__ == "__main__":
    unittest.main()
