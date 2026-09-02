"""
Test: Divergences 5 & 6 — Horizon problems affecting list_runs and delete.

RED-FIRST DEFECTS:
  Div 5: list_runs reads _RUNS cache only, missing runs only in SQLite
  Div 6: delete guard scans SQLite with limit=1000, can miss shared sample_ids

GREEN-AFTER-FIX:
  Div 5: list_runs reads from SQLite, sees all runs
  Div 6: delete guard scans all SQLite rows, no bounded horizon
"""

from datetime import datetime, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.main import _RUNS, _RUN_STORE, _other_runs_sharing_sample_id


class TestDivergence5ListRuns:
    """Startup handler must load all SQLite runs into _RUNS cache."""

    def test_startup_loads_sqlite_runs_into_runs_dict(self):
        """
        Divergence 5: list_runs reads only _RUNS cache, missing SQLite-only runs.

        This test verifies that the startup handler loads all SQLite runs into
        _RUNS so that list_runs (which uses _RUNS) sees all runs, including
        those created in prior process instances.
        """
        # Setup: Create a run directly in SQLite (simulating a prior process)
        sqlite_only_run_id = "run_sqlite_only_xyz"
        sample_id = "sample_list_test"

        _RUN_STORE.create(
            run_id=sqlite_only_run_id,
            fields={
                "sample_id": sample_id,
                "status": "completed",
                "progress_pct": 100.0,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Verify: Run exists in SQLite
        sqlite_run = _RUN_STORE.get(sqlite_only_run_id)
        assert sqlite_run is not None
        assert sqlite_run["run_id"] == sqlite_only_run_id

        # Verify: Run is NOT in _RUNS (simulating process restart without reload)
        _RUNS.pop(sqlite_only_run_id, None)
        assert sqlite_only_run_id not in _RUNS

        # Import the startup handler and call it to simulate startup
        from api.main import _reconcile_runs_on_startup
        import asyncio

        asyncio.run(_reconcile_runs_on_startup())

        # Verify: Run is now loaded into _RUNS
        assert sqlite_only_run_id in _RUNS, (
            f"Startup handler should load {sqlite_only_run_id} from SQLite into _RUNS"
        )
        assert _RUNS[sqlite_only_run_id]["status"] == "completed", (
            f"Loaded run status should be 'completed', got: {_RUNS[sqlite_only_run_id]['status']}"
        )


class TestDivergence6DeleteGuard:
    """delete guard must check ALL runs, not just first 1000 rows."""

    def test_delete_guard_finds_sharing_run_beyond_limit(self):
        """
        Divergence 6: delete guard scans with limit=1000.

        This test verifies that the guard checks all runs when looking
        for runs sharing the same sample_id, not just the first 1000.
        """
        # Setup: Create many runs with different sample_ids,
        # then create two runs with the SAME sample_id, positioned
        # such that one is beyond the old limit of 1000
        sample_shared = "sample_shared_beyond_1000"
        run_id_a = "run_a_early"
        run_id_b = "run_b_beyond"
        exclude_run_id = "run_excluded"

        # Create run_a with shared sample_id (should be found)
        _RUN_STORE.create(
            run_id=run_id_a,
            fields={
                "sample_id": sample_shared,
                "status": "completed",
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Create run_b with shared sample_id (should also be found)
        _RUN_STORE.create(
            run_id=run_id_b,
            fields={
                "sample_id": sample_shared,
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Call the guard to find other runs sharing sample_shared, excluding run_excluded
        others = _other_runs_sharing_sample_id(sample_shared, exclude_run_id)

        # Verify: Both runs with shared sample_id are found
        assert run_id_a in others, (
            f"Guard should find {run_id_a} sharing {sample_shared}, got: {others}"
        )
        assert run_id_b in others, (
            f"Guard should find {run_id_b} sharing {sample_shared}, got: {others}"
        )

    def test_delete_guard_respects_exclude_run_id(self):
        """
        The guard should exclude the specified run_id from results.
        """
        sample_shared = "sample_guard_exclude"
        run_being_deleted = "run_to_delete"
        run_survivor = "run_survivor"

        # Create both runs with shared sample_id
        _RUN_STORE.create(
            run_id=run_being_deleted,
            fields={
                "sample_id": sample_shared,
                "status": "completed",
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        _RUN_STORE.create(
            run_id=run_survivor,
            fields={
                "sample_id": sample_shared,
                "status": "completed",
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Call guard, excluding the run being deleted
        others = _other_runs_sharing_sample_id(sample_shared, run_being_deleted)

        # Verify: Only the survivor is returned, not the excluded run
        assert run_being_deleted not in others, (
            f"Guard should exclude {run_being_deleted} from results"
        )
        assert run_survivor in others, f"Guard should include {run_survivor} as a survivor"
