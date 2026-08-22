"""
INTENTIONALLY UNCOMMITTED / NOT YET GREEN.

This file tests the "bounded re-probe" feature for
`utils/service_health.py::ServiceHealthRegistry` (card
`health-probe-bounded-reprobe`, follow-up to
`ensembl-health-probe-false-negative`). That feature does NOT exist in
the shipped code yet -- only the instrumentation it depends on
(`measure_latched_services()`, covered separately and already
committed in `test_service_health.py::TestMeasureLatchedServicesIsObservationOnly`)
has landed.

WHY THIS IS SEPARATE FROM test_service_health.py: all 4 tests below
were written ahead of the implementation, per the dispatch's own
TDD framing. Three of them are DELIBERATELY RED right now and will
stay red until Kelly lands the feature -- committing them into the
main test file would put `origin/master` red with exactly the kind of
failure the repo fixed this morning (see commit 1007b99). They are
kept in this separate, clearly-named, git-ignored-by-convention file
instead so the working tree still has real test coverage on disk
(protected against a stash accident) without breaking the committed
suite.

WHAT HAS TO EXIST BEFORE THIS GOES GREEN: `ServiceHealthRegistry` (or
whatever internal helper backs `is_offline()`) needs to retain each
service's probe callable after `run_startup_checks()` latches it
offline, and internally re-invoke that same probe -- bounded to a cap
of ~3 attempts per service per run -- once EITHER N accumulated
`note_skip()` calls (N~20-25) OR T seconds elapsed (T~120) have passed
since the latch, whichever comes first. A successful re-probe must
flip `is_offline()` back to False; a still-failing re-probe must not,
and once the cap is exhausted no further probe calls may fire for that
service for the rest of the run. `note_success()`/`note_failure()`
must remain advisory-only and must never affect the latch themselves
(this is what distinguishes the agreed design, shape (a), from the
rejected shape (b) that would have piggybacked recovery on client
traffic -- see the full reasoning in the class docstring below).

DO NOT mark these `@pytest.mark.xfail` to make a suite "look" clean --
that was explicitly rejected (human + god, 2026-08-21): an xfail
nobody ever removes is the same "green test asserting nothing" shape
already fixed three times this week. Leave them red and out of the
committed suite until the feature is real, then move this file's
contents back into test_service_health.py (as
`TestBoundedReprobeAfterLatch`) and delete this file.

`test_note_success_and_note_failure_do_not_unlatch_on_their_own` is
the one test of the four that already passes today -- it pins an
invariant the fix must not violate, not a capability the fix must add.
It stays here with the other three rather than in the committed file
so the whole feature's coverage lands as one unit when the class moves
back.
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
