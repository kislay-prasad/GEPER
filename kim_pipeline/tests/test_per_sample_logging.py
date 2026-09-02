"""
tests/test_per_sample_logging.py
──────────────────────────────────
Tests for Task 2: per-sample pipeline.log FileHandler.

Verifies:
- pipeline.log is created in work_dir after a mocked run
- FileHandler is removed from geper logger after run() returns
- log_path is set on PipelineResult
- Mock all stages so no real binaries are needed
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.orchestration.runner import NO_KILL_TRACKING, PipelineRunner, PipelineResult


def _make_mock_stages():
    """Return a dict of patches that mock all pipeline stages."""
    return [
        patch("pipeline.orchestration.runner.validate_config"),
        patch("pipeline.orchestration.runner.FastqValidator"),
        patch("pipeline.orchestration.runner.QCStage"),
        patch("pipeline.orchestration.runner.AlignmentStage"),
        patch("pipeline.orchestration.runner.VariantCallingStage"),
        patch("pipeline.orchestration.runner.AnnotationStage"),
        patch("pipeline.orchestration.runner.ReportingStage"),
    ]


def _run_mocked_pipeline(output_dir: str, sample_id: str = "TEST01") -> PipelineResult:
    """Run PipelineRunner with all stages mocked."""
    patches = _make_mock_stages()
    mocks = [p.start() for p in patches]

    try:
        # Configure mock return values
        (
            mock_validate,
            mock_fq_cls,
            mock_qc_cls,
            mock_align_cls,
            mock_vc_cls,
            mock_ann_cls,
            mock_rep_cls,
        ) = mocks

        # FastqValidator
        fq_stats = MagicMock()
        fq_stats.to_dict.return_value = {"reads": 1000}
        mock_fq_cls.return_value.validate_single.return_value = fq_stats

        # QCStage
        qc_result = MagicMock()
        qc_result.metrics_r1.to_dict.return_value = {"qc": "ok"}
        qc_result.metrics_r2 = None
        qc_result.report_json_path = str(Path(output_dir) / sample_id / "qc" / "qc.json")
        qc_result.report_html_path = str(Path(output_dir) / sample_id / "qc" / "qc.html")
        qc_result.qc_passed = True
        mock_qc_cls.return_value.run.return_value = qc_result

        # AlignmentStage
        bam_path = str(Path(output_dir) / sample_id / "alignment" / "sorted.bam")
        align_result = MagicMock()
        align_result.to_dict.return_value = {"sorted_bam_path": bam_path}
        mock_align_cls.return_value.run.return_value = align_result

        # VariantCallingStage
        vcf_path = str(Path(output_dir) / sample_id / "variant_calling" / "filtered.vcf")
        vc_result = MagicMock()
        vc_result.to_dict.return_value = {"filtered_vcf_path": vcf_path}
        mock_vc_cls.return_value.run.return_value = vc_result

        # AnnotationStage
        ann_result = MagicMock()
        ann_result.to_dict.return_value = {"variants": []}
        mock_ann_cls.return_value.run.return_value = ann_result

        # ReportingStage
        rep_result = MagicMock()
        rep_result.to_dict.return_value = {"json": "report.json", "html": "report.html"}
        mock_rep_cls.return_value.run.return_value = rep_result

        # Also mock ACMG imports
        with patch.dict(
            "sys.modules",
            {
                "pipeline.acmg": MagicMock(),
                "pipeline.acmg.classifier": MagicMock(),
                "pipeline.evidence": MagicMock(),
                "pipeline.evidence.aggregator": MagicMock(),
                "pipeline.hotspot": MagicMock(),
                "pipeline.hotspot.lookup": MagicMock(),
            },
        ):
            runner = PipelineRunner(cfg={}, resume=False)
            runner.register_kill_callback(NO_KILL_TRACKING)
            result = runner.run(
                fastq_r1="/fake/r1.fastq",
                reference_fasta="/fake/ref.fa",
                output_dir=output_dir,
                sample_id=sample_id,
            )
    finally:
        for p in patches:
            p.stop()

    return result


class TestPerSampleLogFile(unittest.TestCase):
    """pipeline.log is created in work_dir after a mocked run."""

    def test_log_file_exists_after_run(self):
        """pipeline.log must be present in work_dir after run() returns."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _run_mocked_pipeline(tmpdir, sample_id="S001")
            log_file = Path(tmpdir) / "S001" / "pipeline.log"
            self.assertTrue(
                log_file.exists(),
                f"Expected pipeline.log at {log_file}",
            )

    def test_log_file_contains_output(self):
        """pipeline.log should contain log messages from the run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _run_mocked_pipeline(tmpdir, sample_id="S002")
            log_file = Path(tmpdir) / "S002" / "pipeline.log"
            content = log_file.read_text(errors="replace")
            # At minimum the START/END markers should be logged
            self.assertTrue(len(content) > 0, "Log file should not be empty")


class TestHandlerCleanup(unittest.TestCase):
    """FileHandler is removed from geper logger after run() returns."""

    def test_handler_count_unchanged_after_run(self):
        """Handler count on geper logger should be same before and after run."""
        geper_logger = logging.getLogger("geper")
        handler_count_before = len(geper_logger.handlers)

        with tempfile.TemporaryDirectory() as tmpdir:
            _run_mocked_pipeline(tmpdir, sample_id="S003")

        handler_count_after = len(geper_logger.handlers)
        self.assertEqual(
            handler_count_before,
            handler_count_after,
            f"Handler leak: {handler_count_before} before vs {handler_count_after} after",
        )

    def test_no_filehandler_on_geper_logger_after_run(self):
        """No FileHandler pointing to pipeline.log should remain on geper logger."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _run_mocked_pipeline(tmpdir, sample_id="S004")
            geper_logger = logging.getLogger("geper")
            pipeline_log = str(Path(tmpdir) / "S004" / "pipeline.log")
            for h in geper_logger.handlers:
                if isinstance(h, logging.FileHandler):
                    self.assertNotEqual(
                        h.baseFilename,
                        pipeline_log,
                        "FileHandler for pipeline.log still attached to geper logger",
                    )

    def test_handler_cleaned_up_on_exception(self):
        """Handler is removed even if the pipeline raises an exception."""
        geper_logger = logging.getLogger("geper")
        handler_count_before = len(geper_logger.handlers)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "pipeline.orchestration.runner.validate_config", side_effect=RuntimeError("boom")
            ):
                try:
                    runner = PipelineRunner(cfg={}, resume=False)
                    runner.register_kill_callback(NO_KILL_TRACKING)
                    runner.run(
                        fastq_r1="/fake/r1.fastq",
                        reference_fasta="/fake/ref.fa",
                        output_dir=tmpdir,
                        sample_id="S005",
                    )
                except Exception:
                    pass

        handler_count_after = len(geper_logger.handlers)
        self.assertEqual(handler_count_before, handler_count_after)


class TestLogPathOnResult(unittest.TestCase):
    """log_path should be set on PipelineResult."""

    def test_log_path_is_set(self):
        """result.log_path should be a non-empty string pointing to pipeline.log."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _run_mocked_pipeline(tmpdir, sample_id="S006")
            self.assertIsNotNone(result.log_path)
            self.assertTrue(len(result.log_path) > 0, "log_path should not be empty")

    def test_log_path_points_to_existing_file(self):
        """result.log_path should point to a real file that exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _run_mocked_pipeline(tmpdir, sample_id="S007")
            self.assertTrue(
                Path(result.log_path).exists(),
                f"result.log_path={result.log_path!r} does not exist",
            )

    def test_log_path_in_work_dir(self):
        """log_path should be inside work_dir/sample_id/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _run_mocked_pipeline(tmpdir, sample_id="S008")
            expected_prefix = str(Path(tmpdir) / "S008")
            self.assertTrue(
                result.log_path.startswith(expected_prefix),
                f"log_path {result.log_path!r} not under {expected_prefix!r}",
            )

    def test_log_path_filename_is_pipeline_log(self):
        """The file should be named 'pipeline.log'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _run_mocked_pipeline(tmpdir, sample_id="S009")
            self.assertEqual(Path(result.log_path).name, "pipeline.log")

    def test_log_path_field_on_dataclass(self):
        """PipelineResult dataclass should have log_path field defaulting to empty string."""
        pr = PipelineResult()
        self.assertTrue(hasattr(pr, "log_path"))
        self.assertEqual(pr.log_path, "")


if __name__ == "__main__":
    unittest.main()
