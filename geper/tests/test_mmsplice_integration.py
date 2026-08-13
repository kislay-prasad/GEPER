"""
Integration tests for the MMSplice wiring into
`pipeline/interpretation.py` and `report/json_builder.py`.

These verify requirements #8 ("do not overwrite existing ACMG
evidence", additive/configurable) and #10 ("maintain backward
compatibility") directly against the real (non-mocked) modules.
"""

import unittest
from unittest import mock

from pipeline.interpretation import InterpretationEngine
from report.json_builder import build_variant_result


class TestJsonBuilderBackwardCompatibility(unittest.TestCase):
    def test_omitting_mmsplice_result_still_produces_a_complete_record(self):
        # A caller written before this integration existed (positional/
        # keyword args identical to before, no `mmsplice_result` kwarg)
        # must still get a fully-formed result.
        result = build_variant_result(
            variant_dict={"chrom": "1", "pos": 100, "ref": "A", "alt": "G"},
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
        self.assertIn("mmsplice", result)
        self.assertEqual(result["mmsplice"], {"supported": False, "predicted": False})
        # every pre-existing key must still be present and unaffected
        for key in (
            "variant",
            "sequence_context",
            "dna_model_results",
            "rna_analysis",
            "protein_analysis",
            "alphamissense",
            "blast",
            "clinvar",
            "dbsnp",
            "interpretation",
            "errors",
        ):
            self.assertIn(key, result)

    def test_passing_mmsplice_result_is_included_verbatim(self):
        mmsplice_result = {"supported": True, "predicted": True, "delta_logit_psi": -3.2}
        result = build_variant_result(
            variant_dict={},
            sequence_context={},
            dna_model_results={},
            rna_result={},
            protein_result={},
            blast_result={},
            clinvar_result={},
            dbsnp_result={},
            interpretation={},
            errors=[],
            mmsplice_result=mmsplice_result,
        )
        self.assertEqual(result["mmsplice"], mmsplice_result)


class TestInterpretationEngineMMSpliceEvidence(unittest.TestCase):
    def setUp(self):
        self.engine = InterpretationEngine()
        self.base_kwargs = dict(
            variant_dict={"chrom": "1", "pos": 100, "ref": "A", "alt": "G"},
            dna_models_used=[],
            clinvar_result={"records": []},
            dbsnp_result={"found": False},
            protein_result={},
            blast_result={},
        )

    def test_no_mmsplice_result_does_not_change_existing_behavior(self):
        without = self.engine.interpret(**self.base_kwargs)
        with_none = self.engine.interpret(**self.base_kwargs, mmsplice_result=None)
        self.assertEqual(without["legacy_pre_acmg_significance_score"], with_none["legacy_pre_acmg_significance_score"])
        self.assertEqual(without["supporting_evidence"], with_none["supporting_evidence"])

    def test_mmsplice_evidence_is_additive_not_replacing(self):
        without_mmsplice = self.engine.interpret(**self.base_kwargs)
        mmsplice_result = {
            "predicted": True,
            "supported": True,
            "interpretation": "Strong donor site loss.",
            "interpretation_category": "strong_donor_loss",
            "delta_logit_psi": -6.0,
            "confidence": "high",
        }
        with_mmsplice = self.engine.interpret(**self.base_kwargs, mmsplice_result=mmsplice_result)

        # Every piece of evidence present without MMSplice must still
        # be present with it -- nothing gets overwritten, only appended.
        for item in without_mmsplice["supporting_evidence"]:
            self.assertIn(item, with_mmsplice["supporting_evidence"])
        self.assertEqual(len(with_mmsplice["supporting_evidence"]), len(without_mmsplice["supporting_evidence"]) + 1)
        self.assertGreater(
            with_mmsplice["legacy_pre_acmg_significance_score"], without_mmsplice["legacy_pre_acmg_significance_score"]
        )

    def test_mmsplice_weight_zero_excludes_from_score_but_keeps_evidence_text(self):
        import dataclasses

        from config import CONFIG

        mmsplice_result = {
            "predicted": True,
            "supported": True,
            "interpretation": "Strong donor site loss.",
            "interpretation_category": "strong_donor_loss",
            "delta_logit_psi": -6.0,
            "confidence": "high",
        }
        without_mmsplice = self.engine.interpret(**self.base_kwargs)
        zero_weight_mmsplice_config = dataclasses.replace(CONFIG.mmsplice, ACMG_EVIDENCE_WEIGHT=0.0)
        patched_config = dataclasses.replace(CONFIG, mmsplice=zero_weight_mmsplice_config)
        with mock.patch("pipeline.interpretation.CONFIG", patched_config):
            with_mmsplice = self.engine.interpret(**self.base_kwargs, mmsplice_result=mmsplice_result)

        self.assertEqual(
            with_mmsplice["legacy_pre_acmg_significance_score"], without_mmsplice["legacy_pre_acmg_significance_score"]
        )
        self.assertEqual(len(with_mmsplice["supporting_evidence"]), len(without_mmsplice["supporting_evidence"]) + 1)

    def test_unpredicted_mmsplice_result_adds_explanatory_evidence_only(self):
        mmsplice_result = {"supported": True, "predicted": False, "skip_reason": "outside splice window"}
        without_mmsplice = self.engine.interpret(**self.base_kwargs)
        with_mmsplice = self.engine.interpret(**self.base_kwargs, mmsplice_result=mmsplice_result)
        self.assertEqual(
            with_mmsplice["legacy_pre_acmg_significance_score"], without_mmsplice["legacy_pre_acmg_significance_score"]
        )
        self.assertEqual(len(with_mmsplice["supporting_evidence"]), len(without_mmsplice["supporting_evidence"]) + 1)


if __name__ == "__main__":
    unittest.main()
