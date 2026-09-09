"""
tests/test_interpretation_outcome.py
─────────────────────────────────────

Human ruling (2026-09-09/10): the INTERPRETATION-OUTCOME state, a
per-variant judgement carried ALONGSIDE the ACMG classification, never
derived from the per-source retrieval states already in the engine
(`pipeline/stage_schemas.py::StageStatus`). Four values: interpreted,
insufficient_evidence, conflicting_evidence, review_required.

ROUND 2 (2026-09-09/10, this file's second red/green cycle): the
review_required trigger set proposed in round 1 was RULED APPROVED AS
PROPOSED -- exactly two triggers, nothing added, nothing re-opened:
  (1) the evidence pattern that would otherwise be conflicting_evidence
      (comparable-weight pathogenic/benign points).
  (2) acmg_classification in {"Pathogenic", "Likely Pathogenic"}.

THIS IS THE PART OF THE RED THAT CHANGED AN EXISTING TEST, NOT ONLY
ADDED NEW ONES: `test_review_required_is_never_returned_by_this_function`
below used to assert review_required was unreachable -- that stopped
being true the moment the trigger set was approved, so leaving it
unchanged would make this file assert a now-false claim. It is REPLACED
(see `TestReviewRequiredTriggers` below) with tests that pin WHICH
inputs produce review_required and which do NOT, rather than deleted --
deleting it would have quietly dropped the negative-space coverage
(inputs that must still NOT produce it) along with the outdated
positive claim.

THE COLLISION DECISION (mine, stated explicitly, not settled by the
ruling): a variant CAN satisfy both triggers at once -- e.g.
pathogenic_points=12, benign_points=6 is comparable-weight (ratio 0.5)
AND nets to 6, which is Likely Pathogenic. Only one string can be
`interpretation_outcome`, so on EITHER trigger firing, the returned
value is `review_required` (an escalation takes priority over naming
which evidence pattern produced it -- see
`pipeline/interpretation_outcome.py`'s own docstring for the full
argument). WHAT IS LOST: a reader who sees only `review_required` does
not know, from that field alone, whether it fired because of a genuine
evidence disagreement, a P/LP call, or both. THE ANSWER TO "if both
need to be visible, say so": `determine_review_required_reasons()`
(new function, same module) computes the SAME two conditions
independently and returns the list of which one(s) actually fired,
ALWAYS present (an empty list when review_required did not fire,
mirroring `pipeline/provenance.py::get_stale_fallbacks`'s own
"checked, nothing" vs "never checked" discipline) -- see
`TestDetermineReviewRequiredReasons` below.

AN OBSERVATION WORTH STATING PLAINLY (also mine): after this change,
`InterpretationOutcome.CONFLICTING_EVIDENCE` becomes UNREACHABLE via
the public `determine_interpretation_outcome()` -- every input that
would have produced it now produces `review_required` instead, since
trigger (1) IS that exact condition. The enum member still exists (for
`determine_review_required_reasons()`'s own internal use and for any
future caller), but no call to `determine_interpretation_outcome()` can
return it anymore. `test_conflicting_evidence_is_now_unreachable_via_the_public_function`
below tests this directly rather than leaving it as an implicit,
undocumented side effect.

Declared BEFORE running, against master da35c979 (this branch's own
prior interpretation-outcome-state work, already merged, before THIS
round's production changes): EXPECTED RESULT was FAILURE --
`determine_interpretation_outcome()` does not yet accept a
`classification` keyword argument, and `determine_review_required_reasons`
does not exist yet, so every test that passes `classification=` or
imports `determine_review_required_reasons` must fail with a
`TypeError` or `ImportError` respectively. Confirmed failing (see the
commit history: the test-only commit on this branch precedes the
production-code commit) before any production code changed.

THE TEST THIS FILE EXISTS TO WRITE, ROUND 1 (dispatch's own words,
still true and still here, unchanged by round 2): "write the test that
would catch the defect this task exists to prevent: a query that FAILS
must not produce insufficient_evidence. Prove it by making a source
fail on purpose." That is
`TestAcmgRuleEngineNeverRelabelsAFailureAsInsufficiency::
test_a_source_failing_on_purpose_does_not_produce_insufficient_evidence`
below -- a real `ACMGRuleEngine().evaluate()` call, one evidence source
deliberately given a genuine `error` key (not a `found: False`, not a
skip), every other source and the gene resolution status otherwise
clean-empty, and the assertion is that `interpretation_outcome` is
`"interpreted"`, never `"insufficient_evidence"`.
"""

from __future__ import annotations

import unittest
from unittest import mock

from pipeline.acmg_rules import ACMGRuleEngine, CombineResult
from pipeline.interpretation_outcome import (
    InterpretationOutcome,
    determine_interpretation_outcome,
    determine_review_required_reasons,
)

_VARIANT = {"chrom": "1", "pos": 12345, "ref": "A", "alt": "G"}


def _evaluate(**overrides):
    """Minimal, all-clean-empty `ACMGRuleEngine.evaluate()` call --
    mirrors `test_mtdna_compartment_gate.py`'s own `_evaluate` helper
    shape, but for an ordinary nuclear variant (no mtDNA gating)."""
    kwargs = dict(
        clinvar_result={"records": []},
        dbsnp_result={"found": False},
        protein_result={},
        alphamissense_result={"skipped": True},
        mmsplice_result={"predicted": False},
        # `None`, not `{"found": False}`: a confirmed absence from
        # gnomAD is itself PM2 evidence (a real, nonzero pathogenic
        # point), so it is not a "no evidence at all" baseline -- see
        # `pipeline.acmg_rules.ACMGRuleEngine._pm2`'s own `not
        # gnomad_result` check. `None` means "never queried", which is
        # what an actually-empty baseline requires.
        gnomad_result=None,
        conservation_result={},
        clingen_result={"gene_resolution_status": "resolved", "gene_symbol": "BRCA1"},
        interpro_result={},
        ensemble_result={},
        variant_dict=_VARIANT,
        transcript_result={},
        clinvar_codon_result={},
        uniprot_result={},
        spliceformer_result={},
        splicebert_result={},
        hpo_result={},
        phenotype_result=None,
        functional_evidence_result={},
    )
    kwargs.update(overrides)
    return ACMGRuleEngine().evaluate(**kwargs)


class TestDetermineInterpretationOutcomePureFunction(unittest.TestCase):
    """The core of the task, tested in isolation from the engine: the
    definition must hold as a pure function, not only as an enum.
    `classification="Uncertain Significance"` (never Pathogenic/Likely
    Pathogenic) is the neutral default threaded through every test here
    that isn't specifically about the classification trigger, so these
    tests keep testing exactly what they tested before round 2."""

    _NEUTRAL_CLASSIFICATION = "Uncertain Significance"

    def test_no_evidence_at_all_with_a_clean_resolution_is_insufficient(self):
        outcome = determine_interpretation_outcome(
            gene_resolved=True,
            evidence_queries_completed=True,
            pathogenic_points=0.0,
            benign_points=0.0,
            classification=self._NEUTRAL_CLASSIFICATION,
        )
        self.assertIs(outcome, InterpretationOutcome.INSUFFICIENT_EVIDENCE)

    def test_no_evidence_with_an_unresolved_gene_is_not_insufficient(self):
        """Gene never resolved -- the honest answer is NOT
        insufficient_evidence even though no criterion fired."""
        outcome = determine_interpretation_outcome(
            gene_resolved=False,
            evidence_queries_completed=True,
            pathogenic_points=0.0,
            benign_points=0.0,
            classification=self._NEUTRAL_CLASSIFICATION,
        )
        self.assertIsNot(outcome, InterpretationOutcome.INSUFFICIENT_EVIDENCE)
        self.assertIs(outcome, InterpretationOutcome.INTERPRETED)

    def test_no_evidence_with_a_query_failure_is_not_insufficient(self):
        """THE defect this task exists to prevent, at the pure-function
        level: a query that ran and failed must not be relabelled as a
        query that ran and found nothing."""
        outcome = determine_interpretation_outcome(
            gene_resolved=True,
            evidence_queries_completed=False,
            pathogenic_points=0.0,
            benign_points=0.0,
            classification=self._NEUTRAL_CLASSIFICATION,
        )
        self.assertIsNot(outcome, InterpretationOutcome.INSUFFICIENT_EVIDENCE)
        self.assertIs(outcome, InterpretationOutcome.INTERPRETED)

    def test_lopsided_points_do_not_conflict(self):
        """One side overwhelms the other -- not a genuine disagreement,
        just the ordinary case of most evidence pointing one way."""
        outcome = determine_interpretation_outcome(
            gene_resolved=True,
            evidence_queries_completed=True,
            pathogenic_points=8.0,
            benign_points=1.0,
            classification=self._NEUTRAL_CLASSIFICATION,
        )
        self.assertIs(outcome, InterpretationOutcome.INTERPRETED)

    def test_one_sided_points_with_none_on_the_other_is_interpreted_not_conflicting(self):
        outcome = determine_interpretation_outcome(
            gene_resolved=True,
            evidence_queries_completed=True,
            pathogenic_points=6.0,
            benign_points=0.0,
            classification=self._NEUTRAL_CLASSIFICATION,
        )
        self.assertIs(outcome, InterpretationOutcome.INTERPRETED)


class TestReviewRequiredTriggers(unittest.TestCase):
    """
    RULED (2026-09-09/10): review_required's trigger set is exactly the
    two proposed in round 1, approved unchanged. This class REPLACES
    `test_review_required_is_never_returned_by_this_function` -- see
    this file's own module docstring for why replacement, not deletion,
    is the right shape for a claim that stopped being true.
    """

    def test_comparable_pathogenic_and_benign_points_now_escalates_to_review_required(self):
        """What used to be `CONFLICTING_EVIDENCE` (round 1) is now
        `REVIEW_REQUIRED` (round 2, trigger 1) -- the collision decision
        made explicit: an escalation takes priority over naming which
        evidence pattern produced it."""
        outcome = determine_interpretation_outcome(
            gene_resolved=True,
            evidence_queries_completed=True,
            pathogenic_points=4.0,
            benign_points=4.0,
            classification="Uncertain Significance",
        )
        self.assertIs(outcome, InterpretationOutcome.REVIEW_REQUIRED)

    def test_conflict_trigger_fires_even_when_the_gene_did_not_resolve(self):
        """A real interpretive disagreement escalates on its own terms
        -- it is not gated behind gene resolution the way
        insufficient_evidence is."""
        outcome = determine_interpretation_outcome(
            gene_resolved=False,
            evidence_queries_completed=True,
            pathogenic_points=4.0,
            benign_points=4.0,
            classification="Uncertain Significance",
        )
        self.assertIs(outcome, InterpretationOutcome.REVIEW_REQUIRED)

    def test_pathogenic_classification_alone_triggers_review_required(self):
        """No evidence conflict at all (one-sided points) -- the
        classification trigger fires independently."""
        outcome = determine_interpretation_outcome(
            gene_resolved=True,
            evidence_queries_completed=True,
            pathogenic_points=10.0,
            benign_points=0.0,
            classification="Pathogenic",
        )
        self.assertIs(outcome, InterpretationOutcome.REVIEW_REQUIRED)

    def test_likely_pathogenic_classification_alone_triggers_review_required(self):
        outcome = determine_interpretation_outcome(
            gene_resolved=True,
            evidence_queries_completed=True,
            pathogenic_points=6.0,
            benign_points=0.0,
            classification="Likely Pathogenic",
        )
        self.assertIs(outcome, InterpretationOutcome.REVIEW_REQUIRED)

    def test_pathogenic_and_likely_benign_and_benign_do_not_trigger_review_required(self):
        for classification in ("Benign", "Likely Benign"):
            with self.subTest(classification=classification):
                outcome = determine_interpretation_outcome(
                    gene_resolved=True,
                    evidence_queries_completed=True,
                    pathogenic_points=0.0,
                    benign_points=10.0,
                    classification=classification,
                )
                self.assertIsNot(outcome, InterpretationOutcome.REVIEW_REQUIRED)

    def test_the_actual_collision_both_triggers_fire_on_the_same_variant(self):
        """THE case the ruling asked about explicitly: pathogenic=12,
        benign=6 is comparable-weight (ratio 0.5, trigger 1) AND nets to
        6, which is Likely Pathogenic (trigger 2). Both fire; only one
        string is reported; it is review_required either way -- the
        collision itself does not change the answer, only
        `determine_review_required_reasons` (below) records that BOTH
        fired."""
        outcome = determine_interpretation_outcome(
            gene_resolved=True,
            evidence_queries_completed=True,
            pathogenic_points=12.0,
            benign_points=6.0,
            classification="Likely Pathogenic",
        )
        self.assertIs(outcome, InterpretationOutcome.REVIEW_REQUIRED)

    def test_conflicting_evidence_is_now_unreachable_via_the_public_function(self):
        """Explicit, stated observation (mine): no input combination
        reaches CONFLICTING_EVIDENCE via determine_interpretation_outcome()
        anymore -- trigger 1 IS that exact condition, so it always
        escalates to REVIEW_REQUIRED instead. Swept across every
        combination that used to produce CONFLICTING_EVIDENCE in round
        1, plus points that clearly would if the escalation didn't
        exist."""
        for pathogenic_points, benign_points in ((4.0, 4.0), (8.0, 8.0), (2.0, 1.5), (12.0, 6.0)):
            with self.subTest(pathogenic_points=pathogenic_points, benign_points=benign_points):
                outcome = determine_interpretation_outcome(
                    gene_resolved=True,
                    evidence_queries_completed=True,
                    pathogenic_points=pathogenic_points,
                    benign_points=benign_points,
                    classification="Uncertain Significance",
                )
                self.assertIsNot(outcome, InterpretationOutcome.CONFLICTING_EVIDENCE)

    def test_review_required_does_not_fire_outside_the_two_ruled_triggers(self):
        """Negative-space coverage carried over from round 1's blanket
        test, narrowed to exclude the two now-legitimate triggers: every
        OTHER combination of these inputs must still never produce
        review_required."""
        for gene_resolved in (True, False):
            for evidence_queries_completed in (True, False):
                for pathogenic_points in (0.0, 1.0, 8.0, None):
                    for benign_points in (0.0, 1.0, None):
                        for classification in (None, "Uncertain Significance", "Benign", "Likely Benign"):
                            # Exclude any input that would itself be a
                            # comparable-weight conflict (trigger 1) --
                            # that combination is covered by its own
                            # tests above, not this negative sweep.
                            p = pathogenic_points or 0.0
                            b = benign_points or 0.0
                            if p > 0 and b > 0 and min(p, b) >= max(p, b) * 0.5:
                                continue
                            outcome = determine_interpretation_outcome(
                                gene_resolved=gene_resolved,
                                evidence_queries_completed=evidence_queries_completed,
                                pathogenic_points=pathogenic_points,
                                benign_points=benign_points,
                                classification=classification,
                            )
                            self.assertIsNot(outcome, InterpretationOutcome.REVIEW_REQUIRED)


class TestDetermineReviewRequiredReasons(unittest.TestCase):
    """`determine_review_required_reasons()`: the answer to "if both
    need to be visible, say so" -- always a list, never omitted, naming
    every trigger that actually fired."""

    def test_empty_when_neither_trigger_fires(self):
        reasons = determine_review_required_reasons(
            pathogenic_points=8.0, benign_points=1.0, classification="Uncertain Significance"
        )
        self.assertEqual(reasons, [])

    def test_conflicting_evidence_reason_alone(self):
        reasons = determine_review_required_reasons(
            pathogenic_points=4.0, benign_points=4.0, classification="Uncertain Significance"
        )
        self.assertEqual(reasons, ["conflicting_evidence"])

    def test_classification_reason_alone_for_pathogenic(self):
        reasons = determine_review_required_reasons(
            pathogenic_points=10.0, benign_points=0.0, classification="Pathogenic"
        )
        self.assertEqual(reasons, ["classification_pathogenic_or_likely_pathogenic"])

    def test_classification_reason_alone_for_likely_pathogenic(self):
        reasons = determine_review_required_reasons(
            pathogenic_points=6.0, benign_points=0.0, classification="Likely Pathogenic"
        )
        self.assertEqual(reasons, ["classification_pathogenic_or_likely_pathogenic"])

    def test_both_reasons_present_on_the_actual_collision_case(self):
        reasons = determine_review_required_reasons(
            pathogenic_points=12.0, benign_points=6.0, classification="Likely Pathogenic"
        )
        self.assertEqual(reasons, ["conflicting_evidence", "classification_pathogenic_or_likely_pathogenic"])

    def test_benign_and_likely_benign_never_produce_the_classification_reason(self):
        for classification in ("Benign", "Likely Benign"):
            with self.subTest(classification=classification):
                reasons = determine_review_required_reasons(
                    pathogenic_points=0.0, benign_points=10.0, classification=classification
                )
                self.assertEqual(reasons, [])


class TestAcmgRuleEngineNeverRelabelsAFailureAsInsufficiency(unittest.TestCase):
    """The integration-level proof: `ACMGRuleEngine.evaluate()` itself,
    not just the pure function, must not relabel a failure."""

    def test_all_clean_and_empty_with_gene_resolved_is_insufficient_evidence(self):
        """The baseline this file's other tests are contrasted against:
        genuinely no usable evidence, cleanly, IS insufficient_evidence."""
        result = _evaluate()
        self.assertEqual(result["interpretation_outcome"], InterpretationOutcome.INSUFFICIENT_EVIDENCE.value)

    def test_a_source_failing_on_purpose_does_not_produce_insufficient_evidence(self):
        """THE test the dispatch exists to require: make a source fail
        on purpose (a genuine `error` key, not `found: False`, not a
        skip) and prove the outcome is NOT insufficient_evidence."""
        result = _evaluate(gnomad_result={"found": False, "error": "gnomAD API timed out after 30s"})
        self.assertNotEqual(result["interpretation_outcome"], InterpretationOutcome.INSUFFICIENT_EVIDENCE.value)
        self.assertEqual(result["interpretation_outcome"], InterpretationOutcome.INTERPRETED.value)

    def test_an_unresolved_gene_does_not_produce_insufficient_evidence(self):
        result = _evaluate(clingen_result={"gene_resolution_status": "not_found"})
        self.assertNotEqual(result["interpretation_outcome"], InterpretationOutcome.INSUFFICIENT_EVIDENCE.value)
        self.assertEqual(result["interpretation_outcome"], InterpretationOutcome.INTERPRETED.value)

    def test_an_ambiguous_gene_does_not_produce_insufficient_evidence(self):
        result = _evaluate(
            clingen_result={"gene_resolution_status": "ambiguous", "gene_resolution_candidates": ["A", "B"]}
        )
        self.assertNotEqual(result["interpretation_outcome"], InterpretationOutcome.INSUFFICIENT_EVIDENCE.value)

    def test_a_structural_skip_is_not_a_failure_and_can_still_be_insufficient(self):
        """A `found: False`/skip (no `error` key) is not a query
        failure -- it must NOT block insufficient_evidence the way a
        real error does, or the gate would be too broad and everything
        would fall through to `interpreted`."""
        result = _evaluate(alphamissense_result={"skipped": True, "reason": "not eligible for this variant"})
        self.assertEqual(result["interpretation_outcome"], InterpretationOutcome.INSUFFICIENT_EVIDENCE.value)

    def test_review_required_reasons_is_always_present_and_empty_in_the_clean_baseline(self):
        """Always-present, never omitted -- same discipline as
        `data_freshness_warnings` -- even when empty."""
        result = _evaluate()
        self.assertIn("review_required_reasons", result)
        self.assertEqual(result["review_required_reasons"], [])


class TestAcmgRuleEngineWiresReviewRequired(unittest.TestCase):
    """`ACMGRuleEngine.evaluate()`'s ACTUAL wiring of the classification
    trigger, not just the pure function it delegates to.
    `ACMGRuleEngine._combine` is patched to a fixed, synthetic
    `CombineResult` -- deliberately, so this test exercises the wiring
    (does `evaluate()` correctly read `combine_result.classification`
    and pass it through?) without re-testing `_combine`'s own point
    arithmetic, which `tests/test_acmg_net_points.py` already covers."""

    def test_a_pathogenic_classification_from_combine_produces_review_required(self):
        fake_combine_result = CombineResult(
            classification="Pathogenic", trace=["synthetic"], pathogenic_points=10.0, benign_points=0.0, net_points=10.0
        )
        with mock.patch.object(ACMGRuleEngine, "_combine", return_value=fake_combine_result):
            result = _evaluate()
        self.assertEqual(result["classification"], "Pathogenic")
        self.assertEqual(result["interpretation_outcome"], InterpretationOutcome.REVIEW_REQUIRED.value)
        self.assertEqual(result["review_required_reasons"], ["classification_pathogenic_or_likely_pathogenic"])

    def test_an_uncertain_significance_classification_from_combine_does_not_trigger_it(self):
        fake_combine_result = CombineResult(
            classification="Uncertain Significance",
            trace=["synthetic"],
            pathogenic_points=2.0,
            benign_points=0.0,
            net_points=2.0,
        )
        with mock.patch.object(ACMGRuleEngine, "_combine", return_value=fake_combine_result):
            result = _evaluate()
        self.assertEqual(result["interpretation_outcome"], InterpretationOutcome.INTERPRETED.value)
        self.assertEqual(result["review_required_reasons"], [])


class TestInterpretationResultCarriesTheOutcome(unittest.TestCase):
    """The outcome must thread through `build_interpretation_result`
    (Phase 2's canonical object) exactly the way `acmg_net_points` and
    `acmg_classification` already do -- carried alongside, not
    replacing."""

    def test_interpretation_result_to_dict_exposes_interpretation_outcome(self):
        from pipeline.interpretation_result import build_interpretation_result

        acmg_result = _evaluate()
        interpretation = {"acmg_evaluation": acmg_result, "supporting_evidence": []}
        result_obj = build_interpretation_result(
            variant_dict=_VARIANT,
            interpretation=interpretation,
            clinvar_result={"records": []},
            dbsnp_result={"found": False},
            protein_result={},
            blast_result={"hit_count": 0},
            clingen_result={"gene_resolution_status": "resolved", "gene_symbol": "BRCA1"},
        )
        as_dict = result_obj.to_dict()
        self.assertIn("interpretation_outcome", as_dict)
        self.assertEqual(as_dict["interpretation_outcome"], InterpretationOutcome.INSUFFICIENT_EVIDENCE.value)
        # ALONGSIDE, not replacing: the ACMG classification is still
        # present and unrelated to the outcome value.
        self.assertIn("acmg_classification", as_dict)

    def test_interpretation_result_to_dict_exposes_review_required_reasons(self):
        from pipeline.interpretation_result import build_interpretation_result

        fake_combine_result = CombineResult(
            classification="Pathogenic", trace=["synthetic"], pathogenic_points=10.0, benign_points=0.0, net_points=10.0
        )
        with mock.patch.object(ACMGRuleEngine, "_combine", return_value=fake_combine_result):
            acmg_result = _evaluate()
        interpretation = {"acmg_evaluation": acmg_result, "supporting_evidence": []}
        result_obj = build_interpretation_result(
            variant_dict=_VARIANT,
            interpretation=interpretation,
            clinvar_result={"records": []},
            dbsnp_result={"found": False},
            protein_result={},
            blast_result={"hit_count": 0},
            clingen_result={"gene_resolution_status": "resolved", "gene_symbol": "BRCA1"},
        )
        as_dict = result_obj.to_dict()
        self.assertEqual(as_dict["interpretation_outcome"], InterpretationOutcome.REVIEW_REQUIRED.value)
        self.assertEqual(as_dict["review_required_reasons"], ["classification_pathogenic_or_likely_pathogenic"])


if __name__ == "__main__":
    unittest.main()
