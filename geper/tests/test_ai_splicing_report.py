"""
Tests for the AI Splicing Analysis section in:
  - report/json_builder.py::build_variant_result (JSON schema)
  - report/report_generator.py::ReportGenerator._render_ai_splicing_ensemble (Markdown)

Both must:
  - include/render the section ONLY when real ensemble evidence
    exists (`models_used` non-empty)
  - be fully backward compatible: any existing caller that never
    passes ensemble data gets an identical result to before this
    integration existed (JSON: key entirely absent; Markdown: no
    heading/lines added at all)
"""

import unittest

from report.json_builder import build_variant_result
from report.report_generator import ReportGenerator


def _minimal_variant_kwargs(**overrides):
    base = dict(
        variant_dict={"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
        sequence_context={},
        dna_model_results={},
        rna_result={},
        protein_result={},
        blast_result={},
        clinvar_result={},
        dbsnp_result={},
        interpretation={},
        errors=[],
    )
    base.update(overrides)
    return base


def _ensemble(
    models_used, classification="large_effect", consensus_score=0.8, agreement_percentage=95.0, confidence=0.6
):
    if not models_used:
        return {
            "models_used": [],
            "individual_scores": {},
            "consensus_score": None,
            "confidence": None,
            "agreement_percentage": None,
            "classification": None,
            "basis": "no_models",
            "reasoning": "No splicing/regulatory AI model was available.",
        }
    return {
        "models_used": models_used,
        "individual_scores": {
            m: {
                "score": consensus_score,
                "classification": classification,
                "confidence": confidence,
                "meta": {"model": m, "version": f"{m}-test-version"},
            }
            for m in models_used
        },
        "consensus_score": consensus_score,
        "confidence": confidence,
        "agreement_percentage": agreement_percentage if len(models_used) == 2 else None,
        "classification": classification,
        "basis": "single_model" if len(models_used) == 1 else "two_model_consensus",
        "reasoning": f"test reasoning for {models_used}",
    }


class TestJSONReportSchema(unittest.TestCase):
    def test_key_absent_when_no_ensemble_kwarg_passed_at_all(self):
        result = build_variant_result(**_minimal_variant_kwargs())
        self.assertNotIn("ai_splicing_ensemble", result)

    def test_key_absent_when_ensemble_result_is_none(self):
        result = build_variant_result(**_minimal_variant_kwargs(ai_splicing_ensemble_result=None))
        self.assertNotIn("ai_splicing_ensemble", result)

    def test_key_absent_when_zero_models_were_used(self):
        result = build_variant_result(**_minimal_variant_kwargs(ai_splicing_ensemble_result=_ensemble([])))
        self.assertNotIn("ai_splicing_ensemble", result)

    def test_key_present_when_one_model_was_used(self):
        result = build_variant_result(**_minimal_variant_kwargs(ai_splicing_ensemble_result=_ensemble(["enformer"])))
        self.assertIn("ai_splicing_ensemble", result)
        self.assertEqual(result["ai_splicing_ensemble"]["models_used"], ["enformer"])

    def test_key_present_when_two_models_were_used(self):
        result = build_variant_result(
            **_minimal_variant_kwargs(ai_splicing_ensemble_result=_ensemble(["enformer", "borzoi"]))
        )
        self.assertIn("ai_splicing_ensemble", result)
        self.assertEqual(sorted(result["ai_splicing_ensemble"]["models_used"]), ["borzoi", "enformer"])

    def test_all_pre_existing_keys_are_unaffected_by_this_integration(self):
        # Backward compatibility check: build with and without ensemble
        # data and confirm every OTHER key is identical either way.
        without_ensemble = build_variant_result(**_minimal_variant_kwargs())
        with_ensemble = build_variant_result(
            **_minimal_variant_kwargs(ai_splicing_ensemble_result=_ensemble(["enformer"]))
        )
        keys_without = set(without_ensemble) - {"ai_splicing_ensemble"}
        keys_with = set(with_ensemble) - {"ai_splicing_ensemble"}
        self.assertEqual(keys_without, keys_with)
        for key in keys_without:
            self.assertEqual(without_ensemble[key], with_ensemble[key])


class TestMarkdownReportRendering(unittest.TestCase):
    def test_hidden_entirely_when_ensemble_result_is_empty_dict(self):
        lines = ReportGenerator._render_ai_splicing_ensemble({})
        self.assertEqual(lines, [])

    def test_hidden_entirely_when_ensemble_result_is_none_like_falsy(self):
        lines = ReportGenerator._render_ai_splicing_ensemble(None)
        self.assertEqual(lines, [])

    def test_hidden_entirely_when_zero_models_used(self):
        lines = ReportGenerator._render_ai_splicing_ensemble(_ensemble([]))
        self.assertEqual(lines, [])

    def test_rendered_when_one_model_used(self):
        lines = ReportGenerator._render_ai_splicing_ensemble(_ensemble(["borzoi"]))
        text = "\n".join(lines)
        self.assertIn("AI Splicing Analysis", text)
        self.assertIn("borzoi", text)
        self.assertIn("borzoi-test-version", text)

    def test_rendered_when_two_models_used_includes_all_required_fields(self):
        lines = ReportGenerator._render_ai_splicing_ensemble(
            _ensemble(
                ["enformer", "borzoi"], classification="moderate_effect", consensus_score=0.3, agreement_percentage=87.5
            )
        )
        text = "\n".join(lines)
        self.assertIn("AI Splicing Analysis", text)
        # Models + scores (table)
        self.assertIn("enformer", text)
        self.assertIn("borzoi", text)
        self.assertIn("0.300", text)  # score
        self.assertIn("moderate_effect", text)
        # Consensus / Confidence / Agreement / Interpretation labeled lines
        self.assertIn("**Consensus score:**", text)
        self.assertIn("**Confidence:**", text)
        self.assertIn("**Agreement:**", text)
        self.assertIn("87.5%", text)
        self.assertIn("**Interpretation:**", text)

    def test_never_contains_stack_traces_or_technical_error_text(self):
        lines = ReportGenerator._render_ai_splicing_ensemble(_ensemble(["enformer", "borzoi"]))
        text = "\n".join(lines)
        for forbidden in ("Traceback", "Error 403", "Exception", "urllib", "HTTPError"):
            self.assertNotIn(forbidden, text)

    def test_single_model_reports_agreement_as_not_applicable(self):
        lines = ReportGenerator._render_ai_splicing_ensemble(_ensemble(["enformer"]))
        text = "\n".join(lines)
        self.assertIn("not applicable", text)


class TestJSONAndMarkdownAgreeOnWhenSectionExists(unittest.TestCase):
    """The two report formats must agree on the "does ensemble evidence
    exist" condition -- this is the exact property that motivated
    changing json_builder.py's default-placeholder approach to a
    conditional key."""

    def test_both_hide_for_zero_models(self):
        ensemble = _ensemble([])
        result = build_variant_result(**_minimal_variant_kwargs(ai_splicing_ensemble_result=ensemble))
        markdown_lines = ReportGenerator._render_ai_splicing_ensemble(result.get("ai_splicing_ensemble", {}))
        self.assertNotIn("ai_splicing_ensemble", result)
        self.assertEqual(markdown_lines, [])

    def test_both_show_for_two_models(self):
        ensemble = _ensemble(["enformer", "borzoi"])
        result = build_variant_result(**_minimal_variant_kwargs(ai_splicing_ensemble_result=ensemble))
        markdown_lines = ReportGenerator._render_ai_splicing_ensemble(result.get("ai_splicing_ensemble", {}))
        self.assertIn("ai_splicing_ensemble", result)
        self.assertGreater(len(markdown_lines), 0)


if __name__ == "__main__":
    unittest.main()
