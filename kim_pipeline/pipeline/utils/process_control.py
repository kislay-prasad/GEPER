"""
pipeline/utils/process_control.py
──────────────────────────────────
Cross-platform subprocess lifecycle tracking, so a pipeline run started via
the API can actually be cancelled mid-flight on both POSIX and Windows.

Background (see hive dispatch 2026-09-02, "Windows process killing"):
``api/main.py``'s ``DELETE /api/v1/pipeline/{run_id}`` was supposed to kill
the running external tool (bwa, freebayes, ...) via ``PipelineRunner
.register_kill_callback()``, but that callback was never actually invoked
anywhere — every pipeline subprocess ran through ``pipeline/fastq/errors.py
::_run()``, which used the blocking ``subprocess.run()`` and never exposed
a live ``Popen`` handle to anything outside itself. Cancellation was
structurally impossible, not merely unwired. On top of that, nothing spawned
children with ``start_new_session``/``creationflags``, so had a kill signal
ever been sent, ``os.killpg()`` on POSIX would have targeted the API
server's own process group (children inherit their parent's pgid by
default) rather than a group scoped to the child.

This module is the single place that:

1. Spawns every pipeline subprocess (``spawn_tracked``), giving it its own
   killable scope (POSIX: new session/process group; Windows: new process
   group) and, when a run is in progress, handing the live ``Popen`` object
   to whatever callback ``PipelineRunner.run()`` registered via the
   ``_CURRENT_KILL_CALLBACK`` contextvar — so ``pipeline/fastq/errors.py``,
   ``pipeline/utils/reference_cache.py``, and any future caller get
   cancellability for free, with no signature changes to any of their own
   callers.
2. Terminates a tracked ``Popen`` and its full descendant tree
   (``kill_process_tree_now`` / ``request_termination_async``), using
   ``psutil`` to reach grandchildren neither ``os.killpg`` (POSIX, if a
   child itself calls ``setsid``) nor a bare Windows ``TerminateProcess``
   (which never reaches children at all) can.

Why a contextvar instead of threading a callback parameter through every
call site: ``PipelineRunner.run()`` executes entirely inside one dedicated
thread-pool worker thread per run_id (see ``api/main.py``'s
``ThreadPoolExecutor``), so a contextvar set once at the top of ``run()``
is visible to every ``_run()``/``spawn_tracked()`` call made anywhere
below it in that same call stack — including through ``resolve_reference``
→ ``ensure_bwa_index`` → ``_stream_subprocess_with_heartbeat`` — without
any of those 8+ intermediate functions needing to know tracking exists.
Concurrent runs never cross-talk: each worker thread has its own
contextvar value.
"""

from __future__ import annotations

import contextvars
import logging
import os
import signal
import subprocess
import threading
from typing import Callable, List, Optional

import psutil

logger = logging.getLogger("geper.pipeline.utils.process_control")

# The kill callback PipelineRunner.run() registers for the duration of one
# run, read by spawn_tracked() every time a new subprocess starts. None
# outside an active run() call (or for a stage run standalone, e.g. in a
# script/test, where no run() ever set it) -- spawn_tracked() just skips
# the notification in that case; the process is still spawned normally.
_CURRENT_KILL_CALLBACK: contextvars.ContextVar[Optional[Callable[[subprocess.Popen], None]]] = (
    contextvars.ContextVar("_CURRENT_KILL_CALLBACK", default=None)
)


def spawn_tracked(cmd: List[str], **popen_kwargs) -> subprocess.Popen:
    """``subprocess.Popen(cmd, **popen_kwargs)``, plus killable-scope setup
    and kill-callback notification.

    Every pipeline subprocess should be spawned through this function
    (directly, or via ``_run()``/``_stream_subprocess_with_heartbeat``)
    rather than calling ``subprocess.Popen``/``subprocess.run`` directly --
    that's what makes it reachable by DELETE.
    """
    if os.name == "posix":
        popen_kwargs.setdefault("start_new_session", True)
    else:
        popen_kwargs.setdefault("creationflags", subprocess.CREATE_NEW_PROCESS_GROUP)

    proc = subprocess.Popen(cmd, **popen_kwargs)

    callback = _CURRENT_KILL_CALLBACK.get()
    if callback is not None:
        try:
            callback(proc)
        except Exception:
            logger.debug("kill-tracking callback raised; ignoring", exc_info=True)

    return proc


def _graceful_stop(proc: subprocess.Popen) -> None:
    """Best-effort graceful stop: SIGTERM to the process group (POSIX,
    valid because spawn_tracked used start_new_session=True) or
    CTRL_BREAK_EVENT (Windows, valid because spawn_tracked used
    CREATE_NEW_PROCESS_GROUP). Never raises."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
    except (ProcessLookupError, OSError):
        pass


def _hard_kill(proc: subprocess.Popen) -> None:
    """Force-kill proc (and, on POSIX, its process group). Never raises."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _kill_children(children: List["psutil.Process"], grace_seconds: float) -> None:
    """Terminate already-collected descendant processes, escalating to
    kill() for anything still alive after grace_seconds. Grandchildren
    exist because a tool the pipeline shells out to (bwa, samtools, ...)
    itself forked further children -- os.killpg only reaches them if they
    stayed in the same POSIX process group, and a Windows
    TerminateProcess/CTRL_BREAK_EVENT never reaches them at all."""
    for child in children:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    _gone, alive = psutil.wait_procs(children, timeout=grace_seconds)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass


def kill_process_tree_now(proc: subprocess.Popen, grace_seconds: float = 5.0) -> None:
    """Synchronous: graceful stop, wait up to grace_seconds, hard-kill if
    still alive, plus the same for any descendant processes. Blocks the
    caller for up to ~2 * grace_seconds in the worst case.

    Used by ``_run()``'s own timeout handling, where the caller is already
    blocked waiting for this command and a bounded synchronous kill is the
    correct behavior (there is nothing else to do concurrently).
    """
    try:
        children = psutil.Process(proc.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        children = []

    _graceful_stop(proc)
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        _hard_kill(proc)

    _kill_children(children, grace_seconds)


def request_termination_async(proc: subprocess.Popen, grace_seconds: float = 5.0) -> None:
    """Fire a graceful stop now; escalate to a hard kill on a daemon thread
    if proc is still alive after grace_seconds, without blocking the
    caller. Used by DELETE /api/v1/pipeline/{run_id}, which must return its
    HTTP response immediately rather than block on the grace period.
    """
    try:
        children = psutil.Process(proc.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        children = []

    _graceful_stop(proc)

    def _escalate() -> None:
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            _hard_kill(proc)
        _kill_children(children, grace_seconds)

    threading.Thread(target=_escalate, daemon=True).start()
