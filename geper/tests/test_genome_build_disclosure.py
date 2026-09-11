"""
Pins the genome-build disclosure ("Genome reference build"/"Reference
build") across the four places `document["assembly"]` is rendered:
Markdown (`report_generator.py:284`, once), the full PDF
(`summary.py:997` table row and `summary.py:1676` clinician-summary
line, twice), and the short PDF (`summary_short.py:413`, once).

Commissioned after `bf3f625` fixed the Markdown gap (the human ruling:
"put the build on every rendered surface; the markdown omitting what
the PDF prints is the same defect as the ISO conjunction") with no
test pinning any of the four renderings -- a refactor could have
silently dropped a human-ruled line with nothing failing. Each test
here was shown red by removing the line it pins (see the commit
message) before being restored green; a test that would pass with the
line present OR absent pins nothing, which is the exact gap this file
exists to close.
"""

import os
import tempfile
import unittest

from report.report_generator import ReportGenerator
from report.summary import generate_pdf
from report.summary_short import generate_short_pdf

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False


def _document(assembly="GRCh38", n_variants=1):
    return {
        "geper_version": "test",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "input_vcf": "synthetic.vcf",
        "assembly": assembly,
        "vcf_samples": ["SAMPLE01"],
        "variant_count": n_variants,
        "variants": [
            {
                "variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "T"},
                "interpretation": {},
                "candidate_interpretation": None,
                "ai_model_status": {},
                "errors": [],
            }
            for _ in range(n_variants)
        ],
    }


class TestMarkdownGenomeBuildPinned(unittest.TestCase):
    def test_assembly_value_is_rendered(self):
        md = ReportGenerator().generate(_document(assembly="GRCh38"))
        self.assertIn("**Genome reference build:** GRCh38", md)

    def test_missing_assembly_renders_not_specified(self):
        md = ReportGenerator().generate(_document(assembly=None))
        self.assertIn("**Genome reference build:** Not specified", md)


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestFullPdfGenomeBuildPinned(unittest.TestCase):
    """Both occurrences, per `bf3f625`'s own count -- table row (:997)
    and clinician-summary line (:1676)."""

    def test_assembly_value_is_rendered_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.pdf")
            generate_pdf(_document(assembly="GRCh38"), out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self.assertIn("Genome Reference Build", text)
        self.assertIn("Reference build:", text)
        self.assertEqual(text.count("GRCh38"), 2, "assembly value must appear exactly twice (table row + summary line)")

    def test_missing_assembly_renders_fallback_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.pdf")
            generate_pdf(_document(assembly=None), out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        # summary.py:997 falls back to "Not specified"; summary.py:1676
        # falls back to lowercase "not specified" -- a real, existing
        # casing difference between the two occurrences, not a typo in
        # this test. Both are asserted so a refactor that "normalizes"
        # the casing (and thereby changes what the reader sees) is
        # visible here rather than silently passing.
        self.assertIn("Not specified", text)
        self.assertIn("not specified", text)


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestShortPdfGenomeBuildPinned(unittest.TestCase):
    def test_assembly_value_is_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r_short.pdf")
            generate_short_pdf(_document(assembly="GRCh38"), out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self.assertIn("Reference Build", text)
        self.assertIn("GRCh38", text)

    def test_missing_assembly_renders_not_specified(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r_short.pdf")
            generate_short_pdf(_document(assembly=None), out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self.assertIn("Not specified", text)


if __name__ == "__main__":
    unittest.main()
