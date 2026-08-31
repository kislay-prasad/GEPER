"""
FIX #10 follow-up, report-path split: `capture_ensembl_release` and
`capture_blast_local_tool_versions` (pipeline/provenance.py:868, :893)
build their failure result with a bare `str(exc)`. `str(exc)` is the
empty string for any exception raised with no message, so a genuine
capture failure returns `{"error": ""}`. Same mechanism as FIX #10
(commit 8df8b9f) and its precedent d1128ee; matches that shape:

    detail = str(exc) or type(exc).__name__

WHAT THIS RENDERS TO A READER TODAY (the reason this was split out from
the other 21 sibling sites, rather than swept with them): these two are
the only two of the 23 whose `error` field reaches a rendered report.

  ENSEMBL (pipeline/orchestrator.py:511): unlike the dbSNP/ClinVar
  defect this is NOT a truthiness guard that makes the record vanish --
  the gate is on `version`, not `error`, so `VersionStatus.UNKNOWN` is
  recorded correctly either way. The empty string instead lands inside
  an f-string that is unconditionally non-empty
  (`f"Could not reach Ensembl's /info/data endpoint: {error}"`), so
  `report/report_generator.py::_render_provenance`'s `if record.get(
  "notes")` gate always passes and the record always renders -- but
  with a garbled, unfinished-looking line:

      - _Could not reach Ensembl's /info/data endpoint: _

  (trailing colon-space, nothing after it). Post-fix it reads e.g.:

      - _Could not reach Ensembl's /info/data endpoint: ExternalAPIError_

  `report/summary.py::_build_provenance_flowables` (the PDF) never
  reads `notes` at all -- this defect has NO visible effect on the PDF
  today, only on the Markdown report and the raw JSON provenance list
  (`report/json_builder.py`'s `"provenance"` key, the same list both
  renderers consume).

  BLAST (pipeline/orchestrator.py:519-527): `blast.get("error")` is
  never referenced anywhere -- the notes text on a version-unknown
  BLAST record is a fixed string that does not mention the error at
  all. Fixing this site changes the internal dict value's correctness
  (worth doing, matches the class) but currently changes nothing a
  reader sees; confirmed by grep, no consumer reads this dict's
  `error` key.

WHAT THIS FILE ASSERTS
  A. Both capture functions: a no-arg exception must not produce an
     empty `error` string (red-first).
  B. A real message is still reported verbatim (control).
  C. End-to-end for Ensembl only (the one with a reader-visible
     effect): the exact Markdown line rendered by `_render_provenance`
     before and after, proving the "endpoint: _" garble is gone.
"""

import unittest
from unittest import mock

from pipeline.provenance import capture_blast_local_tool_versions, capture_ensembl_release
from report.report_generator import ReportGenerator


class TestCaptureEnsemblReleaseNoArgExceptionDoesNotProduceEmptyError(unittest.TestCase):
    def test_no_arg_exception_error_field_is_not_empty(self):
        with mock.patch("requests.get", side_effect=Exception()):
            result = capture_ensembl_release()
        self.assertIsNone(result["version"])
        self.assertNotEqual(
            result["error"],
            "",
            "a no-argument exception must not collapse capture_ensembl_release's error to '' -- "
            "it is interpolated directly into the reader-facing provenance notes line",
        )

    def test_a_real_message_is_still_reported_verbatim(self):
        with mock.patch("requests.get", side_effect=Exception("connection refused")):
            result = capture_ensembl_release()
        self.assertEqual(result["error"], "connection refused")


class TestCaptureBlastLocalToolVersionsNoArgExceptionDoesNotProduceEmptyError(unittest.TestCase):
    def test_no_arg_exception_error_field_is_not_empty(self):
        with mock.patch("database.blast_client.get_blast_tool_versions", side_effect=Exception()):
            result = capture_blast_local_tool_versions()
        self.assertIsNone(result["version"])
        self.assertNotEqual(result["error"], "")

    def test_a_real_message_is_still_reported_verbatim(self):
        with mock.patch("database.blast_client.get_blast_tool_versions", side_effect=Exception("permission denied")):
            result = capture_blast_local_tool_versions()
        self.assertEqual(result["error"], "permission denied")


class TestEnsemblRenderedMarkdownLineIsNoLongerGarbled(unittest.TestCase):
    """End-to-end: the exact line `_render_provenance` writes for an
    Ensembl capture that failed with a no-argument exception."""

    def _rendered_ensembl_note(self, error_value):
        document = {
            "provenance": [
                {
                    "source": "Ensembl",
                    "status": "unknown",
                    "endpoint": "https://rest.ensembl.org",
                    "notes": f"Could not reach Ensembl's /info/data endpoint: {error_value}",
                }
            ]
        }
        lines = ReportGenerator._render_provenance(document)
        note_lines = [line for line in lines if line.strip().startswith("- _Could not reach")]
        self.assertEqual(len(note_lines), 1)
        return note_lines[0]

    def test_pre_fix_shape_renders_a_trailing_empty_colon(self):
        """Documents the defect: this is what a reader sees TODAY,
        pre-fix, when the underlying exception has no message."""
        line = self._rendered_ensembl_note("")
        self.assertEqual(line.strip(), "- _Could not reach Ensembl's /info/data endpoint: _")

    def test_post_fix_shape_names_the_exception_type(self):
        """What the same line reads once orchestrator.py's `notes=`
        f-string is fed the fixed `detail` (`str(exc) or type(exc).
        __name__`) instead of a bare possibly-empty `str(exc)`."""
        line = self._rendered_ensembl_note("ExternalAPIError")
        self.assertEqual(line.strip(), "- _Could not reach Ensembl's /info/data endpoint: ExternalAPIError_")


if __name__ == "__main__":
    unittest.main()
