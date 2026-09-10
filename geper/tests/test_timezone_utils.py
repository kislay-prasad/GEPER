"""
Tests for `utils/timezone_utils.py` and its two call sites
(`report/summary.py`'s PDF "Report Generated" field,
`report/report_generator.py`'s Markdown "Generated" line).

IST (UTC+5:30) has no daylight saving, so a handful of fixed known
UTC -> IST conversions are exhaustive, deterministic ground truth --
no live call or model weight needed.
"""

import unittest
from datetime import datetime, timezone

from utils.timezone_utils import IST, format_ist, format_ist_from_iso


class TestFormatIST(unittest.TestCase):
    def test_known_utc_time_converts_correctly(self):
        # The exact example from the task: 01:43 UTC -> 07:13 IST.
        dt = datetime(2026, 7, 31, 1, 43, 0, tzinfo=timezone.utc)
        self.assertEqual(format_ist(dt), "2026-07-31 07:13 IST")

    def test_labeled_as_ist_not_utc(self):
        dt = datetime(2026, 7, 31, 1, 43, 0, tzinfo=timezone.utc)
        result = format_ist(dt)
        self.assertIn("IST", result)
        self.assertNotIn("UTC", result)

    def test_day_rollover_forward(self):
        # 20:00 UTC + 5:30 crosses midnight into the next day.
        dt = datetime(2026, 7, 30, 20, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(format_ist(dt), "2026-07-31 01:30 IST")

    def test_naive_datetime_assumed_utc(self):
        # No tzinfo -- must be treated as UTC (this codebase's own
        # convention), not the local system timezone.
        dt = datetime(2026, 7, 31, 1, 43, 0)
        self.assertEqual(format_ist(dt), "2026-07-31 07:13 IST")

    def test_offset_is_exactly_5_30_no_dst(self):
        self.assertEqual(IST.utcoffset(None).total_seconds(), 5.5 * 3600)


class TestFormatISTFromISO(unittest.TestCase):
    def test_real_iso_timestamp(self):
        self.assertEqual(format_ist_from_iso("2026-07-31T00:00:00+00:00"), "2026-07-31 05:30 IST")

    def test_naive_iso_string_assumed_utc(self):
        self.assertEqual(format_ist_from_iso("2026-07-31T01:43:00"), "2026-07-31 07:13 IST")

    def test_none_returns_placeholder_not_crash(self):
        self.assertEqual(format_ist_from_iso(None), "Not available")

    def test_empty_string_returns_placeholder_not_crash(self):
        self.assertEqual(format_ist_from_iso(""), "Not available")

    def test_unparseable_placeholder_returned_unchanged(self):
        # Real fixture pattern used elsewhere in this test suite
        # (tests/test_report_consistency.py uses `"generated_at": "now"`)
        # -- must not crash report generation over a display nicety.
        self.assertEqual(format_ist_from_iso("now"), "now")


class TestReportIntegration(unittest.TestCase):
    """Confirms both real call sites actually use the IST formatter, not just that the formatter itself works."""

    def test_report_generator_markdown_shows_ist_not_raw_utc(self):
        from report.report_generator import ReportGenerator

        doc = {
            "generated_at": "2026-07-31T01:43:00+00:00",
            "input_vcf": "test.vcf",
            "variant_count": 0,
            "variants": [],
        }
        markdown = ReportGenerator().generate(doc)
        self.assertIn("2026-07-31 07:13 IST", markdown)
        self.assertNotIn("2026-07-31T01:43:00+00:00", markdown)

    def test_pdf_header_uses_ist_label(self):
        from report import summary as summary_module

        styles = summary_module._build_stylesheet()
        table = summary_module._build_patient_header_table(
            {"deidentified": True},
            "SAMPLE01",
            "RUN01",
            "GRCh38",
            styles,
        )
        # Last row is "Report Generated" -- its value cell must carry the IST label.
        label_cell, value_cell = table._cellvalues[-1]
        self.assertEqual(label_cell.text, "Report Generated")
        self.assertIn("IST", value_cell.text)
        self.assertNotIn("UTC", value_cell.text)


if __name__ == "__main__":
    unittest.main()
