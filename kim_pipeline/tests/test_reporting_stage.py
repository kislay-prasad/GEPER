"""
tests/test_reporting_stage.py
──────────────────────────────
Tests for pipeline/reporting/stage.py.

No external tools required — report generation is pure Python.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.reporting.stage import ReportingStage


def _fake_qc() -> dict:
    return {
        "path": "r1.fastq",
        "total_records": 1000,
        "min_read_length": 100,
        "max_read_length": 100,
        "mean_read_length": 100.0,
        "is_gzipped": False,
    }


def _fake_align() -> dict:
    return {
        "sample_id": "S01",
        "aligner_used": "bwa",
        "total_reads": 2000,
        "mapped_reads": 1980,
        "pct_mapped": 99.0,
        "mean_depth": 25.0,
    }


def _fake_vc() -> dict:
    return {
        "sample_id": "S01",
        "caller": "freebayes",
        "total_variants": 10,
        "pass_variants": 8,
        "snvs_pass": 7,
        "indels_pass": 1,
    }


def _fake_annotation() -> dict:
    return {
        "sample_id": "S01",
        "total_variants": 8,
        "annotated_count": 7,
        "unannotated_count": 1,
        "variants": [
            {
                "chrom": "chr17",
                "pos": 43057051,
                "ref": "A",
                "alt": "T",
                "qual": 200.0,
                "filter_field": "PASS",
                "gene_name": "BRCA1",
                "transcript_id": "NM_007294.4",
                "hgvs": "NM_007294.4:g.43057051A>T",
                "zygosity": "Heterozygous",
                "gt": "0/1",
                "dp": "25",
                "ad": "15,10",
                "info": ".",
                "genotype": "0/1",
            },
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Issue 3 — clinician-grade report overhaul: patient metadata, QC status
# badges, variant dashboard, clinical interpretation, footer, ReportLab PDF.
# ─────────────────────────────────────────────────────────────────────────────


class TestIssue3ClinicalReportOverhaul:
    def _run(self, tmp_path, **overrides):
        stage = ReportingStage({"reporting": {"generate_pdf": False}})
        kwargs = dict(
            sample_id="S01",
            output_dir=str(tmp_path),
            qc_summary=_fake_qc(),
            alignment_stats=_fake_align(),
            variant_stats=_fake_vc(),
            annotation_result=_fake_annotation(),
            acmg_results=[
                {
                    "chrom": "chr17",
                    "pos": 43057051,
                    "ref": "A",
                    "alt": "T",
                    "gene": "BRCA1",
                    "classification": "Pathogenic",
                    "score": 8,
                    "criteria_met": ["PVS1"],
                    "criteria_unknown": [],
                    "gnomad_af": 0.0001,
                    "gnomad_af_popmax": 0.0002,
                }
            ],
        )
        kwargs.update(overrides)
        return stage.run(**kwargs)

    def test_patient_metadata_appears_in_html(self, tmp_path):
        result = self._run(
            tmp_path,
            patient_metadata={
                "name": "Jane Doe",
                "dob": "1990-01-01",
                "sex": "F",
                "physician": "Dr. Smith",
            },
        )
        html = Path(result.html_path).read_text()
        assert "Jane Doe" in html
        assert "1990-01-01" in html
        assert "Dr. Smith" in html

    def test_no_patient_metadata_falls_back_to_deidentified(self, tmp_path):
        result = self._run(tmp_path)  # no patient_metadata kwarg at all
        html = Path(result.html_path).read_text()
        assert "De-identified / Research Sample" in html

    def test_patient_metadata_not_added_to_json(self, tmp_path):
        """Explicit requirement: JSON report structure stays unchanged."""
        result = self._run(tmp_path, patient_metadata={"name": "Jane Doe"})
        payload = json.loads(Path(result.json_path).read_text())
        assert "patient_metadata" not in payload
        assert "patient" not in payload

    def test_qc_status_badges_rendered(self, tmp_path):
        result = self._run(tmp_path)
        html = Path(result.html_path).read_text()
        assert 'class="badge' in html
        assert "Mean Coverage" in html
        assert "Mapping Rate" in html
        assert "Q30 Bases" in html

    def test_variant_dashboard_counts_rendered(self, tmp_path):
        result = self._run(tmp_path)
        html = Path(result.html_path).read_text()
        assert "dash-tile" in html
        assert "Pathogenic" in html

    def test_clinical_interpretation_section_rendered(self, tmp_path):
        result = self._run(tmp_path)
        html = Path(result.html_path).read_text()
        assert "Clinical Interpretation" in html
        assert "Recommended Follow-up" in html
        assert "genetic counseling" in html.lower()  # Pathogenic finding -> this recommendation

    def test_footer_contains_disclaimer_reference_genome_version_timestamp(self, tmp_path):
        result = self._run(
            tmp_path,
            reference_versions={"reference_genome": "GRCh38 (test)"},
        )
        html = Path(result.html_path).read_text()
        assert "report-footer" in html
        assert "GRCh38 (test)" in html
        assert "Bij AI v8" in html
        assert "Generated:" in html

    def test_signature_block_not_rendered(self, tmp_path):
        """Inverted, not deleted, from the original `assert "Reviewing
        Pathologist" in html`: that assertion encoded a claim -- "this
        report carries a lab sign-off block" -- that the product now
        rules false, rather than documenting a held defect (see
        pipeline/reporting/stage.py's own history: the signature-block/
        signature-line div pair implied a review process kim_pipeline
        has never had anywhere in its code -- no review_status field,
        no approve()/override(), no export gate). A control
        indistinguishable from no control is worse than no control,
        because it launders the absence of one; removing the artefact
        and inverting this assertion to a reintroduction guard is the
        correct pair, not two separate cleanups. Guarding this (rather
        than just deleting the test, as a purely-cosmetic static-layout
        removal like the PDF twin in cfba75e would only need) is
        deliberate: the artefact has demonstrated, across both the PDF
        and HTML report surfaces, that it recurs. If this assertion
        starts failing, that means the block came back -- do not
        "fix" it by reverting to `in html`.
        """
        result = self._run(tmp_path)
        html = Path(result.html_path).read_text()
        assert "Reviewing Pathologist" not in html

    def test_html_still_contains_backward_compatible_markers(self, tmp_path):
        """Preserve the original test's own assertions: BRCA1, S01, a table."""
        result = self._run(tmp_path)
        html = Path(result.html_path).read_text()
        assert "BRCA1" in html
        assert "S01" in html
        assert "<table>" in html

    def test_html_is_responsive_has_viewport_meta(self, tmp_path):
        result = self._run(tmp_path)
        html = Path(result.html_path).read_text()
        assert 'name="viewport"' in html

    def test_html_has_print_media_query(self, tmp_path):
        result = self._run(tmp_path)
        html = Path(result.html_path).read_text()
        assert "@media print" in html

    def test_reportlab_pdf_generated_by_default(self, tmp_path):
        stage = ReportingStage({"reporting": {"generate_pdf": True}})
        result = stage.run(
            sample_id="S01",
            output_dir=str(tmp_path),
            qc_summary=_fake_qc(),
            alignment_stats=_fake_align(),
            variant_stats=_fake_vc(),
            annotation_result=_fake_annotation(),
            patient_metadata={"name": "Jane Doe"},
        )
        assert result.pdf_path is not None
        assert Path(result.pdf_path).exists()
        from pypdf import PdfReader

        text = "".join(p.extract_text() for p in PdfReader(result.pdf_path).pages)
        assert "Jane Doe" in text
        assert "BRCA1" in text

    def test_consequence_column_in_variant_table(self, tmp_path):
        annotation = dict(_fake_annotation())
        annotation["variants"] = [dict(annotation["variants"][0], consequence="missense_variant")]
        result = self._run(tmp_path, annotation_result=annotation)
        html = Path(result.html_path).read_text()
        assert "missense_variant" in html
        assert "Consequence" in html

    def test_empty_run_still_works_with_new_sections(self, tmp_path):
        """Backward-compat / robustness: an empty run (no acmg, no patient
        metadata, no pgx/ancestry) must not crash with the new sections."""
        stage = ReportingStage({"reporting": {"generate_pdf": False}})
        result = stage.run(sample_id="EMPTY", output_dir=str(tmp_path))
        assert Path(result.json_path).exists()
        assert Path(result.html_path).exists()
        html = Path(result.html_path).read_text()
        assert "No variants met inclusion criteria" in html


class TestReportingStage:
    def test_json_report_written(self, tmp_path):
        stage = ReportingStage({"reporting": {"generate_pdf": False}})
        result = stage.run(
            sample_id="S01",
            output_dir=str(tmp_path),
            qc_summary=_fake_qc(),
            alignment_stats=_fake_align(),
            variant_stats=_fake_vc(),
            annotation_result=_fake_annotation(),
        )
        assert Path(result.json_path).exists()
        payload = json.loads(Path(result.json_path).read_text())
        assert payload["sample_id"] == "S01"
        assert "variants" in payload
        assert len(payload["variants"]) == 1
        assert payload["variants"][0]["gene_name"] == "BRCA1"

    def test_html_report_written(self, tmp_path):
        stage = ReportingStage({"reporting": {"generate_pdf": False}})
        result = stage.run(
            sample_id="S01",
            output_dir=str(tmp_path),
            qc_summary=_fake_qc(),
            alignment_stats=_fake_align(),
            variant_stats=_fake_vc(),
            annotation_result=_fake_annotation(),
        )
        assert Path(result.html_path).exists()
        html = Path(result.html_path).read_text()
        assert "BRCA1" in html
        assert "S01" in html
        assert "<table>" in html

    def test_one_report_per_sample_not_per_variant(self, tmp_path):
        """Only one JSON and one HTML should be written."""
        stage = ReportingStage({"reporting": {"generate_pdf": False}})
        # Annotation with 5 variants
        annotation = dict(_fake_annotation())
        annotation["variants"] = annotation["variants"] * 5
        stage.run(
            sample_id="MULTI",
            output_dir=str(tmp_path),
            annotation_result=annotation,
        )
        out = Path(tmp_path)  # Fix 1: reports go directly in output_dir, not output_dir/sample_id
        json_files = list(out.glob("*.json"))
        html_files = list(out.glob("*.html"))
        assert len(json_files) == 1
        assert len(html_files) == 1

    def test_report_contains_all_sections(self, tmp_path):
        stage = ReportingStage({"reporting": {"generate_pdf": False}})
        result = stage.run(
            sample_id="FULL",
            output_dir=str(tmp_path),
            qc_summary=_fake_qc(),
            alignment_stats=_fake_align(),
            variant_stats=_fake_vc(),
            annotation_result=_fake_annotation(),
            acmg_results={"classification": "Likely_Pathogenic", "criteria": ["PM1", "PP3"]},
        )
        payload = json.loads(Path(result.json_path).read_text())
        assert "qc_summary" in payload
        assert "alignment_statistics" in payload
        assert "variant_statistics" in payload
        assert "acmg_results" in payload
        assert "variants" in payload
        assert payload["acmg_results"]["classification"] == "Likely_Pathogenic"

    def test_pdf_skipped_gracefully_when_not_requested(self, tmp_path):
        stage = ReportingStage({"reporting": {"generate_pdf": False}})
        result = stage.run(sample_id="S01", output_dir=str(tmp_path))
        assert result.pdf_path is None

    def test_empty_run_produces_valid_report(self, tmp_path):
        """Stage must handle None inputs gracefully."""
        stage = ReportingStage({"reporting": {"generate_pdf": False}})
        result = stage.run(sample_id="EMPTY", output_dir=str(tmp_path))
        assert Path(result.json_path).exists()
        assert Path(result.html_path).exists()


# ─────────────────────────────────────────────────────────────────────────────
# FIX 1 regression — report.json/report.html must be written directly inside
# output_dir, NOT inside output_dir/<sample_id>/
# ─────────────────────────────────────────────────────────────────────────────


class TestFix1ReportingPathRegression:
    """Regression tests for: ReportingStage writes to wrong subdirectory."""

    def _run_stage(self, tmp_path, sample_id="S01"):
        stage = ReportingStage({"reporting": {"generate_pdf": False}})
        return stage.run(
            sample_id=sample_id,
            output_dir=str(tmp_path),
            qc_summary=_fake_qc(),
            alignment_stats=_fake_align(),
            variant_stats=_fake_vc(),
            annotation_result=_fake_annotation(),
        )

    def test_report_json_in_output_dir_not_sample_subdir(self, tmp_path):
        """report.json must be directly in output_dir, not output_dir/sample_id/."""
        result = self._run_stage(tmp_path, sample_id="S01")
        json_path = Path(result.json_path)
        assert json_path.exists(), "report.json must exist"
        assert json_path.parent == tmp_path, (
            f"report.json is in wrong dir: {json_path.parent}, expected {tmp_path}"
        )

    def test_report_html_in_output_dir_not_sample_subdir(self, tmp_path):
        """report.html must be directly in output_dir, not output_dir/sample_id/."""
        result = self._run_stage(tmp_path, sample_id="S01")
        html_path = Path(result.html_path)
        assert html_path.exists(), "report.html must exist"
        assert html_path.parent == tmp_path, (
            f"report.html is in wrong dir: {html_path.parent}, expected {tmp_path}"
        )

    def test_no_sample_id_subdir_created(self, tmp_path):
        """The sample_id subdirectory must NOT be created inside output_dir."""
        sample_id = "PATIENT42"
        self._run_stage(tmp_path, sample_id=sample_id)
        wrong_dir = tmp_path / sample_id
        assert not wrong_dir.exists(), f"sample_id subdir should not exist: {wrong_dir}"

    def test_result_json_path_matches_actual_file(self, tmp_path):
        """ReportResult.json_path must point to the file that actually exists."""
        result = self._run_stage(tmp_path, sample_id="S01")
        assert Path(result.json_path).exists(), (
            f"ReportResult.json_path={result.json_path!r} does not exist on disk"
        )

    def test_result_html_path_matches_actual_file(self, tmp_path):
        """ReportResult.html_path must point to the file that actually exists."""
        result = self._run_stage(tmp_path, sample_id="S01")
        assert Path(result.html_path).exists(), (
            f"ReportResult.html_path={result.html_path!r} does not exist on disk"
        )
