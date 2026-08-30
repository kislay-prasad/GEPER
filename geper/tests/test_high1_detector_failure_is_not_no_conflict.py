"""
HIGH-1 follow-up: a crashed conflict detector must not read as "no
conflicts found".

The first form of the hoist's failure handler in
`pipeline/interpretation.py` was `real_conflicts = []`. That is the
absence-versus-failure defect, and it failed in the flattering
direction: an empty list is what a variant with no conflicts gets, and
it REMOVES a penalty -- so a broken detector made a variant look more
confident and higher-priority than a working one would have. Worse, it
was silent: `logger.exception` is not part of the output bundle, so a
reviewer reading the report saw a clean score with nothing to indicate
the detector never ran.

Both halves are pinned here:

  * the SCORES: detection failure leaves `confidence_pending` /
    `priority_pending` True, so the report renders "Pending" rather
    than a number nobody can stand behind;
  * the REPORT: the failure reaches the document through the same
    `service_health` snapshot Finding 5 built, and the caveat says a
    component failed -- not that a data source "failed and was
    retried", which would be false.

The contrast test is the load-bearing one: the same fixture, once with
a working detector and once with a raising one, must not produce the
same output.
"""

import io
import json
import os
import unittest
from unittest import mock

from pipeline.conflict_resolution_engine import ConflictResolutionEngine
from pipeline.interpretation import InterpretationEngine
from report.clinical_report_builder import _offline_sources_caveat_text
from utils.service_health import HEALTH

_FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_data")


def _fixture():
    with io.open(os.path.join(_FIXTURE, "high1_tp53_r248w_caveats.json"), encoding="utf-8") as fh:
        payload = json.load(fh)
    return payload.pop("variant_dict"), payload


def _interpret(raise_in_detection=False):
    variant, stages = _fixture()
    if not raise_in_detection:
        return InterpretationEngine().interpret(variant_dict=variant, dna_models_used=[], **stages)
    with mock.patch.object(
        ConflictResolutionEngine,
        "real_conflicts_for_scoring",
        side_effect=RuntimeError("detector exploded"),
    ):
        return InterpretationEngine().interpret(variant_dict=variant, dna_models_used=[], **stages)


class TestDetectorFailureWithholdsTheScores(unittest.TestCase):
    def setUp(self):
        HEALTH.reset()
        self.addCleanup(HEALTH.reset)

    def test_a_working_detector_produces_scores(self):
        """Pins the premise, so the failure test below cannot pass by accident."""
        ir = _interpret().get("interpretation_result") or {}
        self.assertFalse(ir.get("confidence_pending"))
        self.assertFalse(ir.get("priority_pending"))
        self.assertIsNotNone(ir.get("confidence_score"))
        self.assertIsNotNone(ir.get("priority_score"))

    def test_a_raising_detector_leaves_both_scores_pending(self):
        ir = _interpret(raise_in_detection=True).get("interpretation_result") or {}
        self.assertTrue(
            ir.get("confidence_pending"),
            "confidence was scored despite conflict detection failing -- i.e. scored as conflict-free",
        )
        self.assertTrue(
            ir.get("priority_pending"),
            "review priority was scored despite conflict detection failing. This is the direction "
            "that promotes a variant up a reviewer's worklist on the strength of a crash.",
        )
        self.assertIsNone(ir.get("confidence_score"))
        self.assertIsNone(ir.get("priority_score"))

    def test_failure_does_not_look_like_a_clean_run(self):
        """
        The load-bearing contrast: same fixture, working detector vs.
        raising detector. If these two agree on the scores, the defect
        is back regardless of what any other test says.
        """
        working = _interpret().get("interpretation_result") or {}
        HEALTH.reset()
        broken = _interpret(raise_in_detection=True).get("interpretation_result") or {}
        self.assertNotEqual(
            (working.get("confidence_score"), working.get("confidence_label")),
            (broken.get("confidence_score"), broken.get("confidence_label")),
        )
        self.assertNotEqual(
            (working.get("priority_score"), working.get("priority_category")),
            (broken.get("priority_score"), broken.get("priority_category")),
        )

    def test_the_variant_is_still_classified_identically(self):
        """
        Withholding the scores must not cost the variant its ACMG
        result, or change it. The failure posture everywhere else in
        this method is per-phase degradation, not losing the finding.

        Asserted against the working run rather than a hard-coded
        label: this fixture is reduced to the minimum that reproduces
        the three caveats, not to the inputs that drive the real
        variant's classification, so its own label is an artefact of
        the reduction and pinning it would be pinning the wrong thing.
        """
        working = _interpret().get("interpretation_result") or {}
        HEALTH.reset()
        broken = _interpret(raise_in_detection=True).get("interpretation_result") or {}
        self.assertEqual(broken.get("acmg_classification"), working.get("acmg_classification"))
        self.assertEqual(broken.get("triggered_rules"), working.get("triggered_rules"))
        self.assertTrue(broken.get("triggered_rules"))


class TestTheFailureReachesTheReport(unittest.TestCase):
    def setUp(self):
        HEALTH.reset()
        self.addCleanup(HEALTH.reset)

    def test_the_failure_is_recorded_as_a_component_not_a_data_source(self):
        _interpret(raise_in_detection=True)
        snapshot = HEALTH.snapshot()
        failed = [s for s in snapshot if s.get("failure_count")]
        self.assertEqual(len(failed), 1, f"expected exactly one recorded failure, got {snapshot}")
        self.assertTrue(
            failed[0].get("is_local_component"),
            "the detector failure was recorded as an external data source, which makes the caveat claim it was retried",
        )
        self.assertTrue(failed[0].get("degraded"))

    def test_the_caveat_says_a_component_failed_and_does_not_claim_a_retry(self):
        _interpret(raise_in_detection=True)
        caveat = _offline_sources_caveat_text({"service_health": HEALTH.snapshot()})
        self.assertIsNotNone(caveat, "a detector failure produced no caveat -- it is invisible in the report")
        self.assertIn("internal analysis component(s) failed", caveat)
        self.assertIn("Pending", caveat)
        self.assertNotIn(
            "were retried",
            caveat,
            "the component failure is being described with the data-source sentence, which says it "
            "was retried. Nothing retried it.",
        )

    def test_a_clean_run_produces_no_caveat(self):
        _interpret()
        self.assertIsNone(_offline_sources_caveat_text({"service_health": HEALTH.snapshot()}))

    def test_a_data_source_failure_still_gets_the_data_source_sentence(self):
        """
        The new branch must not swallow the case it was carved out of.
        """
        HEALTH.note_failure("Ensembl")
        caveat = _offline_sources_caveat_text({"service_health": HEALTH.snapshot()})
        self.assertIn("data source(s) failed at least once during this run and were retried", caveat)
        self.assertIn("Ensembl", caveat)
        self.assertNotIn("internal analysis component(s)", caveat)

    def test_both_kinds_of_failure_are_reported_separately(self):
        HEALTH.note_failure("Ensembl")
        _interpret(raise_in_detection=True)
        caveat = _offline_sources_caveat_text({"service_health": HEALTH.snapshot()})
        self.assertIn("Ensembl", caveat)
        self.assertIn("internal analysis component(s) failed", caveat)


if __name__ == "__main__":
    unittest.main()
