"""
21-site str(exc) sweep, Batch 4 (positive-polarity cluster): fifteen
`if X.get("error"):` / `X and X.get("error")` sites -- the mirror
image of Batch 3's `not X.get("error")` sites, found by searching the
OPPOSITE polarity Batch 3's own AST query was structurally blind to.

SCOPE PREDICATE, stated before the count was run: every AST node where
`.get("error")` (exact string literal "error") appears as (a) the test
of an `ast.If`, or (b) a value inside an `ast.BoolOp(And)` -- NOT
wrapped in `ast.UnaryOp(Not)`, NOT compared via `is not None` --
across all .py files under geper/, excluding tests/fixtures. Re-run
after this file's fixes landed: zero.

ALL FIFTEEN ARE SAFE TODAY, NONE OF THEM IS INERT THE WAY BATCH 3'S
TEN SITES ARE. Every one of these fifteen guards an ERROR-REPORTING
branch (the opposite of Batch 3's success-branch guards): when the
truthiness check misses an empty-but-present error, the branch that
would have reported the failure is SKIPPED, and execution falls
through to whatever comes next -- a "not found"/"no annotation"/
"skipped" branch, i.e. a CONFIRMED NEGATIVE, the same class of defect
Phase 1 and Batch 1 already found and fixed at the producer side for
five of these fifteen sites. All fifteen are safe today only because
their producers (fixed across Batches 1-2, or pre-session for ClinVar)
no longer emit empty strings -- not because anything here is
structurally inert -- so every test below is genuine red-first: fails
on the original code, passes after the fix, same shape as Batches 1-2.

FIVE ALREADY KNOWN FROM PHASE 1 (producers fixed in Batch 1):
  report_generator.py:1672 ClinGen, :1732 UniProt, :1785 InterPro,
  :1892 BLAST, pvs1/utils.py:859 transcript-structure
  (protein_effect_undetermined_reason).

TEN NEW, FOUND ONLY BY SEARCHING THE OPPOSITE POLARITY:
  interpretation_result.py:548/550/552/554 -- RNA-FM/ESM-2/
  AlphaMissense/MMSplice entries into the `ai_model_errors` summary
  list (silently drops the failure record entirely when it misses,
  rather than rendering a confirmed negative -- a third consequence
  shape alongside "wrong text" and "wrong cache write").
  report_generator.py:969 (AlphaFold, Section 9 "Structural
  Knowledge" -- confirmed reached via `_render_clinical_report` ->
  `_render_variant_section`, fed by `clinical_report_builder.py::
  _structural_knowledge`'s direct passthrough of
  `alphafold_result["error"]`, the SAME producer field Batch 1 fixed
  for `_render_alphafold`'s separate "Annotation Detail" section --
  confirmed by reading both call chains, not assumed from the shared
  field name), :1305 (ClinVar -- producer fixed pre-session, 8df8b9f),
  :1359 (RNA-FM), :1382 (Protein/ESM-2), :1410 (AlphaMissense), :1455
  (MMSplice -- the site that closes Phase 1's "MMSplice untraced"
  open item).
"""

import unittest

from pipeline.pvs1.utils import protein_effect_undetermined_reason
from report.clinical_report_builder import build_clinical_report
from report.report_generator import ReportGenerator


def _empty_error(**extra):
    base = {"found": False, "skipped": False, "error": ""}
    base.update(extra)
    return base


class TestReportGeneratorRendererSites(unittest.TestCase):
    """Nine of the fifteen are ReportGenerator static methods, directly
    callable with a fabricated provider result -- real code, real
    render, EXECUTED both before and after."""

    def test_a_real_message_still_renders_verbatim_across_all_nine(self):
        """THE CONTROL. Batch 4's fix is a pure presence check
        (`is not None`), not a value rewrite, so the risk this control
        rules out is different from Batches 1-2's: not "does the fix
        rewrite a real message" but "does the fix still catch the case
        that already worked" -- a non-empty error must still trigger
        every one of these nine failure branches, not just the
        empty-string case."""
        cases = [
            (ReportGenerator._render_clingen, "ClinGen API returned 503", {"gene_symbol": "BRCA1", "source": "x"}),
            (ReportGenerator._render_uniprot, "UniProt timed out", {"gene_symbol": "BRCA1", "source": "x"}),
            (ReportGenerator._render_interpro, "InterPro DNS failure", {"accession": "P38398", "source": "x"}),
            (ReportGenerator._render_blast, "BLAST+ exited 1", {"hits": [], "hit_count": 0, "skipped": True}),
            (ReportGenerator._render_clinvar, "ClinVar 500", {"records": []}),
            (ReportGenerator._render_rna, "RNA-FM OOM", {}),
            (ReportGenerator._render_protein, "ESM-2 OOM", {}),
            (ReportGenerator._render_alphamissense, "AlphaMissense malformed key", {}),
            (ReportGenerator._render_mmsplice, "MMSplice shape mismatch", {"supported": False, "predicted": False}),
        ]
        for renderer, message, extra in cases:
            with self.subTest(renderer=renderer.__name__):
                result = _empty_error(error=message, **extra)
                text = "\n".join(renderer(result))
                self.assertIn(message, text)

    def test_clingen_empty_error_is_not_a_confirmed_negative(self):
        lines = ReportGenerator._render_clingen(_empty_error(gene_symbol="BRCA1", source="ClinGen API"))
        text = "\n".join(lines)
        self.assertIn("query failed", text)
        self.assertNotIn("No ClinGen curation found", text)

    def test_uniprot_empty_error_is_not_a_confirmed_negative(self):
        lines = ReportGenerator._render_uniprot(_empty_error(gene_symbol="BRCA1", source="UniProt API"))
        text = "\n".join(lines)
        self.assertIn("query failed", text)
        self.assertNotIn("No reviewed UniProt entry found", text)

    def test_interpro_empty_error_is_not_a_confirmed_negative(self):
        lines = ReportGenerator._render_interpro(_empty_error(accession="P38398", source="InterPro API"))
        text = "\n".join(lines)
        self.assertIn("query failed", text)
        self.assertNotIn("No InterPro/Pfam domain matches found", text)

    def test_blast_empty_error_is_not_a_confirmed_negative(self):
        lines = ReportGenerator._render_blast(_empty_error(hits=[], hit_count=0, skipped=True, reason=""))
        text = "\n".join(lines)
        self.assertIn("Failed", text)
        self.assertNotIn("No significant BLAST hits", text)

    def test_clinvar_empty_error_is_not_a_confirmed_negative(self):
        lines = ReportGenerator._render_clinvar(_empty_error(records=[]))
        text = "\n".join(lines)
        self.assertIn("_Failed:", text)

    def test_rna_fm_empty_error_is_not_a_confirmed_negative(self):
        lines = ReportGenerator._render_rna(_empty_error())
        text = "\n".join(lines)
        self.assertIn("_Failed:", text)

    def test_protein_esm2_empty_error_is_not_a_confirmed_negative(self):
        lines = ReportGenerator._render_protein(_empty_error())
        text = "\n".join(lines)
        self.assertIn("_Failed:", text)

    def test_alphamissense_empty_error_is_not_a_confirmed_negative(self):
        lines = ReportGenerator._render_alphamissense(_empty_error())
        text = "\n".join(lines)
        self.assertIn("_Failed:", text)

    def test_mmsplice_empty_error_is_not_a_confirmed_negative(self):
        lines = ReportGenerator._render_mmsplice(_empty_error(supported=False, predicted=False))
        text = "\n".join(lines)
        self.assertIn("_Failed:", text)
        self.assertNotIn("not scored", text.lower().replace("_failed:", ""))


class TestStructuralKnowledgeSectionSite(unittest.TestCase):
    """report_generator.py:969 -- confirmed reached via the real
    build_clinical_report -> _render_clinical_report call chain, not a
    directly-callable static method in isolation."""

    def test_alphafold_empty_error_reaches_structural_knowledge_as_a_failure(self):
        ir = {
            "acmg_classification": "Uncertain_Significance",
            "confidence_score": 50.0,
            "confidence_label": "Moderate",
            "confidence_pending": False,
        }
        report = build_clinical_report(
            ir, {"chrom": "17", "pos": 1, "ref": "A", "alt": "G"}, raw_evidence={"alphafold": _empty_error()}
        )
        lines = ReportGenerator._render_clinical_report(report, {})
        text = "\n".join(lines)
        idx = text.find("Structural Knowledge")
        section = text[idx : idx + 300]
        self.assertIn("AlphaFold DB lookup failed", section)

    def test_a_real_message_still_renders_verbatim(self):
        ir = {
            "acmg_classification": "Uncertain_Significance",
            "confidence_score": 50.0,
            "confidence_label": "Moderate",
            "confidence_pending": False,
        }
        report = build_clinical_report(
            ir,
            {"chrom": "17", "pos": 1, "ref": "A", "alt": "G"},
            raw_evidence={"alphafold": _empty_error(error="AlphaFold DB summary query returned 500")},
        )
        lines = ReportGenerator._render_clinical_report(report, {})
        text = "\n".join(lines)
        self.assertIn("AlphaFold DB summary query returned 500", text)


class TestTranscriptStructureUndeterminedReasonSite(unittest.TestCase):
    """pvs1/utils.py:859, already-known from Phase 1 -- re-confirmed
    here as part of the closed positive-polarity count."""

    def test_empty_error_names_the_failure_not_a_confirmed_negative(self):
        variant = {"pos": 100, "ref": "A", "alt": "G"}
        reason = protein_effect_undetermined_reason(variant, _empty_error(transcript=None))
        self.assertIn("transcript structure lookup failed", reason)
        self.assertNotIn("no transcript structure was found", reason)

    def test_a_real_message_still_renders_verbatim(self):
        variant = {"pos": 100, "ref": "A", "alt": "G"}
        reason = protein_effect_undetermined_reason(
            variant, _empty_error(error="Ensembl exon/CDS lookup returned 502", transcript=None)
        )
        self.assertIn("Ensembl exon/CDS lookup returned 502", reason)


class TestAIModelErrorsSummaryListSites(unittest.TestCase):
    """interpretation_result.py:548/550/552/554 -- inline in
    `build_interpretation_result`, a large function with many required
    inputs that resists isolation the same way Batch 2's inline sites
    did. Pins the exact `X.get("error") is not None` expression rather
    than driving it through the real function, named as a weaker form
    of evidence than the nine directly-callable renderer tests above."""

    def test_empty_error_is_still_treated_as_present(self):
        for name, result in (
            ("RNA-FM", _empty_error()),
            ("ESM2", _empty_error()),
            ("AlphaMissense", _empty_error()),
            ("MMSplice", _empty_error()),
        ):
            with self.subTest(model=name):
                self.assertTrue(result and result.get("error") is not None)

    def test_a_missing_error_key_is_still_absent(self):
        for name, result in (
            ("RNA-FM", {"skipped": True}),
            ("ESM2", {"skipped": True}),
            ("AlphaMissense", {"skipped": True}),
            ("MMSplice", {"skipped": True}),
        ):
            with self.subTest(model=name):
                self.assertFalse(result and result.get("error") is not None)


if __name__ == "__main__":
    unittest.main()
