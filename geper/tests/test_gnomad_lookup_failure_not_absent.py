"""
Rubric 2b -- lookup-failure sentinel integrity, for gnomAD, in geper.

THE PROPERTY: for every consumer that turns a gnomAD lookup into ACMG
evidence or into reader-facing text, a lookup that FAILED must be
distinguishable from a lookup that SUCCEEDED AND FOUND NOTHING, and
must never default to the confirmed-positive reading. In gnomAD's case
the confirmed positive is the dangerous direction: "absent from gnomAD"
is PM2's own criterion ("Absent from controls in a population
database"), so a failure that reads as absence does not merely lose
evidence -- it MANUFACTURES PATHOGENIC EVIDENCE THE PIPELINE NEVER
EARNED.

*** THE DEFECT THIS FILE WAS WRITTEN AGAINST (found while writing it,
2026-08-22, geper). `GnomadAnnotation.not_found()` and
`GnomadAnnotation.from_error()` BOTH set `found=False` -- by design;
the field that separates them is `error`. Every consumer knew that and
guarded on it. But all of them guarded on the error string's
TRUTHINESS, not its PRESENCE:

    if not gnomad_result or gnomad_result.get("skipped") or gnomad_result.get("error"):

so a failure whose message happens to be the empty string walks
straight through the guard and is read as a confirmed absence. This is
the same shape as the `candidate_interpretation` accessor defect
(`report/clinical_report_builder.py:301`): a falsy-but-present value
falling through a guard written for a missing one. TRUTHINESS IS NOT
PRESENCE, and `""` is a state the producer can actually emit. ***

REACHABILITY, MEASURED RATHER THAN ASSUMED (pre-fix, 2026-08-22):
`orchestrator.py::_run_gnomad_stage`'s defense-in-depth `except
Exception` returns `{"error": str(exc)}`, and `str(exc)` IS `""` for
any exception raised without a message -- `RuntimeError()`,
`MemoryError()`, `TimeoutError()`, `RecursionError()`. Driving a
message-less `RuntimeError` through the real `_run_gnomad_stage`
produced exactly `{'found': False, 'skipped': False, 'error': ''}`,
which fed to the real `_pm2()` returns status "triggered" with the
reason "Variant is absent from gnomAD". `MemoryError` on a large VCF is
not an exotic hypothetical. The two provider-side `from_error(...)`
call sites happen to pass non-empty strings today
(`provider.py:251` relays `_post`'s fixed message, `provider.py:400`
prefixes the provider name), so the orchestrator path is the one
reachable route as the code stands -- that is a property of two string
literals, not a guarantee, which is why the fix is on the consumer
side where the property actually lives.

WHAT PRE-FIX ACTUALLY WENT WRONG, NAMED PER CONSUMER RATHER THAN
COUNTED (an "all consumers are fine" claim that does not name them is
agreeing with a number):

  acmg_rules.py::_pm2                              PM2 "triggered"  <-- the dangerous one
  interpretation.py::_gnomad_acmg_evidence         +1.0 pathogenic evidence line
  prioritization_engine.py::_population_rarity_factor
                                                   factor 1.0, "Variant absent from gnomAD."
  confidence_engine.py::_population_quality        quality 0.8, rationale asserting
                                                   "gnomAD queried successfully; variant is absent"
                                                   -- a positively false statement about a
                                                   lookup that never completed
  report_generator.py::_render_gnomad              "Status: Not found in gnomAD (consistent
                                                   with PM2 ...)" in the Markdown report
  pvs1/utils.py::population_af_from_gnomad         returned (0.0, "absent from gnomAD") --
                                                   a FABRICATED allele frequency for a
                                                   variant nobody looked up, feeding PVS1's
                                                   frequency caveat and PS4's context. Its
                                                   own docstring already drew the line
                                                   ("a variant GENUINELY absent from gnomAD
                                                   is reported as 0.0"); truthiness is what
                                                   broke the word "genuinely"
  acmg_rules.py::_ps4                              reached the wrong branch; outcome was
                                                   "not_evaluated" either way, so no
                                                   observable harm -- pinned anyway
  acmg_rules.py::_ba1_bs1                          same: "not found" already forces
                                                   not_evaluated, so no observable harm
  orchestrator.py::_run_gnomad_stage (:2411)       `if result.get("error")` -- a provider
                                                   returning an empty-message error would
                                                   not be appended to the report's Stage
                                                   Warnings section at all
  orchestrator.py (:1664, provenance)              gnomAD's "Query failed" provenance note
                                                   would not be recorded

TWO of those ten are named as harmless. That is deliberate: an
exception list with nothing in it is the part of this note that would
be doing no work. (I first wrote this list with THREE harmless entries
-- `population_af_from_gnomad` was one of them, on a reading of the
source rather than a run. The test disagreed: it returns a fabricated
0.0. The list above is the one the run produced.)

THE CONTROLS MATTER AS MUCH AS THE DANGEROUS CASE. A "fix" that made
every uncertain gnomAD result read as unavailable would pass the
dangerous case and destroy PM2. So each consumer below is asserted in
both directions: a genuine confirmed-absence must STILL award PM2 /
still score as rarity, and a genuine hit must still read as found.
"""

import unittest
from unittest import mock

from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.confidence_engine import ConfidenceEngine
from pipeline.gnomad.models import GnomadAnnotation
from pipeline.interpretation import InterpretationEngine
from pipeline.orchestrator import GeperPipeline
from pipeline.prioritization_engine import PrioritizationEngine
from pipeline.pvs1.utils import population_af_from_gnomad
from report.report_generator import ReportGenerator

# A lookup that FAILED, whose exception carried no message. This is the
# whole point of the file: `error` is PRESENT, and falsy.
FAILED_EMPTY_MESSAGE = {"found": False, "skipped": False, "error": ""}

# A lookup that FAILED with an ordinary message -- the case every
# consumer already handled correctly. Kept as the control that proves
# these assertions are about the empty string, not about failure.
FAILED_WITH_MESSAGE = {"found": False, "skipped": False, "error": "network unreachable"}

# A lookup that SUCCEEDED and found nothing. Indistinguishable from
# FAILED_EMPTY_MESSAGE by `found` alone -- `error` is the only field
# that separates them, which is exactly why it must be read by presence.
CONFIRMED_ABSENT = {"found": False, "skipped": False, "error": None, "source": "local_index", "build": "GRCh38"}

# A lookup that SUCCEEDED and found a common variant.
CONFIRMED_PRESENT = {
    "found": True,
    "skipped": False,
    "error": None,
    "source": "local_index",
    "build": "GRCh38",
    "global_af": 0.15,
    "population_breakdown": {},
}


class TestFailedLookupNeverReadsAsConfirmedAbsence(unittest.TestCase):
    """THE DANGEROUS DIRECTION, one assertion per consumer that can
    turn absence into evidence."""

    def test_pm2_is_not_triggered_by_an_empty_message_failure(self):
        result = ACMGRuleEngine._pm2(FAILED_EMPTY_MESSAGE)
        self.assertNotEqual(
            result.status,
            "triggered",
            "a gnomAD lookup that FAILED must never award PM2 -- 'absent from controls' is a "
            "finding the pipeline has to earn, and an empty error message is still an error",
        )
        self.assertEqual(result.status, "not_evaluated")

    def test_interpretation_adds_no_pm2_evidence_for_an_empty_message_failure(self):
        self.assertEqual(
            InterpretationEngine._gnomad_acmg_evidence(FAILED_EMPTY_MESSAGE),
            [],
            "a failed lookup must contribute no gnomAD evidence and no score weight",
        )

    def test_priority_rarity_factor_is_not_scored_for_an_empty_message_failure(self):
        reasons: list = []
        factor = PrioritizationEngine._population_rarity_factor(FAILED_EMPTY_MESSAGE, 1.0, reasons)
        self.assertEqual(factor.value, 0.0)
        self.assertEqual(reasons, [], "a failed lookup must not produce an 'Absent from gnomAD' reason")

    def test_confidence_does_not_claim_the_lookup_succeeded_after_an_empty_message_failure(self):
        score = ConfidenceEngine._population_quality(FAILED_EMPTY_MESSAGE, None, 1.0)
        self.assertNotIn(
            "queried successfully",
            score.rationale,
            "the confidence rationale is reader-facing text; asserting a lookup succeeded when it "
            "raised is a false statement in the report, not merely a mis-scored input",
        )
        self.assertNotIn("gnomAD", score.sources_checked)

    def test_markdown_report_does_not_state_not_found_after_an_empty_message_failure(self):
        lines = ReportGenerator._render_gnomad(dict(FAILED_EMPTY_MESSAGE, source="error", build="GRCh38"))
        text = "\n".join(lines)
        self.assertNotIn(
            "Not found in gnomAD",
            text,
            "the rendered report must not tell a clinician the variant is absent from gnomAD when "
            "the lookup never completed",
        )
        self.assertIn("query failed", text)

    def test_ps4_and_ba1_bs1_take_the_unavailable_branch_for_an_empty_message_failure(self):
        # No observable harm pre-fix (see this module's docstring: both
        # already reached "not_evaluated" by another route). Pinned so a
        # future edit to either branch cannot quietly acquire the defect.
        ps4 = ACMGRuleEngine._ps4(FAILED_EMPTY_MESSAGE)
        ba1, bs1 = ACMGRuleEngine._ba1_bs1(FAILED_EMPTY_MESSAGE)
        self.assertEqual(ps4.status, "not_evaluated")
        self.assertEqual(ba1.status, "not_evaluated")
        self.assertEqual(bs1.status, "not_evaluated")
        self.assertEqual(population_af_from_gnomad(FAILED_EMPTY_MESSAGE), (None, None))


class TestFailedLookupWithAMessageStillBehaves(unittest.TestCase):
    """CONTROL: the ordinary failure path every consumer already got
    right. If these ever diverge from the empty-message cases above,
    the guard has gone back to reading the string instead of its
    presence."""

    def test_every_consumer_treats_a_message_carrying_failure_as_unavailable(self):
        self.assertEqual(ACMGRuleEngine._pm2(FAILED_WITH_MESSAGE).status, "not_evaluated")
        self.assertEqual(InterpretationEngine._gnomad_acmg_evidence(FAILED_WITH_MESSAGE), [])
        reasons: list = []
        self.assertEqual(PrioritizationEngine._population_rarity_factor(FAILED_WITH_MESSAGE, 1.0, reasons).value, 0.0)
        self.assertEqual(reasons, [])
        self.assertNotIn(
            "queried successfully", ConfidenceEngine._population_quality(FAILED_WITH_MESSAGE, None, 1.0).rationale
        )


class TestConfirmedAbsenceStillCountsAsEvidence(unittest.TestCase):
    """CONTROL, and the one that stops the fix from being a blunt
    instrument: a lookup that genuinely completed and found nothing is
    PM2 evidence and must stay that way."""

    def test_confirmed_absence_still_triggers_pm2(self):
        self.assertEqual(ACMGRuleEngine._pm2(CONFIRMED_ABSENT).status, "triggered")

    def test_confirmed_absence_still_contributes_interpretation_evidence(self):
        evidence = InterpretationEngine._gnomad_acmg_evidence(CONFIRMED_ABSENT)
        self.assertEqual(len(evidence), 1)
        self.assertIn("PM2", evidence[0][0])

    def test_confirmed_absence_still_scores_as_rarity(self):
        reasons: list = []
        factor = PrioritizationEngine._population_rarity_factor(CONFIRMED_ABSENT, 1.0, reasons)
        self.assertEqual(factor.value, 1.0)
        self.assertEqual(len(reasons), 1)

    def test_confirmed_absence_still_counts_as_a_successful_query_for_confidence(self):
        score = ConfidenceEngine._population_quality(CONFIRMED_ABSENT, None, 1.0)
        self.assertIn("queried successfully", score.rationale)
        self.assertIn("gnomAD", score.sources_checked)

    def test_confirmed_absence_still_renders_as_not_found_in_the_report(self):
        self.assertIn("Not found in gnomAD", "\n".join(ReportGenerator._render_gnomad(CONFIRMED_ABSENT)))


class TestConfirmedPresenceIsNotSweptIntoUnavailable(unittest.TestCase):
    """CONTROL: a real hit must not be folded into the failure bucket by
    an over-broad fix."""

    def test_common_variant_does_not_award_pm2_and_is_not_unavailable(self):
        result = ACMGRuleEngine._pm2(CONFIRMED_PRESENT)
        self.assertNotEqual(result.status, "triggered")
        self.assertNotIn("skipped or errored", result.rationale)

    def test_common_variant_still_renders_its_allele_frequency(self):
        self.assertIn("Global AF", "\n".join(ReportGenerator._render_gnomad(CONFIRMED_PRESENT)))


class TestNotFoundAndFromErrorAreOnlySeparableByTheErrorField(unittest.TestCase):
    """The reason every consumer has to read `error` by presence: the
    two producers agree on every other field. This is a characterisation
    test -- if a future change makes `from_error` distinguishable some
    other way (a status enum, `found=None`), this test is the record of
    why the consumers were written the way they were."""

    def test_not_found_and_from_error_differ_only_in_error(self):
        absent = GnomadAnnotation.not_found("17", 43057051, "A", "T", "GRCh38", "local_index").to_dict()
        failed = GnomadAnnotation.from_error("17", 43057051, "A", "T", "GRCh38", "boom").to_dict()
        differing = {k for k in absent if absent[k] != failed[k]}
        self.assertEqual(
            differing,
            {"error", "source"},
            "if these two shapes start differing by more than the error string and the source "
            "label, the consumers' presence checks should be revisited -- and if they start "
            "differing by LESS, the failure state has become unrepresentable",
        )
        self.assertFalse(absent["found"])
        self.assertFalse(failed["found"], "both are found=False -- `found` alone cannot discriminate")


class TestOrchestratorNeverEmitsAMessagelessError(unittest.TestCase):
    """The producer half, pinned separately from the consumer half.

    Consumers now read `error` by presence, so an empty message is
    handled correctly -- but an empty message is still useless to the
    operator reading the report's Stage Warnings section, which prints
    the string verbatim. This asserts the stage substitutes the
    exception's type name when the exception carries no message.

    Deliberately a SECOND pin rather than a replacement for the
    consumer tests: if this producer fix were the only one, the property
    would depend on no other code path ever constructing an
    empty-message error, which is exactly the assumption that failed the
    first time.
    """

    @staticmethod
    def _stage_with(exc):
        pipeline = GeperPipeline.__new__(GeperPipeline)
        pipeline.gnomad_client = mock.Mock()
        pipeline.gnomad_client.query_variant.side_effect = exc
        pipeline.sequence_context_gen = mock.Mock(assembly="GRCh38")
        pipeline._timer = mock.MagicMock()
        errors: list = []
        return pipeline._run_gnomad_stage(mock.Mock(), errors), errors

    def test_message_less_exception_still_produces_a_non_empty_error(self):
        result, errors = self._stage_with(RuntimeError())
        self.assertTrue(result["error"], "a message-less exception must not become an empty error string")
        self.assertIn("RuntimeError", result["error"])
        self.assertEqual(len(errors), 1)

    def test_exception_with_a_message_keeps_its_message(self):
        result, _ = self._stage_with(RuntimeError("tabix segfaulted"))
        self.assertIn("tabix segfaulted", result["error"])


if __name__ == "__main__":
    unittest.main()
