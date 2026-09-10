"""
The gnomAD score weight IS reader-visible, end to end, through the renderer.

WHY THIS FILE EXISTS. Three production comments used to record, as settled
fact, that `InterpretationEngine`'s `significance_score` was "provably unread
by every report renderer (verified by search)". A search for the FIELD NAME
had indeed been run, and it was right about the field and wrong about the
value: `legacy_pre_acmg_significance_score` is printed by no renderer, but
the number is not the score's only output. `_build_summary(...,
significance_score, ...)` turns it into `summary` and `confidence`, and
`report/report_generator.py::_render_interpretation` renders both.

    THE NUMBER IS NOT RENDERED; WHAT IT DECIDES IS.

That false comment had a cost: eight assertions in tests/test_gnomad_acmg.py
spent two weeks pinning `_gnomad_acmg_evidence`'s discarded SENTENCE -- the
half no reader can see -- while its WEIGHT, the half a clinician acts on,
could be changed without turning anything red. Prose cannot be
regression-tested, so the correction is a test as well as a comment.

THE TWO-SIDED PROPERTY THIS FILE HAS TO HAVE:
  * a change to a gnomAD WEIGHT must turn it RED, and it asserts on the
    RENDERED LINES, never on `significance_score` -- asserting on the number
    would re-create the exact gap the file exists to close;
  * a change to a gnomAD SENTENCE must leave it GREEN --
    `test_the_gnomad_sentence_itself_still_reaches_no_reader` re-reads the
    sentence at run time instead of hard-coding it, so a reword cannot make
    it pass or fail, while re-appending the sentence to a rendered surface
    would turn it red.

HOW THE FIRST HALF IS MADE TO HOLD, BECAUSE IT DOES NOT HOLD FOR FREE.
`_build_summary` is a STEP function -- four buckets, at score >= 3, >= 1,
<= -2, and everything else. A weight change is reader-visible only when it
carries the total across one of those edges, so a single fixture per branch
catches a weight change in one direction and silently tolerates the other.
The first version of this file did exactly that: it stayed green when BS1's
weight moved -2.0 -> -1.0 and green when PM2's moved 1.0 -> 0.0 -- the same
failure it was written to correct, one level up.

So each branch is asserted at TWO ClinVar baselines chosen to BRACKET an
edge: one where the branch's own weight is what lands the total on a
boundary, one where it is what keeps the total just below the next. Any
change of +/-1 to a branch's weight flips at least one of its two rendered
lines. The baselines come from `_SIGNIFICANCE_WEIGHT` (pathogenic 3, likely
pathogenic 2, uncertain significance 1) plus one AlphaMissense
likely-pathogenic bump (+1) -- ordinary classifications, not contrivances.

This file asserts nothing about whether the HIGH 3 Q4-B deletion was right.
It was the argument recorded for that deletion that was false, and those are
different claims.
"""

import unittest

from config import CONFIG
from pipeline.interpretation import InterpretationEngine
from report.report_generator import ReportGenerator

_VARIANT = {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"}
_AM_LIKELY_PATHOGENIC = {"am_class": "likely_pathogenic", "am_pathogenicity": 0.9, "available": True, "found": True}


def _clinvar(significance):
    record = {"clinical_significance": significance, "review_status": "criteria provided", "variant_match": True}
    return {"records": [record], "match_status": "matched", "primary_record": record}


def _at(af):
    return {"skipped": False, "found": True, "global_af": af, "population_breakdown": {}}


# The frequencies are DERIVED FROM `CONFIG.gnomad`'s own thresholds rather
# than written as literals: those thresholds are deployer-adjustable
# (GEPER_GNOMAD_BA1_AF and friends), so a hard-coded 0.2 would select a
# different branch under a supported config and this file would then be
# asserting about a row it did not mean.
_G = CONFIG.gnomad
_BA1 = _at(_G.BA1_AF_THRESHOLD * 2)
_BS1 = _at((_G.BS1_AF_THRESHOLD + _G.BA1_AF_THRESHOLD) / 2)
_PM2 = _at(_G.PM2_AF_THRESHOLD / 10)

# (label, gnomAD dict, ClinVar significance, AlphaMissense, rendered
# confidence). Two rows per branch, bracketing a `_build_summary` edge.
_CASES = [
    ("BA1 (-3.0) at baseline 4", _BA1, "Pathogenic", _AM_LIKELY_PATHOGENIC, "moderate"),
    ("BA1 (-3.0) at baseline 3", _BA1, "Pathogenic", None, "low"),
    ("BS1 (-2.0) at baseline 3", _BS1, "Pathogenic", None, "moderate"),
    ("BS1 (-2.0) at baseline 2", _BS1, "Likely pathogenic", None, "low"),
    ("PM2 (+1.0) at baseline 2", _PM2, "Likely pathogenic", None, "high"),
    ("PM2 (+1.0) at baseline 1", _PM2, "Uncertain significance", None, "moderate"),
]


class TestTheGnomadWeightIsReaderVisible(unittest.TestCase):
    def setUp(self):
        # Nothing is patched: the config, the engine and the renderer are
        # all real. The point of the file is the path between them.
        self.assertLess(_G.PM2_AF_THRESHOLD, _G.BS1_AF_THRESHOLD)
        self.assertLess(_G.BS1_AF_THRESHOLD, _G.BA1_AF_THRESHOLD)
        self.engine = InterpretationEngine()

    def _render(self, gnomad_result, significance="Pathogenic", alphamissense=None):
        result = self.engine.interpret(
            _VARIANT,
            [],
            _clinvar(significance),
            {},
            {},
            {},
            gnomad_result=gnomad_result,
            alphamissense_result=alphamissense,
        )
        return ReportGenerator._render_interpretation(result)

    @staticmethod
    def _line(lines, prefix):
        for line in lines:
            if line.startswith(prefix):
                return line
        return ""

    def test_each_gnomad_branch_renders_its_own_confidence_to_the_reader(self):
        for label, gnomad_result, significance, am, expected in _CASES:
            with self.subTest(case=label):
                lines = self._render(gnomad_result, significance, am)
                self.assertEqual(
                    self._line(lines, "**Confidence (legacy):**"),
                    f"**Confidence (legacy):** {expected}",
                    f"{label}: this gnomAD weight no longer reaches the rendered confidence line. "
                    f"This is the assertion the 'provably unread by every report renderer' claim "
                    f"was missing -- the score is not printed, but it decides what is.",
                )

    def test_a_stand_alone_benign_variant_does_not_render_as_suggestive_of_relevance(self):
        # The concrete harm behind the abstract one. BA1 is stand-alone
        # benign; when its weight was moved -3.0 -> -2.0 the whole gnomAD
        # suite stayed green and this variant's rendered summary became
        # "shows some evidence suggestive of clinical relevance".
        summary = self._line(self._render(_BA1), "**Summary:**")
        self.assertIn("uncertain clinical significance", summary)
        self.assertNotIn("suggestive of clinical relevance", summary)
        self.assertNotIn("consistent with pathogenicity", summary)

    def test_dropping_gnomad_entirely_changes_the_rendered_lines(self):
        # Control: the differences above are gnomAD's doing and not a
        # property of the fixture variant.
        self.assertNotEqual(
            self._line(self._render(_BA1), "**Confidence (legacy):**"),
            self._line(self._render(None), "**Confidence (legacy):**"),
        )

    def test_the_gnomad_sentence_itself_still_reaches_no_reader(self):
        # The other side of the two-sided property, and the reason this file
        # cannot be defeated by a reword: the sentence is READ AT RUN TIME
        # from the function itself rather than hard-coded here.
        for label, gnomad_result, significance, am, _ in _CASES:
            with self.subTest(case=label):
                evidence = InterpretationEngine._gnomad_acmg_evidence(gnomad_result)
                self.assertEqual(len(evidence), 1)
                rendered = "\n".join(self._render(gnomad_result, significance, am))
                self.assertNotIn(evidence[0][0], rendered)


if __name__ == "__main__":
    unittest.main()
