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

from pipeline.reporting.stage import ReportingStage, _acmg_to_html_table, _variants_to_html_table


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
        metadata, no ancestry) must not crash with the new sections."""
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


class TestAcmgCrashRowDoesNotFabricate:
    """A per-variant result whose classification stage raised (shared.py's
    per-variant Exception handler appends {..., "error": ...} with no
    classification/clinvar_unavailable_reason/gnomad_unavailable_reason keys,
    by construction) must render as a failure, never as a normal-looking
    Uncertain_Significance / Found / Found row.
    """

    def _crashed_item(self):
        return {
            "chrom": "17",
            "pos": 41244936,
            "ref": "A",
            "alt": "G",
            "gene": "BRCA1",
            "error": "IndexError: list index out of range",
            "disclaimer": "Exploratory only.",
        }

    def _normal_item(self):
        return {
            "chrom": "13",
            "pos": 32340000,
            "ref": "C",
            "alt": "T",
            "gene": "BRCA2",
            "classification": "Pathogenic",
            "score": "8.5",
            "criteria_met": ["PS1", "PM2"],
            "criteria_unknown": [],
            "clinvar_unavailable_reason": None,
            "gnomad_af": 0.0001,
            "gnomad_af_popmax": 0.0002,
            "final_tier": "Tier1",
            "composite_score": "0.95",
            "disclaimer": "Exploratory only.",
        }

    def test_crashed_row_does_not_render_fabricated_defaults(self):
        html = _acmg_to_html_table([self._crashed_item()])
        assert "Uncertain_Significance" not in html, (
            "a crashed variant must not render the real ACMG category "
            "'Uncertain_Significance' as if it were a genuine classification"
        )
        assert ">Found<" not in html, (
            "a crashed variant must not render a bare 'Found' for ClinVar/gnomAD "
            "-- evidence gathering never ran"
        )
        assert "Found (frequency not available)" not in html, (
            "a crashed variant must not render gnomAD's genuine-but-empty-AF "
            "fallback text -- gnomAD was never queried"
        )

    def test_crashed_row_renders_the_captured_error(self):
        html = _acmg_to_html_table([self._crashed_item()])
        assert "Classification failed" in html
        assert "IndexError: list index out of range" in html

    def test_normal_row_unaffected_by_the_error_branch(self):
        """Control: a variant with no `error` key renders exactly as before."""
        html = _acmg_to_html_table([self._normal_item()])
        assert "Pathogenic" in html
        assert "Found" in html
        assert "AF: 1.00e-04 (popmax: 2.00e-04)" in html
        assert "Classification failed" not in html


# ══════════════════════════════════════════════════════════════════════════════
# DEFECT-zygosity-collapses-absent-and-malformed: caller wiring
# ══════════════════════════════════════════════════════════════════════════════
# `ZygosityExtractor` now distinguishes a malformed GT token from a valid
# genotype of the same shape via `ZygosityResult.malformed` (see
# tests/test_zygosity.py). This section proves the two named callers
# actually surface it: `_variants_to_html_table` must not render a
# malformed-derived category as a plain confident value, must not blank the
# cell either (a blank cell reads as "nothing found" -- the fabricated-
# absence defect this floor has spent the day removing), and must NOT
# annotate a genuine no_call, which is a legitimate absence, not a failure.


def _variant_row(**overrides) -> dict:
    row = {
        "chrom": "chr1",
        "pos": 100,
        "ref": "A",
        "alt": "G",
        "qual": 99,
        "gene_name": "BRCA1",
        "transcript_id": "ENST1",
        "hgvs": "c.1A>G",
        "consequence": "missense_variant",
        "zygosity": "Hemizygous",
        "gt": "1",
        "dp": "30",
        "ad": "0,30",
        "zygosity_malformed": False,
    }
    row.update(overrides)
    return row


class TestZygosityMalformedDisclosure:
    def test_valid_zygosity_renders_plain_value(self):
        html = _variants_to_html_table([_variant_row()])
        assert "<td>Hemizygous</td>" in html
        assert "unreliable" not in html

    def test_no_call_not_flagged_as_a_failure(self):
        """The case that matters most: a legitimate absence must not start
        looking like a failure just because malformed disclosure exists."""
        html = _variants_to_html_table(
            [_variant_row(zygosity="No_call", gt="./.", zygosity_malformed=False)]
        )
        assert "<td>No_call</td>" in html
        assert "unreliable" not in html

    def test_malformed_zygosity_discloses_value_and_reason_not_a_blank_cell(self):
        html = _variants_to_html_table(
            [_variant_row(zygosity="Hemizygous", gt="BAD", zygosity_malformed=True)]
        )
        # The category is still shown (not suppressed into a blank cell --
        # that would read as "nothing found", the mirror defect).
        assert "Hemizygous" in html
        # And it says untrustworthy AND why, not just "do not trust".
        assert "unreliable" in html
        assert "GT field unparseable" in html
        # It must not render as the same bare cell as the valid case.
        assert "<td>Hemizygous</td>" not in html


class TestZygosityMalformedWiredOntoAnnotatedVariant:
    def test_malformed_gt_sets_zygosity_malformed_true(self):
        from pipeline.annotation.stage import _iter_vcf
        import tempfile
        import os

        vcf = (
            "##fileformat=VCFv4.2\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS01\n"
            "chr1\t100\t.\tA\tG\t99\tPASS\t.\tGT\tBAD\n"
        )
        fd, path = tempfile.mkstemp(suffix=".vcf")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(vcf)
            variants = list(_iter_vcf(path))
        finally:
            os.unlink(path)
        assert len(variants) == 1
        assert variants[0].zygosity_malformed is True

    def test_valid_gt_leaves_zygosity_malformed_false(self):
        from pipeline.annotation.stage import _iter_vcf
        import tempfile
        import os

        vcf = (
            "##fileformat=VCFv4.2\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS01\n"
            "chr1\t100\t.\tA\tG\t99\tPASS\t.\tGT\t0/1\n"
        )
        fd, path = tempfile.mkstemp(suffix=".vcf")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(vcf)
            variants = list(_iter_vcf(path))
        finally:
            os.unlink(path)
        assert len(variants) == 1
        assert variants[0].zygosity_malformed is False
