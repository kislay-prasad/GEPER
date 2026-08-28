"""
Bounded re-probe after latch -- ALL 4 TESTS ARE GREEN AND THE FEATURE
IS SHIPPED. This file is committed, not ignored; do not skip its
results.

This file tests the "bounded re-probe" feature for
`utils/service_health.py::ServiceHealthRegistry` (card
`health-probe-bounded-reprobe`, follow-up to
`ensembl-health-probe-false-negative`). That feature EXISTS in the
shipped code: `ServiceHealthRegistry._maybe_reprobe()`, called from
`is_offline()`, driven by `CONFIG.health_check.REPROBE_AFTER_SKIPS`
(default 20), `REPROBE_AFTER_SECS` (default 120) and
`REPROBE_MAX_ATTEMPTS` (default 3).

HISTORY, because the text this replaces said the opposite. The
docstring here used to open "INTENTIONALLY UNCOMMITTED / NOT YET
GREEN", state that the feature "does NOT exist in the shipped code
yet", that three of the four tests were "DELIBERATELY RED right now
and will stay red until Kelly lands the feature", and that the file
was "git-ignored-by-convention". All four claims are false and appear
to have been false from the start: commit 72aec5a added THIS FILE and
`_maybe_reprobe()` in the same commit, so the feature landed at the
moment these tests were committed. The file is tracked, `git
check-ignore` does not match it, and all 4 tests pass.

WHY THAT MATTERED ENOUGH TO REWRITE RATHER THAN DELETE: a test file
that tells its reader its own failures are expected disarms every
test in it. These 4 are live and load-bearing -- they are the only
coverage of the re-probe contract.

THESE TESTS ARE NOT VACUOUS -- they were checked, not assumed. With
`_maybe_reprobe` patched to a no-op, exactly the three feature tests
fail (`test_recovery_after_many_skips_unlatches`,
`test_recovery_after_long_elapsed_time_unlatches`,
`test_reprobe_attempts_are_capped_and_eventually_stop_spamming`) and
`test_note_success_and_note_failure_do_not_unlatch_on_their_own`
still passes, which is correct: that fourth test pins an invariant
the feature must NOT violate (`note_success()`/`note_failure()` stay
advisory and never un-latch by themselves -- the difference between
the agreed shape (a) and the rejected shape (b)), rather than a
capability the feature had to add.

The tests deliberately assert on the probe's own call count with
generous margins (200 skips against a threshold of 20, a 10,000s jump
against 120s, 12 cycles against a cap of 3) instead of binding to the
shipped constants, so they stay valid if those defaults are retuned.
Do not tighten them onto the current values.

REMAINING TIDY-UP, not yet done and not required for correctness:
this file's original plan was to fold its contents back into
`test_service_health.py` as `TestBoundedReprobeAfterLatch` (no such
class exists there today) and delete this file, so the feature's
coverage lives with the rest of the health-probe suite. That is a
pure test-file move; it is left for whoever owns that consolidation.
"""

import unittest
from unittest import mock

from utils import service_health as sh


class TestBoundedReprobeAfterLatch(unittest.TestCase):
    """
    Regression tests for card `health-probe-bounded-reprobe` (follow-up
    to `ensembl-health-probe-false-negative`/`TestRetryBeforeLatch` in
    test_service_health.py).

    SHAPE, per Kelly's design answer (2026-08-21, overriding my own
    earlier draft's shape (b) assumption -- see that message for the
    full reasoning): SELF-CONTAINED IN THE REGISTRY, not piggybacked on
    client traffic. The registry retains each service's probe
    (implicitly, via whatever internal storage the fix adds -- these
    tests never assume a specific attribute name, only that the SAME
    probe callable handed to `run_startup_checks` keeps getting
    reinvoked) and re-runs it itself, internally, once EITHER N
    accumulated `note_skip()` calls (N~20-25) OR T seconds elapsed
    (T~120) have passed since the latch -- whichever comes first --
    bounded to a cap of ~3 such internal re-probes per service per run.
    `is_offline()` flips to False ONLY when a re-probe genuinely
    succeeds; there is no "open window" a client walks through under
    this shape (that was shape (b)'s mechanism, rejected on 3 grounds:
    it would have made note_success()/note_failure() load-bearing,
    silently disabling recovery for any client that forgets to call
    one, in a codebase whose named recurring defect is exactly this
    class of silence; it would cost up to a full ~90s retry budget per
    failed reprobe attempt vs. ~4s for a dedicated fast-timeout probe,
    ~22x cheaper; and (a) matches the module's existing "no new client
    call sites" philosophy). `note_success()`/`note_failure()` stay
    exactly what they are today -- advisory counters, never load-
    bearing for the latch.

    THE TWO DANGEROUS CASES, in the human's own words (relayed via the
    card): a service that latches offline and then genuinely recovers
    must be un-latched; a service that stays down must not be
    re-probed indefinitely (attempt cap must prevent spam).

    N/T/cap are still ranges, not final numbers -- Kelly confirmed the
    log evidence cannot size them yet (only 5 real latency samples
    exist post-5094620, and zero recoveries have ever been observed,
    since nothing re-probes yet -- the data can only be produced by the
    feature meant to size it). Every test below therefore uses
    generous safety margins well outside the stated ranges (200 skips
    against ~20-25, a 10,000s/1,000,000s time jump against ~120s, 12
    threshold-crossing cycles against a ~3 cap) and asserts on the
    PROBE'S OWN CALL COUNT (via a controllable closure) rather than any
    specific constant, so these tests are valid for any value Kelly
    eventually picks within each range -- do not bind to specific
    constants once they land; that would pin a guess. Patches BOTH
    `time.monotonic` and `time.time` to the same fake clock, since
    which one the elapsed-time check uses is still unspecified.
    """

    def setUp(self):
        self.registry = sh.ServiceHealthRegistry()
        # Real CONFIG.health_check, not _fake_config()'s Mock -- same
        # reasoning as TestRetryBeforeLatch: any new
        # REPROBE_AFTER_SKIPS/REPROBE_AFTER_SECS/REPROBE_MAX_ATTEMPTS-
        # shaped config field the fix adds should read back as its real
        # shipped default, not an unconfigured Mock.

    @staticmethod
    def _controllable_probe():
        """A probe that reports OFFLINE until the test flips
        `state["healthy"] = True` to simulate a genuine external
        recovery, then reports HEALTHY forever after. `state["n"]`
        counts every real invocation (startup attempts AND any later
        internal re-probes) -- the only way a shape-(a) test can prove
        a re-probe actually fired, since there is no separate "window"
        event to observe."""
        state = {"healthy": False, "n": 0}

        def probe():
            state["n"] += 1
            if state["healthy"]:
                return sh.ServiceStatus.HEALTHY, ""
            return sh.ServiceStatus.OFFLINE, "Timeout"

        return probe, state

    def _latch_offline(self, probe, name="Ensembl"):
        with mock.patch("utils.service_health.time.sleep", return_value=None):
            self.registry.run_startup_checks([sh.ServiceCheck(name, probe)])
        self.assertTrue(self.registry.is_offline(name), "setup failed: service did not latch offline")

    def test_recovery_after_many_skips_unlatches(self):
        """THE FIRST DANGEROUS CASE, skip-count path: latch, let the
        service genuinely recover, accumulate well past the skip
        threshold -- an internal re-probe must fire, detect the
        recovery, and un-latch. Asserting the probe's call count
        actually grew is what proves a real re-probe happened, not
        merely that is_offline()'s answer changed some other way."""
        probe, state = self._controllable_probe()
        self._latch_offline(probe)
        calls_before = state["n"]

        state["healthy"] = True  # the service has now genuinely recovered
        for _ in range(200):
            self.registry.note_skip("Ensembl")

        self.assertFalse(
            self.registry.is_offline("Ensembl"),
            "after far more skips than any plausible threshold, an internal re-probe must fire and detect the recovery",
        )
        self.assertGreater(
            state["n"],
            calls_before,
            "is_offline() must have actually re-invoked the probe internally, not changed its answer some other way",
        )

    def test_recovery_after_long_elapsed_time_unlatches(self):
        """THE FIRST DANGEROUS CASE, time path: latch, let the service
        genuinely recover, then let a lot of wall-clock time pass with
        ZERO skips at all -- the internal re-probe must still fire on
        the time trigger alone."""
        probe, state = self._controllable_probe()
        self._latch_offline(probe)
        calls_before = state["n"]

        state["healthy"] = True
        fake_now = [1_000_000.0]
        with (
            mock.patch("utils.service_health.time.monotonic", side_effect=lambda: fake_now[0]),
            mock.patch("utils.service_health.time.time", side_effect=lambda: fake_now[0]),
        ):
            fake_now[0] += 10_000.0
            self.assertFalse(
                self.registry.is_offline("Ensembl"),
                "after far more elapsed time than any plausible threshold, an internal re-probe "
                "must fire even with zero note_skip() calls",
            )
            self.assertGreater(state["n"], calls_before)

    def test_note_success_and_note_failure_do_not_unlatch_on_their_own(self):
        """THE CONTRACT DIFFERENCE FROM SHAPE (b): note_success()/
        note_failure() are advisory counters, exactly as they are
        today -- calling either directly must NEVER clear or affect
        the latch by itself, even if the service has genuinely
        recovered. Only an internal re-probe (triggered by the
        skip/time threshold) may clear it. If this test fails, the
        fix accidentally rebuilt shape (b)'s rejected load-bearing
        design."""
        probe, state = self._controllable_probe()
        self._latch_offline(probe)
        state["healthy"] = True  # genuinely recovered...

        self.registry.note_success("Ensembl")  # ...but this alone must change nothing
        self.assertTrue(
            self.registry.is_offline("Ensembl"),
            "note_success() must be advisory only under shape (a) -- it must not un-latch on its own",
        )
        self.registry.note_failure("Ensembl")
        self.assertTrue(self.registry.is_offline("Ensembl"))

    def test_reprobe_attempts_are_capped_and_eventually_stop_spamming(self):
        """THE SECOND DANGEROUS CASE: a service that stays down forever
        must stop consuming fresh probe calls once the attempt cap is
        exhausted. Directly observable under shape (a) as the probe's
        own call count plateauing, rather than inferred from any
        window concept."""
        probe, state = self._controllable_probe()  # state["healthy"] stays False throughout
        self._latch_offline(probe)
        calls_before = state["n"]

        call_counts = []
        num_batches = 12  # well past any plausible ~3 cap
        for _ in range(num_batches):
            for _ in range(200):
                self.registry.note_skip("Ensembl")
            self.assertTrue(self.registry.is_offline("Ensembl"), "a genuinely still-down service must never un-latch")
            call_counts.append(state["n"])

        total_reprobes = call_counts[-1] - calls_before
        self.assertGreater(total_reprobes, 0, "the service must have been given at least one real re-probe attempt")
        self.assertLess(
            total_reprobes,
            num_batches,
            "a permanently-down service must stop consuming a fresh probe call every batch -- "
            "otherwise the cap is not bounding anything",
        )
        self.assertEqual(
            call_counts[-1],
            call_counts[-3],
            "once the cap is exhausted, further skip-threshold crossings must trigger no more "
            "probe calls at all -- the count must have already plateaued a couple of batches ago",
        )

        # The plateau must be a genuine, permanent stop for the rest of
        # the run, not a temporary lull: a huge further time jump plus
        # more skips must not trigger even one more probe call.
        fake_now = [1_000_000.0]
        with (
            mock.patch("utils.service_health.time.monotonic", side_effect=lambda: fake_now[0]),
            mock.patch("utils.service_health.time.time", side_effect=lambda: fake_now[0]),
        ):
            fake_now[0] += 1_000_000.0
            for _ in range(200):
                self.registry.note_skip("Ensembl")
            self.assertEqual(
                state["n"],
                call_counts[-1],
                "once exhausted, no amount of further time or skips may trigger another probe call",
            )
            self.assertTrue(self.registry.is_offline("Ensembl"))


if __name__ == "__main__":
    unittest.main()
