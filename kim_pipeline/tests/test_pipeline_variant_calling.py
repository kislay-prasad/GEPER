"""
tests/test_pipeline_variant_calling.py
─────────────────────────────────────────
Real integration tests for pipeline/variant_calling.

FreeBayes itself is not installable in every test environment (notably:
not available via apt/pip in this project's CI sandbox at the time of
writing). Tests that need to actually execute FreeBayes are skipped
(not faked) when it's absent — same policy as the rest of GEPER.

`filtering.py` doesn't depend on FreeBayes specifically — it filters
any VCF — so its tests run against a real VCF produced by `bcftools
call` on a real BAM (both already required elsewhere in GEPER), giving
real coverage of the PASS-filtering logic even in environments without
FreeBayes installed.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixtures.generate_synthetic_reads import build_fixture_set
from pipeline.alignment.stage import AlignmentStage
from pipeline.variant_calling import freebayes_runner
from pipeline.variant_calling.filtering import apply_pass_filter, FilterThresholds
from pipeline.variant_calling.stage import VariantCallingStage
from geper.pipeline.fastq.pipeline import FastqPipelineError

HAVE_BWA = shutil.which("bwa") is not None
HAVE_SAMTOOLS = shutil.which("samtools") is not None
HAVE_BCFTOOLS = shutil.which("bcftools") is not None
HAVE_FREEBAYES = freebayes_runner.is_available()


@pytest.fixture(scope="module")
def fixture_set(tmp_path_factory):
    out = tmp_path_factory.mktemp("synthetic_fixtures_vc")
    return build_fixture_set(str(out))


@pytest.fixture(scope="module")
def aligned_bam(fixture_set, tmp_path_factory):
    """A real, indexed BAM from the synthetic fixture — shared input for
    all variant-calling tests in this module."""
    if not (HAVE_BWA and HAVE_SAMTOOLS):
        pytest.skip("bwa/samtools not installed")
    out_dir = tmp_path_factory.mktemp("align_for_vc")
    stage = AlignmentStage({"alignment": {"aligner": "bwa", "threads": 2}})
    result = stage.run(
        fixture_set["fastq_r1"], fixture_set["reference_fasta"], str(out_dir),
        fastq_r2=fixture_set["fastq_r2"], sample_id="VCFIXTURE",
    )
    return {"bam_path": result.sorted_bam_path, "reference_fasta": fixture_set["reference_fasta"],
            "injected_variants": fixture_set["variants"]}


@pytest.fixture(scope="module")
def real_raw_vcf(aligned_bam, tmp_path_factory):
    """A real VCF from `bcftools mpileup | bcftools call` against the
    real BAM above — used to exercise `filtering.py` independently of
    FreeBayes availability. Deliberately not a freebayes_runner test
    fixture; it's there to give filtering.py real input."""
    if not HAVE_BCFTOOLS:
        pytest.skip("bcftools not installed")
    out_dir = tmp_path_factory.mktemp("bcftools_vcf")
    vcf_path = str(out_dir / "raw.vcf")
    mpileup = subprocess.run(
        ["bcftools", "mpileup", "-f", aligned_bam["reference_fasta"], aligned_bam["bam_path"]],
        capture_output=True, text=True, check=True,
    )
    call = subprocess.run(
        ["bcftools", "call", "-mv", "-Ov", "-o", vcf_path],
        input=mpileup.stdout, capture_output=True, text=True, check=True,
    )
    assert Path(vcf_path).exists()
    return vcf_path


@pytest.mark.skipif(not HAVE_BCFTOOLS, reason="bcftools not installed")
class TestFiltering:
    def test_pass_filter_recovers_known_high_confidence_snps(self, real_raw_vcf, tmp_path):
        out_vcf = str(tmp_path / "filtered.vcf")
        summary = apply_pass_filter(real_raw_vcf, out_vcf)
        # 4 of the 5 injected variants are high-depth SNPs covered ~10x;
        # the 1bp-anchored deletion call lands at very low depth/QUAL at
        # the edge of read coverage and should be filtered out.
        assert summary.total_pass == 4
        assert summary.snvs_pass == 4
        assert Path(out_vcf).exists()
        passed_lines = [l for l in Path(out_vcf).read_text().splitlines() if not l.startswith("#")]
        assert len(passed_lines) == 4
        assert all("\tPASS\t" in l for l in passed_lines)

    def test_stricter_thresholds_filter_more(self, real_raw_vcf, tmp_path):
        out_vcf = str(tmp_path / "strict.vcf")
        strict = FilterThresholds(min_qual=300.0, min_depth=10)  # higher than any real QUAL here
        summary = apply_pass_filter(real_raw_vcf, out_vcf, thresholds=strict)
        assert summary.total_pass == 0

    def test_missing_input_vcf_raises(self, tmp_path):
        with pytest.raises(FastqPipelineError):
            apply_pass_filter(str(tmp_path / "nope.vcf"), str(tmp_path / "out.vcf"))


@pytest.mark.skipif(not HAVE_FREEBAYES, reason="freebayes is not installed in this environment")
class TestFreebayesRunnerLive:
    """Only runs where FreeBayes is actually installed (not this
    project's current sandbox — see module docstring)."""

    def test_run_freebayes_produces_vcf_with_injected_variants(self, aligned_bam, tmp_path):
        out_vcf = str(tmp_path / "variants.vcf")
        freebayes_runner.run_freebayes(
            aligned_bam["bam_path"], aligned_bam["reference_fasta"], out_vcf,
        )
        assert Path(out_vcf).exists()
        lines = [l for l in Path(out_vcf).read_text().splitlines() if not l.startswith("#")]
        assert len(lines) > 0

    def test_full_stage_writes_both_outputs(self, aligned_bam, tmp_path):
        stage = VariantCallingStage({"variant_calling": {"threads": 1}})
        result = stage.run(aligned_bam["bam_path"], aligned_bam["reference_fasta"], str(tmp_path))
        assert Path(result.raw_vcf_path).name == "variants.vcf"
        assert Path(result.filtered_vcf_path).name == "filtered_variants.vcf"
        assert Path(result.raw_vcf_path).exists()
        assert Path(result.filtered_vcf_path).exists()
        assert result.pass_variants <= result.total_variants


def test_stage_raises_clearly_when_freebayes_absent(aligned_bam, tmp_path, monkeypatch):
    """Deterministic test of the no-fallback behavior, independent of
    whether this host happens to have FreeBayes installed."""
    import pipeline.variant_calling.stage as vc_stage
    monkeypatch.setattr(vc_stage.freebayes_runner, "is_available", lambda: False)
    stage = VariantCallingStage()
    with pytest.raises(FastqPipelineError) as exc_info:
        stage.run(aligned_bam["bam_path"], aligned_bam["reference_fasta"], str(tmp_path))
    assert exc_info.value.tool == "freebayes"
    # Confirms no other caller (e.g. bcftools) was silently substituted:
    assert "freebayes" in str(exc_info.value).lower()
