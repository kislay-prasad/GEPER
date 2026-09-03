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

import os
import subprocess
import sys
import time

import psutil
import pytest

from ..process_control import (
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


class TestBugCSelfKillGate:
    """The specific gate required before this branch may merge (ruled
    2026-09-02, "HOLD MERGE: POSIX gate test required before shipping"):
    without `start_new_session=True` at spawn, a POSIX child inherits its
    parent's process group by default, so `os.killpg(pid, SIGTERM)` on the
    kill path would target the API SERVER's own process group -- not
    "still doesn't kill the pipeline," but "kills the server trying to do
    the killing," the first time DELETE actually worked. This is a
    self-inflicted-outage risk, not a correctness nicety, so it gets its
    own dedicated, maximally explicit test rather than relying on the
    other tests in this file proving it only by implication (a test whose
    own process got SIGTERM'd would simply abort rather than fail an
    assertion -- true, but not a proof anyone should have to reconstruct
    by reasoning about what pytest would do in that case).

    This class's own passing is the actual gate: CI runs this file on
    ubuntu-latest (.github/workflows/pytest.yml), so a green run here on
    Linux is the POSIX execution proof the merge is waiting on -- not
    Windows verification, not design-level confidence that
    start_new_session=True is correct.
    """

    def test_kill_path_kills_the_child_and_leaves_the_parent_untouched(self):
        parent_pid = os.getpid()
        parent_pgid_before = os.getpgid(parent_pid) if os.name == "posix" else None

        # Step 1: spawn a real child through the tracked machinery (not a
        # raw subprocess.Popen) -- spawn_tracked is what applies
        # start_new_session=True on POSIX / CREATE_NEW_PROCESS_GROUP on
        # Windows, which is the fix under test.
        child = spawn_tracked(_sleep_cmd(30))
        assert child.poll() is None, "child did not start running"

        if os.name == "posix":
            child_pgid = os.getpgid(child.pid)
            assert child_pgid != parent_pgid_before, (
                "child was NOT placed in its own process group -- "
                "start_new_session=True did not take effect, so a "
                "SIGTERM to the child's pgid would also hit this test "
                "runner's own process group"
            )

        # Step 2: invoke the real kill path DELETE uses (not a hand-rolled
        # substitute).
        kill_process_tree_now(child, grace_seconds=2.0)

        # Step 3: the child is actually dead.
        assert child.poll() is not None, "child process was still running after the kill path ran"

        # Step 4: the parent (this test runner process) survived --
        # checked three ways, not just "the test function kept executing"
        # (true, but the point of this test is to make that fact
        # unmissable rather than implicit):
        assert os.getpid() == parent_pid, "process identity changed unexpectedly"
        assert psutil.pid_exists(parent_pid), "parent process no longer exists"
        assert psutil.Process(parent_pid).is_running(), "parent process is not in a running state"
        if os.name == "posix":
            assert os.getpgid(parent_pid) == parent_pgid_before, (
                "parent's own process group changed -- the kill path had "
                "some side effect on the test runner's group, which is "
                "exactly the self-inflicted-outage shape this gate exists "
                "to catch"
            )
        # And the parent process is still genuinely functional, not just
        # technically alive with a pending signal -- prove it by doing
        # more real work: spawning and completing another subprocess.
        proof = spawn_tracked([sys.executable, "-c", "print('still alive')"], stdout=subprocess.PIPE, text=True)
        out, _ = proof.communicate(timeout=5)
        assert proof.returncode == 0
        assert "still alive" in out
