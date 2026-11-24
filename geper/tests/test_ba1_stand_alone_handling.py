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


if __name__ == "__main__":
    unittest.main()
