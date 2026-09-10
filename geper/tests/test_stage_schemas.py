"""
Tests for pipeline/stage_schemas.py -- Pydantic validation at the
InterpretationResult -> report-builder boundary (and the two
neighboring boundaries it also covers).

The central regression case (`TestRawEvidenceBundleCatchesTheRealBug`)
reproduces the exact historical bug this module exists to catch: an
empty `{}` standing in for "no raw evidence" -- precisely what
`ir.get("raw_evidence") or {}` always produced before commit 9f5a2bf
(see tests/test_report_consistency.py for that fix's own regression
tests) -- must fail LOUDLY (a Pydantic ValidationError) rather than
silently validating as "every source checked, found nothing".
"""

import unittest

from pydantic import ValidationError

from pipeline.stage_schemas import (
    AcmgEvaluationSchema,
    InterpretationResultForReport,
    RawEvidenceBundle,
    StageEvidence,
    StageStatus,
    build_raw_evidence_bundle,
    validate_acmg_evaluation,
    validate_interpretation_result_for_report,
)


class TestRawEvidenceBundleCatchesTheRealBug(unittest.TestCase):
    """The exact failure mode from the real, shipped bug."""

    def test_empty_dict_raises_not_validates(self):
        # `ir.get("raw_evidence") or {}`'s actual failure value.
        with self.assertRaises(ValidationError) as ctx:
            RawEvidenceBundle.model_validate({})
        # All 11 stages must be reported as missing, not just the first.
        missing_fields = {err["loc"][0] for err in ctx.exception.errors()}
        self.assertEqual(
            missing_fields,
            {
                "clinvar",
                "dbsnp",
                "protein",
                "blast",
                "alphamissense",
                "mmsplice",
                "gnomad",
                "clingen",
                "uniprot",
                "interpro",
                "alphafold",
            },
        )

    def test_boundary_helper_logs_loudly_on_a_malformed_stage_dict(self):
        # The boundary function (`build_raw_evidence_bundle`, what
        # json_builder.py actually calls) must log the failure at
        # ERROR level -- loud in every environment, not just "dev" --
        # rather than merely raising/returning silently.
        with self.assertLogs("geper.pipeline.stage_schemas", level="ERROR") as log_ctx:
            bundle, err = build_raw_evidence_bundle(clinvar_result="not-a-dict-at-all")
        self.assertIsNone(bundle)
        self.assertIsNotNone(err)
        self.assertTrue(any("raw_evidence schema validation failed" in msg for msg in log_ctx.output))

    def test_from_raw_with_all_none_is_a_legitimate_not_run_bundle(self):
        # Unlike bare model_validate({}), .from_raw() with every kwarg
        # omitted is a legitimate state (everything genuinely not run
        # yet) -- NOT the bug. This is what distinguishes "structurally
        # absent" (rejected) from "structurally present and NOT_RUN"
        # (accepted).
        bundle, err = build_raw_evidence_bundle()
        self.assertIsNone(err)
        self.assertEqual(bundle.clinvar.status, StageStatus.NOT_RUN)
        self.assertEqual(bundle.uniprot.status, StageStatus.NOT_RUN)


class TestStageEvidenceStatusDerivation(unittest.TestCase):
    """The not_run / error / not_found / found four-way distinction."""

    def test_none_is_not_run(self):
        ev = StageEvidence.from_raw(None)
        self.assertEqual(ev.status, StageStatus.NOT_RUN)

    def test_skipped_true_is_not_run_even_with_a_reason(self):
        ev = StageEvidence.from_raw({"skipped": True, "reason": "disabled via config"})
        self.assertEqual(ev.status, StageStatus.NOT_RUN)

    def test_error_key_is_error_not_not_found(self):
        # This is the exact distinction commit 9f5a2bf's
        # `*_error`/`*_available` fields were introduced to make in
        # report/clinical_report_builder.py -- a failed lookup must
        # never render the same as "queried, nothing there".
        ev = StageEvidence.from_raw({"error": "EBI InterPro API timeout", "found": False})
        self.assertEqual(ev.status, StageStatus.ERROR)
        self.assertEqual(ev.error, "EBI InterPro API timeout")

    def test_found_true_is_found(self):
        ev = StageEvidence.from_raw({"found": True, "accession": "P12345"})
        self.assertEqual(ev.status, StageStatus.FOUND)
        self.assertEqual(ev.data["accession"], "P12345")

    def test_found_false_no_error_is_not_found(self):
        ev = StageEvidence.from_raw({"found": False})
        self.assertEqual(ev.status, StageStatus.NOT_FOUND)

    def test_error_and_status_must_agree(self):
        with self.assertRaises(ValidationError):
            StageEvidence(status=StageStatus.FOUND, error="should not coexist with FOUND")
        with self.assertRaises(ValidationError):
            StageEvidence(status=StageStatus.ERROR)  # error status with no error message

    def test_blast_uses_hit_count_not_found_key(self):
        ev = StageEvidence.from_raw(
            {"hits": [{"id": "x"}], "hit_count": 1}, found_when=lambda r: (r.get("hit_count") or 0) > 0
        )
        self.assertEqual(ev.status, StageStatus.FOUND)
        ev_empty = StageEvidence.from_raw(
            {"hits": [], "hit_count": 0}, found_when=lambda r: (r.get("hit_count") or 0) > 0
        )
        self.assertEqual(ev_empty.status, StageStatus.NOT_FOUND)

    def test_mmsplice_uses_predicted_key(self):
        ev = StageEvidence.from_raw({"supported": True, "predicted": True}, found_key="predicted")
        self.assertEqual(ev.status, StageStatus.FOUND)

    def test_protein_translation_stage_is_found_whenever_not_skipped(self):
        # Not a lookup -- reaching here with skipped=False already
        # means real translation/ESM-2 data exists.
        ev = StageEvidence.from_raw({"skipped": False, "translation": {}, "esm2": {}}, found_when=lambda r: True)
        self.assertEqual(ev.status, StageStatus.FOUND)


class TestStageEvidenceEmptyStringErrorIsNotDroppedAsAbsent(unittest.TestCase):
    """
    Regression tests for the fix in `8612e45`, written against the
    pre-fix code. Everything below describes `stage_schemas.py` AT
    `8612e45^` and reads in that pre-fix present tense; the code
    coordinates cited are that revision's line numbers, not HEAD's.

    `test_error_key_is_error_not_not_found` above already pins
    that a NON-EMPTY error string produces `StageStatus.ERROR`. It never
    exercises the one value that actually breaks the check that reads it --
    `""`. `StageEvidence.from_raw`'s `if error:` (stage_schemas.py:199) and
    the class-level validator's `not self.error` (stage_schemas.py:138,143)
    are both TRUTHINESS checks on a field whose contract is PRESENCE:
    `error = raw.get("error")` returns `""` whenever a producer's own
    `error` key is explicitly set to the empty string, and `if error:` is
    `False` for `""` exactly as it is for `None` -- so a stage that
    genuinely errored, with nothing to say about why, is silently
    reclassified as `NOT_FOUND`/`FOUND`/`NOT_RUN` instead of `ERROR`, and
    whatever it did say (an empty string, but a REAL one) is discarded
    entirely rather than surfaced.

    THIS IS NOT A HYPOTHETICAL VALUE. This is the exact same defect class
    already found and fixed for gnomAD's own producer in `d1128ee`
    (`_run_gnomad_stage`'s `except Exception` used to do bare
    `errors.append(...); return {..., "error": str(exc)}`, and `str(exc)`
    is `""` for any exception raised with no message -- `RuntimeError()`,
    `MemoryError()`, `TimeoutError()` -- until that commit added a
    `type(exc).__name__` fallback specifically to stop producing `""`).
    `stage_schemas.py::StageEvidence.from_raw` is the SHARED conversion
    boundary every one of `RawEvidenceBundle`'s 11 stages passes through
    (`stage_schemas.py:264-275` -- clinvar, dbsnp, protein, blast,
    alphamissense, gnomad, clingen, uniprot, interpro, alphafold, and
    mmsplice all call it), so this defect is not confined to whichever
    producer's own except-block happens to still do bare `str(exc)` today
    -- it is a property of the boundary itself, and will bite again for any
    producer that regresses to (or was never fixed away from) an
    unqualified `str(exc)`. Checked directly, not assumed: dbSNP's and
    ClinVar's own stage handlers (`orchestrator.py:2384`, `:2395`) still do
    exactly this -- `return {..., "error": str(exc)}`, no fallback -- so
    the same empty-string producer-side risk gnomAD had before `d1128ee`
    is, as of this writing, still live for at least these two.

    Both tests below are RED at `8612e45^` and GREEN from `8612e45`
    onward. Reconstructed 2026-08-31 by loading `8612e45^`'s
    `stage_schemas.py` under this test module and re-running this class
    against it: with `found=False` the status came back `NOT_FOUND`,
    with `found=True` it came back `FOUND` -- both instead of `ERROR`,
    which is exactly the misclassification described above. They pass
    at HEAD, and a green run here is the fix holding, not the claim
    being empty.

    THIS CLAIM'S OWN CORRECTION COMMIT: `8311ee8` (2026-08-31), which
    replaced an earlier, uncheckable "both tests below are RED" /
    "written before the fix exists" wording with the reconstruction
    above -- because the fix commit (`8612e45`) is an ancestor of the
    commit that added these tests (`2bc81b6`), no commit ever existed
    with the tests present and the fix absent, so the red state cannot
    be recovered by `git checkout` and is stated here from a manual
    reconstruction instead. Independently re-run and re-confirmed
    2026-09-10 (conv-stageschemas): re-verified 29 passed at HEAD and
    the two NOT_FOUND/FOUND failures above at `8612e45^`, matching this
    docstring exactly, rather than trusting either the docstring or
    `8311ee8`'s own commit message on their word. Citing `8311ee8` here
    directly so a future re-check of this claim starts with `git show
    8311ee8`, not a fresh investigation from a stale paraphrase of it.

    The fix touches two sites in
    tandem, not one -- fixing `from_raw`'s `if error:` alone (to
    `is not None`) without also fixing the validator's `not self.error`
    (to `is None`) would convert today's silent misclassification into a
    hard crash for this exact input, which is not an improvement. Neither
    test below asserts on which specific fix shape lands (e.g. whether
    `""` is preserved verbatim or normalized to a fallback string) -- only
    on what a caller of `StageEvidence.from_raw` can observe: the status
    must be ERROR, and the error state must not collapse into any other
    status.
    """

    def test_empty_string_error_produces_error_status_not_not_found(self):
        # Realistic shape: any producer whose exception handler does bare
        # `str(exc)` with no fallback (dbSNP/ClinVar today, gnomAD before
        # d1128ee) emits exactly this for a message-less exception.
        ev = StageEvidence.from_raw({"error": "", "found": False})
        self.assertEqual(
            ev.status,
            StageStatus.ERROR,
            "A stage result carrying an explicit (even empty) 'error' key "
            "must never resolve to the same status as a lookup that ran "
            "cleanly and found nothing -- that is the exact shape of the "
            "gnomAD defect fixed in d1128ee, reachable here through any "
            "other producer's own empty-message exception.",
        )

    def test_empty_string_error_produces_error_status_not_found_true(self):
        # Same defect, the other branch: a producer that also happens to
        # set found=True (or any other confirmed-positive field) alongside
        # an empty error must not let that positive field win either.
        ev = StageEvidence.from_raw({"error": "", "found": True, "accession": "P12345"})
        self.assertEqual(ev.status, StageStatus.ERROR)


class TestRawEvidenceBundleFromRaw(unittest.TestCase):
    def test_realistic_mixed_bundle(self):
        bundle, err = build_raw_evidence_bundle(
            variant_ref="chr17:43106534C>A",
            uniprot_result={"found": True, "accession": "P38398", "protein_name": "BRCA1", "reviewed": True},
            interpro_result={"found": False},
            gnomad_result={"error": "gnomAD GraphQL timeout after 3 attempts", "found": False},
            clinvar_result=None,
            blast_result={"hits": [], "hit_count": 0},
        )
        self.assertIsNone(err)
        self.assertEqual(bundle.uniprot.status, StageStatus.FOUND)
        self.assertEqual(bundle.interpro.status, StageStatus.NOT_FOUND)
        self.assertEqual(bundle.gnomad.status, StageStatus.ERROR)
        self.assertEqual(bundle.clinvar.status, StageStatus.NOT_RUN)
        self.assertEqual(bundle.blast.status, StageStatus.NOT_FOUND)

    def test_to_legacy_dict_round_trips_original_payload_unchanged(self):
        # This is what keeps report/clinical_report_builder.py's
        # section builders (`_protein_knowledge` etc.) working
        # unmodified: they must see the exact same dict shape as
        # before this schema layer existed.
        original_uniprot = {"found": True, "accession": "P38398", "protein_name": "BRCA1", "reviewed": True}
        bundle, _ = build_raw_evidence_bundle(uniprot_result=original_uniprot)
        legacy = bundle.to_legacy_dict()
        self.assertEqual(legacy["uniprot"], original_uniprot)
        self.assertEqual(
            set(legacy.keys()),
            {
                "clinvar",
                "dbsnp",
                "protein",
                "blast",
                "alphamissense",
                "mmsplice",
                "gnomad",
                "clingen",
                "uniprot",
                "interpro",
                "alphafold",
            },
        )

    def test_bundle_is_frozen(self):
        bundle, _ = build_raw_evidence_bundle()
        with self.assertRaises(ValidationError):
            bundle.clinvar = StageEvidence.from_raw({"found": True})


class TestInterpretationResultForReport(unittest.TestCase):
    def _good_ir(self, **overrides):
        base = {
            "variant": {"chrom": "17", "pos": 100},
            "gene_symbol": "BRCA1",
            "acmg_classification": "Pathogenic",
            "triggered_rules": [],
            "not_triggered_rules": [],
            "not_evaluated_rules": [],
            "combining_rule_trace": [],
            "supporting_evidence": [],
            "conflicting_evidence": [],
            "ai_consensus": [],
            "ai_context_models": [],
            "confidence_pending": False,
            "confidence_score": 0.9,
            "confidence_label": "High",
            "confidence_breakdown": {},
            "priority_pending": False,
            "priority_score": 0.8,
            "priority_category": "Critical",
            "priority_rank": None,
            "priority_explanation": [],
            "priority_breakdown": {},
            "conflict_summary": None,
            "conflict_list": [],
            "conflict_score": 0.0,
            "conflict_severity": None,
            "conflict_resolution": None,
            "explainability": None,
            "recommendations": [],
            "evidence_sources": [],
            "ai_model_errors": [],
        }
        base.update(overrides)
        return base

    def test_valid_ir_passes(self):
        result, err = validate_interpretation_result_for_report(self._good_ir())
        self.assertIsNone(err)
        self.assertIsInstance(result, InterpretationResultForReport)

    def test_none_is_not_an_error(self):
        result, err = validate_interpretation_result_for_report(None)
        self.assertIsNone(result)
        self.assertIsNone(err)

    def test_errored_aggregation_sentinel_is_not_a_schema_error(self):
        result, err = validate_interpretation_result_for_report(
            {"error": "InterpretationResult aggregation failed; see logs."}
        )
        self.assertIsNone(result)
        self.assertIsNone(err)  # documented "aggregation failed" state, not a validation failure

    def test_missing_required_list_field_fails_loudly(self):
        broken = self._good_ir()
        del broken["triggered_rules"]
        result, err = validate_interpretation_result_for_report(broken, variant_ref="chr17:100A>G")
        self.assertIsNone(result)
        self.assertIsNotNone(err)
        self.assertIn("chr17:100A>G", err)

    def test_confidence_not_pending_but_score_none_fails_loudly(self):
        # The structural version of the pending/populated pairing that
        # was previously just a convention.
        broken = self._good_ir(confidence_pending=False, confidence_score=None)
        result, err = validate_interpretation_result_for_report(broken)
        self.assertIsNone(result)
        self.assertIsNotNone(err)

    def test_priority_not_pending_but_category_none_fails_loudly(self):
        broken = self._good_ir(priority_pending=False, priority_category=None)
        result, err = validate_interpretation_result_for_report(broken)
        self.assertIsNone(result)
        self.assertIsNotNone(err)

    def test_pending_true_with_none_values_is_valid(self):
        # The legitimate "engine didn't run / failed for this variant" state.
        pending = self._good_ir(
            confidence_pending=True,
            confidence_score=None,
            confidence_label=None,
            confidence_breakdown=None,
            priority_pending=True,
            priority_score=None,
            priority_category=None,
            priority_breakdown=None,
        )
        result, err = validate_interpretation_result_for_report(pending)
        self.assertIsNone(err)
        self.assertTrue(result.confidence_pending)


class TestAcmgEvaluationSchema(unittest.TestCase):
    def test_valid_evaluation_passes(self):
        result, err = validate_acmg_evaluation(
            {
                "classification": "Likely Pathogenic",
                "triggered_criteria": [{"code": "PM2"}],
                "not_triggered_criteria": [],
                "not_evaluated_criteria": [],
                "combining_rule_trace": ["PM2 -> Likely Pathogenic"],
            }
        )
        self.assertIsNone(err)
        self.assertIsInstance(result, AcmgEvaluationSchema)
        self.assertEqual(result.classification, "Likely Pathogenic")

    def test_none_or_empty_is_not_an_error(self):
        result, err = validate_acmg_evaluation(None)
        self.assertIsNone(result)
        self.assertIsNone(err)
        result2, err2 = validate_acmg_evaluation({})
        self.assertIsNone(result2)
        self.assertIsNone(err2)

    def test_wrong_type_fails_loudly(self):
        result, err = validate_acmg_evaluation({"triggered_criteria": "not-a-list"}, variant_ref="chr1:1A>T")
        self.assertIsNone(result)
        self.assertIsNotNone(err)
        self.assertIn("chr1:1A>T", err)


class TestNeverRaisesPastTheBoundary(unittest.TestCase):
    """
    Point 4 of the task: loud in every environment, never fatal. Every
    boundary helper must return a (None, message) pair on a validation
    failure -- never let a ValidationError (or any other exception)
    propagate out to the orchestrator and abort an otherwise-successful
    clinical run over a schema mismatch on one variant.
    """

    def test_build_raw_evidence_bundle_never_raises(self):
        # A value that cannot possibly satisfy the schema (wrong type
        # entirely) must still come back as (None, message), not raise.
        bundle, err = build_raw_evidence_bundle(clinvar_result="not-a-dict-at-all")  # type: ignore[arg-type]
        self.assertIsNotNone(err)
        self.assertIsNone(bundle)

    def test_validate_interpretation_result_never_raises_on_garbage(self):
        result, err = validate_interpretation_result_for_report(
            {"triggered_rules": "not-a-list", "confidence_pending": "not-a-bool"}
        )
        self.assertIsNone(result)
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main()
