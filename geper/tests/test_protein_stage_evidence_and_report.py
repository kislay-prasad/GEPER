"""
Two follow-ups to the variant-aware ESM-2 input (`pipeline/variant_peptides.py`):

1. `pipeline/stage_schemas.py::_protein_stage_found` -- the protein stage's `StageEvidence` must not read
   FOUND when ESM-2 was skipped just because the legacy window translation happens to exist.
2. `report/report_generator.py::ReportGenerator._render_protein` -- result files written before
   `esm2_input` existed must keep their original wording (their local-window translation WAS what ESM-2
   received); only new-shape results may say it was not given to ESM-2.
"""

import copy
import unittest

from pipeline.stage_schemas import StageStatus, build_raw_evidence_bundle
from report.report_generator import ReportGenerator

_TRANSLATION = {"ref_protein": "MYSANTEHQVHFLPFLCLFPDCG*", "alt_protein": "MYSANTEHQVHFLPFLCLFPDCG*"}
_ESM2 = {"embedding_mean": [0.1], "embedding_dim": 1280, "meta": {"sequence_length": 1022}}

# ---- result shapes -------------------------------------------------------------------------
OLD_RAN = {"skipped": False, "translation": _TRANSLATION, "esm2": _ESM2}
OLD_NO_ORF = {
    "skipped": True,
    "reason": "no open reading frame (start codon) found in window",
    "translation": {"ref_protein": None, "alt_protein": None},
}
NEW_USED = {
    "skipped": False,
    "translation": _TRANSLATION,
    "legacy_window_orf_found": True,
    "esm2": _ESM2,
    "esm2_input": {
        "basis": "transcript_cds",
        "status": "used",
        "transcript_id": "ENST00000357654",
        "codon_number": 694,
        "ref_aa": "S",
        "alt_aa": "R",
        "window_start": 184,
        "window_end": 1205,
        "alt_peptide": "PNEQTSKRHDRDTFPELKLTN",
    },
}
NEW_SKIPPED_LEGACY_ORF = {  # ESM-2 skipped, but the legacy window found an ORF -> top-level skipped False
    "skipped": False,
    "translation": _TRANSLATION,
    "legacy_window_orf_found": True,
    "reason": "No trusted transcript structure was resolved for this variant.",
    "esm2_skip_code": "no_trusted_transcript",
    "esm2_input": {
        "basis": "transcript_cds",
        "status": "skipped",
        "skip_code": "no_trusted_transcript",
        "skip_reason": "No trusted transcript structure was resolved for this variant.",
    },
}
NEW_SYNONYMOUS = {
    "skipped": False,
    "translation": _TRANSLATION,
    "legacy_window_orf_found": True,
    "reason": "Synonymous change: the reference and alternate proteins are identical.",
    "esm2_skip_code": "protein_unchanged",
    "esm2_input": {
        "basis": "transcript_cds",
        "status": "skipped",
        "skip_code": "protein_unchanged",
        "skip_reason": "Synonymous change: the reference and alternate proteins are identical.",
        "category": "synonymous",
    },
}
NEW_NO_ORF_SKIPPED = {  # legacy window had no ORF AND ESM-2 was skipped -> top-level skipped True
    "skipped": True,
    "translation": {"ref_protein": None, "alt_protein": None},
    "legacy_window_orf_found": False,
    "reason": "No trusted transcript structure was resolved for this variant.",
    "esm2_skip_code": "no_trusted_transcript",
    "esm2_input": {"basis": "transcript_cds", "status": "skipped", "skip_code": "no_trusted_transcript"},
}


def _protein_status(protein_result):
    bundle, err = build_raw_evidence_bundle(variant_ref="17:43093449G>T", protein_result=protein_result)
    assert err is None, err
    return bundle.protein.status


class TestProteinStageStatusEvidence(unittest.TestCase):
    """The four required cases, exercised through the real `RawEvidenceBundle` wiring."""

    def test_esm2_ran_is_found(self):
        self.assertIs(_protein_status(NEW_USED), StageStatus.FOUND)

    def test_new_result_with_esm2_skipped_is_not_found_even_though_the_legacy_window_has_an_orf(self):
        self.assertTrue(NEW_SKIPPED_LEGACY_ORF["translation"]["alt_protein"])
        self.assertIs(_protein_status(NEW_SKIPPED_LEGACY_ORF), StageStatus.NOT_FOUND)
        self.assertIs(_protein_status(NEW_SYNONYMOUS), StageStatus.NOT_FOUND)

    def test_old_result_without_esm2_input_keeps_its_existing_behaviour(self):
        self.assertIs(_protein_status(OLD_RAN), StageStatus.FOUND)
        # Old shape had no esm2_input; "not skipped" alone meant FOUND, and still does.
        self.assertIs(_protein_status({"skipped": False, "translation": _TRANSLATION}), StageStatus.FOUND)

    def test_top_level_skipped_stays_not_run_for_both_shapes(self):
        self.assertIs(_protein_status(OLD_NO_ORF), StageStatus.NOT_RUN)
        self.assertIs(_protein_status(NEW_NO_ORF_SKIPPED), StageStatus.NOT_RUN)

    def test_error_still_wins_over_everything(self):
        errored = dict(NEW_SKIPPED_LEGACY_ORF, skipped=True, error="ESM-2 CUDA OOM")
        self.assertIs(_protein_status(errored), StageStatus.ERROR)

    def test_empty_or_missing_result_is_not_run(self):
        self.assertIs(_protein_status(None), StageStatus.NOT_RUN)
        self.assertIs(_protein_status({}), StageStatus.NOT_RUN)

    def test_building_the_bundle_does_not_mutate_the_stage_result(self):
        for shape in (OLD_RAN, NEW_USED, NEW_SKIPPED_LEGACY_ORF, NEW_NO_ORF_SKIPPED):
            before = copy.deepcopy(shape)
            _protein_status(shape)
            self.assertEqual(shape, before)

    def test_confidence_and_completeness_are_driven_by_the_esm2_result_not_by_this_status(self):
        """The status change must not alter what the scoring readers see: they key on `esm2` being present."""
        from pipeline.confidence_engine import ConfidenceEngine

        rna = {"skipped": False, "embedding_dim": 640}
        ran = ConfidenceEngine._sequence_context_quality(["HyenaDNA"], rna, NEW_SKIPPED_LEGACY_ORF, 1.0)
        self.assertNotIn("ESM2", ran.sources_checked)
        used = ConfidenceEngine._sequence_context_quality(["HyenaDNA"], rna, NEW_USED, 1.0)
        self.assertIn("ESM2", used.sources_checked)
        old = ConfidenceEngine._sequence_context_quality(["HyenaDNA"], rna, OLD_RAN, 1.0)
        self.assertEqual(old.to_dict(), used.to_dict())  # same scoring for an old-shape run that did use ESM-2


class TestRenderProteinWording(unittest.TestCase):
    def _text(self, protein_result):
        return "\n".join(ReportGenerator._render_protein(protein_result))

    # ---- old shapes: original wording preserved -------------------------------------------------
    def test_old_result_keeps_original_wording_and_does_not_say_esm2_was_not_given_it(self):
        text = self._text(OLD_RAN)
        self.assertIn("**Reference protein (local window):**", text)
        self.assertIn("**Alternate protein (local window):**", text)
        self.assertIn("this is the raw sequence handed to ESM-2", text)
        self.assertIn("(no exon/splicing awareness)", text)
        self.assertIn("Embedding dim: 1280", text)
        self.assertNotIn("NOT given to ESM-2", text)
        self.assertNotIn("legacy local window", text)
        self.assertNotIn("ESM-2 input (transcript", text)
        self.assertNotIn("ESM-2 was not run", text)

    def test_old_result_rendering_is_byte_identical_to_master(self):
        expected = [
            "### Protein / ESM-2 Analysis",
            "",
            "- **Reference protein (local window):** `MYSANTEHQVHFLPFLCLFPDCG*`",
            "- **Alternate protein (local window):** `MYSANTEHQVHFLPFLCLFPDCG*`",
            "  - _Translated from the first start codon found in a short flanking DNA window "
            "(no exon/splicing awareness); this is the raw sequence handed to ESM-2, not a "
            "clinical call. It is NOT the transcript-verified consequence -- see the ACMG "
            "criteria (BP7/BP1/PS1/PM5/PVS1) in the Clinical Report above for that._",
            "- Embedding dim: 1280",
            "",
        ]
        self.assertEqual(ReportGenerator._render_protein(OLD_RAN), expected)

    def test_old_shaped_result_with_top_level_skipped_true(self):
        self.assertEqual(
            ReportGenerator._render_protein(OLD_NO_ORF),
            [
                "### Protein / ESM-2 Analysis",
                "",
                "_Skipped: no open reading frame (start codon) found in window._",
                "",
            ],
        )

    def test_old_error_shape_unchanged(self):
        text = self._text({"skipped": True, "reason": "x", "error": "ESM-2 CUDA OOM"})
        self.assertIn("_Failed: ESM-2 CUDA OOM", text)

    # ---- new shapes ------------------------------------------------------------------------------
    def test_new_result_with_esm2_used_explains_the_transcript_aware_input(self):
        text = self._text(NEW_USED)
        self.assertIn("ESM-2 input (transcript ENST00000357654, codon 694, S>R, residues 184-1205)", text)
        self.assertIn("`PNEQTSKRHDRDTFPELKLTN`", text)
        self.assertIn("legacy local window", text)
        self.assertIn("NOT given to ESM-2", text)
        self.assertIn("Embedding dim: 1280", text)
        self.assertNotIn("ESM-2 was not run", text)

    def test_new_result_with_esm2_skipped_states_the_reason_and_labels_the_legacy_translation(self):
        text = self._text(NEW_SKIPPED_LEGACY_ORF)
        self.assertIn("ESM-2 was not run for this variant (no_trusted_transcript)", text)
        self.assertIn("No trusted transcript structure was resolved for this variant.", text)
        self.assertIn("(legacy local window)", text)
        self.assertIn("NOT given to ESM-2", text)
        self.assertNotIn("Embedding dim", text)
        self.assertNotIn("ESM-2 input (transcript", text)

    def test_new_synonymous_skip_is_reported_with_its_code(self):
        text = self._text(NEW_SYNONYMOUS)
        self.assertIn("(protein_unchanged)", text)
        self.assertIn("Synonymous change", text)
        self.assertNotIn("Embedding dim", text)

    def test_skip_message_has_exactly_one_closing_period(self):
        for reason, expected in (
            ("A full sentence.", "_Skipped: A full sentence._"),
            ("No trailing period", "_Skipped: No trailing period._"),
            ("Trailing space then period. ", "_Skipped: Trailing space then period._"),
        ):
            with self.subTest(reason=reason):
                lines = ReportGenerator._render_protein({"skipped": True, "reason": reason})
                self.assertEqual(lines[2], expected)
                self.assertNotIn("..", lines[2])

    def test_new_result_with_no_usable_orf_keeps_the_accurate_skip_explanation(self):
        self.assertEqual(
            ReportGenerator._render_protein(NEW_NO_ORF_SKIPPED),
            [
                "### Protein / ESM-2 Analysis",
                "",
                "_Skipped: No trusted transcript structure was resolved for this variant._",
                "",
            ],
        )


if __name__ == "__main__":
    unittest.main()
