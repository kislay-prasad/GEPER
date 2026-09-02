"""
Sub-pattern B (ruled): per-model `calibration_status` recording in run
documents, alongside the existing USED/SKIPPED/DISABLED/FAILED status
tracking in `pipeline/models/status.py`.

Five models write `details.calibration_status` on their own results
(Enformer, Borzoi, SpliceFormer, SpliceBERT -- each labels itself
"uncalibrated"; SPiP labels itself "calibrated", but SPiP is not
wired into any per-variant status flow yet -- see
`pipeline/models/pending_plugins.py`'s own docstring -- and is
therefore out of scope for this change, unlike the boundary drafted
against it). MMSplice runs but never writes the field at all.

Covers, in order:
  1. Red-first (boundary 4): a model that never fires must record
     NOT_EVALUATED, never CALIBRATED -- the exact truthiness-trap
     failure mode boundary 3 warns about, since all four states are
     non-empty strings and a `if calibration_status:` read site would
     say yes to every one of them.
  2. Per-variant classification for each of the four calibration-
     writing models plus MMSplice's NOT_REPORTED case.
  3. Run-level worst-case aggregation (`rollup_run_status`, boundary
     2): any single uncalibrated variant wins over 99 calibrated ones,
     with a tally in the run-level value.
  4. `finalize_model_checkpoint_provenance` carries the run-level
     calibration_status through to the enriched checkpoint record, and
     leaves models outside `_CALIBRATION_MODEL_KEYS` without the key
     entirely (no fifth state fabricated for HyenaDNA/Evo2/RNA-FM/
     ESM-2/AlphaMissense).
"""

import unittest

from pipeline.models.status import (
    CALIBRATED,
    NOT_EVALUATED,
    NOT_REPORTED,
    UNCALIBRATED,
    build_ai_model_status,
    rollup_run_status,
)
from pipeline.provenance import finalize_model_checkpoint_provenance


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
        plugin_availability={"enformer": False, "borzoi": False, "spliceformer": False, "splicebert": False},
        model_stage_errors={},
        spliceformer_result=None,
        splicebert_result=None,
    )
    kwargs.update(overrides)
    return kwargs


class TestNotEvaluatedNeverReadAsCalibrated(unittest.TestCase):
    """
    Boundary 4, red-first: a model that never fires for a variant must
    record NOT_EVALUATED. This is the assertion that fails if that
    distinction is violated -- not merely "calibration_status is
    truthy" (which NOT_EVALUATED also satisfies, being a non-empty
    string), but the literal state value, checked by equality against
    each of the three *other* possible strings individually so a
    regression that swaps NOT_EVALUATED for any one of them is caught.
    """

    def test_model_that_never_fires_records_not_evaluated_not_calibrated(self):
        status = build_ai_model_status(
            **_base_kwargs(
                plugin_availability={"enformer": False, "borzoi": False, "spliceformer": True, "splicebert": True},
                spliceformer_result={"skip_reason": "Variant is outside the model's supported window."},
                splicebert_result={"skip_reason": "Variant is outside the model's supported window."},
            )
        )
        for key in ("enformer", "borzoi", "spliceformer", "splicebert", "mmsplice"):
            cal = status[key]["calibration_status"]
            self.assertEqual(cal, NOT_EVALUATED, f"{key}: expected not_evaluated, got {cal!r}")
            self.assertNotEqual(cal, CALIBRATED, f"{key}: not_evaluated must never read as calibrated")
            self.assertNotEqual(cal, UNCALIBRATED)
            self.assertNotEqual(cal, NOT_REPORTED)


class TestPerVariantCalibrationClassification(unittest.TestCase):
    def test_enformer_used_is_uncalibrated(self):
        status = build_ai_model_status(
            **_base_kwargs(
                ensemble_result={
                    "models_used": ["enformer"],
                    "individual_scores": {
                        "enformer": {
                            "score": 0.7,
                            "classification": "large_effect",
                            "details": {
                                "calibration_status": "uncalibrated -- raw Enformer track-delta summary, not "
                                "validated against clinical ground truth"
                            },
                        }
                    },
                },
            )
        )
        self.assertEqual(status["enformer"]["status"], "used")
        self.assertEqual(status["enformer"]["calibration_status"], UNCALIBRATED)

    def test_borzoi_used_is_uncalibrated(self):
        status = build_ai_model_status(
            **_base_kwargs(
                ensemble_result={
                    "models_used": ["borzoi"],
                    "individual_scores": {
                        "borzoi": {
                            "score": 0.7,
                            "classification": "large_effect",
                            "details": {"calibration_status": "uncalibrated -- raw Borzoi summary"},
                        }
                    },
                },
            )
        )
        self.assertEqual(status["borzoi"]["calibration_status"], UNCALIBRATED)

    def test_spliceformer_used_is_uncalibrated(self):
        status = build_ai_model_status(
            **_base_kwargs(
                plugin_availability={"spliceformer": True, "splicebert": False},
                spliceformer_result={
                    "classification": "large_effect",
                    "details": {"calibration_status": "uncalibrated -- raw SpliceFormer summary"},
                },
            )
        )
        self.assertEqual(status["spliceformer"]["calibration_status"], UNCALIBRATED)

    def test_splicebert_used_is_uncalibrated(self):
        status = build_ai_model_status(
            **_base_kwargs(
                plugin_availability={"spliceformer": False, "splicebert": True},
                splicebert_result={
                    "classification": "large_effect",
                    "details": {"calibration_status": "uncalibrated -- raw SpliceBERT summary"},
                },
            )
        )
        self.assertEqual(status["splicebert"]["calibration_status"], UNCALIBRATED)

    def test_mmsplice_used_is_not_reported(self):
        """MMSplice runs but never writes `details.calibration_status`
        at all -- honest absence (NOT_REPORTED), never silently read
        as CALIBRATED just because the field is missing."""
        status = build_ai_model_status(
            **_base_kwargs(
                mmsplice_result={"supported": True, "predicted": True, "exon_skipping": 0.2},
            )
        )
        self.assertEqual(status["mmsplice"]["status"], "used")
        self.assertEqual(status["mmsplice"]["calibration_status"], NOT_REPORTED)

    def test_models_outside_calibration_scope_have_no_calibration_key(self):
        """HyenaDNA/Evo2/RNA-FM/ESM-2/AlphaMissense never had a
        calibration claim to qualify -- absence of the key, not a
        fifth NOT_EVALUATED-shaped state (boundary 5)."""
        status = build_ai_model_status(**_base_kwargs())
        for key in ("hyenadna", "evo2", "rna_fm", "esm2", "alphamissense"):
            self.assertNotIn("calibration_status", status[key], f"{key} must not carry a calibration_status key at all")


class TestRunLevelWorstCaseAggregation(unittest.TestCase):
    def test_single_uncalibrated_variant_wins_over_ninety_nine_calibrated(self):
        """Boundary 2: worst-case direction. A model that reported
        calibrated on 99 variants and uncalibrated on 1 must still
        report uncalibrated for the run, with a tally."""
        per_variant = [{"enformer": {"status": "used", "reason": "r", "calibration_status": CALIBRATED}}] * 99
        per_variant.append({"enformer": {"status": "used", "reason": "r", "calibration_status": UNCALIBRATED}})

        result = rollup_run_status(per_variant)

        self.assertEqual(result["enformer"]["calibration_status"], "uncalibrated (1 of 100 variants)")

    def test_all_calibrated_reports_bare_calibrated(self):
        per_variant = [{"enformer": {"status": "used", "reason": "r", "calibration_status": CALIBRATED}}] * 3
        result = rollup_run_status(per_variant)
        self.assertEqual(result["enformer"]["calibration_status"], CALIBRATED)

    def test_never_fired_reports_not_evaluated(self):
        per_variant = [
            {"enformer": {"status": "skipped", "reason": "not applicable", "calibration_status": NOT_EVALUATED}}
        ] * 3
        result = rollup_run_status(per_variant)
        self.assertEqual(result["enformer"]["calibration_status"], NOT_EVALUATED)

    def test_not_reported_outranks_calibrated(self):
        """MMSplice never writes the field when it fires -- absence of
        a report must not be swamped by a coincidentally-present
        calibrated reading elsewhere in the run."""
        per_variant = [
            {"mmsplice": {"status": "used", "reason": "r", "calibration_status": NOT_REPORTED}},
            {"mmsplice": {"status": "used", "reason": "r", "calibration_status": CALIBRATED}},
        ]
        result = rollup_run_status(per_variant)
        self.assertEqual(result["mmsplice"]["calibration_status"], NOT_REPORTED)

    def test_model_absent_from_all_variants_gets_no_calibration_key(self):
        """A key never present in any per-variant entry (e.g. a model
        outside `_CALIBRATION_MODEL_KEYS`) must not gain a fabricated
        calibration_status at the run level either."""
        per_variant = [{"hyenadna": {"status": "used", "reason": "r"}}]
        result = rollup_run_status(per_variant)
        self.assertNotIn("calibration_status", result["hyenadna"])


class TestProvenanceEnrichmentCarriesCalibrationStatus(unittest.TestCase):
    def test_finalize_carries_calibration_status_into_enriched_record(self):
        identifiers = {"enformer": "enformer-pytorch (see pipeline/models/ensemble.py)"}
        run_status = {
            "enformer": {
                "status": "used",
                "reason": "Ran for 3 of 3 variants as part of the AI splicing/regulatory ensemble.",
                "calibration_status": "uncalibrated (1 of 3 variants)",
            }
        }
        enriched = finalize_model_checkpoint_provenance(identifiers, run_status)
        self.assertEqual(enriched["enformer"]["calibration_status"], "uncalibrated (1 of 3 variants)")

    def test_finalize_omits_calibration_status_when_not_tracked(self):
        """A model with no `calibration_status` key in its run-level
        status entry (every model outside `_CALIBRATION_MODEL_KEYS`)
        must not gain a fabricated one in the enriched record."""
        identifiers = {"hyenadna_checkpoint_dir": "/models/hyenadna"}
        run_status = {"hyenadna": {"status": "used", "reason": "Routed to 2 of 3 variants."}}
        enriched = finalize_model_checkpoint_provenance(identifiers, run_status)
        self.assertNotIn("calibration_status", enriched["hyenadna_checkpoint_dir"])


if __name__ == "__main__":
    unittest.main()
