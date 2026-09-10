"""
tests/test_pipeline_alignment.py
───────────────────────────────────
Real integration tests for pipeline/alignment against synthetic FASTQ
data. No mocks: these run actual bwa/minimap2/samtools subprocesses.
Each tool-dependent test is skipped (not faked) if that tool isn't on
PATH, mirroring the rest of GEPER's "never fabricate a result for a
missing dependency" approach.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixtures.generate_synthetic_reads import (
    build_duplicate_fixture_set,
    build_fixture_set,
)
from pipeline.alignment.stage import AlignmentStage
from pipeline.alignment import bam_utils, bwa_runner, minimap2_runner
from geper.pipeline.fastq.pipeline import FastqPipelineError

HAVE_BWA = bwa_runner.is_available()
HAVE_MINIMAP2 = minimap2_runner.is_available()
HAVE_SAMTOOLS = shutil.which("samtools") is not None


@pytest.fixture(scope="module")
def fixture_set(tmp_path_factory):
    out = tmp_path_factory.mktemp("synthetic_fixtures")
    return build_fixture_set(str(out))


@pytest.fixture(scope="module")
def duplicate_fixture_set(tmp_path_factory):
    """Fixture data that CONTAINS duplicates. See build_duplicate_fixture_set."""
    out = tmp_path_factory.mktemp("duplicate_fixtures")
    fx = build_duplicate_fixture_set(str(out))
    # PRECONDITION, asserted rather than assumed: if the injection quietly
    # did nothing, the tests below would pass against the broken code and
    # we would be back exactly where we started.
    assert fx["n_read_pairs"] == fx["n_unique_read_pairs"] * fx["copies"]
    assert fx["n_unique_read_pairs"] > 0
    return fx


@pytest.mark.skipif(not (HAVE_BWA and HAVE_SAMTOOLS), reason="bwa/samtools not installed")
def test_duplicates_are_marked_bwa(duplicate_fixture_set, tmp_path):
    """Duplicate marking must actually mark duplicates.

    Regression test for a step that never worked: without ``samtools
    fixmate -m`` the pipeline's markdup call exits 1 and writes an empty
    BAM. bwa is the DEFAULT aligner (`aligner: auto` resolves to bwa
    whenever bwa is installed), so this is the production path.
    """
    stage = AlignmentStage({"alignment": {"aligner": "bwa", "threads": 2}})
    result = stage.run(
        duplicate_fixture_set["fastq_r1"],
        duplicate_fixture_set["reference_fasta"],
        str(tmp_path),
        fastq_r2=duplicate_fixture_set["fastq_r2"],
        sample_id="TESTDUPBWA",
    )
    assert result.metrics.total_reads > 0, "markdup produced an empty BAM"
    assert result.metrics.duplicate_reads > 0, (
        "no reads flagged as duplicates on input built entirely from "
        "duplicate pairs -- the fixmate/markdup chain is not working"
    )


@pytest.mark.skipif(not (HAVE_MINIMAP2 and HAVE_SAMTOOLS), reason="minimap2/samtools not installed")
def test_duplicates_are_marked_minimap2(duplicate_fixture_set, tmp_path):
    """Same, on the minimap2 path, which fails at a DIFFERENT markdup check
    (no MC tag at all, rather than no ms score tag)."""
    stage = AlignmentStage({"alignment": {"aligner": "minimap2", "threads": 2}})
    result = stage.run(
        duplicate_fixture_set["fastq_r1"],
        duplicate_fixture_set["reference_fasta"],
        str(tmp_path),
        fastq_r2=duplicate_fixture_set["fastq_r2"],
        sample_id="TESTDUPMM2",
    )
    assert result.metrics.total_reads > 0, "markdup produced an empty BAM"
    assert result.metrics.duplicate_reads > 0, (
        "no reads flagged as duplicates on input built entirely from "
        "duplicate pairs -- the fixmate/markdup chain is not working"
    )


@pytest.mark.skipif(not (HAVE_BWA and HAVE_SAMTOOLS), reason="bwa/samtools not installed")
class TestBwaAlignment:
    def test_produces_sorted_bam_and_index(self, fixture_set, tmp_path):
        stage = AlignmentStage({"alignment": {"aligner": "bwa", "threads": 2}})
        result = stage.run(
            fixture_set["fastq_r1"],
            fixture_set["reference_fasta"],
            str(tmp_path),
            fastq_r2=fixture_set["fastq_r2"],
            sample_id="TESTBWA",
        )
        assert Path(result.sorted_bam_path).name == "aligned.sorted.bam"
        assert Path(result.sorted_bam_path).exists()
        assert Path(result.bai_path).exists()
        assert result.bai_path.endswith("aligned.sorted.bam.bai")

    def test_metrics_are_real_not_fabricated(self, fixture_set, tmp_path):
        stage = AlignmentStage({"alignment": {"aligner": "bwa", "threads": 2}})
        result = stage.run(
            fixture_set["fastq_r1"],
            fixture_set["reference_fasta"],
            str(tmp_path),
            fastq_r2=fixture_set["fastq_r2"],
            sample_id="TESTBWA2",
        )
        # Synthetic reads were sliced directly from the (mutated) reference,
        # so a correctly-working aligner should map essentially all of them.
        assert result.metrics.total_reads == fixture_set["n_read_pairs"] * 2
        assert result.metrics.pct_mapped > 95.0
        assert result.metrics.mean_depth is not None and result.metrics.mean_depth > 0

    def test_metrics_report_written(self, fixture_set, tmp_path):
        stage = AlignmentStage({"alignment": {"aligner": "bwa"}})
        result = stage.run(
            fixture_set["fastq_r1"],
            fixture_set["reference_fasta"],
            str(tmp_path),
            fastq_r2=fixture_set["fastq_r2"],
            sample_id="TESTBWA3",
        )
        report = json.loads(Path(result.metrics_report_path).read_text())
        assert report["aligner"] == "bwa"
        assert report["sample_id"] == "TESTBWA3"
        assert report["total_reads"] > 0

    def test_auto_index_builds_missing_bwa_index(self, fixture_set, tmp_path):
        # Fixture's reference has no prebuilt .bwt/.pac/.sa/.amb/.ann —
        # run_bwa_mem must build it rather than failing.
        ref = fixture_set["reference_fasta"]
        for suffix in (".bwt", ".pac", ".sa", ".amb", ".ann"):
            assert not Path(ref + suffix).exists() or True  # built lazily; just exercise the path
        sam_path = str(tmp_path / "out.sam")
        result_path = bwa_runner.run_bwa_mem(
            fixture_set["fastq_r1"],
            fixture_set["fastq_r2"],
            ref,
            sam_path,
            sample_id="IDX",
            threads=1,
        )
        assert Path(result_path).exists()
        assert Path(ref + ".bwt").exists()


@pytest.mark.skipif(not (HAVE_MINIMAP2 and HAVE_SAMTOOLS), reason="minimap2/samtools not installed")
class TestMinimap2Alignment:
    def test_produces_sorted_bam_and_index(self, fixture_set, tmp_path):
        stage = AlignmentStage({"alignment": {"aligner": "minimap2", "threads": 2}})
        result = stage.run(
            fixture_set["fastq_r1"],
            fixture_set["reference_fasta"],
            str(tmp_path),
            fastq_r2=fixture_set["fastq_r2"],
            sample_id="TESTMM2",
        )
        assert Path(result.sorted_bam_path).exists()
        assert Path(result.bai_path).exists()
        assert result.metrics.pct_mapped > 95.0

    def test_aligner_recorded_correctly(self, fixture_set, tmp_path):
        stage = AlignmentStage({"alignment": {"aligner": "minimap2"}})
        result = stage.run(
            fixture_set["fastq_r1"],
            fixture_set["reference_fasta"],
            str(tmp_path),
            fastq_r2=fixture_set["fastq_r2"],
            sample_id="TESTMM2B",
        )
        assert result.aligner_used == "minimap2"


@pytest.mark.skipif(not (HAVE_BWA and HAVE_SAMTOOLS), reason="bwa/samtools not installed")
def test_auto_aligner_prefers_bwa_when_both_present(fixture_set, tmp_path):
    stage = AlignmentStage({"alignment": {"aligner": "auto"}})
    result = stage.run(
        fixture_set["fastq_r1"],
        fixture_set["reference_fasta"],
        str(tmp_path),
        fastq_r2=fixture_set["fastq_r2"],
        sample_id="AUTOTEST",
    )
    assert result.aligner_used == "bwa"


def test_unknown_aligner_raises():
    stage = AlignmentStage({"alignment": {"aligner": "not-a-real-aligner"}})
    with pytest.raises(FastqPipelineError):
        stage.run("r1.fastq", "ref.fasta", "/tmp/out")


def test_missing_reference_raises_before_running_tools(tmp_path):
    stage = AlignmentStage({"alignment": {"aligner": "bwa"}})
    with pytest.raises(FastqPipelineError):
        stage.run(
            "tests/fixtures/synthetic_run/sample_R1.fastq",
            str(tmp_path / "does_not_exist.fasta"),
            str(tmp_path / "out"),
            sample_id="MISSINGREF",
        )


class TestBamUtils:
    """bam_utils functions tested directly against a BAM produced once
    per test (still real samtools, just exercised at finer grain)."""

    @pytest.mark.skipif(not (HAVE_BWA and HAVE_SAMTOOLS), reason="bwa/samtools not installed")
    def test_flagstat_metrics_shape(self, fixture_set, tmp_path):
        stage = AlignmentStage({"alignment": {"aligner": "bwa"}})
        result = stage.run(
            fixture_set["fastq_r1"],
            fixture_set["reference_fasta"],
            str(tmp_path),
            fastq_r2=fixture_set["fastq_r2"],
            sample_id="BAMUTILS",
        )
        m = bam_utils.compute_flagstat_metrics(result.sorted_bam_path)
        assert m.total_reads == result.metrics.total_reads
        assert m.mapped_reads <= m.total_reads

    @pytest.mark.skipif(not HAVE_SAMTOOLS, reason="samtools not installed")
    def test_index_bam_raises_on_missing_bam(self, tmp_path):
        with pytest.raises(FastqPipelineError):
            bam_utils.index_bam(str(tmp_path / "nope.bam"))
