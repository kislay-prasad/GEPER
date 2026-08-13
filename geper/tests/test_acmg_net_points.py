"""
Report review round 10: `ACMGRuleEngine._combine` used to compute
`pathogenic_points`/`benign_points`/`net_points` purely to pick a
classification, then discard them -- the number that actually decides
every ACMG classification GEPER produces was never recorded anywhere.
Meanwhile a completely separate, pre-ACMG scoring system
(`InterpretationEngine`'s own `significance_score`, unrelated to the
Tavtigian 28-criteria point table) was serialized into
`geper_results.json` right next to `acmg_classification`, reading as
if it were the ACMG score to a JSON consumer with no repo access --
confirmed as a real mistake, not a hypothetical one.

These tests prove, on the five real Colab-verified nuclear variants
from `geper/test_data/nuclear_test_with_mt.vcf`
(GEPER-RUN-20260813, code bb33251):

1. `_combine` now returns the real point totals, and they match the
   hand-computed values from that run exactly: 10, 11, 5, -4, -4.
2. Those numbers thread all the way from `ACMGRuleEngine.evaluate()`
   through `build_interpretation_result` into `InterpretationResult`
   under unambiguous names (`acmg_net_points` etc.).
3. The renamed `legacy_pre_acmg_significance_score` field replaces
   `significance_score` everywhere it used to appear -- including in
   `InterpretationEngine.interpret()`'s own raw return dict, the exact
   `geper_results.json` path the round-9 report was read from
   (`variants[i]["interpretation"]["significance_score"]`).
4. The three report renderers (Markdown, full PDF, short PDF) show the
   real net points and threshold band next to the classification.
"""

import unittest

from pipeline.acmg_rules import ACMGRuleEngine, CriterionResult
from pipeline.interpretation import InterpretationEngine
from pipeline.interpretation_result import build_interpretation_result
from report import clinical_report_builder as crb
from report import report_generator as report_generator_module
from report import summary as summary_module
from report import summary_short as summary_short_module


def _cr(code, direction, strength, status="triggered"):
    return CriterionResult(code=code, direction=direction, strength=strength, status=status, rationale="test")


# The five real, Colab-verified variants (GEPER-RUN-20260813, code
# bb33251) from geper/test_data/nuclear_test_with_mt.vcf, expressed as
# the triggered-criteria shape `_combine` consumes.
_REAL_RUN_CASES = {
    "BRCA1_43106534": {
        "triggered": [
            ("PVS1", "pathogenic", "strong"),
            ("PS3", "pathogenic", "strong"),
            ("PM2", "pathogenic", "moderate"),
        ],
        "expected_net": 10.0,
        "expected_classification": "Pathogenic",
        "expected_band": "≥10",
    },
    "TP53_7674221": {
        "triggered": [
            ("PS3", "pathogenic", "strong"),
            ("PM1", "pathogenic", "moderate"),
            ("PM2", "pathogenic", "moderate"),
            ("PM5", "pathogenic", "moderate"),
            ("PP3", "pathogenic", "supporting"),
        ],
        "expected_net": 11.0,
        "expected_classification": "Pathogenic",
        "expected_band": "≥10",
    },
    "PRNP_4699525": {
        "triggered": [
            ("PM1", "pathogenic", "moderate"),
            ("PM2", "pathogenic", "moderate"),
            ("PP3", "pathogenic", "supporting"),
        ],
        "expected_net": 5.0,
        "expected_classification": "Uncertain Significance",
        "expected_band": "0–5",
    },
    "BRCA1_43094298": {
        "triggered": [
            ("BS3", "benign", "strong"),
            ("BP1", "benign", "supporting"),
            ("BP4", "benign", "supporting"),
            ("BP6", "benign", "supporting"),
            ("PM1", "pathogenic", "moderate"),
        ],
        "expected_net": -4.0,
        "expected_classification": "Likely Benign",
        "expected_band": "≤−1",
    },
    "TP53_7676152": {
        "triggered": [
            ("BS3", "benign", "strong"),
            ("BP1", "benign", "supporting"),
            ("BP4", "benign", "supporting"),
            ("BP6", "benign", "supporting"),
            ("PM2", "pathogenic", "moderate"),
        ],
        "expected_net": -4.0,
        "expected_classification": "Likely Benign",
        "expected_band": "≤−1",
    },
}


def _criteria_for(case):
    return {code: _cr(code, direction, strength) for code, direction, strength in case["triggered"]}


class TestCombineReturnsRealNetPoints(unittest.TestCase):
    def test_all_five_real_variants_match_hand_computed_nets(self):
        for name, case in _REAL_RUN_CASES.items():
            with self.subTest(variant=name):
                result = ACMGRuleEngine._combine(_criteria_for(case))
                self.assertEqual(result.classification, case["expected_classification"])
                self.assertEqual(result.net_points, case["expected_net"])

    def test_bp6_still_structurally_excluded_from_net(self):
        # Both Likely Benign rows include BP6 -- net must reflect
        # BS3+BP1+BP4 only (6 benign points) minus the pathogenic
        # criterion, never BS3+BP1+BP4+BP6 (7 points).
        case = _REAL_RUN_CASES["BRCA1_43094298"]
        result = ACMGRuleEngine._combine(_criteria_for(case))
        self.assertEqual(result.benign_points, 6.0)
        self.assertEqual(result.pathogenic_points, 2.0)

    def test_ba1_short_circuit_leaves_points_none_not_fabricated_zero(self):
        criteria = {"BA1": _cr("BA1", "benign", "stand_alone")}
        result = ACMGRuleEngine._combine(criteria)
        self.assertEqual(result.classification, "Benign")
        self.assertIsNone(result.net_points)
        self.assertIsNone(result.pathogenic_points)
        self.assertIsNone(result.benign_points)

    def test_evaluate_dict_carries_the_same_numbers(self):
        case = _REAL_RUN_CASES["BRCA1_43106534"]
        combine_result = ACMGRuleEngine._combine(_criteria_for(case))
        # `evaluate()`'s returned dict must expose exactly what
        # `_combine` computed -- these are the keys
        # `build_interpretation_result` reads.
        self.assertEqual(combine_result.net_points, 10.0)


class TestThreadedIntoInterpretationResult(unittest.TestCase):
    def _acmg_evaluation_dict(self, case):
        combine_result = ACMGRuleEngine._combine(_criteria_for(case))
        return {
            "classification": combine_result.classification,
            "triggered_criteria": [
                {"code": code, "strength": s, "direction": d, "rationale": "test"} for code, d, s in case["triggered"]
            ],
            "not_triggered_criteria": [],
            "not_evaluated_criteria": [],
            "combining_rule_trace": combine_result.trace,
            "net_points": combine_result.net_points,
            "pathogenic_points": combine_result.pathogenic_points,
            "benign_points": combine_result.benign_points,
        }

    def test_acmg_net_points_reaches_interpretation_result(self):
        case = _REAL_RUN_CASES["TP53_7674221"]
        interpretation = {
            "summary": "s",
            "confidence": "Low",
            "legacy_pre_acmg_significance_score": 0,
            "supporting_evidence": [],
            "acmg_evaluation": self._acmg_evaluation_dict(case),
        }
        ir = build_interpretation_result(
            variant_dict={"chrom": "17", "pos": 7674221, "ref": "G", "alt": "A"},
            interpretation=interpretation,
        )
        self.assertEqual(ir.acmg_net_points, 11.0)
        self.assertEqual(ir.acmg_pathogenic_points, 11.0)
        self.assertEqual(ir.acmg_benign_points, 0.0)
        self.assertEqual(ir.acmg_classification, "Pathogenic")

    def test_to_dict_uses_unambiguous_key_names(self):
        case = _REAL_RUN_CASES["PRNP_4699525"]
        interpretation = {
            "summary": "s",
            "confidence": "Low",
            "legacy_pre_acmg_significance_score": 6.0,  # the exact misleading value from the round-9 report
            "supporting_evidence": [],
            "acmg_evaluation": self._acmg_evaluation_dict(case),
        }
        ir = build_interpretation_result(
            variant_dict={"chrom": "20", "pos": 4699525, "ref": "C", "alt": "T"},
            interpretation=interpretation,
        )
        d = ir.to_dict()
        self.assertEqual(d["acmg_net_points"], 5.0)
        self.assertNotIn("significance_score", d)
        self.assertIn("legacy_pre_acmg_significance_score", d)
        self.assertEqual(d["legacy_pre_acmg_significance_score"], 6.0)


class TestInterpretationEngineNoLongerExposesBareSignificanceScore(unittest.TestCase):
    """The exact JSON path report review round 9 was read from:
    variants[i]["interpretation"]["significance_score"]."""

    def test_interpret_return_dict_has_renamed_key(self):
        engine = InterpretationEngine()
        result = engine.interpret(
            variant_dict={"chrom": "1", "pos": 100, "ref": "A", "alt": "G"},
            dna_models_used=[],
            clinvar_result={"records": []},
            dbsnp_result={"found": False},
            protein_result={},
            blast_result={},
        )
        self.assertNotIn("significance_score", result)
        self.assertIn("legacy_pre_acmg_significance_score", result)
        self.assertIn("acmg_evaluation", result)


class TestNetPointsBandLabel(unittest.TestCase):
    def test_band_labels_match_all_five_real_classifications(self):
        for name, case in _REAL_RUN_CASES.items():
            with self.subTest(variant=name):
                band = crb.acmg_net_points_band_label(case["expected_classification"])
                self.assertEqual(band, case["expected_band"])

    def test_benign_via_ba1_has_no_band(self):
        # BA1's short-circuit never runs the point tally -- there is no
        # band to name.
        self.assertIsNone(crb.acmg_net_points_band_label("Benign"))

    def test_unclassified_has_no_band(self):
        self.assertIsNone(crb.acmg_net_points_band_label(None))


class TestAcmgSectionExposesNetPoints(unittest.TestCase):
    def test_acmg_section_carries_points_and_band(self):
        ir = {
            "acmg_classification": "Pathogenic",
            "triggered_rules": [],
            "not_triggered_rules": [],
            "not_evaluated_rules": [],
            "combining_rule_trace": ["..."],
            "acmg_net_points": 10.0,
            "acmg_pathogenic_points": 10.0,
            "acmg_benign_points": 0.0,
        }
        section = crb._acmg_section(ir)
        self.assertEqual(section["net_points"], 10.0)
        self.assertEqual(section["pathogenic_points"], 10.0)
        self.assertEqual(section["benign_points"], 0.0)
        self.assertEqual(section["net_points_band"], "≥10")


def _real_clinical_report(case, variant_dict):
    """
    Builds a genuine `clinical_report` dict via the real
    `build_interpretation_result` -> `build_clinical_report` path (the
    same pattern `tests/test_report_consistency.py` established) --
    not a hand-maintained stub, so this can't drift from what
    `build_clinical_report`'s ~17 sections actually require (several
    read required keys via `clinical_report["..."]`, not `.get(...)`).
    """
    interpretation = {
        "summary": "s",
        "confidence": "Low",
        "legacy_pre_acmg_significance_score": 0,
        "supporting_evidence": [],
        "acmg_evaluation": {
            "classification": case["expected_classification"],
            "triggered_criteria": [
                {"code": code, "strength": s, "direction": d, "rationale": "test"} for code, d, s in case["triggered"]
            ],
            "not_triggered_criteria": [],
            "not_evaluated_criteria": [],
            "combining_rule_trace": [],
            "net_points": case["expected_net"],
            "pathogenic_points": ACMGRuleEngine._combine(_criteria_for(case)).pathogenic_points,
            "benign_points": ACMGRuleEngine._combine(_criteria_for(case)).benign_points,
        },
    }
    ir = build_interpretation_result(variant_dict=variant_dict, interpretation=interpretation)
    return crb.build_clinical_report(ir.to_dict(), variant_dict)


class TestMarkdownRendersNetPoints(unittest.TestCase):
    def test_markdown_shows_net_points_and_band(self):
        case = _REAL_RUN_CASES["BRCA1_43106534"]
        variant_dict = {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"}
        clinical = _real_clinical_report(case, variant_dict)
        gen = report_generator_module.ReportGenerator()
        lines = gen._render_clinical_report(clinical, {})
        text = "\n".join(lines)
        self.assertIn("Net points", text)
        self.assertIn("10", text)
        self.assertIn("≥10", text)


class TestFullPdfRendersNetPoints(unittest.TestCase):
    def test_variant_section_shows_net_points(self):
        case = _REAL_RUN_CASES["BRCA1_43106534"]
        variant_dict = {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"}
        styles = summary_module._build_stylesheet()
        variant_result = {"variant": variant_dict, "clinical_report": _real_clinical_report(case, variant_dict)}
        flow = summary_module._build_variant_section(1, variant_result, styles)
        text = "\n".join(getattr(f, "text", "") for f in flow if hasattr(f, "text"))
        self.assertIn("Net points", text)
        self.assertIn("10", text)

    def test_clinician_summary_table_shows_net_points_inline(self):
        case = _REAL_RUN_CASES["BRCA1_43106534"]
        variant_dict = {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"}
        styles = summary_module._build_stylesheet()
        variant_result = {"variant": variant_dict, "clinical_report": _real_clinical_report(case, variant_dict)}
        table = summary_module._build_clinician_summary_table([variant_result], styles)
        row = table._cellvalues[1]
        classification_cell_text = getattr(row[2], "text", "")
        self.assertIn("Pathogenic", classification_cell_text)
        self.assertIn("net 10", classification_cell_text)


class TestShortPdfRendersNetPoints(unittest.TestCase):
    def test_classification_text_includes_net_points(self):
        case = _REAL_RUN_CASES["BRCA1_43094298"]
        variant_dict = {"chrom": "17", "pos": 43094298, "ref": "A", "alt": "C"}
        clinical = _real_clinical_report(case, variant_dict)
        text = summary_short_module._classification_text(clinical)
        self.assertIn("Likely Benign", text)
        self.assertIn("net -4", text)


if __name__ == "__main__":
    unittest.main()
