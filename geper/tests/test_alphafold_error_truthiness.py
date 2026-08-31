"""
FIX #1: AlphaFold `error` field read by truthiness at three live sites,
same defect class as Pattern A (8ff4d39, 12 other instances across the
codebase, all fixed to `is not None`). An exception with an empty
message (`str(exc) == ""`) leaves `error` PRESENT but falsy, so
`not alphafold_result.get("error")` reads it as ABSENT and the
consumer proceeds as though the lookup succeeded.

Scope, per Meredith's verification: five sites were originally named,
only three are live --
  pipeline/confidence_engine.py::_structural_quality
  pipeline/interpretation.py::InterpretationEngine._biological_context_evidence
  report/report_generator.py::ReportGenerator._render_alphafold
orchestrator.py already used `is not None`; clinical_report_builder.py
gates on `found`, not `error`, at all. Neither is touched here or
tested here -- they were never broken.

Each test below constructs an `alphafold_result` with `error: ""`
(present, empty) and `found: True` (so the ONLY thing distinguishing
"this lookup actually failed" from "this lookup actually succeeded"
is whether the site reads `error` by truthiness or by presence) --
the exact case a truthiness check cannot tell apart from "no error".
A test built around `error: None` would pass on both the broken and
the fixed code and prove nothing; a test that instead makes the call
raise (Finding 5/7 trap) would fail with an AttributeError from a
changed signature, which only proves the signature changed, not that
the truthiness defect was fixed. Neither is used here.
"""

import unittest

from pipeline.confidence_engine import ConfidenceEngine
from pipeline.interpretation import InterpretationEngine
from report.report_generator import ReportGenerator


def _empty_error_result():
    return {
        "skipped": False,
        "error": "",
        "found": True,
        "affected_residue_band": "confident",
        "protein_position": 123,
    }


class TestAlphaFoldEmptyStringErrorIsNotReadAsSuccess(unittest.TestCase):
    def test_confidence_engine_structural_quality_does_not_score_a_failed_lookup(self):
        """
        Before this fix: `not alphafold_result.get("error")` was True for
        `error=""`, so `_structural_quality` fell into the success branch
        and fabricated a full presence=1.0/quality=0.75 contribution from
        a lookup that actually failed. Confirmed against the unmodified
        module by a direct probe before any edit was made:

            presence: 1.0 quality: 0.75 contribution: 0.75
            rationale: AlphaFold DB structure available (confidence band: 'confident').

        A failed lookup must score as absent evidence (presence 0,
        contribution 0), same as a lookup that found nothing.
        """
        score = ConfidenceEngine._structural_quality(_empty_error_result(), weight=1.0)
        self.assertEqual(score.presence, 0.0)
        self.assertEqual(score.quality, 0.0)
        self.assertEqual(score.contribution, 0.0)
        self.assertNotIn("AlphaFold DB", score.sources_checked)

    def test_interpretation_biological_context_evidence_does_not_state_a_failed_lookups_band(self):
        """
        Before this fix: the same truthiness gate let
        `_biological_context_evidence` write a confident-sounding prose
        sentence naming a specific pLDDT confidence band, for a query
        that actually errored. Confirmed against the unmodified module:

            ["AlphaFold DB: predicted structural confidence (pLDDT) at
            residue 123 (transcript-verified) is 'confident'."]

        A failed lookup must contribute no descriptive text at all (same
        as this function's own documented behavior for skipped/erroring/
        not-found sources).
        """
        lines = InterpretationEngine._biological_context_evidence(alphafold_result=_empty_error_result())
        self.assertEqual(lines, [])

    def test_report_generator_render_alphafold_shows_the_failure_not_a_data_panel(self):
        """
        Before this fix: `_render_alphafold` skipped the "query failed"
        status line entirely (its own `if alphafold_result.get("error"):`
        was also False for `error=""`) and rendered straight through to
        the found-data panel -- model version, PDB link, mean pLDDT --
        for a query that never actually returned that data. Confirmed
        against the unmodified module: the rendered lines contained
        "Mean pLDDT: n/a (structure file not fetched)" with no "Status:"
        line anywhere, indistinguishable from "we asked and there is
        genuinely no structure file" rather than "the query errored".
        """
        lines = ReportGenerator._render_alphafold(_empty_error_result())
        text = "\n".join(lines)
        self.assertIn("query failed", text)
        self.assertNotIn("Mean pLDDT:", text)


if __name__ == "__main__":
    unittest.main()
