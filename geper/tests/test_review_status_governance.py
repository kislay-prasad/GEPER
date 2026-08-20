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
from report.summary import _ICMR_OVERRIDDEN_FOOTER_TEXT, generate_pdf
from report.summary_short import generate_short_pdf
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
        # "DRAFT" was only ever a PROXY for "not ready" -- since an
        # override now gets its own dedicated PDF footer
        # (`_ICMR_OVERRIDDEN_FOOTER_TEXT`) rather than being folded into
        # DRAFT, asserting the shared "NOT FOR PATIENT USE" substring
        # says what this test actually means (both the draft and the
        # overridden states agree the run isn't ready), and it stays
        # true regardless of which of those two states the PDF renders.
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
                self.assertIn("NOT FOR PATIENT USE", full_text)  # PDF also says "not ready", draft or overridden

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_override_after_approve_pdf_and_markdown_agree_overridden(self):
        """
        Formerly a documented, deliberately-pinned divergence: after
        approve() then override(), the PDF footer used to still
        literally read "reviewed by {physician}" (untouched by the
        override) while review_status/the Markdown banner correctly
        said "overridden" -- this test pinned that gap so it couldn't
        regress into something worse (e.g. a crash, or the PDF silently
        losing its own signature) without being noticed.

        The pin did its job: `_ICMR_OVERRIDDEN_FOOTER_TEXT` (card
        review-status-two-sources-of-truth) closes the gap as a side
        effect -- an override now gets its own PDF footer regardless of
        any prior approve(), so all three surfaces (JSON, Markdown, PDF)
        agree "overridden" instead of two of them still claiming
        "reviewed". This test now pins that agreement.
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
            self.assertIn(_ICMR_OVERRIDDEN_FOOTER_TEXT, full_text)
            self.assertNotIn("reviewed by Dr. Rajesh Sharma", full_text)  # the old divergence is gone


class TestPdfPhysicianCannotSubstituteForSignoff(unittest.TestCase):
    """
    Card `review-status-two-sources-of-truth`: today, BOTH PDF renderers
    (report/summary.py::generate_pdf, report/summary_short.py::
    generate_short_pdf) decide "DRAFT" vs "reviewed by {physician}"
    SOLELY from the `patient_meta` argument passed to them at render
    time -- a free-text field a caller can supply independently of
    review_status (the field Markdown/JSON/export_lims.py all correctly
    treat as ground truth, and which only review/signoff.py's
    approve()/override() can ever set to a non-"draft" value). This
    class calls `generate_pdf`/`generate_short_pdf` directly with a
    document and a patient_meta it fully controls -- independently of
    each other -- specifically because review/signoff.py's approve()
    always moves review_status and physician together (writing physician
    IS how approve() re-signs the PDF), which makes it impossible to
    isolate "physician present, no sign-off" or "sign-off done, no
    physician" through the CLI-level approve()/override() actions alone.
    Testing generate_pdf/generate_short_pdf's real, observable output
    text for each input combination is the only way to pin down which
    of the two inputs the renderer actually trusts.

    Fix requirement (human, verbatim): "IF NO SIGN-OFF EXISTS THE DRAFT
    FOOTER STAYS REGARDLESS OF --patient-meta." Both renderers must key
    off `review_status`, the same source Markdown/JSON already use.
    """

    def _generate_both_pdfs(self, document, patient_meta, tmp):
        full_path = os.path.join(tmp, "full.pdf")
        short_path = os.path.join(tmp, "short.pdf")
        generate_pdf(document, full_path, patient_meta=patient_meta)
        generate_short_pdf(document, short_path, patient_meta=patient_meta)
        return _all_pdf_text(full_path), _all_pdf_text(short_path)

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_physician_supplied_without_signoff_still_shows_draft(self):
        """THE DANGEROUS CASE, named by the human: a physician string is
        supplied via patient_meta, but no sign-off (approve()/override())
        has ever touched this document -- review_status is still "draft".
        The DRAFT footer must still render on every page, in BOTH PDFs,
        and the "reviewed by" claim must not appear anywhere. Expected to
        FAIL against today's unfixed code (that is the defect this card
        exists to close): both PDFs currently derive the claim from
        patient_meta alone and would affirmatively assert review that
        never happened."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)
            self.assertEqual(document["review_status"], "draft")  # sanity: never signed off

            full_text, short_text = self._generate_both_pdfs(document, {"physician": "Dr. Rajesh Sharma"}, tmp)

        for label, text in (("full", full_text), ("short", short_text)):
            self.assertIn("DRAFT", text, f"{label} PDF must still show DRAFT with no sign-off")
            self.assertNotIn(
                "reviewed by Dr. Rajesh Sharma",
                text,
                f"{label} PDF must not claim review on the strength of patient_meta alone",
            )

        # Sanity anchor: Markdown (already correct, unaffected by this
        # bug) agrees the underlying state really is "draft" -- confirms
        # the fixture itself is well-formed, not just the PDF assertions.
        md_banner = ReportGenerator().generate(document).splitlines()[2]
        self.assertIn("DRAFT", md_banner)

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_signoff_without_physician_no_longer_shows_draft(self):
        """THE INVERSE (flagged by god, not the human's direct mandate):
        a real sign-off has occurred (review_status == "reviewed") but no
        physician was supplied to patient_meta this render. Today this
        wrongly stamps DRAFT on a genuinely reviewed report -- confusing
        rather than dangerous, but it is the other half of the same bug
        and its behaviour changes under the fix. Deliberately does NOT
        assert what positive text should replace DRAFT (e.g. whether
        physician becomes pure attribution or something else) -- that
        wording is an open design question for Kelly/the human per the
        card, not yet settled; only that the false DRAFT claim on an
        actually-reviewed report must go away."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = _write_run(tmp)
            with open(os.path.join(output_dir, s.RESULTS_FILENAME), encoding="utf-8") as fh:
                document = json.load(fh)
            document["review_status"] = "reviewed"  # simulates a real prior sign-off
            document["reviewed_by"] = "Dr. Rajesh Sharma"
            document["reviewed_at"] = "2026-08-21T00:00:00+00:00"

            full_text, short_text = self._generate_both_pdfs(document, None, tmp)

        for label, text in (("full", full_text), ("short", short_text)):
            self.assertNotIn(
                "DRAFT",
                text,
                f"{label} PDF must not claim draft/not-reviewed status on a document "
                "review_status already marks reviewed",
            )

        md_banner = ReportGenerator().generate(document).splitlines()[2]
        self.assertIn("REVIEWED", md_banner)


if __name__ == "__main__":
    unittest.main()
