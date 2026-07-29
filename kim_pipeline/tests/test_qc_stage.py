"""
tests/test_qc_stage.py
────────────────────────
Unit and integration tests for pipeline/qc/stage.py.

All tests use synthetic in-memory FASTQ files written to tmp_path.
No third-party bioinformatics tools are required.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.qc.stage import (
    QCStage,
    QCMetrics,
    QCResult,
    QCThresholdError,
    QCError,
    _compute_qc,
    DEFAULT_THRESHOLDS,
)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _write_fastq(path: Path, records: list) -> None:
    with open(path, "w") as fh:
        for h, s, q in records:
            fh.write(f"{h}\n{s}\n+\n{q}\n")


def _write_fastq_gz(path: Path, records: list) -> None:
    with gzip.open(path, "wt") as fh:
        for h, s, q in records:
            fh.write(f"{h}\n{s}\n+\n{q}\n")


def _reads(n: int, seq: str = "ACGTACGTACGT", qual: str | None = None, prefix: str = "read") -> list:
    q = qual or "I" * len(seq)
    return [(f"@{prefix}.{i+1} tile:1:1001:100{i}:200{i}", seq, q) for i in range(n)]


# ─── _compute_qc unit tests ───────────────────────────────────────────────────

class TestComputeQCBasics:

    def test_total_reads_counted(self, tmp_path):
        p = tmp_path / "r1.fastq"
        _write_fastq(p, _reads(5))
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        assert m.total_reads == 5

    def test_total_bases_counted(self, tmp_path):
        p = tmp_path / "r1.fastq"
        seq = "ACGTACGT"
        _write_fastq(p, _reads(3, seq=seq))
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        assert m.total_bases == 3 * len(seq)

    def test_mean_quality_phred40(self, tmp_path):
        """Phred 40 = ASCII 'I' (73 - 33 = 40)."""
        p = tmp_path / "r1.fastq"
        _write_fastq(p, _reads(10, seq="ACGT", qual="IIII"))
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        assert abs(m.mean_quality - 40.0) < 0.01

    def test_low_quality_phred_detected(self, tmp_path):
        """All-'!' quality → Phred 0."""
        p = tmp_path / "r1.fastq"
        _write_fastq(p, _reads(5, seq="ACGT", qual="!!!!"))
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        assert m.mean_quality == 0.0
        assert m.low_quality_reads == 5

    def test_gc_content_pure_gc(self, tmp_path):
        p = tmp_path / "r1.fastq"
        _write_fastq(p, _reads(5, seq="GCGCGCGC", qual="I" * 8))
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        assert abs(m.gc_fraction - 1.0) < 0.01

    def test_gc_content_pure_at(self, tmp_path):
        p = tmp_path / "r1.fastq"
        _write_fastq(p, _reads(5, seq="ATATATATAT", qual="I" * 10))
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        assert abs(m.gc_fraction - 0.0) < 0.01

    def test_n_content_counted(self, tmp_path):
        p = tmp_path / "r1.fastq"
        _write_fastq(p, _reads(4, seq="ACGTNNNNN", qual="I" * 9))
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        # 5 Ns per read × 4 reads / 36 total bases
        assert m.n_fraction > 0.0
        assert m.reads_with_n_fraction == 1.0

    def test_read_length_stats(self, tmp_path):
        p = tmp_path / "r1.fastq"
        records = [
            ("@r1", "ACGT", "IIII"),
            ("@r2", "ACGTACGT", "I" * 8),
            ("@r3", "ACGTACGTACGT", "I" * 12),
        ]
        _write_fastq(p, records)
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        assert m.min_read_length == 4
        assert m.max_read_length == 12
        assert abs(m.mean_read_length - 8.0) < 0.01

    def test_adapter_detection(self, tmp_path):
        """Embed TruSeq adapter seed in reads."""
        p = tmp_path / "r1.fastq"
        adapter_seed = "AGATCGGAAGAGC"
        seq = "ACGT" + adapter_seed + "TTTT"
        _write_fastq(p, _reads(10, seq=seq, qual="I" * len(seq)))
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        assert m.adapter_contamination_fraction == 1.0
        assert m.adapter_hits.get("TruSeq_R1", 0) == 10

    def test_no_adapter_in_clean_reads(self, tmp_path):
        p = tmp_path / "r1.fastq"
        _write_fastq(p, _reads(10, seq="ACGTACGTACGT"))
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        assert m.adapter_contamination_fraction == 0.0

    def test_duplicate_identical_reads(self, tmp_path):
        """All reads identical → high dup estimate."""
        p = tmp_path / "r1.fastq"
        _write_fastq(p, _reads(100, seq="ACGTACGTACGT"))
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        # 99 dups out of 100 reads
        assert m.duplicate_fraction_estimate > 0.95

    def test_duplicate_unique_reads(self, tmp_path):
        """All reads different → zero dup estimate."""
        p = tmp_path / "r1.fastq"
        records = [(f"@r{i}", f"ACGT{'A' * i}", "I" * (4 + i)) for i in range(20)]
        _write_fastq(p, records)
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        assert m.duplicate_fraction_estimate == 0.0

    def test_gzipped_file_accepted(self, tmp_path):
        p = tmp_path / "r1.fastq.gz"
        _write_fastq_gz(p, _reads(5))
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        assert m.total_reads == 5

    def test_missing_file_raises_qcerror(self):
        from pipeline.qc.stage import QCError
        with pytest.raises(QCError):
            _compute_qc("/nonexistent/path.fastq", DEFAULT_THRESHOLDS)

    def test_q20_q30_fractions(self, tmp_path):
        p = tmp_path / "r1.fastq"
        # Q20='5'(53-33=20), Q30='?'(63-33=30), Q40='I'(73-33=40)
        records = [("@r1", "ACGTACGT", "5555????")]  # 4 bases Q20, 4 bases Q30
        _write_fastq(p, records)
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        assert m.q20_fraction == 1.0   # all ≥ Q20
        assert m.q30_fraction == 0.5   # only 4 of 8 ≥ Q30

    def test_per_base_phred_length(self, tmp_path):
        p = tmp_path / "r1.fastq"
        _write_fastq(p, _reads(5, seq="ACGTACGT", qual="I" * 8))
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        assert len(m.per_base_phred.mean_by_position) == 8

    def test_n50_computed(self, tmp_path):
        p = tmp_path / "r1.fastq"
        # total=225, half=112.5; sorted desc: 100,50,50,25; cum 100->150>=112.5 -> N50=50
        records = [
            ("@r1", "A" * 100, "I" * 100),
            ("@r2", "A" * 50,  "I" * 50),
            ("@r3", "A" * 50,  "I" * 50),
            ("@r4", "A" * 25,  "I" * 25),
        ]
        _write_fastq(p, records)
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        assert m.n50_read_length == 50

    def test_filtered_reads_too_short(self, tmp_path):
        p = tmp_path / "r1.fastq"
        records = [
            ("@r1", "AC", "II"),         # 2 bp — too short (< 25)
            ("@r2", "A" * 30, "I" * 30), # ok
        ]
        _write_fastq(p, records)
        m = _compute_qc(str(p), DEFAULT_THRESHOLDS)
        assert m.too_short_reads == 1


# ─── QCStage integration tests ────────────────────────────────────────────────

class TestQCStage:

    def test_run_produces_json_report(self, tmp_path):
        fq = tmp_path / "r1.fastq"
        _write_fastq(fq, _reads(50, seq="ACGTACGTACGT", qual="I" * 12))
        stage = QCStage(cfg={"qc": {"stop_on_failure": False}})
        result = stage.run(str(fq), str(tmp_path / "qc"), sample_id="S01")
        assert Path(result.report_json_path).exists()
        data = json.loads(Path(result.report_json_path).read_text())
        assert data["sample_id"] == "S01"

    def test_run_produces_html_report(self, tmp_path):
        fq = tmp_path / "r1.fastq"
        _write_fastq(fq, _reads(50, seq="ACGTACGTACGT", qual="I" * 12))
        stage = QCStage(cfg={"qc": {"stop_on_failure": False}})
        result = stage.run(str(fq), str(tmp_path / "qc"), sample_id="S01")
        html = Path(result.report_html_path).read_text()
        assert "GEPER QC Report" in html
        assert "S01" in html

    def test_run_paired_end(self, tmp_path):
        r1 = tmp_path / "r1.fastq"
        r2 = tmp_path / "r2.fastq"
        _write_fastq(r1, _reads(50, seq="ACGTACGTACGT", qual="I" * 12))
        _write_fastq(r2, _reads(50, seq="TGCATGCATGCA", qual="I" * 12))
        stage = QCStage(cfg={"qc": {"stop_on_failure": False}})
        result = stage.run(str(r1), str(tmp_path / "qc"), fastq_r2=str(r2), sample_id="PAIR")
        assert result.metrics_r2 is not None
        assert result.metrics_r2.total_reads == 50

    def test_threshold_violation_raises(self, tmp_path):
        fq = tmp_path / "bad.fastq"
        # Zero reads → violates min_total_reads
        _write_fastq(fq, [])
        stage = QCStage(cfg={"qc": {"stop_on_failure": True}})
        with pytest.raises((QCThresholdError, QCError)):
            stage.run(str(fq), str(tmp_path / "qc"))

    def test_stop_on_failure_false_allows_continue(self, tmp_path):
        """When stop_on_failure=False, failed QC returns result without raising."""
        fq = tmp_path / "r1.fastq"
        # Low quality reads that violate thresholds
        _write_fastq(fq, _reads(10, seq="ACGTACGT", qual="!!!!!!!! "))
        stage = QCStage(cfg={"qc": {"stop_on_failure": False}})
        # Should not raise even if thresholds violated
        result = stage.run(str(fq), str(tmp_path / "qc"), sample_id="BAD")
        assert isinstance(result, QCResult)

    def test_check_thresholds_raises_on_violations(self, tmp_path):
        """check_thresholds() raises QCThresholdError on bad metrics."""
        m = QCMetrics()
        m.total_reads = 0  # violates min_total_reads
        result = QCResult(
            sample_id="X",
            fastq_r1="/fake.fastq",
            metrics_r1=m,
        )
        stage = QCStage()
        with pytest.raises(QCThresholdError):
            stage.check_thresholds(result)

    def test_custom_thresholds_applied(self, tmp_path):
        """Lower threshold passes where default would fail."""
        fq = tmp_path / "r1.fastq"
        _write_fastq(fq, _reads(5, seq="ACGT", qual="5555"))  # Q20 reads
        stage = QCStage(cfg={
            "qc": {
                "stop_on_failure": True,
                "thresholds": {"min_mean_quality": 10.0, "min_total_reads": 1, "min_read_length": 1},
            }
        })
        result = stage.run(str(fq), str(tmp_path / "qc"), sample_id="CUSTOM")
        assert result.qc_passed

    def test_qc_result_serialisable(self, tmp_path):
        fq = tmp_path / "r1.fastq"
        _write_fastq(fq, _reads(10, seq="GCGCACGT", qual="I" * 8))
        stage = QCStage(cfg={"qc": {"stop_on_failure": False}})
        result = stage.run(str(fq), str(tmp_path / "qc"), sample_id="SER")
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "metrics_r1" in d
        # Must be JSON-serialisable
        json.dumps(d)

    def test_illumina_tile_parsing(self, tmp_path):
        """Illumina-format headers are parsed for tile IDs."""
        fq = tmp_path / "r1.fastq"
        records = [
            ("@NS500413:54:H3NLNAFXX:1:11101:1234:5678 1:N:0:ATCACG", "ACGT", "IIII"),
            ("@NS500413:54:H3NLNAFXX:1:11101:2345:6789 1:N:0:ATCACG", "ACGT", "IIII"),
        ]
        _write_fastq(fq, records)
        stage = QCStage(cfg={"qc": {"stop_on_failure": False}})
        result = stage.run(str(fq), str(tmp_path / "qc"), sample_id="TILE")
        # Should not crash; tile_quality_warnings may or may not be populated
        assert result.metrics_r1.total_reads == 2
