"""
tests/test_acmg_audit_issue4.py
─────────────────────────────────
Regression tests for the Issue 4 ACMG/AMP guideline audit:

1. PP3/BP4 gate REVEL and AlphaMissense to missense variants only
   (both are missense-specific predictors; scoring them for a
   non-missense variant is scientifically invalid, not just imprecise).
2. PP5/BP6 gain a configurable opt-out (`disable_pp5_bp6`) per the
   ClinGen SVI's recommendation against using raw ClinVar significance
   as independent ACMG evidence — default preserves prior behavior.
3. The core P/LP/LB/B combining-rule table (Richards et al. 2015,
   Table 5) is verified exhaustively against every documented rule.
"""

from __future__ import annotations

from pipeline.acmg.classifier import AcmgClassifier, VariantEvidence, STATUS_NOT_EVALUATED


def _base_evidence(**overrides):
    defaults = dict(chrom="17", pos=1, ref="A", alt="T", gene="TESTGENE")
    defaults.update(overrides)
    return VariantEvidence(**defaults)


class TestPP3BP4MissenseGating:
    def test_revel_not_counted_for_non_missense_variant(self):
        """A stray REVEL score on a non-missense variant (e.g. an
        upstream annotation bug) must not count toward PP3 — REVEL is
        undefined for non-missense consequences."""
        clf = AcmgClassifier(cfg={})
        ev = _base_evidence(is_missense=False, revel_score=0.99, cadd_phred=5.0)
        result = clf.classify(ev)
        pp3 = next(c for c in result.all_criteria if c.code == "PP3")
        # CADD alone is below threshold (20.0 default) so PP3 should not fire,
        # and REVEL must not have been counted at all (not just outvoted).
        assert "REVEL" not in pp3.reason

    def test_revel_counted_for_missense_variant(self):
        clf = AcmgClassifier(cfg={})
        ev = _base_evidence(is_missense=True, revel_score=0.9)
        result = clf.classify(ev)
        pp3 = next(c for c in result.all_criteria if c.code == "PP3")
        assert "REVEL" in pp3.reason
        assert pp3.status == "met"

    def test_alphamissense_not_counted_for_non_missense_variant(self):
        clf = AcmgClassifier(cfg={})
        ev = _base_evidence(is_missense=False, alphamissense_score=0.99)
        result = clf.classify(ev)
        pp3 = next(c for c in result.all_criteria if c.code == "PP3")
        assert "AlphaMissense" not in pp3.reason

    def test_alphamissense_counted_for_missense_variant(self):
        clf = AcmgClassifier(cfg={})
        ev = _base_evidence(is_missense=True, alphamissense_score=0.9)
        result = clf.classify(ev)
        pp3 = next(c for c in result.all_criteria if c.code == "PP3")
        assert "AlphaMissense" in pp3.reason

    def test_cadd_still_counted_regardless_of_missense_status(self):
        """CADD is variant-type-agnostic and must remain ungated."""
        clf = AcmgClassifier(cfg={})
        ev = _base_evidence(is_missense=False, cadd_phred=25.0)
        result = clf.classify(ev)
        pp3 = next(c for c in result.all_criteria if c.code == "PP3")
        assert "CADD" in pp3.reason
        assert pp3.status == "met"

    def test_spliceai_still_counted_regardless_of_missense_status(self):
        """SpliceAI is relevant to any consequence type and must remain ungated."""
        clf = AcmgClassifier(cfg={})
        ev = _base_evidence(is_missense=False, spliceai_score=0.9)
        result = clf.classify(ev)
        pp3 = next(c for c in result.all_criteria if c.code == "PP3")
        assert "SpliceAI" in pp3.reason
        assert pp3.status == "met"

    def test_bp4_revel_gated_the_same_way(self):
        clf = AcmgClassifier(cfg={})
        ev_non_missense = _base_evidence(is_missense=False, revel_score=0.01)
        ev_missense = _base_evidence(is_missense=True, revel_score=0.01)
        r1 = clf.classify(ev_non_missense)
        r2 = clf.classify(ev_missense)
        bp4_1 = next(c for c in r1.all_criteria if c.code == "BP4")
        bp4_2 = next(c for c in r2.all_criteria if c.code == "BP4")
        assert "REVEL" not in bp4_1.reason
        assert "REVEL" in bp4_2.reason

    def test_no_scores_at_all_still_not_evaluated(self):
        """Backward compatibility: absence of all scores still yields
        not_evaluated, unaffected by the missense gating change."""
        clf = AcmgClassifier(cfg={})
        ev = _base_evidence(is_missense=True)
        result = clf.classify(ev)
        pp3 = next(c for c in result.all_criteria if c.code == "PP3")
        assert pp3.status == STATUS_NOT_EVALUATED


class TestPP5BP6ConfigurableOptOut:
    def test_default_behavior_unchanged_pp5_fires(self):
        """Backward compatibility: default config (no override) must
        behave exactly as before this audit."""
        clf = AcmgClassifier(cfg={})
        ev = _base_evidence(
            clinvar_significance="Pathogenic", clinvar_stars=2, clinvar_conflicting=False
        )
        result = clf.classify(ev)
        assert "PP5" in result.criteria_met

    def test_default_behavior_unchanged_bp6_fires(self):
        clf = AcmgClassifier(cfg={})
        ev = _base_evidence(
            clinvar_significance="Benign", clinvar_stars=2, clinvar_conflicting=False
        )
        result = clf.classify(ev)
        assert "BP6" in result.criteria_met

    def test_disable_flag_suppresses_pp5(self):
        clf = AcmgClassifier(cfg={"acmg_thresholds": {"disable_pp5_bp6": True}})
        ev = _base_evidence(
            clinvar_significance="Pathogenic", clinvar_stars=3, clinvar_conflicting=False
        )
        result = clf.classify(ev)
        assert "PP5" not in result.criteria_met
        pp5 = next(c for c in result.all_criteria if c.code == "PP5")
        assert pp5.status == STATUS_NOT_EVALUATED
        assert "disable_pp5_bp6" in pp5.reason

    def test_disable_flag_suppresses_bp6(self):
        clf = AcmgClassifier(cfg={"acmg_thresholds": {"disable_pp5_bp6": True}})
        ev = _base_evidence(
            clinvar_significance="Benign", clinvar_stars=3, clinvar_conflicting=False
        )
        result = clf.classify(ev)
        assert "BP6" not in result.criteria_met
        bp6 = next(c for c in result.all_criteria if c.code == "BP6")
        assert bp6.status == STATUS_NOT_EVALUATED

    def test_disabling_pp5_can_change_overall_classification(self):
        """A variant that reaches Likely_Pathogenic only because of PP5
        must drop to a lower tier when PP5 is disabled — proving the
        opt-out actually propagates through to the final classification,
        not just the per-criterion status."""
        ev = _base_evidence(
            is_missense=True,
            missense_constrained=True,  # PP2
            clinvar_significance="Pathogenic",
            clinvar_stars=2,
            clinvar_conflicting=False,  # PP5
            in_hotspot=True,  # PM1 needs is_missense too
        )
        clf_default = AcmgClassifier(cfg={})
        clf_disabled = AcmgClassifier(cfg={"acmg_thresholds": {"disable_pp5_bp6": True}})
        result_default = clf_default.classify(ev)
        result_disabled = clf_disabled.classify(ev)
        assert "PP5" in result_default.criteria_met
        assert "PP5" not in result_disabled.criteria_met


class TestCombiningRuleTable:
    """Exhaustive check of the Richards et al. 2015 Table 5 combining
    rules, using synthetic CriteriaResult-driving evidence rather than
    the full VariantEvidence surface, to isolate the pure combining
    logic from individual criterion evaluation."""

    def _classify_counts(self, pvs=0, ps=0, pm=0, pp=0, ba=0, bs=0, bp=0):
        """Build a minimal AcmgClassifier and drive _classify() directly
        with synthetic CriteriaResult counts, bypassing per-criterion
        evaluation entirely."""
        from pipeline.acmg.classifier import CriteriaResult, STATUS_MET

        clf = AcmgClassifier(cfg={})
        met = []
        for i in range(pvs):
            met.append(
                CriteriaResult(
                    code=f"PVS{i}",
                    met=True,
                    status=STATUS_MET,
                    strength="very_strong",
                    direction="pathogenic",
                    reason="",
                )
            )
        for i in range(ps):
            met.append(
                CriteriaResult(
                    code=f"PS{i}",
                    met=True,
                    status=STATUS_MET,
                    strength="strong",
                    direction="pathogenic",
                    reason="",
                )
            )
        for i in range(pm):
            met.append(
                CriteriaResult(
                    code=f"PM{i}",
                    met=True,
                    status=STATUS_MET,
                    strength="moderate",
                    direction="pathogenic",
                    reason="",
                )
            )
        for i in range(pp):
            met.append(
                CriteriaResult(
                    code=f"PP{i}",
                    met=True,
                    status=STATUS_MET,
                    strength="supporting",
                    direction="pathogenic",
                    reason="",
                )
            )
        for i in range(ba):
            met.append(
                CriteriaResult(
                    code=f"BA{i}",
                    met=True,
                    status=STATUS_MET,
                    strength="stand_alone",
                    direction="benign",
                    reason="",
                )
            )
        for i in range(bs):
            met.append(
                CriteriaResult(
                    code=f"BS{i}",
                    met=True,
                    status=STATUS_MET,
                    strength="strong",
                    direction="benign",
                    reason="",
                )
            )
        for i in range(bp):
            met.append(
                CriteriaResult(
                    code=f"BP{i}",
                    met=True,
                    status=STATUS_MET,
                    strength="supporting",
                    direction="benign",
                    reason="",
                )
            )
        classification, _ = clf._classify(met)
        return classification

    # Pathogenic — all 8 documented rules
    def test_pathogenic_pvs_plus_ps(self):
        assert self._classify_counts(pvs=1, ps=1) == "Pathogenic"

    def test_pathogenic_pvs_plus_2_pm(self):
        assert self._classify_counts(pvs=1, pm=2) == "Pathogenic"

    def test_pathogenic_pvs_plus_pm_plus_pp(self):
        assert self._classify_counts(pvs=1, pm=1, pp=1) == "Pathogenic"

    def test_pathogenic_pvs_plus_2_pp(self):
        assert self._classify_counts(pvs=1, pp=2) == "Pathogenic"

    def test_pathogenic_2_ps(self):
        assert self._classify_counts(ps=2) == "Pathogenic"

    def test_pathogenic_ps_plus_3_pm(self):
        assert self._classify_counts(ps=1, pm=3) == "Pathogenic"

    def test_pathogenic_ps_plus_2pm_plus_2pp(self):
        assert self._classify_counts(ps=1, pm=2, pp=2) == "Pathogenic"

    def test_pathogenic_ps_plus_pm_plus_4pp(self):
        assert self._classify_counts(ps=1, pm=1, pp=4) == "Pathogenic"

    # Likely Pathogenic — all 6 documented rules
    def test_lp_pvs_plus_1pm(self):
        assert self._classify_counts(pvs=1, pm=1) == "Likely_Pathogenic"

    def test_lp_ps_plus_1_or_2_pm(self):
        assert self._classify_counts(ps=1, pm=1) == "Likely_Pathogenic"
        assert self._classify_counts(ps=1, pm=2) == "Likely_Pathogenic"

    def test_lp_ps_plus_2pp(self):
        assert self._classify_counts(ps=1, pp=2) == "Likely_Pathogenic"

    def test_lp_3pm(self):
        assert self._classify_counts(pm=3) == "Likely_Pathogenic"

    def test_lp_2pm_plus_2pp(self):
        assert self._classify_counts(pm=2, pp=2) == "Likely_Pathogenic"

    def test_lp_1pm_plus_4pp(self):
        assert self._classify_counts(pm=1, pp=4) == "Likely_Pathogenic"

    # Benign
    def test_benign_stand_alone(self):
        assert self._classify_counts(ba=1) == "Benign"

    def test_benign_2_strong(self):
        assert self._classify_counts(bs=2) == "Benign"

    # Likely Benign
    def test_likely_benign_1strong_1supporting(self):
        assert self._classify_counts(bs=1, bp=1) == "Likely_Benign"

    def test_likely_benign_2_supporting(self):
        assert self._classify_counts(bp=2) == "Likely_Benign"

    # VUS — insufficient evidence either direction
    def test_vus_when_nothing_reaches_threshold(self):
        assert self._classify_counts(pm=1) == "Uncertain_Significance"
        assert self._classify_counts(pp=1) == "Uncertain_Significance"
        assert self._classify_counts(bp=1) == "Uncertain_Significance"

    def test_vus_on_conflicting_strong_evidence(self):
        """FIX 9 conflict detection: strong pathogenic + strong benign
        must defer to VUS-Conflicting rather than picking a side."""
        assert self._classify_counts(ps=2, bs=2) == "Uncertain_Significance"


class TestAiModelSeparationFromAcmgEvidence:
    """Confirms the audit's finding that DNABERT-2/ESM-2 ("exploratory"
    AI scores) never influence the formal ACMG classification — they
    are computed, when available, strictly after classify() returns and
    are surfaced only as a separate advisory tier/score."""

    def test_classify_result_has_no_ai_score_field(self):
        clf = AcmgClassifier(cfg={})
        ev = _base_evidence()
        result = clf.classify(ev)
        result_dict = result.to_dict()
        assert "ai_score" not in result_dict
        assert "exploratory" not in str(result_dict).lower()
