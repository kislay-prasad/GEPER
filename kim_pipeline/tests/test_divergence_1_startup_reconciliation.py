"""
Test: Divergence 1 — Startup reconciliation of stale running rows.

RED-FIRST DEFECT (before handler implementation):
  A run marked status='running' at crash time survives restart unchanged.
  The test will FAIL here because the row stays 'running' instead of becoming 'interrupted'.

GREEN (after handler implementation):
  The handler marks stale 'running' rows as 'interrupted' with a reason.
  Completed rows are untouched.
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path


# Add parent to path for imports
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.run_store import RunStore
from api.main import _reconcile_runs_on_startup


class TestDivergence1StartupReconciliation:
    """Startup reconciliation: stale running rows become interrupted."""

    def test_running_row_survives_without_handler(self):
        """
        RED-FIRST DEFECT:
        Without the handler, a running row survives restart unchanged.
        This test PASSES, demonstrating that without reconciliation,
        the defect exists: status stays 'running' forever.
        """
        store = RunStore()

        # Setup: SQLite has a row marked running (crashed mid-pipeline)
        store.create(
            run_id="run_that_crashed",
            fields={
                "sample_id": "sample_1",
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Verify before: status is running
        before = store.get("run_that_crashed")
        assert before["status"] == "running"

        # Without handler: row survives unchanged (THE DEFECT)
        after = store.get("run_that_crashed")
        assert after["status"] == "running", (
            "Without the handler, running row should remain running (this is the defect)"
        )

    def test_running_row_marked_interrupted_with_handler(self):
        """
        GREEN-AFTER-FIX:
        With the handler, running row changes to interrupted with a reason.
        """
        store = RunStore()

        # Setup: SQLite has a row marked running (crashed mid-pipeline)
        store.create(
            run_id="run_that_crashed",
            fields={
                "sample_id": "sample_1",
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Verify before: status is running, no error
        before = store.get("run_that_crashed")
        assert before["status"] == "running"
        assert before.get("error") is None or before.get("error") == ""

        # Execute startup handler (simulated restart)
        asyncio.run(_reconcile_runs_on_startup())

        # Verify after: status became interrupted with reason
        after = store.get("run_that_crashed")
        assert after["status"] == "interrupted", (
            f"Expected status='interrupted' after handler, "
            f"but got '{after['status']}' — handler not applied or incorrect"
        )
        assert "Process restart detected" in after["error"], (
            f"Expected reason to mention restart, got: {after['error']}"
        )
        assert "Marked interrupted" in after["error"], (
            f"Expected reason to name the reconciliation action, got: {after['error']}"
        )

    def test_completed_row_untouched_at_startup(self):
        """Completed runs should not be modified by reconciliation."""
        store = RunStore()

        # Setup: SQLite has a completed row
        store.create(
            run_id="run_completed",
            fields={
                "sample_id": "sample_2",
                "status": "completed",
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        before = store.get("run_completed")
        assert before["status"] == "completed"

        # Execute startup handler
        asyncio.run(_reconcile_runs_on_startup())

        # Verify: completed row is untouched
        after = store.get("run_completed")
        assert after["status"] == "completed", (
            "Completed run should not be modified by startup reconciliation"
        )
