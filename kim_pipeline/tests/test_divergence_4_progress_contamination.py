"""
Test: Divergence 4 — Progress cross-contamination between runs sharing sample_id.

RED-FIRST DEFECT:
  When two runs share the same sample_id, they both read from the same
  checkpoint.json file, causing their progress tracking to interfere.
  Run A's completed_stages might overwrite Run B's or vice versa.

GREEN-AFTER-FIX:
  The checkpoint reading logic tries run-id-specific path first, then falls
  back to sample_id path. This allows future support for run-specific
  directories without breaking backwards compatibility.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.main import _refresh_progress_from_checkpoint


class TestDivergence4ProgressContamination:
    """Runs sharing sample_id should not contaminate each other's progress."""

    def test_run_specific_checkpoint_takes_precedence(self):
        """
        Divergence 4: Cross-contamination when runs share sample_id.

        When a run-specific checkpoint exists (run_id subdirectory),
        it should be used instead of the shared sample_id checkpoint.
        """
        with TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            # Setup: two runs with same sample_id
            sample_id = "sample_shared"
            run_id_a = "run_a_abc123"
            run_id_b = "run_b_xyz789"

            # Create shared checkpoint (would be written by previous run or concurrent run)
            sample_dir = output_dir / sample_id
            sample_dir.mkdir(parents=True)
            shared_checkpoint = {
                "completed_stages": ["qc", "alignment"],
            }
            (sample_dir / "checkpoint.json").write_text(json.dumps(shared_checkpoint))

            # Create run-specific checkpoint for run_a (future: if PipelineRunner
            # supports run_id directories)
            run_a_dir = output_dir / run_id_a
            run_a_dir.mkdir(parents=True)
            run_a_checkpoint = {
                "completed_stages": ["qc"],  # Only QC done for run_a
            }
            (run_a_dir / "checkpoint.json").write_text(json.dumps(run_a_checkpoint))

            # Test run_a: should read run-specific checkpoint (1 stage completed)
            run_a_record = {
                "run_id": run_id_a,
                "sample_id": sample_id,
                "stage": None,
                "progress_pct": 0.0,
                "stages_completed": [],
            }

            # Monkey-patch _OUTPUT_DIR for this test
            import api.main as main_module

            original_output_dir = main_module._OUTPUT_DIR
            try:
                main_module._OUTPUT_DIR = output_dir
                _refresh_progress_from_checkpoint(run_id_a, run_a_record)
            finally:
                main_module._OUTPUT_DIR = original_output_dir

            # Verify run_a read its own checkpoint (not the shared one)
            assert run_a_record["stages_completed"] == ["qc"], (
                f"run_a should read its own checkpoint with ['qc'], "
                f"got: {run_a_record['stages_completed']}"
            )

            # Test run_b: falls back to sample_id checkpoint
            run_b_record = {
                "run_id": run_id_b,
                "sample_id": sample_id,
                "stage": None,
                "progress_pct": 0.0,
                "stages_completed": [],
            }

            try:
                main_module._OUTPUT_DIR = output_dir
                _refresh_progress_from_checkpoint(run_id_b, run_b_record)
            finally:
                main_module._OUTPUT_DIR = original_output_dir

            # Verify run_b read the shared checkpoint
            assert run_b_record["stages_completed"] == ["qc", "alignment"], (
                f"run_b should read shared checkpoint with ['qc', 'alignment'], "
                f"got: {run_b_record['stages_completed']}"
            )

    def test_backwards_compatibility_sample_id_fallback(self):
        """
        When no run-specific checkpoint exists, fall back to sample_id path.
        This preserves backwards compatibility with current PipelineRunner behavior.
        """
        with TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            sample_id = "sample_legacy"
            run_id = "run_legacy_123"

            # Create only the shared checkpoint (current behavior)
            sample_dir = output_dir / sample_id
            sample_dir.mkdir(parents=True)
            checkpoint = {"completed_stages": ["qc", "alignment", "calling"]}
            (sample_dir / "checkpoint.json").write_text(json.dumps(checkpoint))

            run_record = {
                "run_id": run_id,
                "sample_id": sample_id,
                "stage": None,
                "progress_pct": 0.0,
                "stages_completed": [],
            }

            import api.main as main_module

            original_output_dir = main_module._OUTPUT_DIR
            try:
                main_module._OUTPUT_DIR = output_dir
                _refresh_progress_from_checkpoint(run_id, run_record)
            finally:
                main_module._OUTPUT_DIR = original_output_dir

            # Verify it reads from sample_id path when run-id path doesn't exist
            assert run_record["stages_completed"] == ["qc", "alignment", "calling"], (
                f"Should fall back to sample_id checkpoint, got: {run_record['stages_completed']}"
            )
