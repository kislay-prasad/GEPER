"""
Regression test for I8 (report review round 4, sub-item 3):
`Supporting Evidence` printed the gnomAD "variant not found" fact
twice -- once from `InterpretationEngine.interpret()`'s "legacy"
evidence list ("gnomAD: variant not found in the population database
(PM2 evidence -- absent from gnomAD).") and once from PM2's own
`CriterionResult.supporting_evidence` ("gnomAD: variant not found.").
Two different sentences about the same fact, so
`pipeline/interpretation_result.py`'s exact-string `_dedupe` never
caught it.

Uses `build_interpretation_result` directly (the real merge function),
not a mock -- the legacy-seed list and the ACMG `triggered_criteria`
list are constructed the way `InterpretationEngine.interpret()` and
`ACMGRuleEngine.evaluate()` actually shape them for this exact
gnomAD-not-found scenario.
"""

import unittest

from pipeline.interpretation_result import build_interpretation_result

_VARIANT_DICT = {
    "chrom": "3",
    "pos": 10146594,
    "ref": "A",
    "alt": "AA",
    "variant_type": "INDEL",
    "id": ".",
    "filter": "PASS",
}

_LEGACY_GNOMAD_NOT_FOUND_TEXT = (
    "gnomAD: variant not found in the population database (PM2 evidence -- absent from gnomAD)."
)
_PM2_OWN_TEXT = "gnomAD: variant not found."


def _interpretation_with_gnomad_not_found():
    return {
        "summary": "s",
        "confidence": "Low",
        "significance_score": 1,
        "supporting_evidence": [_LEGACY_GNOMAD_NOT_FOUND_TEXT],
        "acmg_evaluation": {
            "classification": "Uncertain Significance",
            "triggered_criteria": [
                {"code": "PM2", "supporting_evidence": [_PM2_OWN_TEXT], "conflicting_evidence": []},
            ],
            "not_triggered_criteria": [],
            "not_evaluated_criteria": [],
            "combining_rule_trace": [],
        },
    }


class TestGnomadNotFoundNoLongerDuplicated(unittest.TestCase):
    def test_legacy_text_dropped_pm2_text_kept(self):
        result_obj = build_interpretation_result(
            variant_dict=_VARIANT_DICT,
            interpretation=_interpretation_with_gnomad_not_found(),
        )
        occurrences_of_the_fact = [
            line for line in result_obj.supporting_evidence if "gnomAD" in line and "not found" in line
        ]
        self.assertEqual(len(occurrences_of_the_fact), 1)
        self.assertIn(_PM2_OWN_TEXT, result_obj.supporting_evidence)
        self.assertNotIn(_LEGACY_GNOMAD_NOT_FOUND_TEXT, result_obj.supporting_evidence)

    def test_unrelated_legacy_evidence_is_unaffected(self):
        interpretation = _interpretation_with_gnomad_not_found()
        interpretation["supporting_evidence"].append("Sequence context analyzed with: DNABERT-2.")
        result_obj = build_interpretation_result(variant_dict=_VARIANT_DICT, interpretation=interpretation)
        self.assertIn("Sequence context analyzed with: DNABERT-2.", result_obj.supporting_evidence)


if __name__ == "__main__":
    unittest.main()
