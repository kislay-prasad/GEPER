"""
HIGH-1: the confidence and prioritization engines must score against
`ConflictResolutionEngine`'s real-conflict list, not the raw
per-criterion `conflicting_evidence` list.

The defect these tests pin, found on a five-variant run of
`test_data/nuclear_test.vcf`: all eight `conflicting_evidence` items
across the five findings were category "ACMG Criterion" -- exactly the
category `_real_conflicts` excludes from the Attention flag on the
grounds (D4, report review round 3) that they are "not a disagreement
between two independent sources". Yet those eight items supplied 100%
of every confidence and priority penalty in the run, while the single
genuine Clinical disagreement the engine did detect (PRNP P102L: GEPER
Uncertain Significance vs. ClinVar Pathogenic) contributed nothing to
either. The flag and the two penalties read disjoint inputs, so they
could never agree by construction.

The cost, measured on that run: TP53 R248W (`TP53_PS3_pos`) is
Pathogenic at the highest net points of the five (11.0) and its three
self-flagged caveats cut confidence 79.4 -> 43.66 ("High" -> "Moderate")
and review priority 68.0 -> 47.58 ("High" -> "Moderate").

`TestBothCallSitesAreFixed` is deliberately written so it cannot pass
with only one engine repointed -- fixing the confidence engine alone
leaves the review-priority demotion in place, which is the outcome a
clinician actually sorts on.
"""

import io
import json
import os
import unittest

from pipeline.confidence_engine import ConfidenceEngine
from pipeline.conflict_resolution_engine import ConflictItem, ConflictResolutionEngine
from pipeline.interpretation import InterpretationEngine
from pipeline.prioritization_engine import PrioritizationEngine

_FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_data")


def _tp53_r248w_stage_results():
    """
    Real stage output for TP53 R248W, captured from the five-variant
    nuclear_test run and reduced (by automated shrinking, verified to
    still produce all three caveats) to the smallest input that
    reproduces them. Real data rather than a synthetic dict on purpose:
    the claim under test is about which items the ACMG rule engine
    actually files under `conflicting_evidence`, and a hand-written
    fixture would only test my guess about that.
    """
    with io.open(os.path.join(_FIXTURE, "high1_tp53_r248w_caveats.json"), encoding="utf-8") as fh:
        payload = json.load(fh)
    variant = payload.pop("variant_dict")
    return variant, payload


def _run_interpret_capturing_engine_inputs():
    """
    Drives the real Phase 2/3/4 wiring and records what each engine was
    handed. Spies on the two `score()` methods rather than reading the
    output scores, so a failure names the actual defect ("the engine
    was given the raw caveat list") instead of a downstream number.
    """
    engine = InterpretationEngine()
    seen = {}

    real_confidence = engine._confidence_engine.score
    real_priority = engine._prioritization_engine.score

    def confidence_spy(**kwargs):
        seen["confidence"] = kwargs
        return real_confidence(**kwargs)

    def priority_spy(**kwargs):
        seen["priority"] = kwargs
        return real_priority(**kwargs)

    engine._confidence_engine.score = confidence_spy
    engine._prioritization_engine.score = priority_spy

    variant, stages = _tp53_r248w_stage_results()
    engine.interpret(variant_dict=variant, dna_models_used=[], **stages)
    return seen


def _item(conflict_type="Test conflict", category="Clinical", severity="Moderate"):
    return ConflictItem(
        conflict_type=conflict_type,
        category=category,
        evidence_a={"source": "a", "statement": "a"},
        evidence_b={"source": "b", "statement": "b"},
        severity=severity,
        resolution="n/a",
        resolution_rationale="n/a",
        confidence_impact="n/a",
        priority_impact="n/a",
    )


class TestTheCaveatsAreRealAndAreNotConflicts(unittest.TestCase):
    """
    Pins the premise, so a later change that stops producing these
    caveats fails here rather than making the tests below vacuously
    green. This passes before AND after the fix, by design.
    """

    def test_the_fixture_still_produces_three_acmg_caveats(self):
        variant, stages = _tp53_r248w_stage_results()
        result = InterpretationEngine().interpret(variant_dict=variant, dna_models_used=[], **stages)
        caveats = (result.get("interpretation_result") or {}).get("conflicting_evidence")
        self.assertEqual(
            len(caveats or []),
            3,
            "TP53 R248W no longer files three per-criterion caveats, so the rest of this file is "
            "testing nothing. Re-derive the fixture before trusting a green run here.",
        )

    def test_none_of_those_caveats_is_a_real_conflict(self):
        engine = ConflictResolutionEngine()
        variant, stages = _tp53_r248w_stage_results()
        interp = InterpretationEngine().interpret(variant_dict=variant, dna_models_used=[], **stages)
        result_obj = interp.get("interpretation_result") or {}
        real = engine.real_conflicts_for_scoring(
            acmg_classification=result_obj.get("acmg_classification"),
            acmg_conflicting_evidence=result_obj.get("conflicting_evidence") or [],
            ai_consensus=result_obj.get("ai_consensus") or [],
            clinvar_result=stages.get("clinvar_result"),
            clingen_result=stages.get("clingen_result"),
            interpro_result=stages.get("interpro_result"),
            blast_result=stages.get("blast_result"),
        )
        self.assertEqual(
            [c.category for c in real],
            [],
            "A caveat this variant carries is now being counted as a real conflict; that changes "
            "what the Attention flag fires on, not just the penalty.",
        )


class TestBothCallSitesAreFixed(unittest.TestCase):
    """
    The load-bearing test. It asserts on BOTH engines from ONE run of
    the real wiring, so repointing only `interpretation.py`'s confidence
    call leaves this red.
    """

    def setUp(self):
        self.seen = _run_interpret_capturing_engine_inputs()

    def test_confidence_engine_is_given_real_conflicts_not_the_caveat_list(self):
        kwargs = self.seen["confidence"]
        self.assertNotIn(
            "conflicting_evidence",
            kwargs,
            "Phase 3 still passes the raw per-criterion caveat list to the confidence engine.",
        )
        self.assertEqual(list(kwargs.get("real_conflicts") or []), [])

    def test_prioritization_engine_is_given_real_conflicts_not_the_caveat_list(self):
        kwargs = self.seen["priority"]
        self.assertNotIn(
            "conflicting_evidence",
            kwargs,
            "Phase 4 still passes the raw per-criterion caveat list to the prioritization engine. "
            "This is the call site that demoted TP53 R248W's review priority from High to Moderate.",
        )
        self.assertEqual(list(kwargs.get("real_conflicts") or []), [])

    def test_neither_engine_penalises_this_variant_any_more(self):
        for name, engine_cls in (("confidence", ConfidenceEngine), ("priority", PrioritizationEngine)):
            with self.subTest(engine=name):
                penalty, explanation = engine_cls._conflict_penalty(
                    self.seen[name].get("real_conflicts") or [],
                    _cfg_for(name),
                )
                self.assertEqual(penalty, 0.0, f"{name} engine still penalises TP53 R248W")
                self.assertEqual(explanation, "No conflicting evidence detected.")


class TestAiDirectionDisagreementIsCountedExactlyOnce(unittest.TestCase):
    """
    The double-count that no run in hand exercises, which is precisely
    why it is pinned here rather than left to be noticed.

    An AlphaMissense-vs-MMSplice direction disagreement used to add one
    conflict unit through `_conflict_penalty`'s own `ai_consensus`
    branch. It ALSO produces an `AI`/`Moderate` ConflictItem via
    `_ai_conflict`, which is a real conflict. Once the penalty is
    computed from `real_conflicts`, keeping the old branch would charge
    the same discordance twice -- 0.30 where 0.15 is meant for
    confidence, 0.20 where 0.10 is meant for priority.
    """

    DISCORDANT = [
        {"source": "AlphaMissense", "prediction": "likely_pathogenic"},
        {"source": "MMSplice", "prediction": "likely_benign"},
    ]

    def test_the_discordance_produces_exactly_one_real_conflict(self):
        real = ConflictResolutionEngine().real_conflicts_for_scoring(
            acmg_classification="Uncertain Significance",
            acmg_conflicting_evidence=[],
            ai_consensus=self.DISCORDANT,
        )
        ai_items = [c for c in real if c.category == "AI"]
        self.assertEqual(len(ai_items), 1, "expected exactly one AI-category conflict item")
        self.assertEqual(len(real), len(ai_items), f"unexpected extra real conflicts: {real}")

    def test_confidence_charges_one_unit_not_two(self):
        real = ConflictResolutionEngine().real_conflicts_for_scoring(
            acmg_classification="Uncertain Significance",
            acmg_conflicting_evidence=[],
            ai_consensus=self.DISCORDANT,
        )
        penalty, _ = ConfidenceEngine._conflict_penalty(real, _cfg_for("confidence"))
        self.assertAlmostEqual(
            penalty,
            0.15,
            places=6,
            msg="an AI direction disagreement is being charged twice -- once as a ConflictItem and "
            "once through the engine's own ai_consensus branch",
        )

    def test_priority_charges_one_unit_not_two(self):
        real = ConflictResolutionEngine().real_conflicts_for_scoring(
            acmg_classification="Uncertain Significance",
            acmg_conflicting_evidence=[],
            ai_consensus=self.DISCORDANT,
        )
        penalty, _ = PrioritizationEngine._conflict_penalty(real, _cfg_for("priority"))
        self.assertAlmostEqual(penalty, 0.10, places=6)

    def test_the_ai_consensus_branch_is_gone_from_both_engines(self):
        """
        Direct check that neither engine can still charge for
        `ai_consensus` on its own: hand each one an empty real-conflict
        list alongside a discordant consensus and require no penalty.
        A signature-level guard, since the branch's removal is what the
        two tests above depend on.
        """
        for engine_cls, name in ((ConfidenceEngine, "confidence"), (PrioritizationEngine, "priority")):
            with self.subTest(engine=name):
                penalty, _ = engine_cls._conflict_penalty([], _cfg_for(name))
                self.assertEqual(penalty, 0.0)


class TestARealConflictStillCounts(unittest.TestCase):
    """
    The fix must not simply switch the penalty off. A genuine
    disagreement -- the thing the flag already fires on -- has to reach
    both scores, which is the half of option 3 that was never true
    before: PRNP P102L's Clinical conflict contributed 0.00 to either
    engine while its two caveats contributed everything.
    """

    def test_one_clinical_conflict_charges_one_unit_in_both_engines(self):
        real = [_item(conflict_type="GEPER classification disagrees with a curated ClinVar classification")]
        self.assertAlmostEqual(ConfidenceEngine._conflict_penalty(real, _cfg_for("confidence"))[0], 0.15, places=6)
        self.assertAlmostEqual(PrioritizationEngine._conflict_penalty(real, _cfg_for("priority"))[0], 0.10, places=6)

    def test_the_explanation_names_the_conflict_rather_than_counting_caveats(self):
        real = [_item(conflict_type="AlphaMissense vs MMSplice direction disagreement")]
        _, explanation = ConfidenceEngine._conflict_penalty(real, _cfg_for("confidence"))
        self.assertIn("AlphaMissense vs MMSplice direction disagreement", explanation)
        self.assertIn("Moderate", explanation)
        self.assertNotIn(
            "across ACMG criteria",
            explanation,
            "the explanation still describes the penalty as counting ACMG-criterion items",
        )

    def test_flat_count_not_severity_weighted(self):
        """
        Records the decision rather than assuming it: a Critical item
        and a Minor item cost the same. If weighting is introduced
        later this test is the thing that should be changed, and seen
        to be changed.
        """
        critical = [_item(severity="Critical")]
        minor = [_item(severity="Minor")]
        self.assertEqual(
            ConfidenceEngine._conflict_penalty(critical, _cfg_for("confidence"))[0],
            ConfidenceEngine._conflict_penalty(minor, _cfg_for("confidence"))[0],
        )

    def test_the_penalty_is_still_capped(self):
        many = [_item() for _ in range(20)]
        cfg = _cfg_for("confidence")
        self.assertEqual(ConfidenceEngine._conflict_penalty(many, cfg)[0], cfg.MAX_CONFLICT_PENALTY)


def _cfg_for(which):
    from config import CONFIG

    return CONFIG.confidence if which == "confidence" else CONFIG.prioritization


if __name__ == "__main__":
    unittest.main()
