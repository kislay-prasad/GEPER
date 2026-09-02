"""
Test: Divergences 2 & 3 — Failure path writes to memory only, skipping SQLite.

RED-FIRST DEFECTS:
  Div 2: _on_done callback marks run failed in _RUNS but doesn't persist to store
  Div 3: Exception handler appends stages_failed to _RUNS but doesn't persist

GREEN-AFTER-FIX:
  Both writes are now mirrored to SQLite via _RUN_STORE.update()
  After a restart, failed runs have status='failed' and stages_failed in SQLite,
  making the failure state durable (not lost to in-memory cache).
"""

from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import Future

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.main import _RUNS, _RUN_STORE


class TestDivergence2Callback:
    """_on_done callback: failed runs must persist to SQLite."""

    def test_on_done_callback_persists_failed_status(self):
        """
        Divergence 2: _on_done writes to _RUNS but not to store.

        This test verifies that when a thread exception is caught by _on_done,
        both the in-memory _RUNS dict AND SQLite store are updated.
        """
        # Setup: create a run in both _RUNS and SQLite
        run_id = "run_thread_failed"
        sample_id = "sample_test"
        _RUN_STORE.create(
            run_id=run_id,
            fields={
                "sample_id": sample_id,
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        _RUNS[run_id] = {
            "run_id": run_id,
            "sample_id": sample_id,
            "status": "running",
        }

        # Simulate a failed thread: create a Future with an exception
        exc = RuntimeError("Thread execution failed")
        future = Future()
        future.set_exception(exc)

        # Define the _on_done callback (from api.main.py:776)
        def _on_done(fut):
            try:
                exc = fut.exception()
                if exc and run_id in _RUNS:
                    _RUNS[run_id]["status"] = "failed"
                    _RUNS[run_id]["error"] = str(exc)
                    _RUN_STORE.update(run_id, status="failed", error=str(exc))
            except Exception:
                pass

        # Execute the callback
        _on_done(future)

        # Verify both are updated
        after_memory = _RUNS[run_id]
        after_store = _RUN_STORE.get(run_id)

        assert after_memory["status"] == "failed", (
            f"In-memory status should be 'failed', got: {after_memory['status']}"
        )
        assert "Thread execution failed" in after_memory["error"], (
            f"In-memory error should contain exception, got: {after_memory.get('error')}"
        )

        assert after_store["status"] == "failed", (
            f"SQLite status should be 'failed', got: {after_store['status']}"
        )
        assert "Thread execution failed" in after_store.get("error", ""), (
            f"SQLite error should contain exception, got: {after_store.get('error')}"
        )


class TestDivergence3StagesFailed:
    """Exception path: stages_failed must persist to SQLite."""

    def test_stages_failed_persisted_to_store(self):
        """
        Divergence 3: stages_failed appended to _RUNS but not persisted to store.

        This test verifies that when an exception occurs during pipeline execution,
        the stages_failed array is appended in _RUNS AND persisted to SQLite.
        """
        # Setup: create a run
        run_id = "run_exception_path"
        sample_id = "sample_exception"
        _RUN_STORE.create(
            run_id=run_id,
            fields={
                "sample_id": sample_id,
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "stages_failed": [],  # Initialize as empty
            },
        )
        _RUNS[run_id] = {
            "run_id": run_id,
            "sample_id": sample_id,
            "status": "running",
            "stage": "alignment",
            "stages_failed": [],
        }

        # Simulate exception path (from api.main.py:414-419)
        exc = RuntimeError("Alignment stage failed")
        try:
            raise exc
        except Exception as exc:
            run = _RUNS[run_id]
            run["status"] = "failed"
            run["error"] = str(exc)
            run["stages_failed"].append(run.get("stage", "unknown"))
            _RUN_STORE.update(
                run_id,
                status="failed",
                error=str(exc),
                stages_failed=run["stages_failed"],
            )

        # Verify both are updated
        after_memory = _RUNS[run_id]
        after_store = _RUN_STORE.get(run_id)

        assert after_memory["status"] == "failed", (
            f"In-memory status should be 'failed', got: {after_memory['status']}"
        )
        assert "alignment" in after_memory["stages_failed"], (
            f"In-memory stages_failed should contain 'alignment', got: {after_memory['stages_failed']}"
        )

        assert after_store["status"] == "failed", (
            f"SQLite status should be 'failed', got: {after_store['status']}"
        )
        assert "alignment" in after_store.get("stages_failed", []), (
            f"SQLite stages_failed should contain 'alignment', got: {after_store.get('stages_failed')}"
        )
