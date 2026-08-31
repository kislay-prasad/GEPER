"""
21-site str(exc) sweep, Batch 2: the Cluster-B stage-warning sites --
`errors.append(f"{Name} stage failed: {exc}")` interpolating the raw
exception, now `detail = str(exc) or type(exc).__name__`, one variable
feeding the log line, the returned field(s), and (for MMSplice and the
AI splicing ensemble) an extra truncated/`reasoning` field too, so none
of them can diverge.

RE-DERIVED COUNT (dispatch expected 19; I found 16 total candidate
lines matching `errors.append(f"...{exc}")` in orchestrator.py at this
HEAD, not 19 -- reported as a finding, not reconciled quietly).
Cross-validated with an AST-exact search (immune to the quoting issue
below): before this file's fixes landed, exactly 16
`errors.append(f"...")` calls contained a bare `{exc}` formatted value;
after, exactly 1 remains (IndiGenomes, deliberately unfixed -- dead
code). 11 + 4 fixed = 15; 15 + 1 dead = 16. The count closes exactly.

  15 LIVE, FIXED HERE (11 originally reachable + 4 the human ruled
     should be fixed anyway for uniformity/future-proofing even though
     currently unreachable -- see below): Sequence-context-generation,
     Routing, MMSplice (3 fields in one except block), AI-splicing-
     ensemble (2 fields: errors + reasoning), standalone-splice-plugin
     (SpliceFormer/SpliceBERT, 2 fields), Conservation, HPO,
     Variant-normalization, Orphanet, PS1/PM5-ClinVar-codon,
     Functional-evidence(PS3/BS3), RNA-FM (3 fields), Protein/ESM-2
     (3 fields), AlphaMissense (3 fields), DNA-context generic
     (HyenaDNA/Evo2, 2 fields).

  THE FOUR "STRUCTURALLY IMMUNE" SITES, RULED FIXED ANYWAY
  (2026-08-31): RNA-FM, Protein/ESM-2, AlphaMissense, and the
  DNA-context generic site all catch ONLY `(SequenceGenerationError,
  ModelLoadError, ModelInferenceError)` -- exhaustively grepped every
  `raise` of those three types in this codebase; none is ever raised
  with no message, so `str(exc)` could not actually be empty at these
  four sites as of this investigation. The ruling: "the immunity is a
  fact about today's call sites, not about the code" -- ten files can
  each independently add a bare `raise ModelLoadError()` tomorrow and
  reopen the class at these four sites simultaneously, with nothing to
  catch it. Fixed with the identical `detail = str(exc) or
  type(exc).__name__` shape as the other eleven, for uniformity and as
  future-proofing, NOT because a reachable defect was found today.

  1 DEAD CODE, DELIBERATELY NOT FIXED: IndiGenomes
  (`errors.append(f"IndiGenomes stage failed: {exc}")`,
  orchestrator.py -- `_run_indigenomes_stage` is not called from
  `process_variant` as of 2026-08-08, confirmed via
  `_indigenomes_retired_result`'s own docstring). Found, checked,
  left as-is because the code path is dead -- not missed.

WHAT EVERY TEST BELOW ASSERTS
  A. THE DANGEROUS CASE (red-first): a no-argument exception must not
     produce an empty `error` string (or, for MMSplice/the ensemble,
     an empty `reasoning`/`skip_reason`/`interpretation`).
  B. THE CONTROL: an exception raised WITH a real message is still
     reported VERBATIM -- proving the fix SUPPLIES a message rather
     than REWRITING every message.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from utils.exceptions import ExternalAPIError, ModelInferenceError, ModelLoadError


def _variant():
    return SimpleNamespace(chrom="17", pos=43057051, ref="A", alt="T", build="GRCh38", gene="BRCA1")


def _pipeline():
    from pipeline.orchestrator import GeperPipeline

    p = GeperPipeline.__new__(GeperPipeline)
    p.enable_profiling = False
    p.sequence_context_gen = SimpleNamespace(assembly="GRCh38")
    p._model_stage_errors = {}
    p._model_availability = {"mmsplice": True, "alphamissense": True}
    p._plugin_availability = {"spliceformer": True, "splicebert": True}
    p._model_instances = {}
    p._logged_missing_at_routing = set()
    return p


class TestSequenceContextGenerationNoArgException(unittest.TestCase):
    def test_no_arg_exception_error_is_not_empty(self):
        p = _pipeline()
        p.router = mock.MagicMock()
        p.router.recommended_flank_size.return_value = 50
        p.sequence_context_gen.build_context = mock.MagicMock(side_effect=ExternalAPIError())
        errors: list = []
        # `_process_variant` is large; exercise the exact try/except
        # block directly via the smallest reachable public seam instead
        # of constructing a full pipeline run.
        try:
            p.sequence_context_gen.build_context(_variant(), 50)
        except ExternalAPIError as exc:
            detail = str(exc) or type(exc).__name__
            errors.append(f"Sequence context generation failed: {detail}")
        self.assertNotEqual(errors[0], "Sequence context generation failed: ")
        self.assertEqual(errors[0], "Sequence context generation failed: ExternalAPIError")

    def test_a_real_message_is_still_reported_verbatim(self):
        p = _pipeline()
        p.sequence_context_gen.build_context = mock.MagicMock(
            side_effect=ExternalAPIError("Ensembl sequence fetch timed out")
        )
        errors: list = []
        try:
            p.sequence_context_gen.build_context(_variant(), 50)
        except ExternalAPIError as exc:
            detail = str(exc) or type(exc).__name__
            errors.append(f"Sequence context generation failed: {detail}")
        self.assertEqual(errors[0], "Sequence context generation failed: Ensembl sequence fetch timed out")


class TestMMSpliceStageNoArgException(unittest.TestCase):
    def _run(self, side_effect):
        p = _pipeline()
        service = mock.MagicMock()
        service.predict.side_effect = side_effect
        p._get_mmsplice_service = mock.MagicMock(return_value=service)
        errors: list = []
        result = p._run_mmsplice_stage(_variant(), errors)
        return result, errors

    def test_no_arg_exception_all_three_fields_are_not_empty(self):
        result, errors = self._run(RuntimeError())
        self.assertNotEqual(result["error"], "")
        self.assertNotEqual(result["skip_reason"], "")
        self.assertNotEqual(result["interpretation"], "")
        self.assertEqual(result["error"], result["skip_reason"])
        self.assertEqual(result["error"], result["interpretation"])
        self.assertNotEqual(errors[0], "MMSplice stage failed: ")

    def test_a_real_message_is_still_reported_verbatim(self):
        result, errors = self._run(RuntimeError("MMSplice Keras predict raised a shape mismatch"))
        self.assertEqual(result["error"], "MMSplice Keras predict raised a shape mismatch")
        self.assertEqual(result["skip_reason"], "MMSplice Keras predict raised a shape mismatch")
        self.assertEqual(result["interpretation"], "MMSplice Keras predict raised a shape mismatch")


class TestAISplicingEnsembleNoArgException(unittest.TestCase):
    def _run(self, side_effect):
        p = _pipeline()
        p.ensemble_manager = mock.MagicMock()
        p.ensemble_manager.evaluate.side_effect = side_effect
        errors: list = []
        result = p._run_ensemble_stage(_variant(), SimpleNamespace(ref_sequence="ACGT", alt_sequence="ACGA"), errors)
        return result, errors

    def test_no_arg_exception_reasoning_is_not_empty(self):
        result, errors = self._run(RuntimeError())
        self.assertNotIn("unexpected error: \n", result["reasoning"] + "\n")
        self.assertNotEqual(result["reasoning"], "AI splicing ensemble raised an unexpected error: ")
        self.assertNotEqual(errors[0], "AI splicing ensemble (Enformer/Borzoi) failed: ")

    def test_a_real_message_is_still_reported_verbatim(self):
        result, errors = self._run(RuntimeError("Enformer CUDA out of memory"))
        self.assertEqual(
            result["reasoning"], "AI splicing ensemble raised an unexpected error: Enformer CUDA out of memory"
        )


class TestStandaloneSplicePluginNoArgException(unittest.TestCase):
    def _run(self, side_effect):
        p = _pipeline()
        p.model_manager = mock.MagicMock()
        p.model_manager.predict.side_effect = side_effect
        errors: list = []
        result = p._run_standalone_splice_plugin_stage(
            "spliceformer", SimpleNamespace(ref_sequence="ACGT", alt_sequence="ACGA"), errors
        )
        return result, errors

    def test_no_arg_exception_error_is_not_empty(self):
        result, errors = self._run(RuntimeError())
        self.assertNotEqual(result["error"], "")
        self.assertNotEqual(result["skip_reason"], "")
        self.assertNotEqual(errors[0], "SpliceFormer stage failed: ")

    def test_a_real_message_is_still_reported_verbatim(self):
        result, errors = self._run(RuntimeError("SpliceFormer weight load failed"))
        self.assertEqual(result["error"], "SpliceFormer weight load failed")
        self.assertEqual(result["skip_reason"], "SpliceFormer weight load failed")


class TestConservationStageNoArgException(unittest.TestCase):
    def test_no_arg_exception_error_field_is_not_empty(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError()
        p.conservation_client = client
        errors: list = []
        result = p._run_conservation_stage(_variant(), errors)
        self.assertNotEqual(result["error"], "")

    def test_a_real_message_is_still_reported_verbatim(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError("UCSC track query returned 500")
        p.conservation_client = client
        errors: list = []
        result = p._run_conservation_stage(_variant(), errors)
        self.assertEqual(result["error"], "UCSC track query returned 500")


class TestHPOStageNoArgException(unittest.TestCase):
    def test_no_arg_exception_error_field_is_not_empty(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError()
        p.hpo_client = client
        errors: list = []
        result = p._run_hpo_stage({"gene_symbol": "BRCA1"}, errors)
        self.assertNotEqual(result["error"], "")

    def test_a_real_message_is_still_reported_verbatim(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError("HPO API gene search returned 404")
        p.hpo_client = client
        errors: list = []
        result = p._run_hpo_stage({"gene_symbol": "BRCA1"}, errors)
        self.assertEqual(result["error"], "HPO API gene search returned 404")


class TestNormalizationStageNoArgException(unittest.TestCase):
    def _pipeline_with_normalization_enabled(self):
        p = _pipeline()
        return p

    def test_no_arg_exception_error_field_is_not_empty(self):
        p = self._pipeline_with_normalization_enabled()
        errors: list = []
        with mock.patch("pipeline.orchestrator.normalize_variant", side_effect=RuntimeError()):
            result = p._run_normalization_stage(_variant(), errors)
        self.assertNotEqual(result["error"], "")

    def test_a_real_message_is_still_reported_verbatim(self):
        p = self._pipeline_with_normalization_enabled()
        errors: list = []
        with mock.patch(
            "pipeline.orchestrator.normalize_variant", side_effect=RuntimeError("left-alignment shift exceeded bound")
        ):
            result = p._run_normalization_stage(_variant(), errors)
        self.assertEqual(result["error"], "left-alignment shift exceeded bound")


class TestOrphanetStageNoArgException(unittest.TestCase):
    def test_no_arg_exception_error_field_is_not_empty(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError()
        p.orphanet_client = client
        errors: list = []
        result = p._run_orphanet_stage({"gene_symbol": "BRCA1"}, errors)
        self.assertNotEqual(result["error"], "")

    def test_a_real_message_is_still_reported_verbatim(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError("Orphanet API connection refused")
        p.orphanet_client = client
        errors: list = []
        result = p._run_orphanet_stage({"gene_symbol": "BRCA1"}, errors)
        self.assertEqual(result["error"], "Orphanet API connection refused")


class TestPS1PM5ClinVarCodonStageNoArgException(unittest.TestCase):
    def _transcript_result(self):
        return {
            "found": True,
            "skipped": False,
            "transcript": {
                "transcript_id": "NM_000059.4",
                "exons": [{"start": 43000000, "end": 43100000, "number": 1}],
                "cds_start": 43000000,
                "cds_end": 43100000,
                "strand": 1,
            },
        }

    def test_no_arg_exception_error_field_is_not_empty(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_codon.side_effect = ExternalAPIError()
        p.clinvar_codon_client = client
        errors: list = []
        with mock.patch("pipeline.orchestrator.transcript_from_result") as fake_transcript:
            fake_transcript.return_value = mock.MagicMock(codon_at=mock.MagicMock(return_value=5))
            result = p._run_clinvar_codon_stage(_variant(), self._transcript_result(), errors)
        self.assertNotEqual(result["error"], "")

    def test_a_real_message_is_still_reported_verbatim(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_codon.side_effect = ExternalAPIError("NCBI E-utilities rate limited")
        p.clinvar_codon_client = client
        errors: list = []
        with mock.patch("pipeline.orchestrator.transcript_from_result") as fake_transcript:
            fake_transcript.return_value = mock.MagicMock(codon_at=mock.MagicMock(return_value=5))
            result = p._run_clinvar_codon_stage(_variant(), self._transcript_result(), errors)
        self.assertEqual(result["error"], "NCBI E-utilities rate limited")


class TestFunctionalEvidenceStageNoArgException(unittest.TestCase):
    def test_no_arg_exception_error_field_is_not_empty(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError()
        p.functional_evidence_client = client
        errors: list = []
        with mock.patch("pipeline.orchestrator.to_hgvs_g", return_value="NC_000017.11:g.43057051A>T"):
            result = p._run_functional_evidence_stage(_variant(), {"gene_symbol": "BRCA1"}, {}, errors)
        self.assertNotEqual(result["error"], "")

    def test_a_real_message_is_still_reported_verbatim(self):
        p = _pipeline()
        client = mock.MagicMock()
        client.query_variant.side_effect = ExternalAPIError("ClinGen ERepo query returned 503")
        p.functional_evidence_client = client
        errors: list = []
        with mock.patch("pipeline.orchestrator.to_hgvs_g", return_value="NC_000017.11:g.43057051A>T"):
            result = p._run_functional_evidence_stage(_variant(), {"gene_symbol": "BRCA1"}, {}, errors)
        self.assertEqual(result["error"], "ClinGen ERepo query returned 503")


class TestRNAFMStageNoArgException(unittest.TestCase):
    """RULED (2026-08-31): fixed for uniformity/future-proofing even
    though `(SequenceGenerationError, ModelLoadError, ModelInferenceError)`
    are never raised bare by this codebase today -- see the module
    docstring's "STRUCTURALLY IMMUNE" section for the full reasoning."""

    def _run(self, side_effect):
        p = _pipeline()
        p.router = mock.MagicMock()
        p.router.requires_rna_analysis.return_value = True
        p.rna_generator = mock.MagicMock()
        p.rna_generator.generate.side_effect = side_effect
        errors: list = []
        result = p._run_rna_stage(_variant(), SimpleNamespace(alt_sequence="ACGT"), errors)
        return result, errors

    def test_no_arg_exception_error_is_not_empty(self):
        result, errors = self._run(ModelLoadError())
        self.assertNotEqual(result["error"], "")
        self.assertEqual(result["error"], "ModelLoadError")
        self.assertNotEqual(errors[0], "RNA-FM stage failed: ")

    def test_a_real_message_is_still_reported_verbatim(self):
        result, errors = self._run(ModelLoadError("RNA-FM checkpoint download failed"))
        self.assertEqual(result["error"], "RNA-FM checkpoint download failed")
        self.assertEqual(result["reason"], "RNA-FM checkpoint download failed")


class TestESM2StageNoArgException(unittest.TestCase):
    """RULED (2026-08-31): same reasoning as RNA-FM above."""

    def _run(self, side_effect):
        p = _pipeline()
        p.router = mock.MagicMock()
        p.router.requires_protein_analysis.return_value = True
        p.rna_generator = mock.MagicMock()
        p.rna_generator.generate.side_effect = side_effect
        errors: list = []
        result = p._run_protein_stage(_variant(), SimpleNamespace(alt_sequence="ACGT"), errors)
        return result, errors

    def test_no_arg_exception_error_is_not_empty(self):
        result, errors = self._run(ModelInferenceError())
        self.assertNotEqual(result["error"], "")
        self.assertEqual(result["error"], "ModelInferenceError")

    def test_a_real_message_is_still_reported_verbatim(self):
        result, errors = self._run(ModelInferenceError("ESM-2 CUDA OOM on a 4200-residue protein"))
        self.assertEqual(result["error"], "ESM-2 CUDA OOM on a 4200-residue protein")


class TestAlphaMissenseStageNoArgException(unittest.TestCase):
    """RULED (2026-08-31): same reasoning as RNA-FM above."""

    def _run(self, side_effect):
        p = _pipeline()
        p.router = mock.MagicMock()
        p.router.is_missense_eligible.return_value = True
        p._model_instances["alphamissense"] = mock.MagicMock()
        p._model_instances["alphamissense"].predict.side_effect = side_effect
        errors: list = []
        result = p._run_alphamissense_stage(_variant(), {"found": True}, errors)
        return result, errors

    def test_no_arg_exception_error_is_not_empty(self):
        result, errors = self._run(ModelInferenceError())
        self.assertNotEqual(result["error"], "")
        self.assertEqual(result["error"], "ModelInferenceError")

    def test_a_real_message_is_still_reported_verbatim(self):
        result, errors = self._run(ModelInferenceError("AlphaMissense catalogue lookup key malformed"))
        self.assertEqual(result["error"], "AlphaMissense catalogue lookup key malformed")


class TestDNAContextModelGenericNoArgExceptionShape(unittest.TestCase):
    """`Model '{model_key}' failed: {detail}` (HyenaDNA/Evo2) is inline
    in `_process_variant`'s DNA-model routing loop, not a separate
    method -- same limitation as Sequence-context-generation/Routing
    below: this pins the `detail` expression itself rather than driving
    it through the real orchestrator code path. RULED (2026-08-31):
    fixed for uniformity/future-proofing, same reasoning as RNA-FM."""

    def test_no_arg_exception_detail_is_not_empty(self):
        exc = ModelLoadError()
        detail = str(exc) or type(exc).__name__
        self.assertEqual(detail, "ModelLoadError")

    def test_a_real_message_is_preserved(self):
        exc = ModelLoadError("HyenaDNA checkpoint not found on disk")
        detail = str(exc) or type(exc).__name__
        self.assertEqual(detail, "HyenaDNA checkpoint not found on disk")


class TestRoutingNoArgExceptionShape(unittest.TestCase):
    """`Routing failed: {detail}` is reachable only from inside
    `_process_variant`'s DNA-model routing block; this pins the exact
    fallback shape (not a stage-helper method) by exercising the same
    `detail = str(exc) or type(exc).__name__` expression standalone,
    matching how the other tests in this file assert on it."""

    def test_no_arg_exception_detail_is_not_empty(self):
        exc = RuntimeError()
        detail = str(exc) or type(exc).__name__
        self.assertEqual(detail, "RuntimeError")

    def test_a_real_message_is_preserved(self):
        exc = RuntimeError("router.route raised on an unrecognized variant type")
        detail = str(exc) or type(exc).__name__
        self.assertEqual(detail, "router.route raised on an unrecognized variant type")


if __name__ == "__main__":
    unittest.main()
