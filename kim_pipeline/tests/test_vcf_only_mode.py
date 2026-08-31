"""
tests/test_vcf_only_mode.py
────────────────────────────
Regression tests for PipelineRunner's mode="vcf_only" / stop_after=
"variant_calling" early-stop path (GEPER <-> Kim bridge, Stage 1).

These tests mock every external stage so they run without bwa/samtools/
freebayes installed. They assert that:
  1. mode="vcf_only" runs FASTQ validation -> QC -> Alignment ->
     Variant Calling and then returns, without touching VEP/annotation/
     ACMG/PGx/ancestry/reporting.
  2. stop_after="variant_calling" behaves identically to mode="vcf_only".
  3. The default (mode="full", stop_after=None) behavior is completely
     unchanged -- i.e. this feature is fully backward compatible.
  4. An invalid mode/stop_after value raises ValueError before any stage
     runs.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.orchestration.runner import PipelineRunner


def _make_fastq(path: Path, n: int = 5) -> None:
    with open(path, "w") as f:
        for i in range(n):
            f.write(f"@read.{i}\nACGTACGT\n+\nIIIIIIII\n")


def _patch_common_stages(tmp_path: Path):
    """Patch FastqValidator/QCStage/AlignmentStage/VariantCallingStage so
    the run reaches variant_calling without any external binaries."""
    bam_path = tmp_path / "sorted.bam"
    bam_path.touch()
    vcf_path = tmp_path / "filtered_variants.vcf"
    vcf_path.write_text("##fileformat=VCFv4.2\n")

    mock_validator = MagicMock()
    mock_validator.validate_paired.return_value = (
        MagicMock(to_dict=lambda: {"reads": 5}),
        MagicMock(to_dict=lambda: {"reads": 5}),
    )
    mock_validator.validate_single.return_value = MagicMock(to_dict=lambda: {"reads": 5})

    mock_qc_result = MagicMock()
    mock_qc_result.metrics_r1.to_dict.return_value = {"reads": 5}
    mock_qc_result.metrics_r2 = None
    mock_qc_result.qc_passed = True
    mock_qc_result.report_json_path = str(tmp_path / "qc.json")
    mock_qc_result.report_html_path = str(tmp_path / "qc.html")

    mock_align_result = MagicMock()
    mock_align_result.to_dict.return_value = {"sorted_bam_path": str(bam_path)}

    mock_vc_result = MagicMock()
    mock_vc_result.to_dict.return_value = {"filtered_vcf_path": str(vcf_path)}

    return mock_validator, mock_qc_result, mock_align_result, mock_vc_result, vcf_path


class TestVcfOnlyMode:
    def test_vcf_only_stops_after_variant_calling(self, tmp_path):
        r1 = tmp_path / "r1.fastq"
        _make_fastq(r1)

        (mock_validator, mock_qc_result, mock_align_result, mock_vc_result, vcf_path) = (
            _patch_common_stages(tmp_path)
        )

        with (
            patch("pipeline.orchestration.runner.FastqValidator", return_value=mock_validator),
            patch("pipeline.orchestration.runner.QCStage") as MockQC,
            patch("pipeline.orchestration.runner.AlignmentStage") as MockAlign,
            patch("pipeline.orchestration.runner.VariantCallingStage") as MockVC,
            patch("pipeline.orchestration.runner.VEPAnnotationStage") as MockVEP,
            patch("pipeline.orchestration.runner.AnnotationStage") as MockAnnotation,
            patch("pipeline.orchestration.runner.ReportingStage") as MockReporting,
        ):
            MockQC.return_value.run.return_value = mock_qc_result
            MockAlign.return_value.run.return_value = mock_align_result
            MockVC.return_value.run.return_value = mock_vc_result

            runner = PipelineRunner(cfg={}, resume=False)
            result = runner.run(
                fastq_r1=str(r1),
                reference_fasta="/dev/null",
                output_dir=str(tmp_path / "out"),
                sample_id="S01",
                mode="vcf_only",
            )

            assert result.success is True
            assert result.stopped_after == "variant_calling"
            assert result.variant_calling["filtered_vcf_path"] == str(vcf_path)
            assert result.report is None
            assert "variant_calling" in result.stages_completed
            assert "annotation" not in result.stages_completed
            assert "reporting" not in result.stages_completed

            # Downstream Kim-only stages must never have been invoked.
            MockVEP.return_value.run.assert_not_called()
            MockAnnotation.return_value.run.assert_not_called()
            MockReporting.return_value.run.assert_not_called()

    def test_stop_after_variant_calling_equivalent_to_vcf_only(self, tmp_path):
        r1 = tmp_path / "r1.fastq"
        _make_fastq(r1)

        (mock_validator, mock_qc_result, mock_align_result, mock_vc_result, vcf_path) = (
            _patch_common_stages(tmp_path)
        )

        with (
            patch("pipeline.orchestration.runner.FastqValidator", return_value=mock_validator),
            patch("pipeline.orchestration.runner.QCStage") as MockQC,
            patch("pipeline.orchestration.runner.AlignmentStage") as MockAlign,
            patch("pipeline.orchestration.runner.VariantCallingStage") as MockVC,
            patch("pipeline.orchestration.runner.ReportingStage") as MockReporting,
        ):
            MockQC.return_value.run.return_value = mock_qc_result
            MockAlign.return_value.run.return_value = mock_align_result
            MockVC.return_value.run.return_value = mock_vc_result

            runner = PipelineRunner(cfg={}, resume=False)
            result = runner.run(
                fastq_r1=str(r1),
                reference_fasta="/dev/null",
                output_dir=str(tmp_path / "out"),
                sample_id="S02",
                stop_after="variant_calling",
            )

            assert result.stopped_after == "variant_calling"
            MockReporting.return_value.run.assert_not_called()

    def test_invalid_mode_raises_before_any_stage(self, tmp_path):
        runner = PipelineRunner(cfg={}, resume=False)
        with pytest.raises(ValueError):
            runner.run(
                fastq_r1="whatever.fastq",
                reference_fasta="/dev/null",
                output_dir=str(tmp_path / "out"),
                sample_id="S03",
                mode="not_a_real_mode",
            )

    def test_invalid_stop_after_raises_before_any_stage(self, tmp_path):
        runner = PipelineRunner(cfg={}, resume=False)
        with pytest.raises(ValueError):
            runner.run(
                fastq_r1="whatever.fastq",
                reference_fasta="/dev/null",
                output_dir=str(tmp_path / "out"),
                sample_id="S04",
                stop_after="reporting",
            )

    def test_default_mode_is_full_and_unaffected(self, tmp_path):
        """No mode/stop_after passed -> behavior must be identical to
        before this feature existed (result.stopped_after stays None)."""
        r1 = tmp_path / "r1.fastq"
        _make_fastq(r1)

        (mock_validator, mock_qc_result, mock_align_result, mock_vc_result, vcf_path) = (
            _patch_common_stages(tmp_path)
        )

        mock_vep_result = MagicMock(annotated_vcf_path=str(vcf_path), variant_count=1)
        mock_annotation_result = MagicMock()
        mock_annotation_result.to_dict.return_value = {}
        mock_report_result = MagicMock()
        mock_report_result.to_dict.return_value = {"report": "ok"}

        with (
            patch("pipeline.orchestration.runner.FastqValidator", return_value=mock_validator),
            patch("pipeline.orchestration.runner.QCStage") as MockQC,
            patch("pipeline.orchestration.runner.AlignmentStage") as MockAlign,
            patch("pipeline.orchestration.runner.VariantCallingStage") as MockVC,
            patch("pipeline.orchestration.runner.VEPAnnotationStage") as MockVEP,
            patch("pipeline.orchestration.runner.AnnotationStage") as MockAnnotation,
            patch("pipeline.orchestration.runner.ReportingStage") as MockReporting,
        ):
            MockQC.return_value.run.return_value = mock_qc_result
            MockAlign.return_value.run.return_value = mock_align_result
            MockVC.return_value.run.return_value = mock_vc_result
            MockVEP.return_value.run.return_value = mock_vep_result
            MockAnnotation.return_value.run.return_value = mock_annotation_result
            MockReporting.return_value.run.return_value = mock_report_result

            runner = PipelineRunner(cfg={}, resume=False)
            result = runner.run(
                fastq_r1=str(r1),
                reference_fasta="/dev/null",
                output_dir=str(tmp_path / "out"),
                sample_id="S05",
            )

            assert result.stopped_after is None
            assert result.report == {"report": "ok"}
            MockReporting.return_value.run.assert_called_once()


class TestNoBlastDataLeavesAnnotationUnmerged:
    """FIX #9: replaces test_defect_regression.py::TestBlastMergeIntoAnnotation::
    test_no_blast_data_does_not_crash, which built a local `annotation`
    dict and `blast_result_data = None` by hand, guarded them with a
    literal `if blast_result_data and ...: pass` that never executed
    (`pass` does nothing even when it does), and then asserted
    `"variants" in annotation` -- true because the test had just put it
    there two lines above, regardless of what runner.py actually does
    with `blast_result_data`. It never imported or called runner.py at
    all.

    This drives the real merge guard at runner.py:752
    (`if blast_result_data and isinstance(result.annotation.get(
    "variants"), list):`) through the actual `PipelineRunner.run()`,
    using the same external-stage-mocking harness the rest of this file
    already established, with `variants` present as a real list (so the
    ONLY thing keeping `blast_summary` out is `blast_result_data` being
    `None`, not the `isinstance` half of the guard) and blast left at
    its default-disabled config (`cfg={}` -> `blast.enabled` absent ->
    `False`), exactly as a real run has it unless explicitly configured.

    Confirmed live by mutation (2026-08-31): temporarily changing
    runner.py:752's guard from `if blast_result_data and isinstance(...)`
    to `if True and isinstance(...)` (forcing the merge body to run
    regardless of `blast_result_data`) turned this test red -- the
    merge body immediately crashes on `blast_result_data.get("hits", [])`
    when `blast_result_data` is `None`, so the run fails outright rather
    than quietly adding `blast_summary`. Either way the guard's absence
    is caught, which is the point; reverting restored green.
    """

    def test_no_blast_data_does_not_add_blast_summary(self, tmp_path):
        r1 = tmp_path / "r1.fastq"
        _make_fastq(r1)

        (mock_validator, mock_qc_result, mock_align_result, mock_vc_result, vcf_path) = (
            _patch_common_stages(tmp_path)
        )

        mock_vep_result = MagicMock(annotated_vcf_path=str(vcf_path), variant_count=1)
        mock_annotation_result = MagicMock()
        mock_annotation_result.to_dict.return_value = {
            "variants": [{"chrom": "chr1", "pos": 100, "ref": "A", "alt": "T"}]
        }
        mock_report_result = MagicMock()
        mock_report_result.to_dict.return_value = {}

        with (
            patch("pipeline.orchestration.runner.FastqValidator", return_value=mock_validator),
            patch("pipeline.orchestration.runner.QCStage") as MockQC,
            patch("pipeline.orchestration.runner.AlignmentStage") as MockAlign,
            patch("pipeline.orchestration.runner.VariantCallingStage") as MockVC,
            patch("pipeline.orchestration.runner.VEPAnnotationStage") as MockVEP,
            patch("pipeline.orchestration.runner.AnnotationStage") as MockAnnotation,
            patch("pipeline.orchestration.runner.ReportingStage") as MockReporting,
        ):
            MockQC.return_value.run.return_value = mock_qc_result
            MockAlign.return_value.run.return_value = mock_align_result
            MockVC.return_value.run.return_value = mock_vc_result
            MockVEP.return_value.run.return_value = mock_vep_result
            MockAnnotation.return_value.run.return_value = mock_annotation_result
            MockReporting.return_value.run.return_value = mock_report_result

            # blast.enabled left unset -> False, so blast_result_data
            # stays None exactly as a real unconfigured run has it.
            runner = PipelineRunner(cfg={}, resume=False)
            result = runner.run(
                fastq_r1=str(r1),
                reference_fasta="/dev/null",
                output_dir=str(tmp_path / "out"),
                sample_id="S06",
            )

        assert "variants" in result.annotation
        assert "blast_summary" not in result.annotation
        assert "blast_hits" not in result.annotation["variants"][0]
