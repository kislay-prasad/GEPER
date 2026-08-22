"""
Tests for the header-logo feature in `report/summary.py::generate_pdf`.

Generates real, small PDFs with ReportLab (lightweight, not a model
load -- safe to run locally) and inspects the actual PDF page content
via pypdf, so these are genuine rendering checks, not mocked-out unit
tests of the flowable-builder functions alone.
"""

import os
import tempfile
import unittest

from report import summary as summary_module
from report.summary import _build_report_header, _resolve_logo_path, generate_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False


_REAL_LOGO = os.path.join(os.path.dirname(__file__), "..", "assets", "logo.png")

_MINIMAL_DOCUMENT = {
    "geper_version": "test",
    "generated_at": "2026-07-31T00:00:00+00:00",
    "input_vcf": "test.vcf",
    "assembly": "GRCh38",
    "vcf_samples": ["SAMPLE01"],
    "variant_count": 1,
    "variants": [
        {
            "variant": {"chrom": "17", "pos": 43106534, "ref": "C", "alt": "A"},
            "candidate_interpretation": {
                "executive_summary": "Test executive summary for logo smoke test.",
                "acmg_classification": {"classification": "Pathogenic", "triggered_criteria": []},
                "confidence": {"pending": False, "label": "High"},
                "supporting_evidence": [],
                "limitations": [],
            },
        }
    ],
}


def _make_multi_page_document(n_variants: int) -> dict:
    doc = dict(_MINIMAL_DOCUMENT)
    doc["variants"] = [
        {
            "variant": {"chrom": "17", "pos": 43106534 + i, "ref": "C", "alt": "A"},
            "candidate_interpretation": {
                "executive_summary": "Padding finding " + str(i) + " " + ("lorem ipsum dolor sit amet. " * 20),
                "acmg_classification": {"classification": "Uncertain significance", "triggered_criteria": []},
                "confidence": {"pending": False, "label": "Low"},
                "supporting_evidence": [f"Evidence line {j}" for j in range(6)],
                "limitations": [f"Limitation line {j}" for j in range(4)],
            },
        }
        for i in range(n_variants)
    ]
    doc["variant_count"] = n_variants
    return doc


class TestResolveLogoPath(unittest.TestCase):
    def test_explicit_path_wins(self):
        self.assertEqual(_resolve_logo_path("/some/explicit/path.png"), "/some/explicit/path.png")

    def test_explicit_empty_string_forces_disabled(self):
        # Distinct from None: "" is an explicit per-call opt-out even
        # if CONFIG.report_branding.ENABLED is True.
        self.assertIsNone(_resolve_logo_path(""))

    def test_none_defers_to_config_enabled(self):
        result = _resolve_logo_path(None)
        # In this environment CONFIG.report_branding.ENABLED defaults
        # True and the default LOGO_PATH resolves to the real asset --
        # both are exercised together here, more precisely in
        # TestBuildReportHeader below.
        self.assertIsNotNone(result)

    def test_none_defers_to_config_disabled(self):
        original = summary_module.CONFIG.report_branding.ENABLED
        object.__setattr__(summary_module.CONFIG.report_branding, "ENABLED", False)
        try:
            self.assertIsNone(_resolve_logo_path(None))
        finally:
            object.__setattr__(summary_module.CONFIG.report_branding, "ENABLED", original)


class TestBuildReportHeaderFallback(unittest.TestCase):
    """The graceful-degradation contract: report generation must never crash on a bad logo."""

    def setUp(self):
        self.styles = summary_module._build_stylesheet()

    def test_missing_file_falls_back_to_text_only(self):
        flowables = _build_report_header("/does/not/exist/logo.png", self.styles)
        self.assertEqual(len(flowables), 1)  # just the title Paragraph, no Table/Image

    def test_corrupt_file_falls_back_to_text_only(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            fh.write(b"this is not a real png file")
            path = fh.name
        try:
            flowables = _build_report_header(path, self.styles)
            self.assertEqual(len(flowables), 1)
        finally:
            os.unlink(path)

    def test_explicit_empty_path_is_text_only_no_load_attempt(self):
        flowables = _build_report_header("", self.styles)
        self.assertEqual(len(flowables), 1)

    def test_real_logo_produces_two_flowables_worth_of_content(self):
        # Real logo present -> a single Table flowable containing
        # [logo, title] side by side, not just the bare title.
        flowables = _build_report_header(_REAL_LOGO, self.styles)
        self.assertEqual(len(flowables), 1)
        table = flowables[0]
        self.assertEqual(len(table._cellvalues[0]), 2)  # [logo, title] in one row


class TestGeneratePdfEndToEnd(unittest.TestCase):
    """Real PDF generation via ReportLab -- lightweight (no model weights), safe to run locally."""

    def test_generates_valid_pdf_with_real_logo(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            result = generate_pdf(_MINIMAL_DOCUMENT, out, logo_path=_REAL_LOGO)
            self.assertEqual(result, out)
            self.assertTrue(os.path.exists(out))
            self.assertGreater(os.path.getsize(out), 1000)
            with open(out, "rb") as fh:
                self.assertTrue(fh.read(5).startswith(b"%PDF-"))

    def test_generates_valid_pdf_with_missing_logo_no_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report_no_logo.pdf")
            result = generate_pdf(_MINIMAL_DOCUMENT, out, logo_path="/definitely/not/a/real/path.png")
            self.assertTrue(os.path.exists(out))
            with open(out, "rb") as fh:
                self.assertTrue(fh.read(5).startswith(b"%PDF-"))

    def test_generates_valid_pdf_with_default_config_logo(self):
        # logo_path omitted entirely -> CONFIG.report_branding default
        # (GEPER's own assets/logo.png) is what production code paths
        # actually exercise.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report_default.pdf")
            generate_pdf(_MINIMAL_DOCUMENT, out)
            self.assertTrue(os.path.exists(out))

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_logo_image_is_embedded_on_page_one_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            # Force enough variant findings to guarantee a page 2+,
            # so "first page only" is an actual, checkable claim.
            generate_pdf(_make_multi_page_document(15), out, logo_path=_REAL_LOGO)

            reader = PdfReader(out)
            self.assertGreaterEqual(len(reader.pages), 2, "test setup should have produced a multi-page PDF")

            def has_embedded_image(page) -> bool:
                resources = page.get("/Resources")
                if not resources or "/XObject" not in resources:
                    return False
                xobjects = resources["/XObject"].get_object()
                return any(xobjects[name].get("/Subtype") == "/Image" for name in xobjects)

            self.assertTrue(has_embedded_image(reader.pages[0]), "logo image must be embedded on page 1")
            for i, page in enumerate(reader.pages[1:], start=2):
                self.assertFalse(has_embedded_image(page), f"logo image must NOT be embedded on page {i}")

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_logo_dimensions_are_modest_not_dominant(self):
        # The embedded image's rendered height must still be a small
        # fraction of the page (not a dominant graphic), even though
        # it's now sized to genuinely match the title's height rather
        # than a deliberately-tiny fixed constant -- see
        # test_logo_height_matches_title_height for that literal claim.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_MINIMAL_DOCUMENT, out, logo_path=_REAL_LOGO)
            reader = PdfReader(out)
            page = reader.pages[0]
            content = page.extract_text()
            self.assertIn("Bij AI Clinical Genomic Analysis Report", content)

            from reportlab.lib.pagesizes import A4

            styles = summary_module._build_stylesheet()
            table = _build_report_header(_REAL_LOGO, styles)[0]
            logo, _title = table._cellvalues[0]
            page_height_pt = A4[1]
            self.assertLess(
                logo.drawHeight, page_height_pt * 0.05, "logo height should be a small fraction of the page"
            )

    def test_logo_height_matches_title_height(self):
        # The actual bug report this was written to fix: the logo
        # rendered as a "tiny icon" because its source PNG has a lot
        # of transparent padding around the visible mark (~43% of the
        # 500x500 canvas height), so sizing the *canvas* to match the
        # title left the *visible* mark at well under half that height.
        # After cropping to visible content (`_load_cropped_logo_image`),
        # the rendered logo height must equal the title's own measured
        # height exactly -- a literal match, not an approximation.
        styles = summary_module._build_stylesheet()
        content_width = summary_module._PAGE_W - 2 * summary_module._MARGIN
        title_only = summary_module.Paragraph("Bij AI Clinical Genomic Analysis Report", styles["ReportTitle"])
        _, expected_title_height = title_only.wrap(content_width, 1000)

        table = _build_report_header(_REAL_LOGO, styles)[0]
        logo, _title = table._cellvalues[0]
        self.assertAlmostEqual(logo.drawHeight, expected_title_height, places=3)

    def test_cropped_logo_visible_content_fills_its_bounding_box(self):
        # Regression guard for the root cause: cropping to the alpha
        # channel's bounding box must make the *visible* content's
        # bounding box equal the full (cropped) canvas -- i.e. no more
        # leftover transparent padding after the crop.
        from PIL import Image as PILImage

        from report.summary import _load_cropped_logo_image

        cropped_buf = _load_cropped_logo_image(_REAL_LOGO)
        cropped_img = PILImage.open(cropped_buf)
        alpha_bbox = cropped_img.split()[-1].getbbox()
        self.assertEqual(alpha_bbox, (0, 0, cropped_img.width, cropped_img.height))


if __name__ == "__main__":
    unittest.main()
