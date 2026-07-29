"""
Unit tests for the new AI model orchestration work:

  - `pipeline.models.status` (Objective 6: Used/Skipped/Disabled/Failed
    for every model, every variant).
  - `GeperPipeline._run_ensemble_stage` / `_build_ai_model_status`
    (Objectives 3/4/7: Enformer + Borzoi wired into variant
    processing, ACMG, and reporting).
  - `ModelManager.last_inference_errors` (distinguishing a per-variant
    inference failure from a plain "not used").
  - `report/json_builder.py` + `report/report_generator.py`'s new
    `ai_model_status` key/section.

Follows the same `GeperPipeline.__new__` bare-pipeline pattern
`tests/test_clingen_integration.py` / `tests/test_gnomad_integration.py`
already use, so `__init__`'s real torch/tensorflow/model-download side
effects are never triggered.
"""

import unittest
from unittest import mock

from pipeline.models.status import (
    DISABLED,
    DISPLAY_ORDER,
    FAILED,
    SKIPPED,
    USED,
    build_ai_model_status,
    render_status_table_lines,
)


def _base_kwargs(**overrides):
    kwargs = dict(
        dna_models_used=[],
        dna_model_results={},
        rna_result={"skipped": True, "reason": "not applicable"},
        protein_result={"skipped": True, "reason": "non-coding"},
        alphamissense_result={"skipped": True, "reason": "not a missense substitution"},
        mmsplice_result={"supported": False, "predicted": False, "skip_reason": "outside splice window"},
        ensemble_result=None,
        model_availability={},
        plugin_availability={"enformer": False, "borzoi": False},
        model_stage_errors={},
    )
    kwargs.update(overrides)
    return kwargs


class TestBuildAIModelStatusCoversEveryModel(unittest.TestCase):
    def test_covers_every_known_model(self):
        """Objective 6: no silent omissions -- every model in
        DISPLAY_ORDER must appear, always, regardless of input."""
        status = build_ai_model_status(**_base_kwargs())
        for key in DISPLAY_ORDER:
            self.assertIn(key, status, f"{key} missing from AI model status output")
            self.assertIn(status[key]["status"], (USED, SKIPPED, DISABLED, FAILED))
            self.assertTrue(status[key]["reason"])  # never an empty/blank reason

    def test_openspliceai_is_always_disabled(self):
        status = build_ai_model_status(**_base_kwargs())
        self.assertEqual(status["openspliceai"]["status"], DISABLED)
        self.assertIn("GPL-3.0", status["openspliceai"]["reason"])


class TestDNAContextModelStatus(unittest.TestCase):
    def test_used_when_routed_and_result_present(self):
        status = build_ai_model_status(**_base_kwargs(
            dna_models_used=["hyenadna"],
            dna_model_results={"hyenadna": {"embedding_mean": [0.1, 0.2]}},
            model_availability={"hyenadna": True},
        ))
        self.assertEqual(status["hyenadna"]["status"], USED)

    def test_disabled_when_unavailable(self):
        status = build_ai_model_status(**_base_kwargs(
            model_availability={"evo2": False},
        ))
        self.assertEqual(status["evo2"]["status"], DISABLED)

    def test_failed_when_stage_error_recorded(self):
        status = build_ai_model_status(**_base_kwargs(
            model_availability={"hyenadna": True},
            model_stage_errors={"hyenadna": "CUDA out of memory"},
        ))
        self.assertEqual(status["hyenadna"]["status"], FAILED)
        self.assertIn("CUDA", status["hyenadna"]["reason"])

    def test_skipped_when_available_but_not_routed(self):
        status = build_ai_model_status(**_base_kwargs(
            model_availability={"evo2": True},
        ))
        self.assertEqual(status["evo2"]["status"], SKIPPED)


class TestRnaEsm2AlphamissenseMmspliceStatus(unittest.TestCase):
    def test_rna_fm_used(self):
        status = build_ai_model_status(**_base_kwargs(
            rna_result={"skipped": False, "embedding_mean": [0.1]},
            model_availability={"rna_fm": True},
        ))
        self.assertEqual(status["rna_fm"]["status"], USED)

    def test_esm2_used(self):
        status = build_ai_model_status(**_base_kwargs(
            protein_result={"skipped": False, "esm2": {"embedding_mean": [0.1]}},
            model_availability={"esm2": True},
        ))
        self.assertEqual(status["esm2"]["status"], USED)

    def test_alphamissense_used(self):
        status = build_ai_model_status(**_base_kwargs(
            alphamissense_result={"skipped": False, "score": 0.9},
        ))
        self.assertEqual(status["alphamissense"]["status"], USED)

    def test_mmsplice_used(self):
        status = build_ai_model_status(**_base_kwargs(
            mmsplice_result={"supported": True, "predicted": True, "exon_skipping": 0.2},
        ))
        self.assertEqual(status["mmsplice"]["status"], USED)


class TestEnformerBorzoiStatus(unittest.TestCase):
    def test_used_when_in_models_used(self):
        status = build_ai_model_status(**_base_kwargs(
            ensemble_result={"models_used": ["enformer", "borzoi"]},
            plugin_availability={"enformer": True, "borzoi": True},
        ))
        self.assertEqual(status["enformer"]["status"], USED)
        self.assertEqual(status["borzoi"]["status"], USED)

    def test_disabled_when_plugin_unavailable(self):
        status = build_ai_model_status(**_base_kwargs(
            ensemble_result={"models_used": []},
            plugin_availability={"enformer": False, "borzoi": False},
        ))
        self.assertEqual(status["enformer"]["status"], DISABLED)
        self.assertEqual(status["borzoi"]["status"], DISABLED)

    def test_failed_when_plugin_failure_recorded_even_though_available(self):
        """The exact case the task called out: a model that COULD have
        run but errored must report Failed, never a silent Skipped."""
        status = build_ai_model_status(**_base_kwargs(
            ensemble_result={"models_used": ["borzoi"]},  # enformer absent from the result
            plugin_availability={"enformer": True, "borzoi": True},
            plugin_failures={"enformer": "weight download timed out"},
        ))
        self.assertEqual(status["enformer"]["status"], FAILED)
        self.assertIn("timed out", status["enformer"]["reason"])
        self.assertEqual(status["borzoi"]["status"], USED)

    def test_failed_when_whole_ensemble_stage_errored(self):
        status = build_ai_model_status(**_base_kwargs(
            ensemble_result=None,
            plugin_availability={"enformer": True, "borzoi": True},
            model_stage_errors={"ai_splicing_ensemble": "unexpected exception"},
        ))
        self.assertEqual(status["enformer"]["status"], FAILED)
        self.assertEqual(status["borzoi"]["status"], FAILED)


class TestRenderStatusTableLines(unittest.TestCase):
    def test_used_models_rendered_as_checkmarks(self):
        status = build_ai_model_status(**_base_kwargs(
            dna_models_used=["hyenadna"],
            dna_model_results={"hyenadna": {"embedding_mean": [0.1]}},
            model_availability={"hyenadna": True},
        ))
        lines = render_status_table_lines(status)
        text = "\n".join(lines)
        self.assertIn("HyenaDNA", text)
        self.assertIn("Skipped:", text)  # evo2 etc fall here
        self.assertIn("Disabled:", text)  # enformer/borzoi/openspliceai default off

    def test_never_raises_on_incomplete_status_dict(self):
        # Defensive: a malformed/partial status dict must not crash
        # report rendering.
        lines = render_status_table_lines({})
        self.assertIsInstance(lines, list)


class TestModelManagerLastInferenceErrors(unittest.TestCase):
    """`ModelManager.last_inference_errors()` -- new in this change,
    used by `_build_ai_model_status` to distinguish a per-variant
    inference failure from a plain skip."""

    def _manager_with_fake_plugin(self, predict_side_effect):
        from pipeline.models.base import PluginModel
        from pipeline.models.manager import ModelManager
        from pipeline.models.registry import ModelRegistry
        from utils.exceptions import ModelInferenceError

        class _FakePlugin(PluginModel):
            @classmethod
            def metadata(cls):
                from pipeline.models.base import ModelMetadata
                return ModelMetadata(
                    name="fake", version="1", source="n/a", license_name="MIT",
                    license_url="n/a", commercial_use_allowed=True, license_notes="n/a",
                )

            @classmethod
            def is_available(cls):
                return True

            def _load_impl(self):
                return object()

            def _infer_impl(self, *args, **kwargs):
                if predict_side_effect is not None:
                    raise ModelInferenceError(predict_side_effect)
                return {"score": 1.0}

        registry = ModelRegistry()
        registry.register("fake", _FakePlugin)
        return ModelManager(registry=registry)

    def test_records_inference_error_by_key(self):
        manager = self._manager_with_fake_plugin("boom")
        result = manager.predict("fake")
        self.assertIsNone(result)
        self.assertIn("fake", manager.last_inference_errors())
        self.assertIn("boom", manager.last_inference_errors()["fake"])

    def test_clears_on_next_success(self):
        manager = self._manager_with_fake_plugin("boom")
        manager.predict("fake")
        self.assertIn("fake", manager.last_inference_errors())

        # Flip the fake plugin's behavior and predict again -- a real
        # transient failure resolving on retry.
        manager._instances["fake"]._infer_impl = lambda *a, **k: {"score": 1.0}
        result = manager.predict("fake")
        self.assertEqual(result["score"], 1.0)
        self.assertNotIn("fake", manager.last_inference_errors())


class TestOrchestratorEnsembleStage(unittest.TestCase):
    """Exercises `GeperPipeline._run_ensemble_stage` /
    `_build_ai_model_status` directly, the same `__new__`-bypass
    pattern `tests/test_clingen_integration.py` uses."""

    def setUp(self):
        import _fake_heavy_deps

        _fake_heavy_deps.install()
        import importlib

        self.orchestrator_module = importlib.import_module("pipeline.orchestrator")

    def _make_bare_pipeline(self):
        pipeline = self.orchestrator_module.GeperPipeline.__new__(self.orchestrator_module.GeperPipeline)
        pipeline._timer = mock.MagicMock()
        pipeline._timer.return_value.__enter__ = mock.Mock(return_value=None)
        pipeline._timer.return_value.__exit__ = mock.Mock(return_value=False)
        pipeline._model_availability = {}
        pipeline._plugin_availability = {"enformer": True, "borzoi": True}
        pipeline._model_stage_errors = {}
        return pipeline

    def test_no_sequence_context_returns_empty_result_not_none(self):
        pipeline = self._make_bare_pipeline()
        pipeline.ensemble_manager = mock.Mock()
        errors: list = []
        result = pipeline._run_ensemble_stage(mock.Mock(), None, errors)
        self.assertEqual(result["models_used"], [])
        self.assertEqual(errors, [])
        pipeline.ensemble_manager.evaluate.assert_not_called()

    def test_calls_ensemble_manager_with_ref_and_alt_sequence(self):
        pipeline = self._make_bare_pipeline()
        pipeline.ensemble_manager = mock.Mock()
        expected = {"models_used": ["enformer", "borzoi"], "consensus_score": 0.7}
        pipeline.ensemble_manager.evaluate.return_value = expected

        seq_context = mock.Mock(ref_sequence="ACGT", alt_sequence="ACGA")
        errors: list = []
        result = pipeline._run_ensemble_stage(mock.Mock(), seq_context, errors)

        pipeline.ensemble_manager.evaluate.assert_called_once_with("ACGT", "ACGA")
        self.assertEqual(result, expected)
        self.assertEqual(errors, [])

    def test_never_raises_when_ensemble_manager_itself_errors(self):
        pipeline = self._make_bare_pipeline()
        pipeline.ensemble_manager = mock.Mock()
        pipeline.ensemble_manager.evaluate.side_effect = RuntimeError("unexpected bug")

        seq_context = mock.Mock(ref_sequence="ACGT", alt_sequence="ACGA")
        errors: list = []
        result = pipeline._run_ensemble_stage(mock.Mock(), seq_context, errors)  # must not raise

        self.assertEqual(result["models_used"], [])
        self.assertEqual(result["basis"], "error")
        self.assertTrue(errors)
        self.assertIn("Enformer/Borzoi", errors[0])
        self.assertIn("ai_splicing_ensemble", pipeline._model_stage_errors)

    def test_build_ai_model_status_delegates_correctly(self):
        pipeline = self._make_bare_pipeline()
        pipeline.model_manager = mock.Mock()
        pipeline.model_manager.failed_keys.return_value = {}
        pipeline.model_manager.last_inference_errors.return_value = {}

        status = pipeline._build_ai_model_status(
            dna_models_used=["hyenadna"],
            dna_model_results={"hyenadna": {"embedding_mean": [0.1]}},
            rna_result={"skipped": True},
            protein_result={"skipped": True},
            alphamissense_result={"skipped": True},
            mmsplice_result={"predicted": False},
            ensemble_result={"models_used": ["enformer", "borzoi"]},
        )
        for key in DISPLAY_ORDER:
            self.assertIn(key, status)
        self.assertEqual(status["hyenadna"]["status"], USED)
        self.assertEqual(status["enformer"]["status"], USED)
        self.assertEqual(status["borzoi"]["status"], USED)

    def test_orchestrator_init_constructs_model_manager_and_ensemble_manager(self):
        """`GeperPipeline.__init__` must wire up `self.model_manager`
        and `self.ensemble_manager` (Objectives 3/4)."""
        import inspect

        source = inspect.getsource(self.orchestrator_module.GeperPipeline.__init__)
        self.assertIn("self.model_manager", source)
        self.assertIn("self.ensemble_manager", source)
        self.assertIn("build_default_registry", source)


class TestJsonBuilderAndReportGeneratorAIModelStatus(unittest.TestCase):
    def test_ai_model_status_always_present_even_when_omitted(self):
        from report.json_builder import build_variant_result

        result = build_variant_result(
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
        self.assertIn("ai_model_status", result)
        self.assertEqual(result["ai_model_status"], {})

    def test_ai_model_status_passed_through_verbatim(self):
        from report.json_builder import build_variant_result

        status_payload = {"hyenadna": {"status": "used", "reason": "ran"}}
        result = build_variant_result(
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
            ai_model_status=status_payload,
        )
        self.assertEqual(result["ai_model_status"], status_payload)

    def test_report_generator_renders_status_table_section(self):
        from report.report_generator import ReportGenerator

        status = build_ai_model_status(**_base_kwargs(
            dna_models_used=["hyenadna"],
            dna_model_results={"hyenadna": {"embedding_mean": [0.1]}},
            model_availability={"hyenadna": True},
        ))
        lines = ReportGenerator._render_ai_model_status(status)
        text = "\n".join(lines)
        self.assertIn("### AI Models", text)
        self.assertIn("HyenaDNA", text)

    def test_report_generator_never_omits_section_even_when_status_missing(self):
        from report.report_generator import ReportGenerator

        lines = ReportGenerator._render_ai_model_status({})
        text = "\n".join(lines)
        self.assertIn("### AI Models", text)
        self.assertTrue(len(lines) > 1)  # says something explicit, not just the header


if __name__ == "__main__":
    unittest.main()
