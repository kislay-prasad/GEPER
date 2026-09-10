"""
Integration tests for the gnomAD wiring into `report/json_builder.py`
and `pipeline/orchestrator.py`.

Verifies requirement #8's "Maintain backward compatibility" directly:
a caller of `build_variant_result` written before Phase 2 existed
(same positional/keyword args, no `gnomad_result` kwarg) must still get
a complete, valid record. Also verifies `GeperPipeline._run_gnomad_stage`
degrades gracefully and never raises out of the per-variant loop.
"""

import unittest
from unittest import mock

from report.json_builder import build_variant_result


class TestJsonBuilderGnomadBackwardCompatibility(unittest.TestCase):
    def test_omitting_gnomad_result_still_produces_a_complete_record(self):
        # Exactly the call shape that existed before Phase 2 -- no
        # `gnomad_result` kwarg at all.
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
        self.assertIn("gnomad", result)
        self.assertEqual(result["gnomad"], {"skipped": True, "found": False})

    def test_passing_gnomad_result_is_included_verbatim(self):
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

    def test_every_other_key_still_present(self):
        """A minimal sanity check that adding `gnomad` didn't displace any pre-existing top-level key."""
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
        for expected_key in (
            "variant",
            "blast",
            "clinvar",
            "dbsnp",
            "alphamissense",
            "mmsplice",
            "interpretation",
            "errors",
        ):
            self.assertIn(expected_key, result)


class TestOrchestratorGnomadStage(unittest.TestCase):
    """
    Exercises `GeperPipeline._run_gnomad_stage` directly against a
    mocked `gnomad_client` (avoiding the need for the real torch/
    tensorflow model stack this class's __init__ otherwise triggers,
    matching how `_fake_heavy_deps` lets benchmark_blast.py exercise
    the real orchestrator methods without those heavy dependencies).
    """

    def setUp(self):
        import _fake_heavy_deps

        _fake_heavy_deps.install()
        import importlib

        self.orchestrator_module = importlib.import_module("pipeline.orchestrator")

    def _make_bare_pipeline(self):
        # Bypass __init__ (which constructs real model/DB clients) --
        # this test targets `_run_gnomad_stage`'s own error-handling
        # contract in isolation, the same "construct via __new__,
        # attach only what's needed" pattern already used for the
        # other per-stage helper tests in this codebase's mmsplice suite.
        pipeline = self.orchestrator_module.GeperPipeline.__new__(self.orchestrator_module.GeperPipeline)
        pipeline.sequence_context_gen = mock.Mock(assembly="GRCh38")
        pipeline._timer = mock.MagicMock()
        pipeline._timer.return_value.__enter__ = mock.Mock(return_value=None)
        pipeline._timer.return_value.__exit__ = mock.Mock(return_value=False)
        return pipeline

    def test_stage_returns_client_result_on_success(self):
        pipeline = self._make_bare_pipeline()
        pipeline.gnomad_client = mock.Mock()
        pipeline.gnomad_client.query_variant.return_value = {"found": True, "skipped": False}
        errors: list = []
        result = pipeline._run_gnomad_stage(mock.Mock(), errors)
        self.assertEqual(result, {"found": True, "skipped": False})
        self.assertEqual(errors, [])

    def test_stage_records_error_without_raising_when_client_reports_error(self):
        pipeline = self._make_bare_pipeline()
        pipeline.gnomad_client = mock.Mock()
        pipeline.gnomad_client.query_variant.return_value = {"found": False, "error": "network unreachable"}
        errors: list = []
        result = pipeline._run_gnomad_stage(mock.Mock(), errors)
        self.assertEqual(result["error"], "network unreachable")
        self.assertEqual(len(errors), 1)
        self.assertIn("gnomAD", errors[0])

    def test_stage_never_raises_even_if_client_itself_raises(self):
        pipeline = self._make_bare_pipeline()
        pipeline.gnomad_client = mock.Mock()
        pipeline.gnomad_client.query_variant.side_effect = RuntimeError("unexpected bug")
        errors: list = []
        result = pipeline._run_gnomad_stage(mock.Mock(), errors)  # must not raise
        self.assertFalse(result["found"])
        self.assertIn("gnomAD stage failed", errors[0])


if __name__ == "__main__":
    unittest.main()
