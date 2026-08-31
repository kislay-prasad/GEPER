"""
Tests for the short-form clinical report `report/summary_short.py`.

Follows the same pattern as `tests/test_clinician_summary.py` and
`tests/test_summary_pdf_logo.py`: real (small) PDFs are generated with
ReportLab -- lightweight, no model weights, safe to run locally -- and
the resulting bytes are inspected with pypdf.

The load-bearing test here is `TestShortIsGenuinelyShort`: it renders
BOTH reports from the same synthetic multi-variant document and
asserts the short one stays at roughly one page per variant or less
while the full one does not, which is the actual requirement this
module exists to satisfy. A cosmetic-only refactor that quietly
reintroduced the full report's evidence/criteria tables would pass
every other test in this file and fail that one.
"""

import os
import tempfile
import unittest

from report.clinical_report_builder import _variant_hgvs_or_locus
from report.summary import generate_pdf
from report.summary_short import (
    _build_identity_block,
    _build_variant_block,
    _classification_text,
    _confidence_text,
    _ordered_variants,
    _short_interpretation,
    _build_short_stylesheet,
    generate_short_pdf,
)

try:
    from pypdf import PdfReader

    _PYPDF_AVAILABLE = True
except ImportError:
    _PYPDF_AVAILABLE = False


_STYLES = _build_short_stylesheet()

_LONG_SUMMARY = (
    "This variant is classified as Pathogenic under ACMG/AMP criteria. "
    "It introduces a premature termination codon in a gene with a well-established "
    "loss-of-function disease mechanism. "
    "Population frequency data show the allele is absent from gnomAD. "
    "Functional assays in the literature support a damaging effect. "
    "Segregation data from two affected relatives are consistent with pathogenicity."
)


def _clinical(
    classification="Pathogenic",
    confidence_label="High",
    confidence_score=90,
    pending=False,
    executive_summary=_LONG_SUMMARY,
    conflict_severity=None,
):
    return {
        "executive_summary": executive_summary,
        "acmg_classification": {
            "classification": classification,
            "triggered_criteria": [
                {"code": "PVS1", "strength": "Very Strong", "rationale": "Nonsense variant in a LoF gene. " * 6},
                {"code": "PM2", "strength": "Moderate", "rationale": "Absent from population databases. " * 6},
            ],
        },
        "confidence": {"pending": pending, "label": confidence_label, "score": confidence_score},
        "supporting_evidence": [f"Supporting evidence line {i} with some explanatory detail." for i in range(6)],
        "limitations": [f"Limitation {i} described at length for the detailed report." for i in range(4)],
        "evidence_sources": ["ClinVar"],
        "conflict_resolution": {"severity": conflict_severity},
    }


def _variant_result(chrom="17", pos=100, ref="A", alt="T", gene="BRCA1", clinical=None, **extra):
    result = {
        "variant": {"chrom": chrom, "pos": pos, "ref": ref, "alt": alt},
        "interpretation_result": {"gene_symbol": gene},
        "candidate_interpretation": _clinical() if clinical is None else clinical,
    }
    result.update(extra)
    return result


def _document(n_variants=4):
    return {
        "geper_version": "test",
        "generated_at": "2026-08-03T00:00:00+00:00",
        "input_vcf": "synthetic_multi.vcf",
        "assembly": "GRCh38",
        "vcf_samples": ["SAMPLE01"],
        "variant_count": n_variants,
        "provenance": [{"source": "ClinVar", "status": "unknown"}],
        "variants": [
            _variant_result(
                chrom=str(i + 1),
                pos=1000 * (i + 1),
                gene=f"GENE{i + 1}",
                normalization={"hgvs_c": f"NM_00000{i + 1}.4:c.{100 + i}A>T"},
            )
            for i in range(n_variants)
        ],
    }


class TestFieldExtraction(unittest.TestCase):
    def test_hgvs_c_preferred(self):
        # Card T3-F4: this fallback chain moved to
        # `report/clinical_report_builder.py::_variant_hgvs_or_locus` so
        # the full Markdown and full PDF headings can share it instead
        # of each growing their own copy -- see that function's
        # docstring. `summary_short.py::_build_variant_block` now calls
        # the same shared function these tests exercise directly.
        vr = _variant_result(normalization={"hgvs_c": "NM_1.2:c.5A>T", "hgvs_g": "NC_1.2:g.99A>T"})
        self.assertEqual(_variant_hgvs_or_locus(vr), "NM_1.2:c.5A>T")

    def test_falls_back_to_hgvs_g(self):
        vr = _variant_result(normalization={"hgvs_c": None, "hgvs_g": "NC_1.2:g.99A>T"})
        self.assertEqual(_variant_hgvs_or_locus(vr), "NC_1.2:g.99A>T")

    def test_falls_back_to_locus_when_no_hgvs_at_all(self):
        vr = _variant_result(chrom="17", pos=100, ref="A", alt="T")
        self.assertEqual(_variant_hgvs_or_locus(vr), "17:100 A>T")

    def test_classification_missing_is_not_classified(self):
        self.assertEqual(_classification_text(None), "Not classified")

    def test_confidence_pending(self):
        self.assertEqual(_confidence_text({"confidence": {"pending": True}}), "Pending")

    def test_confidence_label_and_score(self):
        # Label only, no percentage (C2, report review round 2): this
        # score measures evidence completeness, not certainty, and a
        # percentage next to the classification on this summary strip
        # reads as doubt about the call itself.
        self.assertEqual(_confidence_text({"confidence": {"pending": False, "label": "High", "score": 91.4}}), "High")


class TestShortInterpretation(unittest.TestCase):
    def test_truncates_to_three_sentences_with_ellipsis(self):
        text = _short_interpretation(_clinical())
        self.assertTrue(text.endswith("[...]"))
        # Verbatim leading sentences, nothing invented.
        self.assertTrue(_LONG_SUMMARY.startswith(text[: -len(" [...]")]))
        self.assertNotIn("Segregation data", text)

    def test_short_summary_passes_through_untouched(self):
        short = "A single sentence summary."
        self.assertEqual(_short_interpretation(_clinical(executive_summary=short)), short)

    def test_no_clinical_report_is_a_data_gap_not_benign(self):
        text = _short_interpretation(None)
        self.assertIn("data gap", text)
        self.assertIn("not a benign finding", text)

    def test_empty_executive_summary_points_at_full_report(self):
        self.assertIn("full report", _short_interpretation(_clinical(executive_summary="")))


# The real `_executive_summary` (`report/clinical_report_builder.py`)
# shape that reproduces the truncation Kelly found in the five-variant
# run: the not-evaluated clause's `e.g.` (from
# `NotEvaluatedReason.CONSEQUENCE_INAPPLICABLE`'s label) sits inside
# what is really the third sentence, and the old regex treated the "e.g."
# period as a sentence end, so `_INTERPRETATION_SENTENCES = 3` cut the
# excerpt off mid-parenthetical instead of after the real third
# sentence.
_ABBREVIATION_BUG_SUMMARY = (
    "Variant 17:100 A>T in BRCA1 was classified as **Pathogenic** (evidence completeness: High). "
    "Assigned Urgent review priority. "
    "Of 28 ACMG/AMP criteria evaluated: 7 triggered, 2 checked but not triggered, 19 could not be "
    "evaluated (9 no data source GEPER integrates for any variant; 2 inapplicable given this "
    "variant's own already-determined protein consequence (e.g. a frameshift, nonsense, or canonical "
    "splice-site change, for which computational predictors are moot); 8 a missing/unavailable "
    "evidence source for this specific variant). "
    "Additional follow-up is recommended given the evidence gaps noted above."
)


class TestShortInterpretationAbbreviations(unittest.TestCase):
    def test_eg_inside_the_third_sentence_does_not_truncate_mid_parenthetical(self):
        text = _short_interpretation(_clinical(executive_summary=_ABBREVIATION_BUG_SUMMARY))
        # The bug: the old regex split right after "(e.g." and the
        # excerpt ended there, with the parenthetical never closed.
        self.assertNotIn("(e.g. [...]", text)
        self.assertFalse(text.rstrip().endswith("(e.g."))
        # The fix: the third real sentence -- the whole ACMG/AMP
        # accounting clause, parenthetical closed -- is kept whole, and
        # only the fourth (follow-up) sentence is dropped.
        self.assertIn(
            "8 a missing/unavailable evidence source for this specific variant).",
            text,
        )
        self.assertTrue(text.endswith("evidence source for this specific variant). [...]"))
        self.assertNotIn("Additional follow-up", text)

    def test_ie_etc_cf_vs_approx_do_not_split_sentences_either(self):
        from report.summary_short import _split_sentences

        second_sentence = (
            "Second sentence uses several abbreviations: i.e. shorthand forms, e.g. real ones, "
            "etc. more examples, cf. related shorthand, vs. contrasting cases, and approx. "
            "estimates, none of which end it."
        )
        text = "First sentence stands alone. " + second_sentence
        sentences = _split_sentences(text)
        self.assertEqual(sentences, ["First sentence stands alone.", second_sentence])


class TestOrdering(unittest.TestCase):
    def test_vcf_order_when_no_case_prioritization(self):
        variants = [_variant_result(pos=1), _variant_result(pos=2)]
        self.assertEqual([idx for idx, _ in _ordered_variants(variants)], [1, 2])

    def test_case_rank_reorders_but_keeps_original_finding_numbers(self):
        variants = [
            _variant_result(pos=1, case_prioritization={"case_rank": 2}),
            _variant_result(pos=2, case_prioritization={"case_rank": 1}),
        ]
        self.assertEqual([idx for idx, _ in _ordered_variants(variants)], [2, 1])


class TestFlowableBuilders(unittest.TestCase):
    def test_identity_block_shows_deidentified_label_by_default(self):
        from report.summary import _parse_patient_meta

        table = _build_identity_block(_parse_patient_meta(None), "S1", "R1", "GRCh38", 3, _STYLES)
        text = " ".join(cell.text for row in table._cellvalues for cell in row)
        self.assertIn("De-identified", text)
        self.assertIn("S1", text)
        self.assertNotIn("Date of Birth", text)

    def test_identity_block_shows_named_patient_fields(self):
        patient = {
            "patient_name": "Test Patient",
            "dob": "1990-01-01",
            "gender": "F",
            "physician": "Dr Who",
            "deidentified": False,
        }
        table = _build_identity_block(patient, "S1", "R1", "GRCh38", 3, _STYLES)
        text = " ".join(cell.text for row in table._cellvalues for cell in row)
        self.assertIn("Test Patient", text)
        self.assertIn("Date of Birth", text)

    def test_variant_block_flags_conflicts(self):
        flow = _build_variant_block(1, _variant_result(clinical=_clinical(conflict_severity="Major")), _STYLES)
        text = " ".join(f.text for f in flow if hasattr(f, "text"))
        self.assertIn("Reviewer attention", text)
        self.assertIn("Conflicting evidence (Major)", text)

    def test_variant_block_has_no_flag_line_when_clean(self):
        flow = _build_variant_block(1, _variant_result(), _STYLES)
        text = " ".join(f.text for f in flow if hasattr(f, "text"))
        self.assertNotIn("Reviewer attention", text)


class TestGenerateShortPdf(unittest.TestCase):
    def test_generates_valid_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(_document(), out)
            with open(out, "rb") as fh:
                self.assertTrue(fh.read(5).startswith(b"%PDF-"))

    def test_single_variant_calling_convention(self):
        single = _document(2)["variants"][0]
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(single, out)
            self.assertTrue(os.path.exists(out))

    def test_empty_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf({"variants": []}, out)
            self.assertTrue(os.path.exists(out))

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_contains_required_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(_document(), out, companion_filename="geper_report_full.pdf")
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
            self.assertIn("SAMPLE01", text)  # patient/sample block
            self.assertIn("GENE1", text)  # gene
            self.assertIn("NM_000001.4:c.100A>T", text)  # HGVS
            self.assertIn("Pathogenic", text)  # classification
            self.assertIn("High", text)  # confidence
            self.assertIn("Clinical Scientist", text)  # sign-off 1
            self.assertIn("Consultant Clinical Scientist", text)  # sign-off 2
            self.assertIn("geper_report_full.pdf", text)  # companion note

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_offline_source_caveat_appears_near_companion_note(self):
        """Short report deliberately omits per-finding limitations (see
        module docstring), so the offline-source caveat must appear as
        a single run-level line near the companion note instead -- not
        silently dropped just because this report stays minimal."""
        from unittest import mock

        from utils.service_health import ServiceCheck, ServiceHealthRegistry, ServiceStatus

        registry = ServiceHealthRegistry()
        fake_config = mock.Mock()
        fake_config.health_check.ENABLED = True
        fake_config.health_check.TIMEOUT_SECS = 1.0
        with mock.patch("utils.service_health.CONFIG", fake_config):
            registry.run_startup_checks([ServiceCheck("IndiGenomes", lambda: (ServiceStatus.OFFLINE, "Timeout"))])

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            # `_offline_sources_caveat_text` lives in
            # `report.clinical_report_builder` (relocated 2026-08-21, see
            # that module's own "Shared run-level caveat helpers" note)
            # and is imported by reference into both PDF renderers'
            # namespaces, but its body still runs in
            # `clinical_report_builder`'s module scope, so patching
            # `report.clinical_report_builder.HEALTH` is enough -- no
            # need to patch either renderer's imported name itself.
            with mock.patch("report.clinical_report_builder.HEALTH", registry):
                generate_short_pdf(_document(), out, companion_filename="geper_report_full.pdf")
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self.assertIn("IndiGenomes", text)
        self.assertIn("unreachable during this analysis run and were not queried", text)

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_no_offline_caveat_when_all_sources_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(_document(), out, companion_filename="geper_report_full.pdf")
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
        self.assertNotIn("unreachable during this analysis run and were not queried", text)

    @unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
    def test_omits_the_full_reports_detail_sections(self):
        # The short report must not be a condensed copy of the long
        # one: no QC table, no ACMG criteria table, no supporting-
        # evidence / limitations dumps.
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "short.pdf")
            generate_short_pdf(_document(), out)
            text = "\n".join(p.extract_text() for p in PdfReader(out).pages)
            self.assertNotIn("Sequencing Quality Control", text)
            self.assertNotIn("Supporting Evidence", text)
            self.assertNotIn("Mean Coverage Depth", text)
            self.assertNotIn("Very Strong", text)  # criteria-table strength column


@unittest.skipUnless(_PYPDF_AVAILABLE, "pypdf not installed in this environment")
class TestShortIsGenuinelyShort(unittest.TestCase):
    """Both PDFs, same synthetic document, page counts compared."""

    def _pages(self, n_variants):
        document = _document(n_variants)
        with tempfile.TemporaryDirectory() as tmp:
            short_out = os.path.join(tmp, "short.pdf")
            full_out = os.path.join(tmp, "full.pdf")
            generate_short_pdf(document, short_out)
            generate_pdf(document, full_out)
            return len(PdfReader(short_out).pages), len(PdfReader(full_out).pages)

    def test_at_most_one_page_per_variant(self):
        for n in (1, 4, 8):
            with self.subTest(n_variants=n):
                short_pages, _ = self._pages(n)
                self.assertLessEqual(short_pages, n, f"{n} variants rendered onto {short_pages} short-report pages")

    def test_strictly_shorter_than_the_full_report(self):
        short_pages, full_pages = self._pages(4)
        self.assertLess(short_pages, full_pages)


if __name__ == "__main__":
    unittest.main()
