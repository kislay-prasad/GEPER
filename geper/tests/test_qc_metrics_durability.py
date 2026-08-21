"""
Tests for QC-metrics durability across review/signoff.py re-renders
(two related cards, landed together, tested together per the
dispatch):

CARD 1: review/signoff.py's approve()/override() now read
document["qc_metrics"] and pass it through to generate_pdf() when
re-rendering after sign-off -- previously they re-rendered with NO
qc_metrics argument at all, regardless of what the original run had.

CARD 2: report/json_builder.py now stores qc_metrics on the document
itself (not only ever as a transient generate_pdf() argument), so a
later re-render (days later, from disk, with no access to the
original --qc-metrics-json file) can still recover it.

THE DANGEROUS CASE, named explicitly in the dispatch: a run WITH real
qc_metrics, once approved/overridden, must NOT render the "no QC
observed" text -- that text is a POSITIVE, specific claim ("GEPER
never ran or observed any upstream sequencing/alignment step") that
becomes FALSE the moment a run that DID have QC gets signed off and
silently re-rendered without it. Asserting its absence specifically
(not just asserting the real QC table's presence) is what actually
catches the regression -- a renderer could show the real QC table AND
also, by accident, leave stale/duplicate no-QC text somewhere else.

Written against OBSERVABLE OUTPUT: the actual rendered PDF text after
a real approve()/override() call, never against internals. Reuses
tests/test_signoff.py's own fixtures rather than a second hand-rolled
one, same reasoning that module's own docstring gives.
"""

import json
import os
import tempfile
import unittest

from report.json_builder import JSONResultBuilder
from review import signoff as s
from tests.test_signoff import _all_pdf_text, _make_document

try:
    from pypdf import PdfReader  # noqa: F401

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False

# The exact reason text `_parse_qc_metrics` stamps on every metric when
# no --qc-metrics-json was ever supplied -- a POSITIVE claim, not a
# gap, and the thing this whole card exists to stop a signed-off,
# QC-carrying run from asserting.
_NO_QC_OBSERVED_TEXT = "GEPER never ran or observed any upstream sequencing/alignment step"

_REAL_QC_METRICS = {
    "mean_coverage_depth": {"status": "found", "value": 42.5, "reason": None},
    "bases_at_20x": {"status": "found", "value": 96.0, "reason": None},
    "q30_score": {"status": "found", "value": 93.0, "reason": None},
}


def _write_run_with_qc_metrics(root: str, name: str = "run1") -> str:
    """Same shape as tests/test_signoff.py::_write_run, plus a real
    qc_metrics block on the document -- the dangerous-case fixture."""
    document = _make_document()
    document["qc_metrics"] = _REAL_QC_METRICS
    output_dir = os.path.join(root, name)
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, s.RESULTS_FILENAME), "w", encoding="utf-8") as fh:
        json.dump(document, fh)
    return output_dir


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestCard1SignoffThreadsQcMetricsThrough(unittest.TestCase):
    """Card 1, isolated from card 2: the document ALREADY carries
    qc_metrics (however it got there) -- does approve()/override()
    actually read and forward it to generate_pdf()? Bypasses
    JSONResultBuilder entirely by hand-writing the document, so this
    turns green as soon as card 1 alone lands, independent of card 2's
    state."""

    def test_approve_renders_real_qc_not_the_no_qc_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run_with_qc_metrics(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            full_text = _all_pdf_text(os.path.join(output_dir, s.FULL_PDF_FILENAME))

        self.assertNotIn(_NO_QC_OBSERVED_TEXT, full_text, "signed PDF still claims no upstream QC was ever observed")
        self.assertIn("42.5", full_text)  # mean_coverage_depth's real value
        self.assertIn("96", full_text)  # bases_at_20x's real value
        self.assertIn("93", full_text)  # q30_score's real value

    def test_override_renders_real_qc_not_the_no_qc_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run_with_qc_metrics(tmp)
            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")
            full_text = _all_pdf_text(os.path.join(output_dir, s.FULL_PDF_FILENAME))

        self.assertNotIn(
            _NO_QC_OBSERVED_TEXT, full_text, "overridden PDF still claims no upstream QC was ever observed"
        )
        self.assertIn("42.5", full_text)


class TestCard2JsonBuilderStoresQcMetrics(unittest.TestCase):
    """Card 2, isolated from card 1 and from any renderer: does
    JSONResultBuilder actually put qc_metrics on the built document at
    all? No signoff.py, no PDF rendering."""

    def test_qc_metrics_stored_verbatim_on_built_document(self):
        document = JSONResultBuilder(input_vcf_path="x.vcf", qc_metrics=_REAL_QC_METRICS).build()
        self.assertIn("qc_metrics", document)
        self.assertEqual(document["qc_metrics"], _REAL_QC_METRICS)

    def test_qc_metrics_defaults_to_none_not_fabricated(self):
        document = JSONResultBuilder(input_vcf_path="x.vcf").build()
        self.assertIn("qc_metrics", document)
        self.assertIsNone(document["qc_metrics"])


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestEndToEndBothCardsTogether(unittest.TestCase):
    """The integration case the dispatch specifically asks to be
    verified RED at two checkpoints: unfixed, AND with card 1 alone
    (before card 2 lands). Goes through the REAL JSONResultBuilder,
    unlike the card-1-isolated tests above -- this is the one that
    stays red if card 2 hasn't landed, because qc_metrics never reaches
    the document in the first place without it, regardless of whether
    signoff.py already knows how to forward it."""

    def test_real_run_with_qc_survives_approve_intact(self):
        builder = JSONResultBuilder(input_vcf_path="e2e_qc_test.vcf", qc_metrics=_REAL_QC_METRICS)
        document = builder.build()
        # Reuse the same 3-variant fixture's variants so the rest of
        # the PDF renders normally -- only qc_metrics is under test here.
        document["variants"] = _make_document()["variants"]
        document["variant_count"] = len(document["variants"])
        document["review_status"] = "draft"
        document["reviewed_by"] = None
        document["reviewed_at"] = None

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = os.path.join(tmp, "run1")
            os.makedirs(output_dir, exist_ok=True)
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), "w", encoding="utf-8") as fh:
                json.dump(document, fh)

            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            full_text = _all_pdf_text(os.path.join(output_dir, s.FULL_PDF_FILENAME))

        self.assertNotIn(_NO_QC_OBSERVED_TEXT, full_text)
        self.assertIn("42.5", full_text)


if __name__ == "__main__":
    unittest.main()
