"""
BA1 stand-alone benign handling -- Priority 1 clinical-correctness remediation.

What this file pins, and why:

1. CLASSIFICATION (`ACMGRuleEngine._combine`). BA1 is stand-alone benign
   evidence (Richards et al. 2015, Table 4) and sits outside the Bayesian
   point model, so it is never netted against pathogenic points. But before
   this remediation a triggered BA1 returned Benign while silently discarding
   ANY pathogenic evidence, including PVS1 + PS3. Richards et al. 2015,
   Table 5 footnote: "Variants should be classified as Uncertain
   Significance if other criteria are unmet or the criteria for benign and
   pathogenic are contradictory." ClinGen SVI (Ghosh et al. 2018) keeps BA1
   stand-alone but maintains an exception list for high-frequency variants
   with credible pathogenic evidence. Policy pinned here (a clinical
   decision awaiting sign-off): BA1 + any strong/very-strong pathogenic
   criterion -> Uncertain Significance + review_required; BA1 with only
   moderate/supporting pathogenic evidence stays Benign, with those
   criteria named in the trace.

2. FAILED OR UNEVALUATED EVIDENCE IS NEVER BENIGN. A gnomAD lookup that
   errored, was skipped, found nothing, or returned no allele frequency
   leaves BA1 not_evaluated, and a not_evaluated/not_triggered BA1 never
   short-circuits the point tally.

3. DOWNSTREAM AGREEMENT. The legacy "Summary"/"Confidence (legacy)" lines
   rendered by `report_generator._render_interpretation` must not contradict
   a BA1-decided ACMG classification. They are left untouched when the ACMG
   engine failed (their documented fallback role) and when BA1 did not
   trigger.

These are software tests. They show the code does what the policy says;
they do not establish clinical validity of the policy itself.
"""

import unittest
from unittest import mock

from config import CONFIG
from pipeline.acmg_rules import ACMGRuleEngine, CombineResult, CriterionResult
from pipeline.conflict_resolution_engine import ConflictResolutionEngine
from pipeline.interpretation import InterpretationEngine
from pipeline.interpretation_outcome import (
    InterpretationOutcome,
    determine_interpretation_outcome,
    determine_review_required_reasons,
)
from report.report_generator import ReportGenerator

_BA1_REASON = "ba1_contradicted_by_strong_pathogenic_evidence"
_G = CONFIG.gnomad


def _crit(code, direction, strength, status="triggered"):
    return CriterionResult(code=code, direction=direction, strength=strength, status=status, rationale="test")


def _ba1(status="triggered"):
    return _crit("BA1", "benign", "stand_alone", status)


class TestCombineBa1Policy(unittest.TestCase):
    def test_ba1_alone_is_benign_and_runs_no_point_tally(self):
        r = ACMGRuleEngine._combine({"BA1": _ba1()})
        self.assertEqual(r.classification, "Benign")
        self.assertIsNone(r.pathogenic_points)
        self.assertIsNone(r.benign_points)
        self.assertIsNone(r.net_points)
        self.assertEqual(r.ba1_contradicted_by, [])

    def test_ba1_with_very_strong_and_strong_pathogenic_is_uncertain_not_benign(self):
        # The reproduced defect: this combination used to return Benign with
        # PVS1/PS3 dropped from the trace without a word.
        r = ACMGRuleEngine._combine(
            {
                "BA1": _ba1(),
                "PVS1": _crit("PVS1", "pathogenic", "very_strong"),
                "PS3": _crit("PS3", "pathogenic", "strong"),
                "PM2": _crit("PM2", "pathogenic", "moderate"),
            }
        )
        self.assertEqual(r.classification, "Uncertain Significance")
        self.assertEqual(r.ba1_contradicted_by, ["PS3", "PVS1"])
        trace = " ".join(r.trace)
        self.assertIn("PVS1 (very_strong)", trace)
        self.assertIn("PS3 (strong)", trace)
        self.assertIn("expert review required", trace)
        # Still not netted: BA1 sits outside the point model.
        self.assertIsNone(r.net_points)

    def test_boundary_a_single_strong_pathogenic_criterion_is_enough(self):
        r = ACMGRuleEngine._combine({"BA1": _ba1(), "PS1": _crit("PS1", "pathogenic", "strong")})
        self.assertEqual(r.classification, "Uncertain Significance")
        self.assertEqual(r.ba1_contradicted_by, ["PS1"])

    def test_boundary_moderate_and_supporting_pathogenic_do_not_override_ba1_but_are_named(self):
        r = ACMGRuleEngine._combine(
            {
                "BA1": _ba1(),
                "PM1": _crit("PM1", "pathogenic", "moderate"),
                "PM5": _crit("PM5", "pathogenic", "moderate"),
                "PP3": _crit("PP3", "pathogenic", "supporting"),
            }
        )
        self.assertEqual(r.classification, "Benign")
        self.assertEqual(r.ba1_contradicted_by, [])
        trace = " ".join(r.trace)
        for code in ("PM1", "PM5", "PP3"):
            self.assertIn(code, trace, f"{code} was triggered but is not named in the BA1 trace")

    def test_ba1_with_other_benign_evidence_stays_benign(self):
        r = ACMGRuleEngine._combine(
            {"BA1": _ba1(), "BS1": _crit("BS1", "benign", "strong"), "BP4": _crit("BP4", "benign", "supporting")}
        )
        self.assertEqual(r.classification, "Benign")
        self.assertEqual(r.ba1_contradicted_by, [])

    def test_non_triggered_ba1_never_short_circuits(self):
        # Failed / unevaluated / unmet BA1 must not be treated as benign:
        # the normal point tally runs and decides.
        for status in ("not_evaluated", "not_triggered"):
            with self.subTest(status=status):
                r = ACMGRuleEngine._combine(
                    {
                        "BA1": _ba1(status),
                        "PVS1": _crit("PVS1", "pathogenic", "very_strong"),
                        "PM2": _crit("PM2", "pathogenic", "moderate"),
                    }
                )
                self.assertEqual(r.classification, "Pathogenic")
                self.assertEqual(r.net_points, 10)
                self.assertEqual(r.ba1_contradicted_by, [])


class TestBa1IsOnlyTriggeredByRealFrequencyEvidence(unittest.TestCase):
    def _ba1_status(self, gnomad_result):
        ba1, bs1 = ACMGRuleEngine._ba1_bs1(gnomad_result)
        return ba1.status, bs1.status

    def test_failed_or_missing_gnomad_leaves_ba1_not_evaluated(self):
        cases = {
            "no result": None,
            "skipped": {"skipped": True},
            "errored": {"skipped": False, "found": False, "error": "timeout"},
            "error with found=True": {"skipped": False, "found": True, "global_af": 0.5, "error": "partial"},
            "empty error string": {"skipped": False, "found": True, "global_af": 0.5, "error": ""},
            "not found": {"skipped": False, "found": False},
            "found but no allele frequency": {"skipped": False, "found": True, "population_breakdown": {}},
        }
        for label, result in cases.items():
            with self.subTest(case=label):
                ba1_status, _ = self._ba1_status(result)
                self.assertEqual(ba1_status, "not_evaluated")

    def test_threshold_boundary(self):
        # Exactly-at-threshold is deliberately NOT asserted: the code uses
        # ">=" while ClinGen SVI (Ghosh et al. 2018) specifies ">0.05". That
        # discrepancy is reported for a policy decision, not pinned here.
        above = {
            "skipped": False,
            "found": True,
            "global_af": _G.BA1_AF_THRESHOLD * 1.001,
            "population_breakdown": {},
        }
        below = {
            "skipped": False,
            "found": True,
            "global_af": _G.BA1_AF_THRESHOLD * 0.999,
            "population_breakdown": {},
        }
        self.assertEqual(self._ba1_status(above)[0], "triggered")
        self.assertEqual(self._ba1_status(below)[0], "not_triggered")


class TestBa1ContradictionEscalatesToReview(unittest.TestCase):
    _BASE = dict(pathogenic_points=None, benign_points=None, classification="Uncertain Significance")

    def test_outcome_and_reasons_when_contradicted(self):
        outcome = determine_interpretation_outcome(
            gene_resolved=True, evidence_queries_completed=True, ba1_contradicted=True, **self._BASE
        )
        self.assertIs(outcome, InterpretationOutcome.REVIEW_REQUIRED)
        self.assertEqual(determine_review_required_reasons(ba1_contradicted=True, **self._BASE), [_BA1_REASON])

    def test_default_is_unchanged_for_existing_callers(self):
        outcome = determine_interpretation_outcome(gene_resolved=True, evidence_queries_completed=True, **self._BASE)
        self.assertIsNot(outcome, InterpretationOutcome.REVIEW_REQUIRED)
        self.assertEqual(determine_review_required_reasons(**self._BASE), [])

    def test_evaluate_wires_the_contradiction_into_its_output(self):
        contradicted = CombineResult(
            classification="Uncertain Significance",
            trace=["test"],
            pathogenic_points=None,
            benign_points=None,
            net_points=None,
            ba1_contradicted_by=["PVS1"],
        )
        with mock.patch.object(ACMGRuleEngine, "_combine", return_value=contradicted):
            out = ACMGRuleEngine().evaluate(
                clinvar_result={},
                dbsnp_result={},
                protein_result={},
                gnomad_result={"skipped": False, "found": True, "global_af": 0.2, "population_breakdown": {}},
            )
        self.assertEqual(out["classification"], "Uncertain Significance")
        self.assertEqual(out["interpretation_outcome"], InterpretationOutcome.REVIEW_REQUIRED.value)
        self.assertIn(_BA1_REASON, out["review_required_reasons"])


_VARIANT = {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"}
_AM_LP = {"am_class": "likely_pathogenic", "am_pathogenicity": 0.9, "available": True, "found": True}
_GNOMAD_BA1 = {"skipped": False, "found": True, "global_af": _G.BA1_AF_THRESHOLD * 2, "population_breakdown": {}}


def _clinvar(significance):
    record = {"clinical_significance": significance, "review_status": "criteria provided", "variant_match": True}
    return {"records": [record], "match_status": "matched", "primary_record": record}


class TestRenderedSummaryAgreesWithBa1Classification(unittest.TestCase):
    def setUp(self):
        self.engine = InterpretationEngine()

    def _interpret(self, gnomad_result, significance="Pathogenic", alphamissense=None):
        return self.engine.interpret(
            _VARIANT,
            [],
            _clinvar(significance),
            {},
            {},
            {},
            gnomad_result=gnomad_result,
            alphamissense_result=alphamissense,
        )

    @staticmethod
    def _line(lines, prefix):
        return next((line for line in lines if line.startswith(prefix)), "")

    def test_ba1_benign_is_not_rendered_as_pathogenic_leaning(self):
        # The reproduced downstream defect: ACMG said Benign while the
        # rendered summary said "suggestive of clinical relevance"/moderate.
        result = self._interpret(_GNOMAD_BA1, "Pathogenic", _AM_LP)
        self.assertEqual(result["acmg_evaluation"]["classification"], "Benign")
        lines = ReportGenerator._render_interpretation(result)
        summary = self._line(lines, "**Summary:**")
        self.assertIn("classified Benign under ACMG/AMP because BA1", summary)
        self.assertNotIn("suggestive of clinical relevance", summary)
        self.assertNotIn("consistent with pathogenicity", summary)
        self.assertEqual(self._line(lines, "**Confidence (legacy):**"), "**Confidence (legacy):** low")

    def test_neutral_legacy_summary_is_left_alone(self):
        # ClinVar Pathogenic (+3) + BA1 (-3) = 0 -> "uncertain" bucket,
        # which does not contradict the BA1 call.
        result = self._interpret(_GNOMAD_BA1, "Pathogenic")
        self.assertIn("uncertain clinical significance", result["summary"])

    def test_no_ba1_means_no_change(self):
        result = self._interpret(None, "Pathogenic", _AM_LP)
        self.assertIn("consistent with pathogenicity", result["summary"])
        self.assertEqual(result["confidence"], "high")

    def test_legacy_fallback_is_untouched_when_the_acmg_engine_fails(self):
        with mock.patch.object(self.engine._acmg_engine, "evaluate", side_effect=RuntimeError("boom")):
            result = self._interpret(_GNOMAD_BA1, "Pathogenic", _AM_LP)
        self.assertIsNone(result["acmg_evaluation"]["classification"])
        self.assertIn("suggestive of clinical relevance", result["summary"])
        self.assertEqual(result["confidence"], "moderate")

    def test_contradicted_ba1_summary_says_uncertain_pending_review_whatever_the_legacy_bucket(self):
        contradicted = {
            "classification": "Uncertain Significance",
            "all_criteria": {"BA1": {"code": "BA1", "status": "triggered"}},
            "review_required_reasons": [_BA1_REASON],
        }
        for significance in ("Pathogenic", "Benign"):
            with self.subTest(clinvar=significance):
                with mock.patch.object(self.engine._acmg_engine, "evaluate", return_value=contradicted):
                    result = self._interpret(_GNOMAD_BA1, significance, _AM_LP)
                self.assertIn("Uncertain Significance pending expert review", result["summary"])
                self.assertEqual(result["confidence"], "low")


class TestStructuredReportHasNoBandWithoutATally(unittest.TestCase):
    def test_band_is_null_when_no_point_tally_ran(self):
        from report.clinical_report_builder import _acmg_section

        # BA1 contradicted by strong pathogenic evidence: Uncertain
        # Significance with no tally. A "0–5" band here would imply one.
        contradicted = _acmg_section({"acmg_classification": "Uncertain Significance", "acmg_net_points": None})
        self.assertIsNone(contradicted["net_points"])
        self.assertIsNone(contradicted["net_points_band"])

    def test_band_unchanged_when_a_tally_ran(self):
        from report.clinical_report_builder import _acmg_section

        tallied = _acmg_section({"acmg_classification": "Uncertain Significance", "acmg_net_points": 3})
        self.assertEqual(tallied["net_points_band"], "0–5")


class TestBa1ReviewSignalReachesEveryOutput(unittest.TestCase):
    """
    A BA1-contradicted variant (Uncertain Significance, review_required) run
    through the real pipeline steps -- InterpretationEngine.interpret ->
    build_interpretation_result -> build_clinical_report -- into all six
    rendered outputs. Only `_combine`'s return is fixed (constructing a real
    PVS1 needs transcript structure); everything downstream is real.

    LIMS CSV deliberately carries only the classification: its header is a
    fixed contract (see report/export_lims.py `_CSV_COLUMNS`) and was not
    extended. That gap is reported, not pinned here.
    """

    def _document(self):
        from pipeline.interpretation_result import build_interpretation_result
        from report.clinical_report_builder import build_clinical_report
        from tests.test_genome_build_on_every_rendered_surface import _document

        variant = {"chrom": "1", "pos": 1000, "ref": "C", "alt": "A"}
        contradicted = CombineResult(
            classification="Uncertain Significance",
            trace=[
                "BA1 triggered (stand-alone benign) but contradicted by strong pathogenic evidence: PVS1 (very_strong)."
            ],
            pathogenic_points=None,
            benign_points=None,
            net_points=None,
            ba1_contradicted_by=["PVS1"],
        )
        with mock.patch.object(ACMGRuleEngine, "_combine", return_value=contradicted):
            interpretation = InterpretationEngine().interpret(variant, [], {}, {}, {}, {}, gnomad_result=_GNOMAD_BA1)
        ir = build_interpretation_result(
            variant_dict=variant, interpretation=interpretation, gnomad_result=_GNOMAD_BA1
        ).to_dict()
        document = _document("GRCh38")
        row = document["variants"][0]
        row["variant"] = variant
        row["interpretation_result"] = ir
        # Production shape: the same dict is reachable from both keys (see
        # pipeline/orchestrator.py's note on build_variant_result).
        row["interpretation"] = {**interpretation, "interpretation_result": ir}
        row["candidate_interpretation"] = build_clinical_report(ir, variant_dict=variant)
        return document

    def test_classification_and_review_signal_on_every_output(self):
        import tempfile

        from tests.test_genome_build_on_every_rendered_surface import SURFACES, _render

        flag = "BA1 contradicted by strong pathogenic evidence (expert review required)"
        document = self._document()
        with tempfile.TemporaryDirectory() as tmp:
            rendered = {surface: " ".join(_render(surface, document, tmp).split()) for surface in SURFACES}

        for surface, text in rendered.items():
            with self.subTest(surface=surface, check="classification"):
                self.assertRegex(text, "Uncertain [Ss]ignificance")
        for surface in ("results_json", "markdown", "lims_json"):
            with self.subTest(surface=surface, check="machine-readable reason"):
                self.assertIn(_BA1_REASON, rendered[surface])
                self.assertIn("review_required", rendered[surface])
        for surface in ("markdown", "full_pdf", "short_pdf"):
            with self.subTest(surface=surface, check="reviewer flag"):
                self.assertIn(flag, rendered[surface])

    def test_reviewer_flag_stays_narrow(self):
        from report.clinical_report_builder import _variant_reviewer_flags

        # A Pathogenic call also sets review_required; it must not raise the
        # BA1 flag (the flag list is deliberately narrow).
        pathogenic = {"review_required_reasons": ["classification_pathogenic_or_likely_pathogenic"]}
        self.assertEqual(_variant_reviewer_flags({}, pathogenic), [])
        ba1 = {"review_required_reasons": [_BA1_REASON]}
        self.assertEqual(
            _variant_reviewer_flags({}, ba1),
            ["BA1 contradicted by strong pathogenic evidence (expert review required)"],
        )

    def test_lims_classification_passes_outcome_and_reasons_through_verbatim(self):
        from report.export_lims import _classification

        for outcome, reasons in (
            ("review_required", [_BA1_REASON]),
            ("review_required", ["classification_pathogenic_or_likely_pathogenic"]),
            ("interpreted", []),
            (None, []),
        ):
            with self.subTest(outcome=outcome, reasons=reasons):
                lims = _classification(
                    {
                        "acmg_classification": {"classification": "Uncertain Significance"},
                        "interpretation_outcome": outcome,
                        "review_required_reasons": reasons,
                    }
                )
                self.assertEqual(lims.interpretation_outcome, outcome)
                self.assertEqual(lims.review_required_reasons, reasons)


# ---------------------------------------------------------------------------
# PR #51 round 2 (F1-F5). These run the REAL path -- real `_ba1_bs1`, real
# `_combine`, real `evaluate()` / `interpret()` -- forcing only `_pvs1`
# (building a genuine PVS1 needs transcript structure). The round-1 tests above
# mock `_combine`, which is why they could not see these.
# ---------------------------------------------------------------------------

_GENE_RESOLVED = {"gene_resolution_status": "resolved"}


def _pvs1(strength):
    return CriterionResult("PVS1", "pathogenic", strength, "triggered", "forced for the test")


def _evaluate(gnomad_result=_GNOMAD_BA1, pvs1=None, extra=None):
    """The real `ACMGRuleEngine.evaluate()`; only `_pvs1` is forced when given."""
    patches = []
    if pvs1 is not None:
        patches.append(mock.patch.object(ACMGRuleEngine, "_pvs1", return_value=pvs1))
    for patch in patches:
        patch.start()
    try:
        return ACMGRuleEngine().evaluate(
            clinvar_result={}, dbsnp_result={}, protein_result={}, gnomad_result=gnomad_result, **(extra or {})
        )
    finally:
        for patch in patches:
            patch.stop()


def _interpret(pvs1, gnomad_result=_GNOMAD_BA1, clinvar_result=None):
    with mock.patch.object(ACMGRuleEngine, "_pvs1", return_value=pvs1):
        return InterpretationEngine().interpret(
            _VARIANT, [], clinvar_result or {}, {}, {}, {}, gnomad_result=gnomad_result, clingen_result=_GENE_RESOLVED
        )["interpretation_result"]


class TestF1Ba1BenignIsNotReportedAsInsufficientEvidence(unittest.TestCase):
    _KW = dict(gene_resolved=True, evidence_queries_completed=True, pathogenic_points=None, benign_points=None)

    def test_the_stand_alone_path_is_interpreted(self):
        outcome = determine_interpretation_outcome(classification="Benign", point_tally_ran=False, **self._KW)
        self.assertIs(outcome, InterpretationOutcome.INTERPRETED)

    def test_default_behaviour_is_unchanged_for_existing_callers(self):
        # No tally flag + zero evidence is still insufficient_evidence.
        zero = dict(self._KW, pathogenic_points=0.0, benign_points=0.0)
        self.assertIs(
            determine_interpretation_outcome(classification="Uncertain Significance", **zero),
            InterpretationOutcome.INSUFFICIENT_EVIDENCE,
        )
        self.assertIs(
            determine_interpretation_outcome(classification="Benign", **self._KW),
            InterpretationOutcome.INSUFFICIENT_EVIDENCE,
        )

    def test_real_path_ba1_benign_is_interpreted(self):
        out = _evaluate(extra={"clingen_result": _GENE_RESOLVED})
        self.assertEqual(out["classification"], "Benign")
        self.assertEqual(out["interpretation_outcome"], InterpretationOutcome.INTERPRETED.value)
        self.assertEqual(out["review_required_reasons"], [])

    def test_a_genuine_no_evidence_variant_is_still_insufficient(self):
        out = _evaluate(gnomad_result=None, extra={"clingen_result": _GENE_RESOLVED})
        self.assertEqual(out["interpretation_outcome"], InterpretationOutcome.INSUFFICIENT_EVIDENCE.value)

    def test_lims_json_no_longer_exports_the_contradiction(self):
        from pipeline.interpretation_result import build_interpretation_result
        from report.clinical_report_builder import build_clinical_report
        from report.export_lims import _classification

        interp = InterpretationEngine().interpret(
            _VARIANT, [], {}, {}, {}, {}, gnomad_result=_GNOMAD_BA1, clingen_result=_GENE_RESOLVED
        )
        ir = build_interpretation_result(variant_dict=_VARIANT, interpretation=interp, gnomad_result=_GNOMAD_BA1)
        lims = _classification(build_clinical_report(ir.to_dict(), variant_dict=_VARIANT))
        self.assertEqual(lims.acmg_classification, "Benign")
        self.assertEqual(lims.interpretation_outcome, "interpreted")


class TestF3Ba1ContradictedByIsStructuredData(unittest.TestCase):
    def test_evaluate_exposes_the_contradicting_codes(self):
        out = _evaluate(pvs1=_pvs1("very_strong"))
        self.assertEqual(out["ba1_contradicted_by"], ["PVS1"])

    def test_empty_list_when_not_contradicted(self):
        self.assertEqual(_evaluate()["ba1_contradicted_by"], [])
        self.assertEqual(_evaluate(pvs1=_pvs1("supporting"))["ba1_contradicted_by"], [])

    def test_carried_to_the_interpretation_result_and_clinical_report(self):
        from pipeline.interpretation_result import build_interpretation_result
        from report.clinical_report_builder import build_clinical_report

        with mock.patch.object(ACMGRuleEngine, "_pvs1", return_value=_pvs1("very_strong")):
            interp = InterpretationEngine().interpret(
                _VARIANT, [], {}, {}, {}, {}, gnomad_result=_GNOMAD_BA1, clingen_result=_GENE_RESOLVED
            )
        ir = build_interpretation_result(variant_dict=_VARIANT, interpretation=interp, gnomad_result=_GNOMAD_BA1)
        self.assertEqual(ir.ba1_contradicted_by, ["PVS1"])
        self.assertEqual(ir.to_dict()["ba1_contradicted_by"], ["PVS1"])
        clinical = build_clinical_report(ir.to_dict(), variant_dict=_VARIANT)
        self.assertEqual(clinical["acmg_classification"]["ba1_contradicted_by"], ["PVS1"])

    def test_default_for_old_results_without_the_key(self):
        from report.clinical_report_builder import _acmg_section

        self.assertEqual(_acmg_section({"acmg_classification": "Benign"})["ba1_contradicted_by"], [])


class TestF4RealPathThroughEvaluate(unittest.TestCase):
    def test_strength_follows_the_effective_pvs1_strength(self):
        # PVS1 can be downgraded (ClinGen SVI); the policy uses the strength
        # the criterion actually carries, exactly like the point tally does.
        cases = {
            "very_strong": ("Uncertain Significance", True),
            "strong": ("Uncertain Significance", True),
            "moderate": ("Benign", False),
            "supporting": ("Benign", False),
        }
        for strength, (classification, contradicted) in cases.items():
            with self.subTest(pvs1_strength=strength):
                out = _evaluate(pvs1=_pvs1(strength))
                self.assertEqual(out["classification"], classification)
                self.assertEqual(bool(out["ba1_contradicted_by"]), contradicted)
                self.assertEqual(_BA1_REASON in out["review_required_reasons"], contradicted)
                self.assertIn("PVS1", " ".join(out["combining_rule_trace"]))

    def test_a_forced_strong_ps1_contradicts_like_pvs1(self):
        ps1 = CriterionResult("PS1", "pathogenic", "strong", "triggered", "forced for the test")
        with mock.patch.object(ACMGRuleEngine, "_ps1", return_value=ps1):
            out = _evaluate()
        self.assertEqual(out["classification"], "Uncertain Significance")
        self.assertEqual(out["ba1_contradicted_by"], ["PS1"])

    def test_a_failed_or_missing_gnomad_lookup_never_counts_as_benign(self):
        # BA1 is not_evaluated, so strong pathogenic evidence decides alone
        # (PVS1 = 8 points = Likely Pathogenic) -- never Benign, never a
        # "contradiction" with a criterion that did not trigger.
        for label, gnomad in (("error", {"skipped": False, "found": False, "error": "timeout"}), ("none", None)):
            with self.subTest(gnomad=label):
                out = _evaluate(gnomad_result=gnomad, pvs1=_pvs1("very_strong"))
                self.assertNotEqual(out["classification"], "Benign")
                self.assertEqual(out["ba1_contradicted_by"], [])

    def test_below_the_threshold_is_not_a_contradiction(self):
        below = dict(_GNOMAD_BA1, global_af=_G.BA1_AF_THRESHOLD * 0.5)
        out = _evaluate(gnomad_result=below, pvs1=_pvs1("very_strong"))
        self.assertEqual(out["ba1_contradicted_by"], [])
        self.assertNotEqual(out["classification"], "Benign")


class TestF2ExecutiveSummaryExplainsTheContradiction(unittest.TestCase):
    def test_summary_names_ba1_the_codes_and_expert_review(self):
        from report.clinical_report_builder import _executive_summary

        text = _executive_summary(
            {
                "variant": _VARIANT,
                "acmg_classification": "Uncertain Significance",
                "review_required_reasons": [_BA1_REASON],
                "ba1_contradicted_by": ["PVS1", "PS3"],
            }
        )
        self.assertIn("BA1", text)
        self.assertIn("PVS1, PS3", text)
        self.assertIn("expert review is required", text)

    def test_summary_is_unchanged_without_the_reason(self):
        from report.clinical_report_builder import _executive_summary

        for reasons in ([], ["classification_pathogenic_or_likely_pathogenic"]):
            with self.subTest(reasons=reasons):
                text = _executive_summary(
                    {"variant": _VARIANT, "acmg_classification": "Pathogenic", "review_required_reasons": reasons}
                )
                self.assertNotIn("BA1", text)

    def test_real_flow_summary_mentions_the_contradiction(self):
        from pipeline.interpretation_result import build_interpretation_result
        from report.clinical_report_builder import build_clinical_report

        with mock.patch.object(ACMGRuleEngine, "_pvs1", return_value=_pvs1("very_strong")):
            interp = InterpretationEngine().interpret(
                _VARIANT, [], {}, {}, {}, {}, gnomad_result=_GNOMAD_BA1, clingen_result=_GENE_RESOLVED
            )
        ir = build_interpretation_result(variant_dict=_VARIANT, interpretation=interp, gnomad_result=_GNOMAD_BA1)
        summary = build_clinical_report(ir.to_dict(), variant_dict=_VARIANT)["executive_summary"]
        self.assertIn("BA1", summary)
        self.assertIn("PVS1", summary)


class TestF5ConflictResolutionAndPrioritization(unittest.TestCase):
    _DETECT = dict(
        acmg_classification="Uncertain Significance",
        acmg_conflicting_evidence=[],
        ai_consensus=[],
        confidence_conflict_penalty=0.0,
        confidence_conflict_explanation="",
        priority_conflict_penalty=0.0,
        priority_conflict_explanation="",
    )

    def test_detector_returns_a_major_population_item(self):
        item = ConflictResolutionEngine._ba1_contradiction_conflict(["PVS1", "PS3"])
        self.assertIsNotNone(item)
        self.assertEqual(item.severity, "Major")
        self.assertEqual(item.category, "Population")
        self.assertIn("PVS1, PS3", item.evidence_b["statement"])

    def test_detector_is_silent_without_a_contradiction(self):
        for empty in ([], (), None, [""]):
            with self.subTest(value=empty):
                self.assertIsNone(ConflictResolutionEngine._ba1_contradiction_conflict(empty))

    def test_detect_and_scoring_agree_and_default_is_unchanged(self):
        engine = ConflictResolutionEngine()
        plain = engine.detect(**self._DETECT)
        self.assertEqual(plain.conflict_severity, "None")  # existing callers: no new item
        with_ba1 = engine.detect(ba1_contradicted_by=["PVS1"], **self._DETECT)
        self.assertEqual(with_ba1.conflict_severity, "Major")
        self.assertGreater(with_ba1.conflict_score, 0.0)
        scoring = engine.real_conflicts_for_scoring(
            **{k: v for k, v in self._DETECT.items() if "penalty" not in k and "explanation" not in k},
            ba1_contradicted_by=["PVS1"],
        )
        self.assertEqual([c.severity for c in scoring], ["Major"])

    def test_real_flow_records_the_conflict_and_floors_priority_at_high(self):
        ir = _interpret(_pvs1("very_strong"))
        self.assertEqual(ir["acmg_classification"], "Uncertain Significance")
        self.assertEqual(ir["conflict_severity"], "Major")
        types = [c["conflict_type"] for c in ir["conflict_list"]]
        self.assertTrue(any(t.startswith("BA1 stand-alone benign frequency") for t in types), types)
        self.assertEqual(ir["priority_category"], "High")
        self.assertTrue(
            any("floored at High" in line for line in ir["priority_explanation"]), ir["priority_explanation"]
        )

    def test_real_flow_without_a_contradiction_is_unchanged(self):
        ir = _interpret(_pvs1("supporting"))  # BA1 stays Benign; PVS1 only named in the trace
        self.assertEqual(ir["acmg_classification"], "Benign")
        self.assertEqual(ir["conflict_severity"], "None")
        self.assertFalse(
            any(c["conflict_type"].startswith("BA1 stand-alone") for c in ir["conflict_list"]), ir["conflict_list"]
        )

    def test_reviewer_flag_reports_the_major_conflict(self):
        from report.clinical_report_builder import _variant_reviewer_flags

        flags = _variant_reviewer_flags(
            {}, {"conflict_resolution": {"severity": "Major"}, "review_required_reasons": []}
        )
        self.assertIn("Conflicting evidence (Major)", flags)


_POP_CONFLICT_PREFIX = "Population rarity inconsistent"
_BA1_CONFLICT_PREFIX = "BA1 stand-alone benign frequency"


def _types(conflict_list):
    return [c["conflict_type"] for c in conflict_list]


class TestF5DuplicatePopulationEvidenceIsScoredOnce(unittest.TestCase):
    """
    The existing population conflict and the BA1-contradiction conflict are
    two descriptions of the same at-or-above-BA1-frequency evidence when the
    first fires at Major. Scoring both doubled the conflict score and lowered
    confidence twice for one fact (found in the round-2 verification).
    """

    _DETECT = TestF5ConflictResolutionAndPrioritization._DETECT

    def _kwargs(self, global_af, clinvar=True):
        kw = dict(
            self._DETECT,
            gnomad_result={"found": True, "global_af": global_af, "population_breakdown": {}},
        )
        if clinvar:
            kw["clinvar_result"] = _clinvar("Pathogenic")
        return kw

    def test_major_population_conflict_suppresses_the_duplicate(self):
        engine = ConflictResolutionEngine()
        kw = self._kwargs(_G.BA1_AF_THRESHOLD * 2)
        without = engine.detect(**kw)
        with_ba1 = engine.detect(ba1_contradicted_by=["PVS1"], **kw)
        types = [c.conflict_type for c in with_ba1.conflict_list]
        self.assertTrue(any(t.startswith(_POP_CONFLICT_PREFIX) for t in types), types)
        self.assertFalse(any(t.startswith(_BA1_CONFLICT_PREFIX) for t in types), types)
        # Same fact is not counted twice: identical score and severity.
        self.assertEqual(with_ba1.conflict_score, without.conflict_score)
        self.assertEqual(with_ba1.conflict_severity, without.conflict_severity)
        self.assertEqual(with_ba1.conflict_severity, "Major")
        self.assertEqual([c.to_dict() for c in with_ba1.conflict_list], [c.to_dict() for c in without.conflict_list])

    def test_the_existing_population_conflict_keeps_its_severity_and_content(self):
        engine = ConflictResolutionEngine()
        kw = self._kwargs(_G.BA1_AF_THRESHOLD * 2)
        pop_alone = [c for c in engine.detect(**kw).conflict_list if c.conflict_type.startswith(_POP_CONFLICT_PREFIX)]
        pop_with_ba1 = [
            c
            for c in engine.detect(ba1_contradicted_by=["PVS1"], **kw).conflict_list
            if c.conflict_type.startswith(_POP_CONFLICT_PREFIX)
        ]
        self.assertEqual(len(pop_with_ba1), 1)
        self.assertEqual(pop_with_ba1[0].severity, "Major")
        self.assertEqual(pop_with_ba1[0].to_dict(), pop_alone[0].to_dict())

    def test_scoring_path_agrees_with_detect(self):
        engine = ConflictResolutionEngine()
        kw = self._kwargs(_G.BA1_AF_THRESHOLD * 2)
        scoring_kw = {k: v for k, v in kw.items() if "penalty" not in k and "explanation" not in k}
        scoring = engine.real_conflicts_for_scoring(ba1_contradicted_by=["PVS1"], **scoring_kw)
        self.assertEqual(
            [c.conflict_type.split(" ")[0] for c in scoring],
            ["Population"],
            [c.conflict_type for c in scoring],
        )
        self.assertEqual([c.severity for c in scoring], ["Major"])

    def test_without_a_population_conflict_the_ba1_conflict_is_still_raised(self):
        engine = ConflictResolutionEngine()
        # No ClinVar pathogenic record and an Uncertain classification: the
        # population detector stays silent, so the BA1 item must carry the conflict.
        kw = self._kwargs(_G.BA1_AF_THRESHOLD * 2, clinvar=False)
        result = engine.detect(ba1_contradicted_by=["PVS1"], **kw)
        types = [c.conflict_type for c in result.conflict_list]
        self.assertFalse(any(t.startswith(_POP_CONFLICT_PREFIX) for t in types), types)
        self.assertTrue(any(t.startswith(_BA1_CONFLICT_PREFIX) for t in types), types)
        self.assertEqual(result.conflict_severity, "Major")

    def test_a_moderate_population_conflict_is_a_different_finding_and_both_remain(self):
        engine = ConflictResolutionEngine()
        # global AF between BS1 and BA1 (BA1 met on popmax only): the population
        # item is Moderate, so it does NOT represent the BA1 evidence.
        kw = self._kwargs((_G.BS1_AF_THRESHOLD + _G.BA1_AF_THRESHOLD) / 2)
        result = engine.detect(ba1_contradicted_by=["PVS1"], **kw)
        by_prefix = {
            "pop": [c for c in result.conflict_list if c.conflict_type.startswith(_POP_CONFLICT_PREFIX)],
            "ba1": [c for c in result.conflict_list if c.conflict_type.startswith(_BA1_CONFLICT_PREFIX)],
        }
        self.assertEqual([c.severity for c in by_prefix["pop"]], ["Moderate"])
        self.assertEqual([c.severity for c in by_prefix["ba1"]], ["Major"])
        self.assertEqual(result.conflict_severity, "Major")

    def test_default_without_ba1_codes_is_unchanged(self):
        engine = ConflictResolutionEngine()
        kw = self._kwargs(_G.BA1_AF_THRESHOLD * 2)
        self.assertEqual(
            [c.to_dict() for c in engine.detect(**kw).conflict_list],
            [c.to_dict() for c in engine.detect(ba1_contradicted_by=(), **kw).conflict_list],
        )

    def test_real_flow_counts_the_evidence_once_and_keeps_the_high_floor(self):
        ir = _interpret(_pvs1("very_strong"), clinvar_result=_clinvar("Pathogenic"))
        types = _types(ir["conflict_list"])
        self.assertEqual(ir["acmg_classification"], "Uncertain Significance")
        self.assertEqual(sum(t.startswith(_POP_CONFLICT_PREFIX) for t in types), 1, types)
        self.assertFalse(any(t.startswith(_BA1_CONFLICT_PREFIX) for t in types), types)
        self.assertEqual(ir["conflict_severity"], "Major")
        # One Major item: same score as the pre-round-2 behaviour (one item), not two.
        self.assertAlmostEqual(ir["conflict_score"], 16.67, places=1)
        # Genuine contradiction still gets the High-priority floor, via the Major item.
        self.assertEqual(ir["priority_category"], "High")
        self.assertTrue(
            any("floored at High" in line for line in ir["priority_explanation"]), ir["priority_explanation"]
        )

    def test_real_flow_keeps_the_contradiction_in_the_acmg_result_and_report(self):
        from pipeline.interpretation_result import build_interpretation_result
        from report.clinical_report_builder import build_clinical_report

        clinvar = _clinvar("Pathogenic")
        with mock.patch.object(ACMGRuleEngine, "_pvs1", return_value=_pvs1("very_strong")):
            interp = InterpretationEngine().interpret(
                _VARIANT, [], clinvar, {}, {}, {}, gnomad_result=_GNOMAD_BA1, clingen_result=_GENE_RESOLVED
            )
        ir = build_interpretation_result(
            variant_dict=_VARIANT, interpretation=interp, clinvar_result=clinvar, gnomad_result=_GNOMAD_BA1
        ).to_dict()
        self.assertEqual(ir["ba1_contradicted_by"], ["PVS1"])
        self.assertEqual(ir["interpretation_outcome"], "review_required")
        self.assertIn(_BA1_REASON, ir["review_required_reasons"])
        report = build_clinical_report(ir, variant_dict=_VARIANT)
        self.assertIn("BA1", report["executive_summary"])
        self.assertIn("PVS1", report["executive_summary"])
        self.assertEqual(report["acmg_classification"]["ba1_contradicted_by"], ["PVS1"])

    def test_ordinary_ba1_benign_is_not_elevated_by_the_new_logic(self):
        # BA1 stays Benign with only supporting pathogenic evidence: no BA1 conflict.
        ir = _interpret(_pvs1("supporting"))
        self.assertEqual(ir["acmg_classification"], "Benign")
        self.assertEqual(ir["conflict_severity"], "None")
        self.assertFalse(any(t.startswith(_BA1_CONFLICT_PREFIX) for t in _types(ir["conflict_list"])))


if __name__ == "__main__":
    unittest.main()
