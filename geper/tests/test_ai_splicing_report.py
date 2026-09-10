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


class TestJSONKeyPresentOnGenuineCrash(unittest.TestCase):
    """
    Third surface of the same defect the Markdown renderer and
    `EnsembleManager.evaluate()` were already fixed for (2026-09-11):
    `build_variant_result`'s `ai_splicing_ensemble` key was gated on
    `models_used` alone, so a genuine double-model-crash (`error` set,
    `models_used=[]`) was indistinguishable, in the JSON output, from a
    stage nobody ever attempted -- no key at all, exactly the shape a
    legacy caller who never passed the kwarg gets. This is a STRONGER
    fabricated absence than the Markdown one it mirrors: a missing JSON
    key is not a claim a downstream SYSTEM consumer can be suspicious
    of the way a human reader can be suspicious of a missing section.

    THE CONTROL THAT MATTERS MOST: a genuinely disabled/unavailable
    pair must still omit the key EXACTLY as it does today -- see
    `test_key_absent_when_zero_models_were_used` above, unchanged and
    re-asserted here for locality with its crash counterpart. A JSON
    consumer must never newly see a key it has never seen for the
    clean-disable case; only the crash case is new.
    """

    @staticmethod
    def _ensemble_crashed():
        """The exact shape `EnsembleManager.evaluate()` returns for
        n==0 with a genuine crash -- pinned to that function's real
        branch via test_ensemble_manager.py::TestZeroModelsBecauseBothCrashed,
        not invented here."""
        return {
            "models_used": [],
            "individual_scores": {},
            "consensus_score": None,
            "confidence": None,
            "agreement_percentage": None,
            "classification": None,
            "basis": "no_models",
            "error": "enformer: CUDA out of memory; borzoi: weights checksum mismatch",
            "reasoning": (
                "AI splicing ensemble inference failed (enformer: CUDA out of memory; "
                "borzoi: weights checksum mismatch) -- this is a failed run, not evidence "
                "that Enformer/Borzoi found no splicing effect."
            ),
        }

    def test_key_present_on_genuine_crash(self):
        result = build_variant_result(**_minimal_variant_kwargs(ai_splicing_ensemble_result=self._ensemble_crashed()))
        self.assertIn("ai_splicing_ensemble", result)
        self.assertEqual(result["ai_splicing_ensemble"]["models_used"], [])
        self.assertIn("CUDA out of memory", result["ai_splicing_ensemble"]["error"])
        self.assertIn("weights checksum mismatch", result["ai_splicing_ensemble"]["error"])

    def test_key_still_absent_when_zero_models_and_no_error_the_control(self):
        """CONTROL, restated for locality: a clean disable (no error,
        models_used=[]) must still omit the key exactly as before this
        fix -- a JSON consumer must never newly see a key it has never
        seen for this case."""
        result = build_variant_result(**_minimal_variant_kwargs(ai_splicing_ensemble_result=_ensemble([])))
        self.assertNotIn("ai_splicing_ensemble", result)

    def test_key_still_absent_when_kwarg_never_passed_the_control(self):
        """CONTROL, restated: every existing/legacy caller that never
        passes this kwarg at all must keep getting a result dict with
        exactly the same key set as before this fix ever existed."""
        result = build_variant_result(**_minimal_variant_kwargs())
        self.assertNotIn("ai_splicing_ensemble", result)

    def test_end_to_end_through_the_real_ensemble_manager_and_json_builder(self):
        """Not just a hand-built fixture: drives the REAL
        `EnsembleManager.evaluate()` down its double-crash branch and
        feeds the REAL result into the REAL `build_variant_result`, so
        this proves the producer and the JSON assembler agree with each
        other, not each with its own assumption about the other's
        shape -- same standard as the Markdown end-to-end test in
        `TestDoubleCrashRendersAFailureNotAnAbsentSection` below."""
        from unittest import mock

        from pipeline.models.ensemble import EnsembleManager

        manager = mock.Mock()
        manager.predict.return_value = None
        manager.last_inference_errors.return_value = {
            "enformer": "CUDA out of memory",
            "borzoi": "weights checksum mismatch",
        }
        ensemble_result = EnsembleManager(manager=manager).evaluate("A" * 10, "T" * 10)

        result = build_variant_result(**_minimal_variant_kwargs(ai_splicing_ensemble_result=ensemble_result))

        self.assertIn("ai_splicing_ensemble", result)
        self.assertIn("CUDA out of memory", result["ai_splicing_ensemble"]["error"])

    def test_all_pre_existing_keys_still_unaffected_on_crash(self):
        """Backward compatibility, extended to the crash case: adding
        the crash's error information must not change any OTHER key in
        the result dict."""
        without_ensemble = build_variant_result(**_minimal_variant_kwargs())
        with_crash = build_variant_result(
            **_minimal_variant_kwargs(ai_splicing_ensemble_result=self._ensemble_crashed())
        )
        keys_without = set(without_ensemble)
        keys_with = set(with_crash) - {"ai_splicing_ensemble"}
        self.assertEqual(keys_without, keys_with)
        for key in keys_without:
            self.assertEqual(without_ensemble[key], with_crash[key])


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


class TestDoubleCrashRendersAFailureNotAnAbsentSection(unittest.TestCase):
    """
    Sweep finding 3 (2026-09-11): before this fix, a genuine double
    crash (both Enformer and Borzoi raised `ModelInferenceError`,
    caught inside `ModelManager.predict()` and never re-raised) reached
    this renderer with `models_used=[]` and no way to distinguish it
    from a clean double-disable -- `_render_ai_splicing_ensemble`
    returned `[]`, THE ENTIRE SECTION VANISHED, not even a "Failed"
    line. `EnsembleManager.evaluate()` now surfaces the crash via a new
    `error` field (only ever set for a genuine crash, never for a clean
    disable -- see `pipeline/models/ensemble.py`), and this renderer
    must turn that into a real section.
    """

    @staticmethod
    def _double_crash_result():
        """The exact shape `EnsembleManager.evaluate()` now returns for
        n==0 with both models genuinely crashed -- pinned to that
        function's real n==0/failed branch, not invented here, via
        `test_ensemble_manager.py::TestZeroModelsBecauseBothCrashed`."""
        return {
            "models_used": [],
            "individual_scores": {},
            "consensus_score": None,
            "confidence": None,
            "agreement_percentage": None,
            "classification": None,
            "basis": "no_models",
            "error": "enformer: CUDA out of memory; borzoi: weights checksum mismatch",
            "reasoning": (
                "AI splicing ensemble inference failed (enformer: CUDA out of memory; "
                "borzoi: weights checksum mismatch) -- this is a failed run, not evidence "
                "that Enformer/Borzoi found no splicing effect."
            ),
        }

    def test_double_crash_renders_a_real_section_not_an_empty_list(self):
        lines = ReportGenerator._render_ai_splicing_ensemble(self._double_crash_result())
        self.assertNotEqual(lines, [], "a genuine crash must produce a section, not silence")
        text = "\n".join(lines)
        self.assertIn("AI Splicing Analysis", text)
        self.assertIn("_Failed:", text)
        self.assertIn("CUDA out of memory", text)
        self.assertIn("weights checksum mismatch", text)
        self.assertIn(
            "see the AI Model Status table above",
            text,
            "must cross-reference the AI Models table, matching _render_mmsplice/_render_rna/"
            "_render_protein/_render_alphamissense's identical wording for the same situation",
        )

    def test_clean_double_disable_still_hides_the_section_entirely(self):
        """CONTROL: this is the case `test_hidden_entirely_when_zero_models_used`
        above already pins, restated here for locality with the crash
        test it is the counterpart to -- a genuinely disabled/absent
        model pair (`error=None`) must still render nothing at all."""
        lines = ReportGenerator._render_ai_splicing_ensemble(_ensemble([]))
        self.assertEqual(lines, [])

    def test_end_to_end_through_the_real_ensemble_manager(self):
        """Not just a hand-built fixture: drives the REAL
        `EnsembleManager.evaluate()` down its double-crash branch and
        renders the REAL result, so this proves the producer and the
        renderer agree with each other, not each with its own
        assumption about the other's shape."""
        from unittest import mock

        from pipeline.models.ensemble import EnsembleManager

        manager = mock.Mock()
        manager.predict.return_value = None
        manager.last_inference_errors.return_value = {
            "enformer": "CUDA out of memory",
            "borzoi": "weights checksum mismatch",
        }
        ensemble_result = EnsembleManager(manager=manager).evaluate("A" * 10, "T" * 10)

        lines = ReportGenerator._render_ai_splicing_ensemble(ensemble_result)
        text = "\n".join(lines)
        self.assertNotEqual(lines, [])
        self.assertIn("_Failed:", text)
        self.assertIn("CUDA out of memory", text)
        self.assertIn("weights checksum mismatch", text)


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
