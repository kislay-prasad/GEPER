"""
Round 30 part 2: review-status governance controls end to end --
`report/json_builder.py`'s `review_status` field,
`review/signoff.py::approve()`/`override()` actually writing it into
`geper_results.json`, `report/export_lims.py`'s hard export gate, and
`report/report_generator.py`'s Markdown banner.

Reuses `tests/test_signoff.py`'s own synthetic 3-variant fixture
(`_make_document`/`_write_run`) rather than a second hand-rolled one --
same reasoning that module's own docstring already gives for using
`build_clinical_report()` output instead of a partial dict: this
exercises the real `approve()`/`override()` code paths against a
document shaped exactly like what those functions actually read.

No model weights, no network, no `pipeline.orchestrator` import.
"""

import json
import os
import tempfile
import unittest

from report.export_lims import export_lims_json
from report.json_builder import JSONResultBuilder
from report.report_generator import ReportGenerator
from review import signoff as s
from tests.test_signoff import _all_pdf_text, _write_run
from utils.exceptions import LIMSExportBlockedError

try:
    from pypdf import PdfReader  # noqa: F401

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False


class TestJSONBuilderDefaultsToDraft(unittest.TestCase):
    def test_fresh_document_is_draft_with_no_reviewer(self):
        doc = JSONResultBuilder(input_vcf_path="x.vcf").build()
        self.assertEqual(doc["review_status"], "draft")
        self.assertIsNone(doc["reviewed_by"])
        self.assertIsNone(doc["reviewed_at"])


class TestApproveWritesReviewStatus(unittest.TestCase):
    def test_approve_rewrites_results_json_with_reviewed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)
            self.assertEqual(document["review_status"], "reviewed")
            self.assertIn("Dr. Rajesh Sharma", document["reviewed_by"])
            self.assertIsNotNone(document["reviewed_at"])


class TestOverrideWritesOverriddenStatus(unittest.TestCase):
    def test_override_alone_sets_overridden_not_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)
            self.assertEqual(document["review_status"], "overridden")

    def test_override_after_approve_sets_overridden_not_reviewed(self):
        # The explicit governance decision this round: an override
        # ALWAYS re-blocks export, even if the run was previously
        # approved -- the prior sign-off no longer covers the CURRENT
        # (changed) content.
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)
            self.assertEqual(document["review_status"], "overridden")

    def test_approve_after_override_re_reviews(self):
        # The correct way to reach an export-eligible state again after
        # an override: a fresh approve() call.
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)
            self.assertEqual(document["review_status"], "reviewed")


class TestLimsGateReflectsSignoffActions(unittest.TestCase):
    """Test 1 & 2 (governance, not just field presence): a real
    approve()/override() run through signoff.py, then a real LIMS
    export attempt against the exact geper_results.json that produced."""

    def test_freshly_written_run_cannot_be_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)
            export_path = os.path.join(tmp, "lims_export.json")
            with self.assertRaises(LIMSExportBlockedError):
                export_lims_json(document, export_path)
            self.assertFalse(os.path.exists(export_path))
            with open(os.path.join(tmp, "geper_signoff_audit.log"), encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual(lines[0]["action"], "export_blocked")

    def test_approved_run_can_be_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)
            export_path = os.path.join(tmp, "lims_export.json")
            export_lims_json(document, export_path)
            self.assertTrue(os.path.exists(export_path))

    def test_overridden_after_approve_cannot_be_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)
            export_path = os.path.join(tmp, "lims_export.json")
            with self.assertRaises(LIMSExportBlockedError):
                export_lims_json(document, export_path)


class TestPdfMarkdownReviewStatusConsistency(unittest.TestCase):
    """
    Test 3: PDF, Markdown, and `review_status` must agree on the same
    underlying state, for every transition this round's governance
    controls can actually reach WITHOUT the one documented, deliberate
    exception (see `review/signoff.py::override()`'s docstring,
    "KNOWN, DELIBERATELY UNRESOLVED TENSION"): overriding an
    already-approved run leaves the PDF footer saying "reviewed by
    {physician}" (untouched, per this round's "sign-off legal weight
    stays PDF-only" decision) while `review_status`/the Markdown banner
    correctly flip to "overridden". That specific sequence is asserted
    separately below (`test_override_after_approve_kn_own_pdf_md_divergence`)
    as a documented, regression-guarded exception, not silently treated
    as "consistent".
    """

    def _md_banner(self, document):
        return ReportGenerator().generate(document).splitlines()[2]

    def test_freshly_written_run_is_draft_everywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)
            self.assertEqual(document["review_status"], "draft")
            self.assertIn("DRAFT", self._md_banner(document))
            self.assertIn("AWAITING CLINICAL REVIEW", self._md_banner(document))

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_approved_run_is_reviewed_everywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)

            self.assertEqual(document["review_status"], "reviewed")
            banner = self._md_banner(document)
            self.assertIn("REVIEWED", banner)
            self.assertIn("Dr. Rajesh Sharma", banner)
            self.assertNotIn("DRAFT", banner)
            self.assertNotIn("OVERRIDDEN", banner)

            full_text = _all_pdf_text(os.path.join(output_dir, s.FULL_PDF_FILENAME))
            self.assertIn("reviewed by Dr. Rajesh Sharma", full_text)
            self.assertNotIn("DRAFT", full_text)

    def test_approved_run_json_and_markdown_agree_even_without_pypdf(self):
        # Same "reviewed" case as above, but the JSON<->Markdown half
        # only -- runnable in any environment, regardless of whether
        # pypdf is installed to also check the PDF's own text.
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)
            self.assertEqual(document["review_status"], "reviewed")
            banner = self._md_banner(document)
            self.assertIn("REVIEWED", banner)
            self.assertIn("Dr. Rajesh Sharma", banner)
            self.assertNotIn("DRAFT", banner)

    def test_overridden_without_prior_approval_agrees_not_ready_everywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)

            self.assertEqual(document["review_status"], "overridden")
            banner = self._md_banner(document)
            self.assertIn("OVERRIDDEN", banner)
            self.assertIn("NOT FOR PATIENT USE", banner)  # agrees with PDF's own "not ready" claim below

            if _PYPDF_AVAILABLE:
                full_text = _all_pdf_text(os.path.join(output_dir, s.FULL_PDF_FILENAME))
                self.assertIn("DRAFT", full_text)  # physician was never set -- PDF also says "not ready"

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_override_after_approve_known_pdf_markdown_divergence(self):
        """
        Documented exception, not a silent gap: after approve() then
        override(), the PDF footer (untouched -- "sign-off legal weight
        stays PDF-only") still literally reads "reviewed by {physician}",
        while review_status/the Markdown banner correctly say
        "overridden". Both facts are individually true (that physician
        DID review a prior state; the CURRENT state is overridden and
        not yet re-reviewed) -- this test pins the known divergence so
        it cannot regress into something worse (e.g. a crash, or the
        PDF silently losing its own signature) without being noticed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            s.approve(output_dir, "Dr. Rajesh Sharma", "MCI-12345", "AIIMS Delhi")
            s.override(output_dir, "2:500:G>T", "Likely Pathogenic", "Family history", "rajesh.sharma@aiims.edu")
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)

            self.assertEqual(document["review_status"], "overridden")
            self.assertIn("OVERRIDDEN", self._md_banner(document))

            full_text = _all_pdf_text(os.path.join(output_dir, s.FULL_PDF_FILENAME))
            self.assertIn("reviewed by Dr. Rajesh Sharma", full_text)  # the known divergence


if __name__ == "__main__":
    unittest.main()
