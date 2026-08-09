"""
Tests for `pipeline/prioritization_engine.py::PrioritizationEngine`'s
`critical_conflict` floor (report review round 4, I2).

Ground truth from `test_data/conflict_tiers.vcf`: Finding 1 (MLH1
p.Gly181=, synonymous) is the run's only A3 Critical-conflict finding
(GEPER Uncertain Significance vs. ClinVar VCV000633499 Pathogenic,
expert panel), yet it scored lower on this engine's own evidence-
completeness factors than Findings 3/4/5 (missense/nonsense variants
that trigger PVS1/PM1/etc., factors a synonymous variant structurally
cannot) -- so review priority ranked it *below* findings with no
clinical conflict at all. `critical_conflict=True` floors the category
at "Critical" regardless of the underlying score, the same shape
`ConflictResolutionEngine._overall_severity` already uses for conflict
severity.
"""

import unittest

from pipeline.prioritization_engine import PrioritizationEngine


def _score(engine, acmg_classification, critical_conflict, triggered_rule_codes=None):
    return engine.score(
        acmg_classification=acmg_classification,
        confidence_score=50.0,
        triggered_rule_codes=triggered_rule_codes or [],
        critical_conflict=critical_conflict,
    )


class TestCriticalConflictFloor(unittest.TestCase):
    def setUp(self):
        self.engine = PrioritizationEngine()

    def test_sparse_evidence_finding_is_not_critical_without_the_flag(self):
        # Finding 1's shape: Uncertain Significance, no PVS1/PM1/PM4 --
        # a synonymous variant triggers none of the protein-impact-
        # driven factors, so the raw score alone lands well below the
        # Critical threshold.
        result = _score(self.engine, "Uncertain Significance", critical_conflict=False)
        self.assertNotEqual(result.category, "Critical")

    def test_sparse_evidence_finding_is_floored_to_critical_with_the_flag(self):
        result = _score(self.engine, "Uncertain Significance", critical_conflict=True)
        self.assertEqual(result.category, "Critical")
        self.assertTrue(any("floored at Critical" in r for r in result.explanation))

    def test_floor_is_a_no_op_when_score_already_earns_critical(self):
        # A variant that already earns Critical on its own factors
        # (PVS1 + high confidence) must not double-annotate or change
        # its score when the flag is also set.
        high = self.engine.score(
            acmg_classification="Pathogenic",
            confidence_score=95.0,
            triggered_rule_codes=["PVS1", "PM1"],
            critical_conflict=False,
        )
        floored = self.engine.score(
            acmg_classification="Pathogenic",
            confidence_score=95.0,
            triggered_rule_codes=["PVS1", "PM1"],
            critical_conflict=True,
        )
        if high.category == "Critical":
            self.assertEqual(floored.score, high.score)
            self.assertFalse(any("floored at Critical" in r for r in floored.explanation))

    def test_findings_3_and_5_style_cases_stay_unfloored(self):
        # Findings 3/5's shape: Likely Pathogenic vs. ClinVar Pathogenic
        # (tier-adjacent, per I4 -- not a Critical conflict), so
        # `has_critical_conflict` upstream would pass `critical_conflict
        # =False` for these; the floor must not apply.
        result = _score(self.engine, "Likely Pathogenic", critical_conflict=False, triggered_rule_codes=["PVS1"])
        # Whatever category this earns on its own merits, it must not
        # carry the floor's explanation text (nothing forced it there).
        self.assertFalse(any("floored at Critical" in r for r in result.explanation))


if __name__ == "__main__":
    unittest.main()
