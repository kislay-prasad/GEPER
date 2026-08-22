"""
Tests for the one-page Clinician Summary and PDF bookmark features in
`report/summary.py`. Generates real, small PDFs with ReportLab
(lightweight, not a model load -- safe to run locally) and inspects the
actual PDF content/outline via pypdf, following the same pattern
`tests/test_summary_pdf_logo.py` already established.
"""

import os
import tempfile
import unittest

from report import summary as summary_module
from report.summary import (
    _Bookmark,
    _build_clinician_summary_flowables,
    _build_clinician_summary_table,
    _normalize_evidence_text,
    _provenance_gap_sources,
    _variant_reviewer_flags,
    generate_pdf,
)

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False


def _clinical(
    classification="Pathogenic",
    confidence_label="High",
    confidence_score=90,
    pending=False,
    supporting_evidence=None,
    evidence_sources=None,
    conflict_severity=None,
):
    return {
        "executive_summary": f"Test executive summary ({classification}).",
        "acmg_classification": {"classification": classification},
        "confidence": {"pending": pending, "label": confidence_label, "score": confidence_score},
        "supporting_evidence": supporting_evidence or [],
        "limitations": [],
        "evidence_sources": evidence_sources or [],
        "conflict_resolution": {"severity": conflict_severity},
    }


def _variant_result(chrom="17", pos=100, ref="A", alt="T", gene=None, clinical=None, clingen=None, transcript=None):
    result = {"variant": {"chrom": chrom, "pos": pos, "ref": ref, "alt": alt}}
    if gene is not None:
        result["interpretation_result"] = {"gene_symbol": gene}
    if clinical is not None:
        result["candidate_interpretation"] = clinical
    if clingen is not None:
        result["clingen"] = clingen
    if transcript is not None:
        result["transcript"] = transcript
    return result


_STYLES = summary_module._build_stylesheet()


class TestNormalizeEvidenceText(unittest.TestCase):
    def test_plain_string_passes_through(self):
        self.assertEqual(_normalize_evidence_text("PVS1 triggered"), "PVS1 triggered")

    def test_dict_extracts_text_field(self):
        self.assertEqual(_normalize_evidence_text({"text": "PS3 triggered", "sources": ["ClinGen"]}), "PS3 triggered")

    def test_dict_without_text_field_is_empty(self):
        self.assertEqual(_normalize_evidence_text({"sources": ["ClinGen"]}), "")

    def test_none_is_empty(self):
        self.assertEqual(_normalize_evidence_text(None), "")


class TestVariantReviewerFlags(unittest.TestCase):
    def test_no_clinical_report_flags_missing_interpretation(self):
        flags = _variant_reviewer_flags(_variant_result(), None)
        self.assertEqual(flags, ["No clinical interpretation available"])

    def test_no_conflict_no_ambiguity_is_no_flags(self):
        clinical = _clinical(conflict_severity=None)
        flags = _variant_reviewer_flags(_variant_result(clingen={"gene_resolution_status": "resolved"}), clinical)
        self.assertEqual(flags, [])

    def test_major_conflict_is_flagged(self):
        clinical = _clinical(conflict_severity="Major")
        flags = _variant_reviewer_flags(_variant_result(), clinical)
        self.assertIn("Conflicting evidence (Major)", flags)

    def test_severity_none_string_is_not_flagged(self):
        # The engine's "no conflicts" state is the *string* "None", not
        # Python None -- must not be misread as a real severity.
        clinical = _clinical(conflict_severity="None")
        flags = _variant_reviewer_flags(_variant_result(), clinical)
        self.assertEqual(flags, [])

    def test_ambiguous_gene_resolution_via_clingen_is_flagged(self):
        clinical = _clinical()
        variant_result = _variant_result(clingen={"gene_resolution_status": "ambiguous"})
        flags = _variant_reviewer_flags(variant_result, clinical)
        self.assertIn("Ambiguous gene resolution", flags)

    def test_ambiguous_gene_resolution_via_transcript_is_flagged(self):
        clinical = _clinical()
        variant_result = _variant_result(transcript={"gene_resolution_status": "ambiguous"})
        flags = _variant_reviewer_flags(variant_result, clinical)
        self.assertIn("Ambiguous gene resolution", flags)

    def test_both_conflict_and_ambiguous_gene_both_flagged(self):
        clinical = _clinical(conflict_severity="Minor")
        variant_result = _variant_result(clingen={"gene_resolution_status": "ambiguous"})
        flags = _variant_reviewer_flags(variant_result, clinical)
        self.assertEqual(len(flags), 2)


class TestProvenanceGapSources(unittest.TestCase):
    def test_cited_unknown_source_is_flagged(self):
        document = {"provenance": [{"source": "ClinVar", "status": "unknown"}]}
        variants = [_variant_result(clinical=_clinical(evidence_sources=["ClinVar"]))]
        gaps = _provenance_gap_sources(document, variants)
        self.assertEqual(gaps, ["ClinVar"])

    def test_not_consulted_is_not_a_gap(self):
        document = {"provenance": [{"source": "ClinVar", "status": "not_consulted"}]}
        variants = [_variant_result(clinical=_clinical(evidence_sources=["ClinVar"]))]
        gaps = _provenance_gap_sources(document, variants)
        self.assertEqual(gaps, [])

    def test_version_known_is_not_a_gap(self):
        document = {"provenance": [{"source": "gnomAD", "status": "version_known", "version": "gnomad_r4"}]}
        variants = [_variant_result(clinical=_clinical(evidence_sources=["gnomAD"]))]
        gaps = _provenance_gap_sources(document, variants)
        self.assertEqual(gaps, [])

    def test_uncited_source_unknown_status_is_not_flagged(self):
        # ClinVar is unknown, but no variant actually cited it as evidence.
        document = {"provenance": [{"source": "ClinVar", "status": "unknown"}]}
        variants = [_variant_result(clinical=_clinical(evidence_sources=["gnomAD"]))]
        gaps = _provenance_gap_sources(document, variants)
        self.assertEqual(gaps, [])

    def test_clingen_prefix_match(self):
        document = {"provenance": [{"source": "ClinGen (gene validity)", "status": "unknown"}]}
        variants = [_variant_result(clinical=_clinical(evidence_sources=["ClinGen"]))]
        gaps = _provenance_gap_sources(document, variants)
        self.assertEqual(gaps, ["ClinGen (gene validity)"])

    def test_no_variants_no_gaps(self):
        document = {"provenance": [{"source": "ClinVar", "status": "unknown"}]}
        self.assertEqual(_provenance_gap_sources(document, []), [])


class TestBuildClinicianSummaryTable(unittest.TestCase):
    def test_one_row_per_variant_plus_header(self):
        variants = [
            _variant_result(pos=100, clinical=_clinical()),
            _variant_result(pos=200, clinical=_clinical()),
        ]
        table = _build_clinician_summary_table(variants, _STYLES)
        self.assertEqual(len(table._cellvalues), 3)  # header + 2 variants

    def test_gene_symbol_included_when_present(self):
        variants = [_variant_result(gene="BRCA1", clinical=_clinical())]
        table = _build_clinician_summary_table(variants, _STYLES)
        gene_cell_text = table._cellvalues[1][1].text
        self.assertIn("BRCA1", gene_cell_text)

    def test_missing_clinical_report_renders_no_interpretation_flag(self):
        variants = [_variant_result(clinical=None)]
        table = _build_clinician_summary_table(variants, _STYLES)
        flags_cell_text = table._cellvalues[1][5].text
        self.assertIn("No clinical interpretation available", flags_cell_text)


class TestBuildClinicianSummaryFlowables(unittest.TestCase):
    def test_ends_with_a_page_break(self):
        from reportlab.platypus import PageBreak

        document = {"provenance": []}
        variants = [_variant_result(clinical=_clinical())]
        flow = _build_clinician_summary_flowables(document, variants, "SAMPLE01", "RUN01", "GRCh38", _STYLES)
        self.assertIsInstance(flow[-1], PageBreak)

    def test_starts_with_a_bookmark(self):
        document = {"provenance": []}
        variants = [_variant_result(clinical=_clinical())]
        flow = _build_clinician_summary_flowables(document, variants, "SAMPLE01", "RUN01", "GRCh38", _STYLES)
        self.assertIsInstance(flow[0], _Bookmark)
        self.assertEqual(flow[0].title, "Clinician Summary")

    def test_no_variants_still_produces_a_valid_flow(self):
        document = {"provenance": []}
        flow = _build_clinician_summary_flowables(document, [], "SAMPLE01", "RUN01", "GRCh38", _STYLES)
        self.assertTrue(len(flow) > 0)


class TestBookmarkFlowable(unittest.TestCase):
    def test_wrap_returns_zero_size(self):
        bookmark = _Bookmark("key1", "Title 1")
        self.assertEqual(bookmark.wrap(1000, 1000), (0, 0))

    def test_width_and_height_are_zero(self):
        bookmark = _Bookmark("key1", "Title 1")
        self.assertEqual(bookmark.width, 0)
        self.assertEqual(bookmark.height, 0)


_DOCUMENT_WITH_FLAGS = {
    "geper_version": "test",
    "generated_at": "2026-08-01T00:00:00+00:00",
    "input_vcf": "test.vcf",
    "assembly": "GRCh38",
    "vcf_samples": ["SAMPLE01"],
    "variant_count": 2,
    "provenance": [{"source": "ClinVar", "status": "unknown"}],
    "variants": [
        {
            "variant": {"chrom": "17", "pos": 100, "ref": "A", "alt": "T"},
            "interpretation_result": {"gene_symbol": "BRCA1"},
            "clingen": {"gene_resolution_status": "ambiguous"},
            "candidate_interpretation": _clinical(
                classification="Uncertain significance",
                confidence_label="Moderate",
                supporting_evidence=["PM1: hotspot region"],
                evidence_sources=["ClinVar"],
                conflict_severity="Moderate",
            ),
        },
        {
            "variant": {"chrom": "20", "pos": 200, "ref": "C", "alt": "G"},
            "interpretation_result": {"gene_symbol": "PRNP"},
            "candidate_interpretation": _clinical(evidence_sources=["gnomAD"]),
        },
    ],
}


class TestGeneratePdfEndToEnd(unittest.TestCase):
    """Real PDF generation via ReportLab -- lightweight (no model weights), safe to run locally."""

    def test_generates_valid_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_DOCUMENT_WITH_FLAGS, out)
            self.assertTrue(os.path.exists(out))
            with open(out, "rb") as fh:
                self.assertTrue(fh.read(5).startswith(b"%PDF-"))

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_outline_has_one_entry_per_major_section_and_per_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_DOCUMENT_WITH_FLAGS, out)
            reader = PdfReader(out)

            def flatten(items):
                titles = []
                for item in items:
                    if isinstance(item, list):
                        titles.extend(flatten(item))
                    else:
                        titles.append(item.title)
                return titles

            titles = flatten(reader.outline)
            self.assertIn("Clinician Summary", titles)
            self.assertIn("Sequencing Quality Control Metrics", titles)
            self.assertIn("Finding 1: 17:100 A>T", titles)
            self.assertIn("Finding 2: 20:200 C>G", titles)
            self.assertIn("Sign-off & Disclaimer", titles)

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_page_one_is_the_clinician_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_DOCUMENT_WITH_FLAGS, out)
            reader = PdfReader(out)
            page_one_text = reader.pages[0].extract_text()
            self.assertIn("Clinician Summary", page_one_text)
            self.assertIn("SAMPLE01", page_one_text)
            self.assertIn("Reviewer Attention", page_one_text)
            self.assertIn("BRCA1", page_one_text)

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_detailed_sections_still_present_after_summary_page(self):
        # "Keep the full detailed sections unchanged below it" --
        # confirm the per-variant Finding sections still render with
        # their full existing content (executive summary text), not
        # just the new summary page.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(_DOCUMENT_WITH_FLAGS, out)
            reader = PdfReader(out)
            full_text = "\n".join(p.extract_text() for p in reader.pages)
            self.assertIn("Finding 1", full_text)
            self.assertIn("Finding 2", full_text)
            self.assertIn("Sequencing Quality Control Metrics", full_text)

    def test_single_variant_calling_convention_still_works(self):
        # generate_pdf also accepts one variant_result dict directly
        # (no top-level "variants" key) -- confirm the summary page
        # doesn't crash that calling convention.
        single = dict(_DOCUMENT_WITH_FLAGS["variants"][0])
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.pdf")
            generate_pdf(single, out)
            self.assertTrue(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
