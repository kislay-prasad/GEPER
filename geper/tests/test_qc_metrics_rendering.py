"""
Tests for `report/summary.py`'s Sequencing Quality Control Metrics
table: `_parse_qc_metrics` (validates a raw `qc_metrics` input -- dict,
JSON sidecar path, or None -- into a per-metric `{"status", "value",
"reason"}` shape) and `_build_qc_flowables` (renders that validated
shape; never reads a raw number itself).

Report review round 7: this table is wired to a real upstream source
for the first time (`bridge/combined_pipeline.py`'s kim_pipeline
checkpoint translation) rather than always rendering "Not supplied".
The three states these tests pin down:
  - FOUND    -- kim_pipeline (or any caller) genuinely measured a value.
  - NOT_RUN  -- "not applicable", not a gap: either GEPER was invoked
    directly against a VCF (no upstream pipeline could have run), or a
    tool to compute this specific metric doesn't exist yet
    (bases_at_20x, permanently, this round).
  - ERROR    -- an upstream step was attempted and did not succeed.

The load-bearing guarantee under all three: no code path may ever let
a raw, unvalidated number reach `_qc_status`'s `>=` comparison. This is
what makes the B-series fabricated-PASS-on-placeholder-numbers
regression structurally impossible here, not merely avoided by
convention -- see `test_bare_number_where_dict_expected_fails_closed_never_reaches_qc_status`.
"""

import json
import unittest

from reportlab.lib import colors

from pipeline.stage_schemas import StageStatus
from report import summary as summary_module
from report.summary import _build_qc_flowables, _parse_qc_metrics, _qc_status, _qc_threshold_pass_min


def _cell_text(paragraph) -> str:
    # Paragraph.text is the raw string passed to the constructor.
    return paragraph.text


def _found(value: float, reason=None):
    return {"status": StageStatus.FOUND.value, "value": value, "reason": reason}


def _not_run(reason=None):
    return {"status": StageStatus.NOT_RUN.value, "value": None, "reason": reason}


def _error(reason=None):
    return {"status": StageStatus.ERROR.value, "value": None, "reason": reason}


class TestQCStatusBoundary(unittest.TestCase):
    """`_qc_status` must match the "Threshold (PASS >=)" column header exactly."""

    def test_above_threshold_is_pass(self):
        threshold = _qc_threshold_pass_min("mean_coverage_depth")
        self.assertEqual(_qc_status("mean_coverage_depth", threshold + 1.0), "PASS")

    def test_below_threshold_is_warning(self):
        threshold = _qc_threshold_pass_min("mean_coverage_depth")
        self.assertEqual(_qc_status("mean_coverage_depth", threshold - 1.0), "WARNING")

    def test_exactly_at_threshold_is_pass_not_warning(self):
        # The header says "Threshold (PASS >=)" -- an inclusive bound.
        # If the comparison were `>` instead of `>=`, a value exactly
        # at threshold would render WARNING while the header still
        # claims PASS at that boundary -- the off-by-one this test
        # guards against.
        for key in ("mean_coverage_depth", "bases_at_20x", "q30_score"):
            threshold = _qc_threshold_pass_min(key)
            with self.subTest(key=key):
                self.assertEqual(_qc_status(key, threshold), "PASS")


class TestParseQcMetricsThreeStates(unittest.TestCase):
    """`_parse_qc_metrics` -- the single choke point between an
    untrusted `qc_metrics` input and anything `_build_qc_flowables`
    (and therefore `_qc_status`) will ever see."""

    def test_none_is_not_run_for_all_three_with_out_of_scope_reason(self):
        parsed = _parse_qc_metrics(None)
        self.assertEqual(set(parsed.keys()), {"mean_coverage_depth", "bases_at_20x", "q30_score"})
        for key, entry in parsed.items():
            with self.subTest(key=key):
                self.assertEqual(entry["status"], StageStatus.NOT_RUN.value)
                self.assertIsNone(entry["value"])
                self.assertIn("VCF", entry["reason"])

    def test_found_entry_with_real_number_passes_through(self):
        parsed = _parse_qc_metrics({"mean_coverage_depth": _found(42.7)})
        self.assertEqual(parsed["mean_coverage_depth"], {"status": "found", "value": 42.7, "reason": None})

    def test_missing_key_in_supplied_dict_is_not_run(self):
        # A dict WAS supplied, but doesn't mention bases_at_20x at all
        # (e.g. kim's own permanent gap) -- distinct from `None` (no
        # dict at all), same NOT_RUN status, but each carries its own
        # per-key reason rather than the generic "no dict supplied" one.
        parsed = _parse_qc_metrics({"mean_coverage_depth": _found(30.0)})
        self.assertEqual(parsed["bases_at_20x"]["status"], "not_run")
        self.assertIn("upstream", parsed["bases_at_20x"]["reason"])

    def test_error_status_is_preserved(self):
        parsed = _parse_qc_metrics({"q30_score": _error("kim_pipeline's QC stage did not complete this run.")})
        self.assertEqual(parsed["q30_score"]["status"], "error")
        self.assertIsNone(parsed["q30_score"]["value"])
        self.assertIn("did not complete", parsed["q30_score"]["reason"])

    def test_missing_file_falls_back_to_not_run_never_raises(self):
        parsed = _parse_qc_metrics("/definitely/not/a/real/path/qc_metrics.json")
        for entry in parsed.values():
            self.assertEqual(entry["status"], "not_run")

    def test_corrupt_json_file_falls_back_to_not_run_never_raises(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as fh:
            fh.write("{not valid json")
            path = fh.name
        try:
            parsed = _parse_qc_metrics(path)
            for entry in parsed.values():
                self.assertEqual(entry["status"], "not_run")
        finally:
            import os

            os.unlink(path)

    def test_real_json_file_is_read_and_validated(self):
        import os
        import tempfile

        payload = {"mean_coverage_depth": _found(50.0), "q30_score": _found(91.2)}
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
            parsed = _parse_qc_metrics(path)
            self.assertEqual(parsed["mean_coverage_depth"]["value"], 50.0)
            self.assertEqual(parsed["q30_score"]["value"], 91.2)
            self.assertEqual(parsed["bases_at_20x"]["status"], "not_run")
        finally:
            os.unlink(path)

    # -- The anti-fake-PASS guarantee ------------------------------------

    def test_bare_number_where_dict_expected_fails_closed_never_reaches_qc_status(self):
        """THE regression test for the B-series fabricated-PASS bug
        class: a malformed sidecar with a bare float where the
        {status, value, reason} object belongs must fail closed to
        ERROR, never be silently coerced into a comparable number."""
        threshold = _qc_threshold_pass_min("mean_coverage_depth")
        # A bare number well above threshold -- if this were ever
        # coerced into a real value, it would render a fabricated PASS.
        parsed = _parse_qc_metrics({"mean_coverage_depth": threshold + 1000.0})
        entry = parsed["mean_coverage_depth"]
        self.assertEqual(entry["status"], "error")
        self.assertIsNone(entry["value"])

    def test_bool_value_is_rejected_even_though_bool_is_int_subclass(self):
        parsed = _parse_qc_metrics({"q30_score": {"status": "found", "value": True, "reason": None}})
        self.assertEqual(parsed["q30_score"]["status"], "error")
        self.assertIsNone(parsed["q30_score"]["value"])

    def test_found_status_with_string_value_fails_closed(self):
        parsed = _parse_qc_metrics({"q30_score": {"status": "found", "value": "93.4", "reason": None}})
        self.assertEqual(parsed["q30_score"]["status"], "error")
        self.assertIsNone(parsed["q30_score"]["value"])

    def test_found_status_with_missing_value_fails_closed(self):
        parsed = _parse_qc_metrics({"q30_score": {"status": "found", "reason": None}})
        self.assertEqual(parsed["q30_score"]["status"], "error")

    def test_unrecognized_status_string_fails_closed_to_error(self):
        parsed = _parse_qc_metrics({"mean_coverage_depth": {"status": "definitely_pass", "value": 999.0}})
        self.assertEqual(parsed["mean_coverage_depth"]["status"], "error")
        self.assertIsNone(parsed["mean_coverage_depth"]["value"])

    def test_list_instead_of_dict_at_top_level_falls_back_to_not_run(self):
        parsed = _parse_qc_metrics({"mean_coverage_depth": [42.0]})
        self.assertEqual(parsed["mean_coverage_depth"]["status"], "error")

    def test_end_to_end_bare_number_never_produces_a_pass_row(self):
        """Full pipeline: a malformed input straight into
        `_build_qc_flowables` (via `_parse_qc_metrics`) must never
        render "PASS" anywhere in the table."""
        threshold = _qc_threshold_pass_min("bases_at_20x")
        parsed = _parse_qc_metrics({"bases_at_20x": threshold + 50.0})  # bare number, not a dict
        styles = summary_module._build_stylesheet()
        flowables = _build_qc_flowables(parsed, styles)
        table = flowables[0]
        rendered_statuses = [_cell_text(row[3]) for row in table._cellvalues[1:]]
        self.assertNotIn("PASS", rendered_statuses)
        self.assertIn("ERROR", rendered_statuses)


class TestQCFlowablesRendering(unittest.TestCase):
    def setUp(self):
        self.styles = summary_module._build_stylesheet()

    def _table_rows(self, qc_metrics):
        flowables = _build_qc_flowables(qc_metrics, self.styles)
        table = flowables[0]
        return table._cellvalues  # [header, mean_coverage, bases_20x, q30]

    def _all_not_run(self):
        return {k: _not_run("out of scope this run") for k in ("mean_coverage_depth", "bases_at_20x", "q30_score")}

    def test_all_not_run_renders_not_applicable(self):
        rows = self._table_rows(self._all_not_run())
        for row in rows[1:]:
            self.assertEqual(_cell_text(row[1]), "Not applicable")
            self.assertEqual(_cell_text(row[3]), "N/A")

    def test_not_run_emits_not_applicable_footnote_naming_qc_metrics_json(self):
        flowables = _build_qc_flowables(self._all_not_run(), self.styles)
        footnote_texts = [_cell_text(f) for f in flowables[1:] if hasattr(f, "text")]
        self.assertTrue(any("Not applicable this run" in t for t in footnote_texts))
        self.assertTrue(any("qc-metrics-json" in t for t in footnote_texts))

    def test_all_found_above_threshold_all_pass(self):
        rows = self._table_rows(
            {"mean_coverage_depth": _found(999.0), "bases_at_20x": _found(100.0), "q30_score": _found(100.0)}
        )
        for row in rows[1:]:
            self.assertNotEqual(_cell_text(row[1]), "Not applicable")
            self.assertEqual(_cell_text(row[3]), "PASS")

    def test_all_found_below_threshold_all_warning(self):
        rows = self._table_rows(
            {"mean_coverage_depth": _found(0.1), "bases_at_20x": _found(0.1), "q30_score": _found(0.1)}
        )
        for row in rows[1:]:
            self.assertEqual(_cell_text(row[3]), "WARNING")

    def test_mixed_states_render_independently(self):
        # The real, expected shape of a combined-pipeline run today:
        # two real values, one permanent NOT_RUN (bases_at_20x).
        threshold_cov = _qc_threshold_pass_min("mean_coverage_depth")
        threshold_q30 = _qc_threshold_pass_min("q30_score")
        rows = self._table_rows(
            {
                "mean_coverage_depth": _found(threshold_cov + 5.0),
                "q30_score": _found(threshold_q30 + 5.0),
                "bases_at_20x": _not_run("no mosdepth/samtools-depth step exists in kim_pipeline yet."),
            }
        )
        mean_cov_row, bases_20x_row, q30_row = rows[1], rows[2], rows[3]
        self.assertEqual(_cell_text(mean_cov_row[3]), "PASS")
        self.assertEqual(_cell_text(q30_row[3]), "PASS")
        self.assertEqual(_cell_text(bases_20x_row[3]), "N/A")
        self.assertEqual(_cell_text(bases_20x_row[1]), "Not applicable")

    def test_error_renders_measurement_failed_distinctly(self):
        rows = self._table_rows(
            {
                "mean_coverage_depth": _error("samtools coverage found zero covered bases."),
                "bases_at_20x": _not_run("no tool wired yet."),
                "q30_score": _found(95.0),
            }
        )
        mean_cov_row = rows[1]
        self.assertEqual(_cell_text(mean_cov_row[1]), "Measurement failed")
        self.assertEqual(_cell_text(mean_cov_row[3]), "ERROR")

    def test_error_emits_distinct_measurement_failed_footnote(self):
        flowables = _build_qc_flowables(
            {
                "mean_coverage_depth": _error("kim_pipeline's alignment stage did not complete this run."),
                "bases_at_20x": _not_run("no tool wired yet."),
                "q30_score": _not_run("no tool wired yet."),
            },
            self.styles,
        )
        footnote_texts = [_cell_text(f) for f in flowables[1:] if hasattr(f, "text")]
        self.assertTrue(any("Measurement failed this run" in t for t in footnote_texts))
        self.assertTrue(any("did not complete this run" in t for t in footnote_texts))
        # And it must NOT be indistinguishable from the not-applicable footnote.
        self.assertTrue(any("Not applicable this run" in t for t in footnote_texts))

    def test_not_run_and_found_and_error_render_three_distinct_things(self):
        # The core three-state guarantee this round exists to build,
        # all three present in one report at once (a real combined-run
        # shape: coverage found, Q30 failed, bases_at_20x never wired).
        rows = self._table_rows(
            {
                "mean_coverage_depth": _found(35.0),
                "q30_score": _error("kim_pipeline's QC stage did not complete this run."),
                "bases_at_20x": _not_run("no tool wired yet."),
            }
        )
        cov_row, q30_row, bases_row = rows[1], rows[3], rows[2]
        result_texts = {_cell_text(cov_row[1]), _cell_text(q30_row[1]), _cell_text(bases_row[1])}
        status_texts = {_cell_text(cov_row[3]), _cell_text(q30_row[3]), _cell_text(bases_row[3])}
        # Three different Result texts, three different Status texts --
        # nothing collapses onto anything else.
        self.assertEqual(len(result_texts), 3)
        self.assertEqual(len(status_texts), 3)
        self.assertEqual(status_texts, {"PASS", "ERROR", "N/A"})

    def test_row_background_colors_distinguish_all_four_states(self):
        def bg_hex_for_row1(entry):
            flowables = _build_qc_flowables({"mean_coverage_depth": entry}, self.styles)
            table = flowables[0]
            for cmd in table._bkgrndcmds:
                if cmd[0] == "BACKGROUND" and cmd[1] == (0, 1):
                    return cmd[3].hexval()
            raise AssertionError("no BACKGROUND command found for row 1")

        threshold = _qc_threshold_pass_min("mean_coverage_depth")
        not_run_hex = bg_hex_for_row1(_not_run())
        pass_hex = bg_hex_for_row1(_found(threshold + 1.0))
        warn_hex = bg_hex_for_row1(_found(threshold - 1.0))
        error_hex = bg_hex_for_row1(_error())

        self.assertEqual(not_run_hex, colors.HexColor("#eeeeee").hexval())
        self.assertEqual(pass_hex, colors.HexColor("#e3f6e8").hexval())
        self.assertEqual(warn_hex, colors.HexColor("#fdf1d6").hexval())
        self.assertEqual(error_hex, colors.HexColor("#fbe0e0").hexval())
        # All four pairwise distinct -- no two states share a color.
        self.assertEqual(len({not_run_hex, pass_hex, warn_hex, error_hex}), 4)


if __name__ == "__main__":
    unittest.main()
