"""
A number in a clinical report must be a number the pipeline RECORDED.

On the human's ruling, 2026-09-11 (#18, option A): "PRINT THE NUMBER ONLY
WHEN RECORDED; a default printed as a measurement is the fabrication family."

Two sites, both rendered by BOTH output formats (Markdown via
`report_generator.py`, PDF via `summary.py`):

1. THE COMMON-VARIANT THRESHOLD. Both renderers printed
   `ipf.get("common_af_threshold", 0.01)` as "at or above the 1% threshold".
   A document that did not record a threshold still got a number, and the
   reader could not tell a recorded 1% from a defaulted one. Now: the number
   only when recorded, otherwise "the pipeline's configured threshold". And a
   recorded 0.5% is printed as 0.5%, not rounded by `:.0%` to a "0%" or "1%"
   the pipeline never used.

2. THE BLAST HOMOLOGY COUNT -- AND THIS ONE IS WORSE THAN A MISSING KEY.
   Every path on which BLAST DOES NOT RUN -- no sequence context
   (orchestrator `_run_blast_stage`), BLAST disabled / AI-only mode, and
   skip mode when no backend is usable (`blast_client._disabled_result`,
   `_skipped_result`) -- returns `{"hit_count": 0, "skipped": True}`. The
   builder kept `hit_count` and dropped `skipped`, so a search that never ran
   was printed as "BLAST: 0 homology hit(s)": a NEGATIVE RESULT for a search
   that did not happen. On an air-gapped site with no local BLAST that is
   every variant. The count key is PRESENT there, so a fix that only removed
   the `.get("hit_count", 0)` default would not have touched it.

THE NEGATIVE THAT MATTERS AS MUCH AS THE FIX: a search that RAN and found
nothing must still say "0 homology hit(s)". Suppressing a real zero would be
the same defect pointed the other way, so it is asserted here, in both
formats.
"""

import os
import tempfile
import unittest
from unittest import mock

from report.clinical_report_builder import build_clinical_report
from report.report_generator import ReportGenerator
from report.summary import generate_pdf
from tests.test_indian_population_frequency import _TGS_FOUND, _ir, _patch_threshold

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False

# gnomAD SAS AF 0.02 is above every threshold used below, so the
# "Common in Indian populations" sentence -- the one that carries the
# threshold -- is always rendered.
_GNOMAD_COMMON = {"skipped": False, "found": True, "population_breakdown": {"sas": {"af": 0.02}}}

# The three real not-run shapes, copied from where they are produced.
_BLAST_NO_SEQUENCE = {"hits": [], "hit_count": 0, "skipped": True}  # orchestrator._run_blast_stage
_BLAST_DISABLED = {"hits": [], "hit_count": 0, "skipped": True, "reason": "BLAST disabled (AI-only mode)"}
_BLAST_RAN_NOTHING = {"hits": [], "hit_count": 0, "skipped": False}
_BLAST_RAN_THREE = {"hits": [{}, {}, {}], "hit_count": 3, "skipped": False}
_BLAST_NO_COUNT = {"hits": [], "skipped": False}  # a result that never recorded a count


def _document(blast=None, threshold=0.01, drop_threshold=False):
    _patch_threshold(threshold)
    raw = {"gnomad": _GNOMAD_COMMON}
    if blast is not None:
        raw["blast"] = blast
    cr = build_clinical_report(_ir(), raw_evidence=raw, thousand_genomes_sas_result=_TGS_FOUND)
    if drop_threshold:
        # A document that does not carry the field -- e.g. one built before
        # the builder recorded it. The builder itself always records it now.
        del cr["indian_population_frequency"]["common_af_threshold"]
    return {
        "geper_version": "t",
        "generated_at": "2026-01-01T00:00:00Z",
        "input_vcf": "x.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["S1"],
        "variant_count": 1,
        "variants": [
            {
                "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
                "interpretation": {},
                "candidate_interpretation": cr,
                "ai_model_status": {},
                "errors": [],
            }
        ],
    }


def _markdown(**kw):
    return ReportGenerator().generate(_document(**kw))


def _pdf_text(**kw):
    document = _document(**kw)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "r.pdf")
        generate_pdf(document, out)
        # Collapse whitespace: ReportLab wraps lines wherever it likes.
        return " ".join("\n".join(p.extract_text() for p in PdfReader(out).pages).split())


class _Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)


class TestThresholdMarkdown(_Base):
    def test_recorded_threshold_is_printed(self):
        self.assertIn("at or above the 1% threshold", _markdown(threshold=0.01))

    def test_unrecorded_threshold_prints_no_number(self):
        md = _markdown(drop_threshold=True)
        self.assertIn("Common in Indian populations", md)
        self.assertIn("at or above the pipeline's configured threshold", md)
        self.assertNotIn("1% threshold", md)

    def test_recorded_fractional_threshold_is_not_rounded(self):
        md = _markdown(threshold=0.005)
        self.assertIn("at or above the 0.5% threshold", md)


# NO subTest LOOPS IN THIS FILE, ON PURPOSE. Under pytest 9 a test whose
# subtests ALL fail is reported as `PASSED` on its own line, with the failures
# on separate `SUBFAILED` lines -- measured on this file's first draft, where
# the two not-run cases below failed against the unfixed renderers and the
# parent test still printed PASSED. One case per method, so every verdict line
# says what happened.
class TestBlastMarkdown(_Base):
    def _assert_not_run(self, blast):
        md = _markdown(blast=blast)
        self.assertNotIn("0 homology hit(s)", md)
        self.assertIn("BLAST:** _not run for this variant", md)

    def test_no_sequence_context_is_not_printed_as_zero_hits(self):
        self._assert_not_run(_BLAST_NO_SEQUENCE)

    def test_blast_disabled_is_not_printed_as_zero_hits(self):
        self._assert_not_run(_BLAST_DISABLED)

    def test_reason_is_carried_when_recorded(self):
        self.assertIn("BLAST disabled (AI-only mode)", _markdown(blast=_BLAST_DISABLED))

    def test_unrecorded_count_prints_no_number(self):
        md = _markdown(blast=_BLAST_NO_COUNT)
        self.assertNotIn("0 homology hit(s)", md)
        self.assertIn("hit count not recorded", md)

    def test_search_that_ran_and_found_nothing_still_says_zero(self):
        self.assertIn("**BLAST:** 0 homology hit(s)", _markdown(blast=_BLAST_RAN_NOTHING))

    def test_recorded_count_is_printed(self):
        self.assertIn("**BLAST:** 3 homology hit(s)", _markdown(blast=_BLAST_RAN_THREE))


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestThresholdPdf(_Base):
    def test_recorded_threshold_is_printed(self):
        self.assertIn("at or above the 1% threshold", _pdf_text(threshold=0.01))

    def test_unrecorded_threshold_prints_no_number(self):
        text = _pdf_text(drop_threshold=True)
        self.assertIn("at or above the pipeline's configured threshold", text)
        self.assertNotIn("1% threshold", text)

    def test_recorded_fractional_threshold_is_not_rounded(self):
        self.assertIn("at or above the 0.5% threshold", _pdf_text(threshold=0.005))


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestBlastPdf(_Base):
    def _assert_not_run(self, blast):
        text = _pdf_text(blast=blast)
        self.assertNotIn("0 homology hit(s)", text)
        self.assertIn("BLAST: not run for this variant", text)

    def test_no_sequence_context_is_not_printed_as_zero_hits(self):
        self._assert_not_run(_BLAST_NO_SEQUENCE)

    def test_blast_disabled_is_not_printed_as_zero_hits(self):
        self._assert_not_run(_BLAST_DISABLED)

    def test_unrecorded_count_prints_no_number(self):
        text = _pdf_text(blast=_BLAST_NO_COUNT)
        self.assertNotIn("0 homology hit(s)", text)
        self.assertIn("hit count not recorded", text)

    def test_search_that_ran_and_found_nothing_still_says_zero(self):
        self.assertIn("BLAST: 0 homology hit(s)", _pdf_text(blast=_BLAST_RAN_NOTHING))

    def test_recorded_count_is_printed(self):
        self.assertIn("BLAST: 3 homology hit(s)", _pdf_text(blast=_BLAST_RAN_THREE))


class TestBlastConflictNote(unittest.TestCase):
    """
    The same fabrication, one layer up: `_sequence_conflict_note` wrote
    `f"{hits} homology hit(s) returned"` with `hits` defaulted to 0, into a
    ConflictItem that the Markdown report prints in its conflict table
    (report_generator.py, `c['evidence_a']['statement']`). Found while
    checking a claim about this very function in the draft commit message --
    it does not only compare `> 0`, it PRINTS the number.
    """

    @staticmethod
    def _statement(blast):
        from pipeline.conflict_resolution_engine import ConflictResolutionEngine

        return ConflictResolutionEngine._sequence_conflict_note(blast).evidence_a["statement"]

    def test_no_sequence_context_is_not_stated_as_zero_hits(self):
        s = self._statement(_BLAST_NO_SEQUENCE)
        self.assertNotIn("0 homology hit(s)", s)
        self.assertIn("not run for this variant", s)

    def test_blast_disabled_is_not_stated_as_zero_hits(self):
        s = self._statement(_BLAST_DISABLED)
        self.assertNotIn("0 homology hit(s)", s)
        self.assertIn("BLAST disabled (AI-only mode)", s)

    def test_no_blast_result_at_all_is_not_stated_as_zero_hits(self):
        s = self._statement(None)
        self.assertNotIn("0 homology hit(s)", s)
        self.assertIn("hit count not recorded", s)

    def test_unrecorded_count_is_not_stated_as_zero_hits(self):
        s = self._statement(_BLAST_NO_COUNT)
        self.assertNotIn("0 homology hit(s)", s)
        self.assertIn("hit count not recorded", s)

    def test_search_that_ran_and_found_nothing_still_says_zero(self):
        self.assertIn("0 homology hit(s) returned", self._statement(_BLAST_RAN_NOTHING))

    def test_recorded_count_is_stated(self):
        self.assertIn("3 homology hit(s) returned", self._statement(_BLAST_RAN_THREE))


if __name__ == "__main__":
    unittest.main()
