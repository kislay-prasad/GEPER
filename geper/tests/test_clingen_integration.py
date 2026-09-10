"""
Integration tests for the ClinGen wiring into `report/json_builder.py`
and `pipeline/orchestrator.py`.

Mirrors `tests/test_gnomad_integration.py` exactly: verifies
requirement #13's backward compatibility (a caller of
`build_variant_result` that doesn't pass `clingen_result` still gets a
complete, valid record) and that `GeperPipeline._run_clingen_stage`
degrades gracefully and never raises out of the per-variant loop.
"""

import unittest
from unittest import mock

from report.json_builder import build_variant_result


class TestJsonBuilderClinGenBackwardCompatibility(unittest.TestCase):
    def test_omitting_clingen_result_still_produces_a_complete_record(self):
        # Exactly the call shape that existed before this integration
        # -- no `clingen_result` kwarg at all.
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
        self.assertIn("clingen", result)
        self.assertEqual(result["clingen"], {"skipped": True, "found": False})

    def test_passing_clingen_result_is_included_verbatim(self):
        clingen_payload = {"skipped": False, "found": True, "gene_symbol": "BRCA1"}
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
            clingen_result=clingen_payload,
        )
        self.assertEqual(result["clingen"], clingen_payload)

    def test_omitting_clingen_does_not_disturb_gnomad_or_other_keys(self):
        """Adding `clingen_result` as yet another optional kwarg must not shift or break the existing gnomad wiring."""
        gnomad_payload = {"skipped": False, "found": True, "global_af": 0.01}
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
            gnomad_result=gnomad_payload,
        )
        self.assertEqual(result["gnomad"], gnomad_payload)
        self.assertEqual(result["clingen"], {"skipped": True, "found": False})
        for expected_key in (
            "variant",
            "blast",
            "clinvar",
            "dbsnp",
            "alphamissense",
            "mmsplice",
            "gnomad",
            "clingen",
            "interpretation",
            "errors",
        ):
            self.assertIn(expected_key, result)


class TestOrchestratorClinGenStage(unittest.TestCase):
    """
    Exercises `GeperPipeline._run_clingen_stage` directly against a
    mocked `clingen_client`, the same __new__-bypass pattern
    `TestOrchestratorGnomadStage` uses to avoid the real torch/
    tensorflow model stack `__init__` would otherwise trigger.
    """

    def setUp(self):
        import _fake_heavy_deps

        _fake_heavy_deps.install()
        import importlib

        self.orchestrator_module = importlib.import_module("pipeline.orchestrator")

    def _make_bare_pipeline(self):
        pipeline = self.orchestrator_module.GeperPipeline.__new__(self.orchestrator_module.GeperPipeline)
        pipeline.sequence_context_gen = mock.Mock(assembly="GRCh38")
        pipeline._timer = mock.MagicMock()
        pipeline._timer.return_value.__enter__ = mock.Mock(return_value=None)
        pipeline._timer.return_value.__exit__ = mock.Mock(return_value=False)
        return pipeline

    def test_stage_returns_client_result_on_success(self):
        pipeline = self._make_bare_pipeline()
        pipeline.clingen_client = mock.Mock()
        pipeline.clingen_client.query_variant.return_value = {"found": True, "skipped": False, "gene_symbol": "BRCA1"}
        errors: list = []
        result = pipeline._run_clingen_stage(mock.Mock(), errors)
        self.assertEqual(result, {"found": True, "skipped": False, "gene_symbol": "BRCA1"})
        self.assertEqual(errors, [])

    def test_stage_records_error_without_raising_when_client_reports_error(self):
        pipeline = self._make_bare_pipeline()
        pipeline.clingen_client = mock.Mock()
        pipeline.clingen_client.query_variant.return_value = {"found": False, "error": "network unreachable"}
        errors: list = []
        result = pipeline._run_clingen_stage(mock.Mock(), errors)
        self.assertEqual(result["error"], "network unreachable")
        self.assertEqual(len(errors), 1)
        self.assertIn("ClinGen", errors[0])

    def test_stage_never_raises_even_if_client_itself_raises(self):
        pipeline = self._make_bare_pipeline()
        pipeline.clingen_client = mock.Mock()
        pipeline.clingen_client.query_variant.side_effect = RuntimeError("unexpected bug")
        errors: list = []
        result = pipeline._run_clingen_stage(mock.Mock(), errors)  # must not raise
        self.assertFalse(result["found"])
        self.assertIn("ClinGen stage failed", errors[0])

    def test_orchestrator_constructs_clingen_client(self):
        """`GeperPipeline.__init__` must wire up `self.clingen_client` alongside `self.gnomad_client`."""
        import inspect

        source = inspect.getsource(self.orchestrator_module.GeperPipeline.__init__)
        self.assertIn("self.clingen_client", source)


if __name__ == "__main__":
    unittest.main()
