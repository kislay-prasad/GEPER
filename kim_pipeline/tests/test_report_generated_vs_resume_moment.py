"""
DEFECT-kim-reporting-stage-658-STAMPS-THE-RESUME-MOMENT-and-kim-HAS-A-REAL-CHECKPOINT-RESUME-PATH-
GEPER-does-not.

`ReportingStage.run()` computed `datetime.now(timezone.utc)` fresh on every call and threaded it
into both the JSON `generated_at` field and the HTML/PDF "Generated:" label -- and kim has a real
checkpoint/resume path (`PipelineRunner`) that can feed a genuinely old, previously-completed
analysis into a reporting stage that only executes on the resumed run. A run interrupted after
QC/alignment/calling/annotation and resumed days later stamped the report with the resume moment,
not the analysis.

Ruled 2026-09-11 (#17, kim half): BOTH (a) read a persisted value with a "Not available" marker
fallback, AND (b) relabel any field that genuinely means "when this file was rendered", AND kim
starts persisting a real analysis-start timestamp (previously the pipeline persisted no such value
at all -- this is a new capability, not just a read-fix).
"""

import unittest
from datetime import datetime, timezone

from pipeline.orchestration.runner import _ensure_analysis_started_at
from pipeline.reporting.stage import ReportingStage


class TestEnsureAnalysisStartedAtIsSetOnceAndKeptOnResume(unittest.TestCase):
    """Unit-level: the checkpoint helper itself, same pattern as this file's
    neighbouring TestCheckpoint class in test_pipeline_orchestration.py."""

    def test_first_run_sets_it(self):
        checkpoint = {}
        value = _ensure_analysis_started_at(checkpoint)
        self.assertEqual(checkpoint["analysis_started_at"], value)
        # A real, parseable ISO timestamp close to now -- not a marker, not empty.
        datetime.fromisoformat(value)

    def test_resume_keeps_the_original_value_not_the_resume_moment(self):
        """The exact defect shape: a checkpoint loaded on RESUME already
        carries an old analysis_started_at from days ago -- it must survive
        unchanged, not be overwritten with today's date."""
        old_value = "2025-01-01T00:00:00+00:00"
        checkpoint = {
            "analysis_started_at": old_value,
            "completed_stages": ["fastq_validation", "alignment"],
        }
        result = _ensure_analysis_started_at(checkpoint)
        self.assertEqual(result, old_value)
        self.assertEqual(checkpoint["analysis_started_at"], old_value)

    def test_calling_it_twice_on_a_fresh_checkpoint_is_idempotent(self):
        checkpoint = {}
        first = _ensure_analysis_started_at(checkpoint)
        second = _ensure_analysis_started_at(checkpoint)
        self.assertEqual(first, second)


class TestReportingStageReadsThePersistedAnalysisTime(unittest.TestCase):
    """Real ReportingStage.run(), real files written to a tmp dir -- not just
    reading the source. Mirrors the shape already proven for GEPER's PDFs."""

    def _run(self, tmp_path, analysis_started_at):
        stage = ReportingStage({"reporting": {"output_dir": str(tmp_path)}})
        return stage.run(
            sample_id="S01",
            output_dir=str(tmp_path),
            analysis_started_at=analysis_started_at,
        )

    def test_old_analysis_time_appears_in_json_not_todays_date(self):
        import json as jsonlib
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            self._run(Path(tmp), "2025-01-01T00:00:00+00:00")
            payload = jsonlib.loads((Path(tmp) / "report.json").read_text())
        self.assertEqual(payload["generated_at"], "2025-01-01T00:00:00+00:00")

    def test_missing_analysis_time_is_a_marker_not_todays_date(self):
        import json as jsonlib
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            self._run(Path(tmp), None)
            payload = jsonlib.loads((Path(tmp) / "report.json").read_text())
        self.assertEqual(payload["generated_at"], "Not available")

    def test_html_generated_shows_the_old_analysis_date_and_report_rendered_shows_today(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            self._run(Path(tmp), "2025-01-01T00:00:00+00:00")
            html = (Path(tmp) / "report.html").read_text()
        self.assertIn("Generated: 2025-01-01 00:00 UTC", html)
        self.assertIn("Report Rendered:", html)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertIn(today, html)

    def test_html_generated_shows_marker_when_no_analysis_time_persisted(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            self._run(Path(tmp), None)
            html = (Path(tmp) / "report.html").read_text()
        self.assertIn("Generated: Not available", html)
        # The render moment is still real even when the analysis time isn't known.
        self.assertIn("Report Rendered:", html)


if __name__ == "__main__":
    unittest.main()
