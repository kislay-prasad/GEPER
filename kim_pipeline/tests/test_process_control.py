"""
tests/test_process_control.py
────────────────────────────────
Covers pipeline/utils/process_control.py -- the fix for "DELETE
/api/v1/pipeline/{run_id} doesn't actually kill anything on Windows" (hive
dispatch 2026-09-02).

Before this module existed, two stacked bugs made cancellation impossible:
  Bug A: PipelineRunner.register_kill_callback()'s callback was never
         invoked anywhere -- every pipeline subprocess ran through the
         blocking subprocess.run(), which never exposed a live Popen to
         anything outside itself.
  Bug B: even if Bug A were fixed, os.getpgid/os.killpg don't exist on
         Windows at all.
  Bug C: no spawn site set start_new_session/creationflags, so a fixed
         Bug A+B would plausibly SIGTERM the API server's own process
         group on POSIX (a child inherits its parent's pgid by default).

These tests exercise real subprocesses (not mocks) end-to-end on whichever
platform CI runs on, so a regression in any of the three shows up as an
actual hung/undead process, not a passing mock assertion.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.utils.process_control import (
    _CURRENT_KILL_CALLBACK,
    kill_process_tree_now,
    request_termination_async,
    spawn_tracked,
)


def _sleep_cmd(seconds: float) -> list:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


class TestSpawnTracked:
    def test_spawns_a_real_running_process(self):
        proc = spawn_tracked(_sleep_cmd(5))
        try:
            assert proc.poll() is None
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_notifies_the_registered_kill_callback_with_the_live_popen(self):
        """This is Bug A's fix: spawn_tracked must hand the Popen to
        whatever PipelineRunner.run() registered via the contextvar, with
        no signature changes required anywhere between them."""
        captured = []
        token = _CURRENT_KILL_CALLBACK.set(lambda proc: captured.append(proc))
        try:
            proc = spawn_tracked(_sleep_cmd(5))
            try:
                assert len(captured) == 1
                assert captured[0] is proc
            finally:
                proc.kill()
                proc.wait(timeout=5)
        finally:
            _CURRENT_KILL_CALLBACK.reset(token)

    def test_no_callback_registered_still_spawns_normally(self):
        """Outside an active PipelineRunner.run() call (e.g. a script or a
        test), _CURRENT_KILL_CALLBACK is None -- spawning must not raise or
        require a callback to be registered."""
        token = _CURRENT_KILL_CALLBACK.set(None)
        try:
            proc = spawn_tracked(_sleep_cmd(0.1))
            proc.wait(timeout=5)
            assert proc.returncode == 0
        finally:
            _CURRENT_KILL_CALLBACK.reset(token)

    def test_callback_raising_does_not_prevent_the_spawn(self):
        """A bug in the API layer's callback must not take down the
        pipeline stage that's trying to spawn a legitimate subprocess."""

        def _bad_callback(proc):
            raise RuntimeError("boom")

        token = _CURRENT_KILL_CALLBACK.set(_bad_callback)
        try:
            proc = spawn_tracked(_sleep_cmd(0.1))
            proc.wait(timeout=5)
            assert proc.returncode == 0
        finally:
            _CURRENT_KILL_CALLBACK.reset(token)


class TestRequestTerminationAsync:
    def test_returns_immediately_without_waiting_for_the_grace_period(self):
        """DELETE must not block the HTTP response for the grace window --
        this is what makes the kill non-blocking from the caller's side."""
        proc = spawn_tracked(_sleep_cmd(10))
        try:
            t0 = time.time()
            request_termination_async(proc, grace_seconds=3.0)
            elapsed = time.time() - t0
            assert elapsed < 1.0, f"request_termination_async blocked for {elapsed:.2f}s"
        finally:
            proc.wait(timeout=10)

    def test_process_is_actually_dead_after_the_escalation_window(self):
        """The end-to-end proof the whole fix exists for: a real process,
        genuinely killed, on whichever platform this test runs on."""
        proc = spawn_tracked(_sleep_cmd(60))
        assert proc.poll() is None

        request_termination_async(proc, grace_seconds=1.0)
        # Wait past the grace period + escalation for the daemon thread to
        # do its work.
        deadline = time.time() + 5.0
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)

        assert proc.poll() is not None, "process was still running after termination + grace period"

    def test_kills_a_real_grandchild_process_too(self):
        """Neither a bare os.killpg (if the child itself called setsid) nor
        a Windows TerminateProcess/CTRL_BREAK_EVENT reaches grandchildren --
        this is what psutil.children(recursive=True) closes."""
        script = (
            "import subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(child.pid, flush=True); "
            "time.sleep(60)"
        )
        proc = spawn_tracked([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        grandchild_pid = int(proc.stdout.readline().strip())
        assert psutil.pid_exists(grandchild_pid)

        request_termination_async(proc, grace_seconds=1.0)

        deadline = time.time() + 5.0
        while (proc.poll() is None or psutil.pid_exists(grandchild_pid)) and time.time() < deadline:
            time.sleep(0.1)

        assert proc.poll() is not None, "parent was still running"
        assert not psutil.pid_exists(grandchild_pid), "grandchild was still running"


class TestPipelineRunnerSetsTheContextvar:
    def test_run_sets_current_kill_callback_from_registered_callback(self, tmp_path):
        """The other end of Bug A's fix: PipelineRunner.run() must actually
        set _CURRENT_KILL_CALLBACK to whatever register_kill_callback()
        was given, before any stage runs -- this is what makes it visible
        to _run()/spawn_tracked() no matter how deep the call stack that
        eventually spawns a subprocess (alignment, variant calling,
        reference-index building, ...).

        Short-circuits real pipeline execution by making config validation
        itself the probe: it's the first thing run() calls after setting
        the contextvar, well before any real FASTQ/reference/subprocess
        work would be needed.
        """
        import pipeline.orchestration.runner as runner_mod

        observed = {}

        def _probe_validate_config(cfg):
            observed["callback"] = _CURRENT_KILL_CALLBACK.get()
            raise RuntimeError("stop here -- probe only, not a real run")

        registered_callback = lambda proc: None  # noqa: E731

        runner = runner_mod.PipelineRunner(cfg={})
        runner.register_kill_callback(registered_callback)

        original = runner_mod.validate_config
        runner_mod.validate_config = _probe_validate_config
        try:
            with pytest.raises(RuntimeError, match="probe only"):
                runner.run(
                    fastq_r1=str(tmp_path / "r1.fastq"),
                    reference_fasta=str(tmp_path / "ref.fasta"),
                    output_dir=str(tmp_path / "out"),
                    sample_id="CTXVARTEST",
                )
        finally:
            runner_mod.validate_config = original

        assert observed.get("callback") is registered_callback

    def test_run_refuses_to_proceed_without_a_registered_callback(self, tmp_path):
        """ "Silence must fail, not pass silently" (ruled 2026-09-02): the
        original defect was a callback that was never invoked, with
        nothing to say so. run() must not repeat that shape by silently
        proceeding when nobody registered a callback at all -- that would
        just move Bug A's silence from "callback never fires" to "callback
        never got registered," which is the same failure from the
        caller's point of view: an unkillable run nobody was told about.
        """
        import pipeline.orchestration.runner as runner_mod

        runner = runner_mod.PipelineRunner(cfg={})
        # Deliberately NOT calling register_kill_callback().

        with pytest.raises(RuntimeError, match="register_kill_callback"):
            runner.run(
                fastq_r1=str(tmp_path / "r1.fastq"),
                reference_fasta=str(tmp_path / "ref.fasta"),
                output_dir=str(tmp_path / "out"),
                sample_id="UNREGISTEREDTEST",
            )

    def test_no_kill_tracking_is_an_explicit_opt_out_not_a_default(self, tmp_path):
        """A caller that genuinely doesn't need cancellation (CLI/script
        use, no DELETE endpoint) must say so by name -- NO_KILL_TRACKING
        -- rather than run() silently tolerating an unregistered callback
        for everyone. Confirms passing it clears the refusal path (proven
        via the same validate_config probe as the registered-callback
        test above, so this doesn't require a real pipeline run either)."""
        import pipeline.orchestration.runner as runner_mod

        observed = {}

        def _probe_validate_config(cfg):
            observed["callback"] = _CURRENT_KILL_CALLBACK.get()
            raise RuntimeError("stop here -- probe only, not a real run")

        runner = runner_mod.PipelineRunner(cfg={})
        runner.register_kill_callback(runner_mod.NO_KILL_TRACKING)

        original = runner_mod.validate_config
        runner_mod.validate_config = _probe_validate_config
        try:
            with pytest.raises(RuntimeError, match="probe only"):
                runner.run(
                    fastq_r1=str(tmp_path / "r1.fastq"),
                    reference_fasta=str(tmp_path / "ref.fasta"),
                    output_dir=str(tmp_path / "out"),
                    sample_id="NOKILLTRACKINGTEST",
                )
        finally:
            runner_mod.validate_config = original

        assert observed.get("callback") is runner_mod.NO_KILL_TRACKING
        # And it's genuinely inert when actually invoked.
        assert runner_mod.NO_KILL_TRACKING(object()) is None


class TestKillProcessTreeNow:
    def test_synchronous_kill_of_a_real_process(self):
        proc = spawn_tracked(_sleep_cmd(60))
        assert proc.poll() is None

        kill_process_tree_now(proc, grace_seconds=1.0)

        assert proc.poll() is not None

    @pytest.mark.parametrize("grace_seconds", [1.0])
    def test_does_not_raise_on_an_already_dead_process(self, grace_seconds):
        """DELETE racing against a run finishing naturally between stages
        must not turn a harmless no-op into a 500."""
        proc = spawn_tracked(_sleep_cmd(0.1))
        proc.wait(timeout=5)
        assert proc.poll() is not None

        kill_process_tree_now(proc, grace_seconds=grace_seconds)  # must not raise
