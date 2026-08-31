"""
FIX #2: `mapping_unavailable_reason` is recorded on `AlphaFoldAnnotation`
(pipeline/alphafold/models.py:73, set by `provider.py::_build_annotation`
from `mapping_gate.py::check_alphafold_mapping_gate`'s verdict) but was
never read by any of the three places that render "residue-specific
AlphaFold confidence is unavailable" to a clinician.

WHAT RENDERING ACTUALLY DID WITH IT, ESTABLISHED BEFORE CHANGING (all
three confirmed by direct execution against the unmodified module, not
described from reading the source):

  1. pipeline/interpretation.py::InterpretationEngine.
     _biological_context_evidence -- when residue-specific confidence
     is unavailable but a whole-protein mean exists, wrote a sentence
     that said NOTHING about why residue-specific data is missing.
     Silent, not wrong -- but silence where a specific reason exists.

  2. report/report_generator.py::ReportGenerator._render_clinical_report,
     "### 9. Structural Knowledge" section (fed by clinical_report_
     builder.py::_structural_knowledge) -- rendered a HARDCODED generic
     label: "whole-protein mean (variant residue position unknown)".
     `mapping_gate.py::check_alphafold_mapping_gate` has SIX distinct
     failure reasons; only the first ("no protein position was resolved
     for this variant") actually matches "position unknown". Confirmed
     by execution: feeding a real reason ("protein position 175 is not
     modelled in the AlphaFold structure" -- position WAS known) still
     rendered "(variant residue position unknown)" -- FACTUALLY WRONG,
     not merely vague, in 5 of the gate's 6 failure branches.

  3. report/report_generator.py::ReportGenerator._render_alphafold,
     the "Annotation Detail" panel -- same defect, worse wording:
     "not checked -- this variant's residue position could not be
     determined from the transcript structure." -- an explicit,
     specific-sounding claim that is simply false whenever the real
     reason is anything other than gate reason #1.

  Neither report/summary.py (the PDF) nor clinical_report_builder.py
  itself render prose from this field -- confirmed by grep, no match
  for AlphaFold/structural_knowledge/mapping_unavailable_reason in
  summary.py. This defect has zero PDF impact; only the Markdown
  report and the raw JSON (`_structural_knowledge`'s returned dict,
  which reaches geper_results.json) are affected.

THE FIX: read `mapping_unavailable_reason` at all three sites and use
it in place of (interpretation.py, _render_alphafold) or alongside
(the Structural Knowledge section keeps the existing generic phrase
only as a fallback for the -- currently unreachable in practice, since
the model's docstring says this field is always set alongside a
found=True/no-residue-band result -- case where the field is somehow
absent) the hardcoded text.
"""

import unittest

from pipeline.interpretation import InterpretationEngine
from report.clinical_report_builder import _structural_knowledge
from report.report_generator import ReportGenerator


def _alphafold_result_with_mean_only(reason):
    return {
        "skipped": False,
        "error": None,
        "found": True,
        "affected_residue_band": None,
        "affected_residue_plddt": None,
        "mean_plddt": 82.4,
        "mean_plddt_band": "confident",
        "protein_position": 175,
        "mapping_unavailable_reason": reason,
    }


class TestBiologicalContextEvidenceStatesTheReason(unittest.TestCase):
    def test_the_fallback_sentence_names_the_actual_reason(self):
        reason = "protein position 175 is not modelled in the AlphaFold structure"
        lines = InterpretationEngine._biological_context_evidence(
            alphafold_result=_alphafold_result_with_mean_only(reason)
        )
        text = "\n".join(lines)
        self.assertIn(
            reason,
            text,
            "the whole-protein-mean fallback sentence said nothing about why residue-specific "
            "confidence is unavailable, even though the real reason is on the result",
        )

    def test_no_reason_present_still_renders_the_fallback_sentence(self):
        """Control: a result with the field genuinely absent (e.g. an
        older cached result, or the not-yet-modelled defensive case)
        must not lose the sentence entirely."""
        result = _alphafold_result_with_mean_only(None)
        lines = InterpretationEngine._biological_context_evidence(alphafold_result=result)
        self.assertTrue(any("confident" in line and "mean pLDDT" in line for line in lines))


class TestStructuralKnowledgeDictCarriesTheReason(unittest.TestCase):
    def test_mapping_unavailable_reason_reaches_the_dict(self):
        reason = "protein position 175 is not modelled in the AlphaFold structure"
        raw = {"alphafold": _alphafold_result_with_mean_only(reason)}
        result = _structural_knowledge(raw)
        self.assertEqual(
            result.get("mapping_unavailable_reason"),
            reason,
            "_structural_knowledge builds the dict every renderer reads from -- it must carry "
            "the reason through, not just confidence_band_is_residue_specific=False",
        )


class TestStructuralKnowledgeSectionRendersTheRealReason(unittest.TestCase):
    """End-to-end: the exact '### 9. Structural Knowledge' line
    `_render_clinical_report` writes, via the smallest fixture that
    satisfies its other nine required top-level keys (all `.get()`-safe
    empty/pending shapes -- this section is the only one under test)."""

    def _document(self, structural_knowledge):
        return {
            "executive_summary": "",
            "acmg_classification": {},
            "confidence": {"pending": True},
            "priority": {"pending": True},
            "ai_consensus": {},
            "protein_knowledge": {"uniprot": {}, "interpro": {}},
            "structural_knowledge": structural_knowledge,
            "population_evidence": {"gnomad": {"queried": False}, "dbsnp": {"queried": False, "found": False}},
            "clinical_evidence": {"clinvar": {}, "clingen": {}},
            "sequence_context": {"blast": {}},
        }

    def _alphafold_line(self, structural_knowledge):
        lines = ReportGenerator._render_clinical_report(self._document(structural_knowledge), {})
        matches = [line for line in lines if line.startswith("- **AlphaFold DB:**")]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_pre_fix_shape_says_position_unknown_even_when_it_was_known(self):
        """Documents the defect as observed by direct execution against
        the unmodified module before this fix: 'protein position 175 is
        not modelled' (position WAS known, gate reason #6) still
        rendered as 'variant residue position unknown' (gate reason
        #1's wording) -- this is what a reader saw."""
        line = self._alphafold_line(
            {
                "available": True,
                "confidence_band": "confident",
                "confidence_band_is_residue_specific": False,
                "protein_position": None,
                "model_version": "v4",
                "mapping_unavailable_reason": "protein position 175 is not modelled in the AlphaFold structure",
            }
        )
        self.assertEqual(
            line,
            "- **AlphaFold DB:** confidence band 'confident' whole-protein mean "
            "(protein position 175 is not modelled in the AlphaFold structure) (model v4)",
        )

    def test_residue_specific_case_is_unaffected(self):
        line = self._alphafold_line(
            {
                "available": True,
                "confidence_band": "confident",
                "confidence_band_is_residue_specific": True,
                "protein_position": 175,
                "model_version": "v4",
                "mapping_unavailable_reason": None,
            }
        )
        self.assertEqual(
            line,
            "- **AlphaFold DB:** confidence band 'confident' at residue 175 (transcript-verified) (model v4)",
        )

    def test_no_reason_present_falls_back_to_the_generic_phrase(self):
        line = self._alphafold_line(
            {
                "available": True,
                "confidence_band": "confident",
                "confidence_band_is_residue_specific": False,
                "protein_position": None,
                "model_version": "v4",
                "mapping_unavailable_reason": None,
            }
        )
        self.assertIn("variant residue position unknown", line)


class TestRenderAlphafoldAnnotationDetailUsesTheRealReason(unittest.TestCase):
    def test_the_real_reason_replaces_the_generic_and_sometimes_false_claim(self):
        reason = "the AlphaFold model numbers residues 1-160, which is not contained in its declared UniProt span 1-200 (fragment-local numbering, truncation, or a mismatched accession)"
        result = _alphafold_result_with_mean_only(reason)
        result.update({"accession": "P38398", "source": "alphafold_db_api", "uniprot_start": 1, "uniprot_end": 200})
        lines = ReportGenerator._render_alphafold(result)
        text = "\n".join(lines)
        self.assertIn(reason, text)
        self.assertNotIn(
            "could not be determined from the transcript structure",
            text,
            "the old generic claim is false for this reason (fragment-local numbering, not an "
            "unresolved transcript position) and must not still be present",
        )

    def test_no_reason_present_falls_back_to_the_old_generic_text(self):
        result = _alphafold_result_with_mean_only(None)
        result.update({"accession": "P38398", "source": "alphafold_db_api", "uniprot_start": 1, "uniprot_end": 200})
        lines = ReportGenerator._render_alphafold(result)
        text = "\n".join(lines)
        self.assertIn("could not be determined from the transcript structure", text)


if __name__ == "__main__":
    unittest.main()
