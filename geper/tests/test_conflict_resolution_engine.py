"""
Tests for `pipeline/conflict_resolution_engine.py::ConflictResolutionEngine`'s
A3 detectors (`_expert_panel_disagreement_conflict`,
`_curated_clinvar_disagreement_conflict`) and their shared
`_clinvar_disagrees`/`_classification_tier` helpers.

Report review round 4, I4: A3 only checked one direction of
disagreement (ClinVar asserts a pathogenic/benign direction GEPER
doesn't share), silently missing the mirror case where GEPER stakes out
a specific pathogenic/benign call and ClinVar's own expert panel
explicitly could not ("Uncertain significance"). Fixtures below are the
five real, live-verified findings from `test_data/conflict_tiers.vcf`
(MLH1 + VHL, see `test_data/conflict_tiers.md`), covering every
combination the fix needed to get right: same-direction cross-category
disagreement (already worked), adjacent-tier P-vs-LP agreement (must
stay quiet), and the previously-missed LP-vs-Uncertain case.
"""

import unittest

from pipeline.conflict_resolution_engine import ConflictResolutionEngine


def _clinvar_result(accession, clinical_significance, review_status):
    record = {
        "accession": accession,
        "clinical_significance": clinical_significance,
        "review_status": review_status,
    }
    return {"match_status": "matched", "primary_record": record}


class TestClassificationTier(unittest.TestCase):
    def test_pathogenic_and_likely_pathogenic_share_a_tier(self):
        self.assertEqual(ConflictResolutionEngine._classification_tier("Pathogenic"), "pathogenic")
        self.assertEqual(ConflictResolutionEngine._classification_tier("Likely pathogenic"), "pathogenic")

    def test_benign_and_likely_benign_share_a_tier(self):
        self.assertEqual(ConflictResolutionEngine._classification_tier("Benign"), "benign")
        self.assertEqual(ConflictResolutionEngine._classification_tier("Likely benign"), "benign")

    def test_uncertain_significance_and_conflicting_share_a_tier(self):
        self.assertEqual(ConflictResolutionEngine._classification_tier("Uncertain significance"), "uncertain")
        self.assertEqual(
            ConflictResolutionEngine._classification_tier("Conflicting classifications of pathogenicity"),
            "uncertain",
        )

    def test_unrecognized_text_is_none(self):
        self.assertIsNone(ConflictResolutionEngine._classification_tier("risk factor"))
        self.assertIsNone(ConflictResolutionEngine._classification_tier("drug response"))


class TestClinvarDisagreesSymmetric(unittest.TestCase):
    def test_uncertain_vs_pathogenic_disagrees(self):
        # Finding 1: MLH1 p.Gly181=, GEPER Uncertain Significance vs
        # ClinVar VCV000633499 Pathogenic, expert panel. Worked before
        # this fix too -- must keep working.
        record = {"clinical_significance": "Pathogenic"}
        self.assertTrue(ConflictResolutionEngine._clinvar_disagrees("Uncertain Significance", record))

    def test_pathogenic_vs_pathogenic_expert_panel_stays_quiet(self):
        # Finding 3: VHL p.Gln195Ter, GEPER Likely Pathogenic vs ClinVar
        # VCV000428794 Pathogenic, expert panel -- tier-adjacent,
        # deliberately still not a disagreement.
        record = {"clinical_significance": "Pathogenic"}
        self.assertFalse(ConflictResolutionEngine._clinvar_disagrees("Likely Pathogenic", record))

    def test_likely_pathogenic_vs_uncertain_now_disagrees(self):
        # Finding 4: VHL p.Glu204Ter, GEPER Likely Pathogenic vs ClinVar
        # VCV000526673 Uncertain significance, expert panel, 3-star --
        # the exact case I4 reports as missed. Previously: clinvar_tier
        # was neither "pathogenic" nor "benign" so the old boolean check
        # (`clinvar_pathogenic and not geper_pathogenic`) was False no
        # matter what GEPER said. Now: tiers differ ("uncertain" vs
        # "pathogenic") -> disagreement fires.
        record = {"clinical_significance": "Uncertain significance"}
        self.assertTrue(ConflictResolutionEngine._clinvar_disagrees("Likely Pathogenic", record))

    def test_pathogenic_vs_duplication_pathogenic_stays_quiet(self):
        # Finding 5: VHL c.422dup, GEPER Likely Pathogenic (per I1's
        # discussion) vs ClinVar VCV000411979 Pathogenic, expert panel
        # -- same tier as Finding 3, deliberately still quiet.
        record = {"clinical_significance": "Pathogenic"}
        self.assertFalse(ConflictResolutionEngine._clinvar_disagrees("Likely Pathogenic", record))

    def test_benign_vs_likely_benign_stays_quiet(self):
        record = {"clinical_significance": "Benign"}
        self.assertFalse(ConflictResolutionEngine._clinvar_disagrees("Likely Benign", record))

    def test_uncertain_vs_uncertain_stays_quiet(self):
        record = {"clinical_significance": "Uncertain significance"}
        self.assertFalse(ConflictResolutionEngine._clinvar_disagrees("Uncertain Significance", record))

    def test_unrecognized_clinvar_text_never_fabricates_a_disagreement(self):
        record = {"clinical_significance": "risk factor"}
        self.assertFalse(ConflictResolutionEngine._clinvar_disagrees("Pathogenic", record))


class TestExpertPanelDisagreementConflictOnConflictTiersFixture(unittest.TestCase):
    """`_expert_panel_disagreement_conflict` end-to-end on the real
    conflict_tiers.vcf findings (expert-panel/practice-guideline tier
    only -- the Critical detector)."""

    def test_finding1_mlh1_synonymous_fires_critical(self):
        clinvar_result = _clinvar_result("VCV000633499", "Pathogenic", "reviewed by expert panel")
        item = ConflictResolutionEngine._expert_panel_disagreement_conflict("Uncertain Significance", clinvar_result)
        self.assertIsNotNone(item)
        self.assertEqual(item.severity, "Critical")

    def test_finding3_vhl_nonsense_p_vs_lp_does_not_fire(self):
        clinvar_result = _clinvar_result("VCV000428794", "Pathogenic", "reviewed by expert panel")
        item = ConflictResolutionEngine._expert_panel_disagreement_conflict("Likely Pathogenic", clinvar_result)
        self.assertIsNone(item)

    def test_finding4_vhl_nonsense_lp_vs_uncertain_now_fires_critical(self):
        clinvar_result = _clinvar_result("VCV000526673", "Uncertain significance", "reviewed by expert panel")
        item = ConflictResolutionEngine._expert_panel_disagreement_conflict("Likely Pathogenic", clinvar_result)
        self.assertIsNotNone(item)
        self.assertEqual(item.severity, "Critical")

    def test_finding5_vhl_dup_p_vs_lp_does_not_fire(self):
        clinvar_result = _clinvar_result("VCV000411979", "Pathogenic", "reviewed by expert panel")
        item = ConflictResolutionEngine._expert_panel_disagreement_conflict("Likely Pathogenic", clinvar_result)
        self.assertIsNone(item)


class TestHasCriticalConflict(unittest.TestCase):
    """`has_critical_conflict` (added for I2) must agree exactly with
    whether `_expert_panel_disagreement_conflict` fires -- it's a thin
    wrapper, not a re-derivation."""

    def test_true_when_expert_panel_disagreement_fires(self):
        clinvar_result = _clinvar_result("VCV000633499", "Pathogenic", "reviewed by expert panel")
        self.assertTrue(ConflictResolutionEngine.has_critical_conflict("Uncertain Significance", clinvar_result))

    def test_false_on_tier_adjacent_agreement(self):
        clinvar_result = _clinvar_result("VCV000428794", "Pathogenic", "reviewed by expert panel")
        self.assertFalse(ConflictResolutionEngine.has_critical_conflict("Likely Pathogenic", clinvar_result))

    def test_false_when_no_clinvar_match(self):
        self.assertFalse(
            ConflictResolutionEngine.has_critical_conflict("Pathogenic", {"match_status": "position_only"})
        )

    def test_false_when_review_status_not_authoritative(self):
        clinvar_result = _clinvar_result("VCV1", "Pathogenic", "criteria provided, single submitter")
        self.assertFalse(ConflictResolutionEngine.has_critical_conflict("Benign", clinvar_result))


if __name__ == "__main__":
    unittest.main()
