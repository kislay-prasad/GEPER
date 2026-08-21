"""
Card: gnomad-lookup-failure-collapses-into-a-confirmed-negative.

`pipeline/orchestration/shared.py::run_acmg_evidence_batch` wraps every
per-variant `gnomad_lkp.lookup()` call in a bare `except Exception`. The
value it assigns to `gnomad_af_absent` there feeds two independent
consumers: `pipeline/acmg/classifier.py::AcmgClassifier._pm2` (PM2 --
"Absent from controls in gnomAD") and this module's own
`_availability_fields` (the reporting-facing `gnomad_status`/
`gnomad_unavailable_reason` pair). Both distinguish three real states --
confirmed absent, confirmed found, and never actually checked -- but
before this card, a *lookup failure* (network/tabix error, or any other
exception) and a *confirmed-not-absent finding* were both represented by
the same value. THE DANGEROUS CASE, in the human's own words: "lookup
raises, classifier must see not-evaluated, not absent-confirmed."

Written against the CLASSIFIER's and REPORTER's observable view --
`AcmgResult.criteria_met`/`criteria_unknown` and the `gnomad_status`
field `run_acmg_evidence_batch` returns per variant -- via the REAL
`run_acmg_evidence_batch`, with only `GnomadLookup.lookup`/
`lookup_batch` mocked. Deliberately does NOT re-implement or duplicate
shared.py's own try/except dispatch (see this module's own docstring
note on `tests/test_fixes_6_to_11.py::_simulate_gnomad_branch`, which
does exactly that and is flagged separately for going stale): a
hand-copied simulation of the dispatch logic cannot catch a change to
the real dispatch logic, and silently drifts from it forever once one
side changes without the other.

Three cases: the dangerous one (lookup raises), and two controls this
class must keep passing exactly as PM2's own docstring instructs --
"keep the control honest, the same way you did for QC: a lookup that
genuinely returns 'present' must still read as present." A fix that
makes the dangerous case pass by making every uncertain result look
like "absent" or "unavailable" would be just as wrong as the original
bug.

CORRECTION (Kelly, relayed by god, 2026-08-21): the
`TestGnomadLookupFailureDoesNotBecomeConfirmedAbsent` class below
passes identically pre-fix and post-fix -- it never went RED and does
not discriminate this change. Root cause, not a test bug in the usual
sense: `_pm2` guards on `e.gnomad_af is None and e.gnomad_af_popmax is
None` BEFORE it ever reads `gnomad_af_absent`, and at that point
`False` and `None` are both falsy -- so PM2's status (and
`_availability_fields`'s derived `gnomad_status`) was ALREADY correct
pre-fix, for both the exception path and the non-exception UNAVAILABLE
path. Confirmed independently by Kelly (diffed a real
`run_acmg_evidence_batch()` run pre-fix vs post-fix: the only
difference in the whole ACMG output is the log level, DEBUG ->
WARNING) and by me (see this file's own commit history / the
pre-fix-RED verification originally reported, which turned out to be
green both times).

Kept below anyway as real, valuable coverage of the classifier's/
reporter's observable behaviour (the property they assert -- "a
lookup failure is never read as confirmed absence" -- is true and
worth protecting even though it wasn't THIS fix that made it true).
The actual regression pin lives in
`TestGnomadAbsentSentinelIsNoneNotFalseOnFailure` and
`TestGnomadLookupFailureLogsAtWarningNotDebug` below, which assert on
the thing that actually changed: the sentinel value itself
(`gnomad_af_absent`, captured via a spy on `AcmgClassifier.classify`
rather than reading it back through PM2), and the log record's level.
`False` is the same token `gnomad_af_absent=False` uses for "checked,
confirmed present" -- so pre-fix, a failed lookup stored a confident
negative it had not earned, and its safety depended entirely on PM2's
own unrelated af-is-None guard holding forever. A future criterion
that reads the flag directly, without that guard in front of it,
would inherit the wrong answer. That dependency-on-an-unrelated-guard
is the actual defect a test needs to pin.
"""

import unittest
from unittest import mock

from pipeline.acmg.classifier import AcmgClassifier
from pipeline.gnomad.lookup import GnomadHit, GnomadLookupOutcome
from pipeline.orchestration.shared import run_acmg_evidence_batch


def _variant():
    return {
        "chrom": "17",
        "pos": 43057051,
        "ref": "A",
        "alt": "T",
        "gene_name": "BRCA1",
        "consequence": "missense_variant",
    }


def _cfg():
    return {"gnomad": {"enabled": True}, "clinvar": {"enabled": False}, "vep": {"enabled": False}}


def _classify_with_mocked_lookup(lookup_side_effect=None, lookup_return_value=None):
    with (
        mock.patch(
            "pipeline.gnomad.lookup.GnomadLookup.lookup",
            side_effect=lookup_side_effect,
            return_value=lookup_return_value,
        ),
        mock.patch("pipeline.gnomad.lookup.GnomadLookup.lookup_batch", return_value=None),
    ):
        results = run_acmg_evidence_batch([_variant()], _cfg(), sample_id="TESTSAMPLE")
    return results[0]


def _classify_capturing_evidence(lookup_side_effect=None, lookup_return_value=None):
    """Same real `run_acmg_evidence_batch()` call, but spies on
    `AcmgClassifier.classify` (delegates to the real implementation,
    just records the `VariantEvidence` it was called with) so the test
    can inspect `gnomad_af_absent` itself -- `run_acmg_evidence_batch`'s
    own return dict never exposes that raw sentinel, only PM2's derived
    status and `_availability_fields`'s derived `gnomad_status`."""
    captured = {}
    real_classify = AcmgClassifier.classify

    def _spy(self, evidence):
        captured["evidence"] = evidence
        return real_classify(self, evidence)

    with (
        mock.patch(
            "pipeline.gnomad.lookup.GnomadLookup.lookup",
            side_effect=lookup_side_effect,
            return_value=lookup_return_value,
        ),
        mock.patch("pipeline.gnomad.lookup.GnomadLookup.lookup_batch", return_value=None),
        mock.patch.object(AcmgClassifier, "classify", _spy),
    ):
        run_acmg_evidence_batch([_variant()], _cfg(), sample_id="TESTSAMPLE")
    return captured["evidence"]


class TestGnomadLookupFailureDoesNotBecomeConfirmedAbsent(unittest.TestCase):
    def test_lookup_exception_leaves_pm2_not_evaluated(self):
        """THE DANGEROUS CASE: lookup() raises -> the classifier must see
        PM2 as not-evaluated, never as confirmed-absent (met)."""
        r = _classify_with_mocked_lookup(lookup_side_effect=RuntimeError("network down"))
        self.assertIn("PM2", r["criteria_unknown"], f"PM2 should be unevaluated; got {r}")
        self.assertNotIn(
            "PM2", r["criteria_met"], "a lookup failure must never be read as confirmed absence"
        )
        self.assertEqual(
            r["gnomad_status"],
            "unavailable",
            "a lookup failure must report as unavailable, not absent",
        )
        self.assertNotEqual(r["gnomad_status"], "absent")

    def test_lookup_returning_unavailable_outcome_also_leaves_pm2_not_evaluated(self):
        """Same dangerous shape via the non-exception UNAVAILABLE path
        (no network/tabix error, lookup just declines to answer)."""
        r = _classify_with_mocked_lookup(lookup_return_value=GnomadLookupOutcome.UNAVAILABLE)
        self.assertIn("PM2", r["criteria_unknown"])
        self.assertNotIn("PM2", r["criteria_met"])
        self.assertEqual(r["gnomad_status"], "unavailable")

    def test_confirmed_absent_still_awards_pm2(self):
        """CONTROL: a lookup that genuinely confirms absence must still
        award PM2 -- the fix must not blur every uncertain-or-not result
        into a single flat 'not evaluated' bucket."""
        r = _classify_with_mocked_lookup(lookup_return_value=GnomadLookupOutcome.ABSENT)
        self.assertIn("PM2", r["criteria_met"], f"confirmed absence should award PM2; got {r}")
        self.assertEqual(r["gnomad_status"], "absent")

    def test_confirmed_present_is_not_swallowed_into_unavailable(self):
        """CONTROL: a lookup that genuinely returns a real hit must still
        read as found/present, not get folded into 'unavailable' by an
        over-broad fix."""
        hit = GnomadHit(af=0.15, af_popmax=0.2, ac=1500, an=10000, backend_used="test")
        r = _classify_with_mocked_lookup(lookup_return_value=hit)
        self.assertEqual(r["gnomad_status"], "found")
        self.assertEqual(r["gnomad_af"], 0.15)
        self.assertNotIn("PM2", r["criteria_met"], "a common variant (af=0.15) must not award PM2")
        self.assertNotIn("PM2", r["criteria_unknown"], "a genuine hit is evaluated, not unknown")


class TestGnomadAbsentSentinelIsNoneNotFalseOnFailure(unittest.TestCase):
    """THE ACTUAL REGRESSION PIN (see this module's docstring
    CORRECTION section). Asserts on `gnomad_af_absent` itself, not on
    a downstream consumer whose own guard already masked the bug."""

    def test_lookup_exception_sets_sentinel_to_none_not_false(self):
        ev = _classify_capturing_evidence(lookup_side_effect=RuntimeError("network down"))
        self.assertIsNone(
            ev.gnomad_af_absent,
            "a lookup failure must leave gnomad_af_absent as None (not evaluated), not the "
            "same False a confirmed-present lookup stores -- that identical value is the "
            "actual regression, even though PM2's own af-is-None guard happened to mask it",
        )

    def test_lookup_returning_unavailable_outcome_sets_sentinel_to_none_not_false(self):
        # Kelly widened scope beyond just the except path: the
        # non-exception UNAVAILABLE outcome gets the same None sentinel
        # now, on the grounds that "the backend could not be reached"
        # is not an answer either. Confirming that's deliberate here.
        ev = _classify_capturing_evidence(lookup_return_value=GnomadLookupOutcome.UNAVAILABLE)
        self.assertIsNone(ev.gnomad_af_absent)

    def test_confirmed_absent_sentinel_is_still_true(self):
        """CONTROL: the fix must not blur a genuine confirmed-absent
        result into None as well."""
        ev = _classify_capturing_evidence(lookup_return_value=GnomadLookupOutcome.ABSENT)
        self.assertIs(ev.gnomad_af_absent, True)

    def test_confirmed_present_sentinel_is_still_false(self):
        """CONTROL: a genuine hit still legitimately sets False -- the
        fix narrows what False means (checked, confirmed present), it
        does not retire the value."""
        hit = GnomadHit(af=0.15, af_popmax=0.2, ac=1500, an=10000, backend_used="test")
        ev = _classify_capturing_evidence(lookup_return_value=hit)
        self.assertIs(ev.gnomad_af_absent, False)


class TestGnomadLookupFailureLogsAtWarningNotDebug(unittest.TestCase):
    """Secondary observable signal of the same fix (Kelly's own
    suggestion, relayed): an operator must see a gnomAD lookup failure
    without re-running at debug level, since it silently determines
    whether gnomAD evidence reaches ACMG at all. Pre-fix this logged at
    DEBUG only, which `assertLogs(level="WARNING")` never observes --
    the context manager itself raises "no logs of level WARNING or
    higher triggered" in that case, which is the correct RED."""

    def test_lookup_exception_logs_at_warning(self):
        with self.assertLogs("geper.pipeline.orchestration.shared", level="WARNING") as cm:
            with (
                mock.patch(
                    "pipeline.gnomad.lookup.GnomadLookup.lookup",
                    side_effect=RuntimeError("network down"),
                ),
                mock.patch("pipeline.gnomad.lookup.GnomadLookup.lookup_batch", return_value=None),
            ):
                run_acmg_evidence_batch([_variant()], _cfg(), sample_id="TESTSAMPLE")
        self.assertTrue(
            any("gnomAD lookup failed" in msg for msg in cm.output),
            f"expected a WARNING-level gnomAD lookup-failed log; got {cm.output}",
        )


if __name__ == "__main__":
    unittest.main()
