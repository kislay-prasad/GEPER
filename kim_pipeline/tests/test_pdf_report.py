"""
tests/test_pdf_report.py
──────────────────────────
Tests for pipeline/reporting/pdf_report.py — the ReportLab-native
clinical PDF generator (Issue 3).
"""

from __future__ import annotations

from pipeline.reporting.pdf_report import render_clinical_pdf, ReportLabUnavailableError
from pipeline.reporting.stage import PIPELINE_VERSION
from pipeline.reporting.clinical_sections import (
    normalize_patient_metadata,
    qc_status_summary,
    variant_dashboard,
    clinical_interpretation,
    merge_variants_with_acmg,
)


def _minimal_kwargs():
    patient = normalize_patient_metadata(None)
    qc_rows = qc_status_summary({"q30_fraction": 0.9}, {"mean_depth": 30, "pct_mapped": 98})
    dashboard = variant_dashboard([{"classification": "Uncertain_Significance"}], {})
    interpretation = clinical_interpretation(dashboard)
    merged = merge_variants_with_acmg(
        [
            {
                "chrom": "MT",
                "pos": 3243,
                "ref": "A",
                "alt": "G",
                "gene_name": "MT-TL1",
                "transcript_id": "rna-TRNL1",
                "hgvs": "MT:g.3243A>G",
                "consequence": "gene_region_variant",
            }
        ],
        [
            {
                "chrom": "MT",
                "pos": 3243,
                "ref": "A",
                "alt": "G",
                "gene": "MT-TL1",
                "classification": "Uncertain_Significance",
                "gnomad_af": 0.0001,
            }
        ],
    )
    return dict(
        patient_meta=patient,
        qc_rows=qc_rows,
        dashboard=dashboard,
        interpretation=interpretation,
        merged_variants=merged,
        reference_genome="GRCh38",
        pipeline_version=PIPELINE_VERSION,
    )


class TestRenderClinicalPdf:
    def test_produces_a_valid_pdf_file(self, tmp_path):
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        result_path = render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        assert result_path == out
        reader = PdfReader(out)
        assert len(reader.pages) >= 1

    def test_patient_metadata_appears_in_pdf_text(self, tmp_path):
        from pypdf import PdfReader

        kwargs = _minimal_kwargs()
        kwargs["patient_meta"] = normalize_patient_metadata(
            {"name": "Jane Doe", "physician": "Dr. Smith"}
        )
        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **kwargs)
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        assert "Jane Doe" in text
        assert "Dr. Smith" in text

    def test_deidentified_note_appears_when_no_patient_metadata(self, tmp_path):
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        assert "de-identified" in text.lower()

    def test_variant_gene_and_classification_appear(self, tmp_path):
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        assert "MT-TL1" in text
        assert "Uncertain_Significance" in text

    def test_footer_contains_reference_genome_and_pipeline_version(self, tmp_path):
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        assert "GRCh38" in text
        # Was "Bij AI v8" -- the whole-product name, superseded 2026-09-11
        # (Option B): the version names the component, as the scope line does.
        assert "Bij AI sequencing-analysis component v8" in text

    def test_lab_disclaimer_present(self, tmp_path):
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        # The footer intentionally truncates a long disclaimer to fit one
        # line — check the start of the default disclaimer, which is
        # guaranteed to survive truncation.
        #
        # UPDATED 2026-08-22 (EJ-01): pipeline/reporting/pdf_report.py's
        # default `lab_disclaimer` text was rewritten that day to the
        # ratified assisted-clinical-use wording, superseding the
        # research/orthogonal-testing framing it previously asserted (see
        # that module's own comment). This specific assertion needed no
        # change: "in-development bioinformatics pipeline" was
        # deliberately kept, verbatim, as the new text's own opening
        # clause, so it still survives the 150-character footer
        # truncation unchanged. Noted here rather than left silent, so
        # this unchanged assertion doesn't read as having been missed.
        #
        # UPDATED again (EJ-01 alignment, human ruling "Option 1"): the
        # default is now component_identity.RESEARCH_USE_DISCLAIMER, which is
        # GEPER's text byte for byte, so "in-development bioinformatics
        # pipeline" is gone. Asserted here: exactly the first 150 characters
        # the footer draws, taken from the constant itself.
        from pipeline.reporting.component_identity import RESEARCH_USE_DISCLAIMER

        assert RESEARCH_USE_DISCLAIMER[:150] in text

    def test_custom_disclaimer_used_when_provided(self, tmp_path):
        from pypdf import PdfReader

        kwargs = _minimal_kwargs()
        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", lab_disclaimer="CUSTOM DISCLAIMER TEXT", **kwargs)
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        assert "CUSTOM DISCLAIMER TEXT" in text

    def test_signature_block_not_rendered(self, tmp_path):
        """Inverted, not deleted, from the original `assert "Pathologist"
        in text`: that assertion encoded a claim -- "this report carries
        a lab sign-off block" -- that the product now rules false, rather
        than documenting a held defect. This is the PDF twin of the same
        signature-block/signature-line pair removed from the HTML report
        (see test_reporting_stage.py::test_signature_block_not_rendered
        for the full reasoning: no review_status field, no
        approve()/override(), no export gate anywhere in kim_pipeline --
        a control indistinguishable from no control is worse than no
        control, because it launders the absence of one). The block
        itself was removed from pipeline/reporting/pdf_report.py in
        cfba75e; this test was missed by that commit's authorization
        (named only pdf_report.py and one other test file) because it
        lives in a second test file pinning the same artefact. If this
        assertion starts failing, that means the block came back -- do
        not "fix" it by reverting to `in text`.
        """
        from pypdf import PdfReader

        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
        text = "".join(p.extract_text() for p in PdfReader(out).pages)
        assert "Pathologist" not in text

    def test_page_x_of_y_pagination(self, tmp_path):
        """Force multiple pages via a long variant list and confirm each
        page's footer states the correct total."""
        from pypdf import PdfReader

        kwargs = _minimal_kwargs()
        many_variants = [
            {
                "chrom": "17",
                "pos": i,
                "ref": "A",
                "alt": "T",
                "gene_name": f"GENE{i}",
                "transcript_id": f"NM_{i}",
                "hgvs": f"g.{i}A>T",
                "consequence": "missense_variant",
            }
            for i in range(200)
        ]
        many_acmg = [
            {
                "chrom": "17",
                "pos": i,
                "ref": "A",
                "alt": "T",
                "gene": f"GENE{i}",
                "classification": "Uncertain_Significance",
                "gnomad_af": 0.001,
            }
            for i in range(200)
        ]
        kwargs["merged_variants"] = merge_variants_with_acmg(many_variants, many_acmg)
        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **kwargs)
        reader = PdfReader(out)
        n = len(reader.pages)
        assert n > 1
        first_text = reader.pages[0].extract_text()
        last_text = reader.pages[-1].extract_text()
        assert f"Page 1 of {n}" in first_text
        assert f"Page {n} of {n}" in last_text

    def test_no_variants_does_not_crash(self, tmp_path):
        kwargs = _minimal_kwargs()
        kwargs["merged_variants"] = []
        out = str(tmp_path / "report.pdf")
        render_clinical_pdf(out, sample_id="S01", **kwargs)  # must not raise

    def test_reportlab_unavailable_raises_specific_error(self, tmp_path, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "reportlab.lib" or name.startswith("reportlab"):
                raise ImportError("simulated missing reportlab")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        out = str(tmp_path / "report.pdf")
        try:
            render_clinical_pdf(out, sample_id="S01", **_minimal_kwargs())
            assert False, "expected ReportLabUnavailableError"
        except ReportLabUnavailableError:
            pass
