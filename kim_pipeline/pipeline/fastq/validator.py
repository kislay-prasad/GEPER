"""
pipeline/fastq/validator.py
─────────────────────────────
Full structural FASTQ validation — Task 1.

Validates FASTQ files before they enter the alignment stage so that
problems with input data fail loudly at the gate rather than producing
a silently empty or corrupt BAM.

Checks performed for every record:
  - Header line starts with ``@``
  - Sequence contains only valid IUPAC nucleotide characters (A C G T N
    R Y S W K M B D H V, case-insensitive)
  - ``+`` separator line is present
  - Quality string length exactly matches sequence length
  - Phred+33 quality values are in the printable ASCII range (33–126)

Additional checks for paired-end FASTQ files (R1 + R2):
  - Both files have the same number of records
  - Record names match (ignoring ``/1`` / ``/2`` suffix and Illumina
    read-number suffixes) so mismatched pairs are caught before alignment

Truncation / corruption detection:
  - Any partial four-line block at end-of-file is flagged
  - Empty input files are flagged
  - File does not exist → ``FastqValidationError``

This module is deliberately self-contained (no third-party libraries).
Gzip-compressed files (``.gz``) are transparently supported via the
standard library ``gzip`` module.

Usage::

    from pipeline.fastq.validator import FastqValidator, FastqValidationError

    FastqValidator().validate_single("sample_R1.fastq.gz")
    FastqValidator().validate_paired("sample_R1.fastq.gz", "sample_R2.fastq.gz")
"""

from __future__ import annotations

import gzip
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Iterator, List, Tuple

logger = logging.getLogger("geper.pipeline.fastq.validator")

# IUPAC degenerate nucleotide codes (upper-case); lower-case handled via .upper()
_IUPAC_RE = re.compile(r"^[ACGTRYMKSWHBVDN]+$")

# Maximum records scanned in full-detail mode before switching to
# count-only mode — prevents O(n) RAM for multi-GB files.
_FULL_CHECK_LIMIT = 500_000


# ─── Error type ───────────────────────────────────────────────────────────────


class FastqValidationError(Exception):
    """Raised when a FASTQ file fails structural validation.

    Attributes:
        path:   Path to the offending file.
        record: 1-based record number within the file (0 = file-level error).
        detail: Human-readable description of the failure.
    """

    def __init__(self, path: str, detail: str, record: int = 0) -> None:
        location = f"record {record}" if record else "file"
        super().__init__(f"FASTQ validation failed ({location}) in {path!r}: {detail}")
        self.path = path
        self.record = record
        self.detail = detail


# ─── Result type ──────────────────────────────────────────────────────────────


@dataclass
class FastqStats:
    """Summary statistics produced by successful validation.

    All counts refer to the number of *read records* (4-line blocks),
    not lines.
    """

    path: str = ""
    total_records: int = 0
    min_read_length: int = 0
    max_read_length: int = 0
    mean_read_length: float = 0.0
    is_gzipped: bool = False
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "total_records": self.total_records,
            "min_read_length": self.min_read_length,
            "max_read_length": self.max_read_length,
            "mean_read_length": round(self.mean_read_length, 2),
            "is_gzipped": self.is_gzipped,
            "warnings": self.warnings,
        }


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _open_fastq(path: str) -> IO:
    """Open a FASTQ file in text mode, transparently decompressing .gz."""
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def _iter_records(
    fh: IO,
) -> Iterator[Tuple[str, str, str, str]]:
    """Yield (header, sequence, plus, quality) 4-tuples from an open FASTQ handle."""
    while True:
        h = fh.readline()
        if not h:
            return
        s = fh.readline()
        p = fh.readline()
        q = fh.readline()
        yield h.rstrip("\n"), s.rstrip("\n"), p.rstrip("\n"), q.rstrip("\n")


def _strip_read_suffix(name: str) -> str:
    """Normalise a FASTQ read name for paired-end matching.

    Strips Illumina ``/1`` / ``/2`` suffixes and the ``1:N:0:`` style
    read-number field so that R1 and R2 names can be compared as equal.

    Examples::
        "@READ.1/1"  → "READ.1"
        "@READ.1 1:N:0:ATCACG"  → "READ.1"
    """
    # Strip leading @
    name = name.lstrip("@").split()[0]
    # Strip /1 /2
    if name.endswith(("/1", "/2")):
        name = name[:-2]
    return name


# ─── Validator class ──────────────────────────────────────────────────────────


class FastqValidator:
    """Structural FASTQ validator.

    Args:
        max_full_check:
            Number of records to validate character-by-character (IUPAC,
            quality range). Beyond this limit the validator only counts
            records (still catches truncation). Default 500 000.
    """

    def __init__(self, max_full_check: int = _FULL_CHECK_LIMIT) -> None:
        self._max_full_check = max_full_check

    # ── single-file validation ────────────────────────────────────────────────

    def validate_single(self, path: str) -> FastqStats:
        """Validate a single FASTQ file (R1 or single-end).

        Args:
            path: Filesystem path to the FASTQ file (plain or gzipped).

        Returns:
            ``FastqStats`` with per-file summary statistics.

        Raises:
            FastqValidationError: on any structural problem.
        """
        p = Path(path)
        if not p.exists():
            raise FastqValidationError(path, "file does not exist")
        if p.stat().st_size == 0:
            raise FastqValidationError(path, "file is empty (0 bytes)")

        is_gz = path.endswith(".gz")
        stats = FastqStats(path=path, is_gzipped=is_gz)
        total_length = 0

        logger.info("Validating FASTQ: %s", path)

        with _open_fastq(path) as fh:
            for record_idx, (header, seq, plus, qual) in enumerate(_iter_records(fh), start=1):
                # ── structural checks ────────────────────────────────────
                if not header:
                    # readline returned empty → file ended mid-block
                    raise FastqValidationError(
                        path,
                        f"Truncated file: record {record_idx} has no header line "
                        f"(file ended after {record_idx - 1} complete records).",
                        record=record_idx,
                    )
                if not header.startswith("@"):
                    raise FastqValidationError(
                        path,
                        f"Header line does not start with '@': {header[:80]!r}",
                        record=record_idx,
                    )
                if not seq:
                    raise FastqValidationError(path, "Empty sequence line.", record=record_idx)
                if not plus.startswith("+"):
                    raise FastqValidationError(
                        path,
                        f"Expected '+' separator, got: {plus[:80]!r}",
                        record=record_idx,
                    )
                if len(qual) != len(seq):
                    raise FastqValidationError(
                        path,
                        f"Quality length ({len(qual)}) != sequence length ({len(seq)}).",
                        record=record_idx,
                    )

                # ── character-level checks (up to max_full_check) ────────
                if record_idx <= self._max_full_check:
                    seq_upper = seq.upper()
                    if not _IUPAC_RE.match(seq_upper):
                        bad = [c for c in seq_upper if not re.match(r"[ACGTRYMKSWHBVDN]", c)]
                        raise FastqValidationError(
                            path,
                            f"Invalid IUPAC nucleotide character(s) in sequence: {bad[:5]!r}",
                            record=record_idx,
                        )
                    for qi, qc in enumerate(qual):
                        code = ord(qc)
                        if code < 33 or code > 126:
                            raise FastqValidationError(
                                path,
                                f"Quality character at position {qi} has ASCII code "
                                f"{code}, outside printable range 33–126.",
                                record=record_idx,
                            )
                elif record_idx == self._max_full_check + 1:
                    warn = (
                        f"File has >{self._max_full_check:,} records; switching to "
                        "count-only mode (truncation still detected)."
                    )
                    logger.warning(warn)
                    stats.warnings.append(warn)

                rlen = len(seq)
                total_length += rlen
                if record_idx == 1:
                    stats.min_read_length = rlen
                    stats.max_read_length = rlen
                else:
                    if rlen < stats.min_read_length:
                        stats.min_read_length = rlen
                    if rlen > stats.max_read_length:
                        stats.max_read_length = rlen

        stats.total_records = record_idx  # type: ignore[possibly-undefined]
        if stats.total_records == 0:
            raise FastqValidationError(path, "File contains zero FASTQ records.")
        stats.mean_read_length = total_length / stats.total_records

        logger.info(
            "FASTQ OK: %s — %d records, read length %d–%d bp",
            path,
            stats.total_records,
            stats.min_read_length,
            stats.max_read_length,
        )
        return stats

    # ── paired-end validation ─────────────────────────────────────────────────

    def validate_paired(self, r1_path: str, r2_path: str) -> Tuple[FastqStats, FastqStats]:
        """Validate a paired-end FASTQ pair (R1 + R2).

        Validates each file individually for structural correctness, then
        checks that both files have the same number of records and that
        read names correspond.

        Args:
            r1_path: Path to the R1 (forward) FASTQ file.
            r2_path: Path to the R2 (reverse) FASTQ file.

        Returns:
            ``(r1_stats, r2_stats)`` tuple.

        Raises:
            FastqValidationError: if any structural check fails.
        """
        r1_stats = self.validate_single(r1_path)
        r2_stats = self.validate_single(r2_path)

        if r1_stats.total_records != r2_stats.total_records:
            raise FastqValidationError(
                r1_path,
                f"Paired-end record count mismatch: R1 has "
                f"{r1_stats.total_records:,} records but R2 has "
                f"{r2_stats.total_records:,} records — files are not properly paired.",
            )

        # Spot-check name correspondence for the first 1 000 records
        # (full check is O(n) which is prohibitive for large files)
        mismatch_limit = min(1_000, r1_stats.total_records)
        name_mismatches: List[str] = []

        with _open_fastq(r1_path) as fh1, _open_fastq(r2_path) as fh2:
            for idx, ((h1, _, _, _), (h2, _, _, _)) in enumerate(
                zip(_iter_records(fh1), _iter_records(fh2)), start=1
            ):
                if idx > mismatch_limit:
                    break
                n1 = _strip_read_suffix(h1)
                n2 = _strip_read_suffix(h2)
                if n1 != n2:
                    name_mismatches.append(f"record {idx}: R1={h1[:60]!r} vs R2={h2[:60]!r}")
                if len(name_mismatches) >= 5:
                    break

        if name_mismatches:
            raise FastqValidationError(
                r1_path,
                "Paired-end read name mismatch (R1/R2 are not in the same order "
                "or are from different samples). First mismatches:\n" + "\n".join(name_mismatches),
            )

        logger.info(
            "Paired-end FASTQ OK: %d records in each of R1=%s and R2=%s",
            r1_stats.total_records,
            r1_path,
            r2_path,
        )
        return r1_stats, r2_stats
