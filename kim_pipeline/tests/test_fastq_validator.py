"""
tests/test_fastq_validator.py
──────────────────────────────
Integration tests for pipeline/fastq/validator.py.

All tests write real FASTQ files and call the actual validator — no mocks.
Tests are fast because the input data is tiny (synthetic, in-memory).
"""

from __future__ import annotations

import gzip
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.fastq.validator import FastqValidator, FastqValidationError


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _write_fastq(path: Path, records: list[tuple[str, str, str, str]]) -> None:
    """Write a FASTQ file from a list of (header, seq, plus, qual) tuples."""
    with open(path, "w") as fh:
        for h, s, p, q in records:
            fh.write(f"{h}\n{s}\n{p}\n{q}\n")


def _minimal_record(seq: str = "ACGT", qual: str | None = None) -> tuple:
    qual = qual or "I" * len(seq)
    return ("@read.1", seq, "+", qual)


# ─── Single-file tests ────────────────────────────────────────────────────────

class TestValidateSingle:
    def test_valid_fastq_returns_stats(self, tmp_path):
        p = tmp_path / "r1.fastq"
        _write_fastq(p, [_minimal_record("ACGTACGT")])
        stats = FastqValidator().validate_single(str(p))
        assert stats.total_records == 1
        assert stats.min_read_length == 8
        assert stats.max_read_length == 8
        assert stats.mean_read_length == 8.0
        assert not stats.is_gzipped

    def test_multi_record_fastq(self, tmp_path):
        p = tmp_path / "r1.fastq"
        records = [_minimal_record(f"ACGT{'A' * i}") for i in range(5)]
        _write_fastq(p, records)
        stats = FastqValidator().validate_single(str(p))
        assert stats.total_records == 5
        assert stats.min_read_length == 4
        assert stats.max_read_length == 8  # 4 + 4 bases on last record

    def test_gzipped_fastq_accepted(self, tmp_path):
        p = tmp_path / "r1.fastq.gz"
        content = "@read.1\nACGT\n+\nIIII\n"
        with gzip.open(str(p), "wt") as fh:
            fh.write(content)
        stats = FastqValidator().validate_single(str(p))
        assert stats.is_gzipped
        assert stats.total_records == 1

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FastqValidationError) as exc_info:
            FastqValidator().validate_single(str(tmp_path / "nope.fastq"))
        assert "does not exist" in str(exc_info.value)

    def test_empty_file_raises(self, tmp_path):
        p = tmp_path / "empty.fastq"
        p.write_text("")
        with pytest.raises(FastqValidationError) as exc_info:
            FastqValidator().validate_single(str(p))
        assert "empty" in str(exc_info.value).lower()

    def test_header_without_at_raises(self, tmp_path):
        p = tmp_path / "bad_header.fastq"
        _write_fastq(p, [("NOTAHEADER", "ACGT", "+", "IIII")])
        with pytest.raises(FastqValidationError) as exc_info:
            FastqValidator().validate_single(str(p))
        assert "Header" in str(exc_info.value) or "header" in str(exc_info.value).lower()

    def test_quality_length_mismatch_raises(self, tmp_path):
        p = tmp_path / "qual_mismatch.fastq"
        _write_fastq(p, [("@read.1", "ACGTACGT", "+", "III")])  # qual shorter than seq
        with pytest.raises(FastqValidationError) as exc_info:
            FastqValidator().validate_single(str(p))
        assert "length" in str(exc_info.value).lower()

    def test_invalid_iupac_character_raises(self, tmp_path):
        p = tmp_path / "bad_seq.fastq"
        _write_fastq(p, [("@read.1", "ACGT1XYZ", "+", "IIIIIIII")])
        with pytest.raises(FastqValidationError) as exc_info:
            FastqValidator().validate_single(str(p))
        assert "IUPAC" in str(exc_info.value) or "invalid" in str(exc_info.value).lower()

    def test_missing_plus_separator_raises(self, tmp_path):
        p = tmp_path / "no_plus.fastq"
        _write_fastq(p, [("@read.1", "ACGT", "WRONG", "IIII")])
        with pytest.raises(FastqValidationError) as exc_info:
            FastqValidator().validate_single(str(p))
        assert "separator" in str(exc_info.value).lower() or "'+'" in str(exc_info.value)

    def test_truncated_file_raises(self, tmp_path):
        """A file that ends mid-record (only 3 lines for the last record)."""
        p = tmp_path / "truncated.fastq"
        # Two complete records then a truncated one
        with open(p, "w") as fh:
            fh.write("@read.1\nACGT\n+\nIIII\n")
            fh.write("@read.2\nACGT\n")  # missing + and qual
        with pytest.raises(FastqValidationError) as exc_info:
            FastqValidator().validate_single(str(p))
        # Should catch missing + separator for truncated record
        assert exc_info.value is not None

    def test_iupac_degenerate_bases_accepted(self, tmp_path):
        """N and IUPAC degenerate codes should pass validation."""
        p = tmp_path / "iupac.fastq"
        _write_fastq(p, [("@read.1", "ACGTNRYMK", "+", "IIIIIIIII")])
        stats = FastqValidator().validate_single(str(p))
        assert stats.total_records == 1


# ─── Paired-end tests ─────────────────────────────────────────────────────────

class TestValidatePaired:
    def _make_pair(
        self,
        tmp_path: Path,
        n: int = 3,
        r1_seq: str = "ACGT",
        r2_seq: str = "TTTT",
    ) -> tuple[Path, Path]:
        r1 = tmp_path / "r1.fastq"
        r2 = tmp_path / "r2.fastq"
        _write_fastq(r1, [("@read.{i}", r1_seq, "+", "I" * len(r1_seq)) for i in range(n)])
        # Replace {i} with proper formatting
        records_r1 = [(f"@read.{i}", r1_seq, "+", "I" * len(r1_seq)) for i in range(n)]
        records_r2 = [(f"@read.{i}", r2_seq, "+", "I" * len(r2_seq)) for i in range(n)]
        _write_fastq(r1, records_r1)
        _write_fastq(r2, records_r2)
        return r1, r2

    def test_valid_pair_returns_both_stats(self, tmp_path):
        r1, r2 = self._make_pair(tmp_path)
        s1, s2 = FastqValidator().validate_paired(str(r1), str(r2))
        assert s1.total_records == 3
        assert s2.total_records == 3

    def test_record_count_mismatch_raises(self, tmp_path):
        r1 = tmp_path / "r1.fastq"
        r2 = tmp_path / "r2.fastq"
        _write_fastq(r1, [("@r1", "ACGT", "+", "IIII"), ("@r2", "ACGT", "+", "IIII")])
        _write_fastq(r2, [("@r1", "ACGT", "+", "IIII")])  # 1 fewer record
        with pytest.raises(FastqValidationError) as exc_info:
            FastqValidator().validate_paired(str(r1), str(r2))
        assert "mismatch" in str(exc_info.value).lower()

    def test_read_name_mismatch_raises(self, tmp_path):
        r1 = tmp_path / "r1.fastq"
        r2 = tmp_path / "r2.fastq"
        _write_fastq(r1, [("@readA", "ACGT", "+", "IIII")])
        _write_fastq(r2, [("@readB", "ACGT", "+", "IIII")])  # different name
        with pytest.raises(FastqValidationError) as exc_info:
            FastqValidator().validate_paired(str(r1), str(r2))
        assert "name" in str(exc_info.value).lower() or "mismatch" in str(exc_info.value).lower()

    def test_illumina_suffix_stripped_correctly(self, tmp_path):
        """@read.1/1 and @read.1/2 should be treated as the same read name."""
        r1 = tmp_path / "r1.fastq"
        r2 = tmp_path / "r2.fastq"
        _write_fastq(r1, [("@read.1/1", "ACGT", "+", "IIII")])
        _write_fastq(r2, [("@read.1/2", "TTTT", "+", "IIII")])
        s1, s2 = FastqValidator().validate_paired(str(r1), str(r2))
        assert s1.total_records == 1
