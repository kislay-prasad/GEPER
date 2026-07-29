"""
Pre-flight structural validation for raw sequencing input files
(FASTQ, BAM, CRAM), meant to run BEFORE any expensive downstream
processing (alignment, variant calling) starts.

ARCHITECTURE NOTE -- read before wiring this in anywhere: GEPER's
implemented pipeline (`pipeline/orchestrator.py::GeperPipeline`,
invoked via `main.py --vcf`) does not itself ingest FASTQ/BAM/CRAM,
align reads, or call variants -- it consumes an already-called VCF
only (see `pipeline/vcf_parser.py`). The "9-stage FASTQ-to-report
pipeline" mentioned in `pipeline/acmg_rules.py`'s PP4 docstring is this
project's documented *target* architecture; the stages upstream of VCF
calling (raw-read intake, alignment, variant calling) are not
implemented anywhere in this codebase today. This module is therefore
standalone, dependency-free infrastructure for wherever that intake
eventually happens (a future GEPER stage, or a pre-flight check ahead
of an external aligner/caller a lab already runs) -- it is intentionally
NOT wired into `GeperPipeline.run()`, because no code path there
consumes these file types. See `validate_input.py` (repo root) for a
standalone CLI gate that can be run ahead of an external pipeline today.

Design choice: pure Python stdlib (gzip/struct/zlib), no pysam/htslib
dependency -- pysam has inconsistent prebuilt-wheel availability on
Windows, and this validates *structural* integrity (magic bytes,
headers, index files) rather than parsing every alignment record,
which stdlib parsing handles correctly without a new C-extension
dependency. Scope is deliberately bounded and disclosed per format --
see each validator's docstring for exactly what is and isn't checked.
"""

from __future__ import annotations

import gzip
import os
import struct
import zlib
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from utils.exceptions import RawInputValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

# Standard nucleotide codes (case-insensitive). 'N' is always legal
# (unknown base); IUPAC ambiguity codes (R/Y/S/W/K/M/B/D/H/V) are legal
# in some FASTQ dialects but not raw sequencer output -- treated as a
# warning, not a hard failure, below.
_VALID_BASES = frozenset("ACGTNacgtn")
_IUPAC_AMBIGUITY_BASES = frozenset("RYSWKMBDHVryswkmbdhv")

# Phred quality characters span ASCII 33 ('!', Phred+33 Q0) through 126
# ('~') across every encoding in real-world use (Phred+33 modern
# Illumina/Sanger, and the legacy Phred+64 Illumina 1.3-1.7 encoding,
# whose scores top out well under 126) -- this is intentionally the
# union of both, since GEPER has no way to know the encoding a given
# file uses ahead of time and must not reject a legitimate file in
# either encoding.
_PHRED_MIN_ORD = 33
_PHRED_MAX_ORD = 126

_BAM_MAGIC = b"BAM\x01"
_BAI_MAGIC = b"BAI\x01"
_CRAM_MAGIC = b"CRAM"
_PLAIN_GZIP_MAGIC = bytes([0x1F, 0x8B])

_HUMAN_FORMAT_NAMES = {
    "fastq": "FASTQ (plain text)",
    "gzip_fastq": "gzip-compressed text (likely FASTQ.gz)",
    "bam": "BAM",
    "cram": "CRAM",
    "bai_index": "BAM index (.bai)",
    "bgzf_unknown": "a BGZF-compressed file that is not BAM (e.g. BCF, tabix .gz)",
    "corrupt_gzip": "a file with a gzip-looking header but corrupt/unreadable compressed data",
    "empty": "an empty (0-byte) file",
    "missing": "a missing file",
    "unknown": "an unrecognized binary format",
}


@dataclass
class ValidationResult:
    """Result of validating one raw input file. `errors` are always specific and actionable (see each validator's docstring)."""

    path: str
    detected_format: Optional[str]
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def raise_if_invalid(self) -> None:
        """Convenience for callers that want a gate (raise) rather than a result object to inspect."""
        if not self.is_valid:
            raise RawInputValidationError(
                f"Input file '{self.path}' failed pre-flight validation:\n"
                + "\n".join(f"  - {e}" for e in self.errors)
            )

    def summary_line(self) -> str:
        status = "PASS" if self.is_valid else "FAIL"
        return f"[{status}] {self.path} (detected format: {self.detected_format})"


# ---------------------------------------------------------------------------
# Format sniffing -- never trusts the file extension, exactly so a
# wrong-file-type-entirely mistake (e.g. a FASTQ named "sample.bam") is
# still caught.
# ---------------------------------------------------------------------------

def sniff_format(path: str) -> str:
    """Best-effort format detection from magic bytes / leading content, independent of the file's extension."""
    if not os.path.exists(path):
        return "missing"
    if os.path.getsize(path) == 0:
        return "empty"

    with open(path, "rb") as fh:
        prefix = fh.read(18)

    if prefix[:4] == _CRAM_MAGIC:
        return "cram"
    if prefix[:4] == _BAI_MAGIC:
        return "bai_index"

    if prefix[:2] == _PLAIN_GZIP_MAGIC:
        # Distinguish BGZF (BAM's/CRAI's container format) from a
        # plain, non-BGZF gzip stream (e.g. an ordinary fastq.gz):
        # BGZF sets the FEXTRA flag (gzip header byte 3, bit 0x04) and
        # carries a "BC" extra subfield -- see the SAM spec's BGZF
        # section. A plain gzip stream (no FEXTRA) is not BGZF at all.
        if len(prefix) >= 4 and prefix[3] & 0x04:
            try:
                with gzip.GzipFile(path, "rb") as gz:
                    decompressed_head = gz.read(4)
            except (OSError, EOFError):
                return "corrupt_gzip"
            if decompressed_head == _BAM_MAGIC:
                return "bam"
            return "bgzf_unknown"
        return "gzip_fastq"

    if prefix[:1] == b"@":
        return "fastq"

    return "unknown"


# ---------------------------------------------------------------------------
# FASTQ
# ---------------------------------------------------------------------------

def validate_fastq(path: str, max_errors_to_report: int = 5) -> ValidationResult:
    """
    Validates FASTQ structural integrity:
      - the file exists and is non-empty
      - it is actually FASTQ (or gzip-compressed FASTQ), not some other format
      - every record has the full 4-line block shape: '@'-prefixed
        header / sequence / '+'-prefixed separator / quality
      - the sequence and quality lines are the same length (one
        quality score per base -- the single most common real-world
        FASTQ corruption)
      - sequence characters are valid nucleotide codes (A/C/G/T/N,
        case-insensitive; IUPAC ambiguity codes warn rather than fail)
      - quality characters fall within the legal Phred ASCII range

    Stops after `max_errors_to_report` distinct problems (a garbled
    file can otherwise produce thousands of near-duplicate error lines
    that bury the actual diagnosis) but always reports that more may
    remain.
    """
    result = ValidationResult(path=path, detected_format=None, is_valid=True)

    if not os.path.exists(path):
        result.is_valid = False
        result.errors.append(f"File does not exist: '{path}'.")
        return result
    if os.path.getsize(path) == 0:
        result.detected_format = "empty"
        result.is_valid = False
        result.errors.append(f"File '{path}' is empty (0 bytes) -- there is no sequencing data to process.")
        return result

    fmt = sniff_format(path)
    if fmt not in ("fastq", "gzip_fastq"):
        result.detected_format = fmt
        result.is_valid = False
        result.errors.append(
            f"'{path}' does not look like a FASTQ file (detected: {_HUMAN_FORMAT_NAMES.get(fmt, fmt)}). "
            f"A FASTQ file must start with '@' (plain text) or be a gzip-compressed FASTQ (.fastq.gz). "
            f"Check that the correct file was supplied."
        )
        return result
    result.detected_format = "fastq"
    is_gz = fmt == "gzip_fastq"

    opener = gzip.open if is_gz else open
    try:
        with opener(path, "rt", encoding="ascii", errors="strict") as fh:
            _scan_fastq_lines(fh, result, max_errors_to_report)
    except gzip.BadGzipFile as exc:
        result.is_valid = False
        result.errors.append(f"'{path}' has a gzip-looking header but is not a valid gzip stream (corrupt compression): {exc}")
        return result
    except EOFError:
        result.is_valid = False
        result.errors.append(
            f"'{path}' is a truncated gzip file -- the compressed stream ends abruptly before its declared "
            f"end. The file was likely cut off during transfer or writing."
        )
        return result
    except UnicodeDecodeError as exc:
        result.is_valid = False
        result.errors.append(
            f"'{path}' contains a non-ASCII/binary byte at offset {exc.start} where FASTQ text was expected. "
            f"This usually means the file is not actually FASTQ, or is corrupted/truncated mid-character."
        )
        return result

    if result.errors:
        result.is_valid = False
    return result


def _scan_fastq_lines(fh, result: ValidationResult, max_errors: int) -> None:
    record_index = 0
    line_number = 0
    records_scanned = 0

    while True:
        header = fh.readline()
        if header == "":
            break  # clean EOF between records -- the normal, valid end of file
        line_number += 1
        seq = fh.readline()
        plus = fh.readline()
        qual = fh.readline()
        line_number += 3
        record_index += 1

        if seq == "" or plus == "" or qual == "":
            result.errors.append(
                f"Record {record_index} (starting at line {line_number - 3}) is truncated: the file ends "
                f"mid-record instead of completing the 4-line FASTQ block (header / sequence / '+' / "
                f"quality). The file was likely cut off during transfer or writing."
            )
            break

        header = header.rstrip("\n")
        seq = seq.rstrip("\n")
        plus = plus.rstrip("\n")
        qual = qual.rstrip("\n")

        if not header.startswith("@"):
            result.errors.append(
                f"Record {record_index}, line {line_number - 3}: expected a FASTQ header starting with '@', "
                f"found '{_truncate(header)}'. Every 4-line FASTQ record must start with an '@' header line."
            )
        if not plus.startswith("+"):
            result.errors.append(
                f"Record {record_index}, line {line_number - 1}: expected the FASTQ separator line to start "
                f"with '+', found '{_truncate(plus)}'."
            )
        if len(seq) != len(qual):
            result.errors.append(
                f"Record {record_index} ('{_truncate(header, 40)}'): sequence line is {len(seq)} character(s) "
                f"but the quality line is {len(qual)} character(s) -- these must always match exactly, one "
                f"quality score per base."
            )

        bad_base = _find_invalid_char(seq, _VALID_BASES | _IUPAC_AMBIGUITY_BASES)
        if bad_base is not None:
            char, pos = bad_base
            result.errors.append(
                f"Record {record_index} ('{_truncate(header, 40)}'): invalid character '{char}' in the "
                f"sequence at position {pos + 1} (expected A/C/G/T/N, case-insensitive)."
            )
        elif _find_invalid_char(seq, _VALID_BASES) is not None:
            char, pos = _find_invalid_char(seq, _VALID_BASES)
            result.warnings.append(
                f"Record {record_index} ('{_truncate(header, 40)}'): IUPAC ambiguity code '{char}' at "
                f"position {pos + 1} in the sequence -- valid, but unusual for raw sequencer output."
            )

        bad_qual = _find_invalid_quality(qual)
        if bad_qual is not None:
            char, pos = bad_qual
            result.errors.append(
                f"Record {record_index} ('{_truncate(header, 40)}'): quality string has character '{char}' "
                f"(ASCII {ord(char)}) at position {pos + 1}, outside the valid Phred quality range "
                f"(ASCII {_PHRED_MIN_ORD}-{_PHRED_MAX_ORD})."
            )

        records_scanned += 1
        if len(result.errors) >= max_errors:
            result.errors.append(
                f"... stopping after {max_errors} distinct problem(s) found in the first {records_scanned} "
                f"record(s); there may be more. Fix the issues above and re-validate."
            )
            break


def _truncate(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[:n] + "..."


def _find_invalid_char(text: str, allowed: frozenset) -> Optional[Tuple[str, int]]:
    for i, ch in enumerate(text):
        if ch not in allowed:
            return ch, i
    return None


def _find_invalid_quality(qual: str) -> Optional[Tuple[str, int]]:
    for i, ch in enumerate(qual):
        code = ord(ch)
        if code < _PHRED_MIN_ORD or code > _PHRED_MAX_ORD:
            return ch, i
    return None


# ---------------------------------------------------------------------------
# BAM
# ---------------------------------------------------------------------------

def validate_bam(path: str, index_path: Optional[str] = None) -> ValidationResult:
    """
    Validates BAM structural integrity: a correct BGZF container, the
    correct 'BAM\\x01' magic literal once decompressed, and a
    parseable SAM header block (header text length + text, reference
    count, reference name/length entries) -- the same header
    information `samtools view -H` needs to succeed.

    Scope, disclosed: this checks the file's HEADER structure only, not
    individual alignment records -- a full binary alignment-record
    parser is out of scope for a pre-flight structural gate (and is
    what pysam/htslib exist for; see this module's docstring for why
    that dependency isn't taken on here). A file that passes this
    check has a structurally sound header but could still have
    corruption later in its alignment records.

    If `index_path` is given, or a '<path>.bai' sibling file exists,
    that index's own structural integrity is checked too (see
    `_validate_bai_index`).
    """
    result = ValidationResult(path=path, detected_format=None, is_valid=True)

    if not os.path.exists(path):
        result.is_valid = False
        result.errors.append(f"File does not exist: '{path}'.")
        return result
    if os.path.getsize(path) == 0:
        result.detected_format = "empty"
        result.is_valid = False
        result.errors.append(f"File '{path}' is empty (0 bytes) -- there is no alignment data to process.")
        return result

    fmt = sniff_format(path)
    if fmt == "cram":
        result.detected_format = "cram"
        result.is_valid = False
        result.errors.append(
            f"'{path}' is a CRAM file (detected the CRAM file signature), not BAM. If CRAM was intended, use "
            f"validate_cram() instead; if BAM was intended, the wrong file was supplied."
        )
        return result
    if fmt == "fastq":
        result.detected_format = "fastq"
        result.is_valid = False
        result.errors.append(
            f"'{path}' looks like a FASTQ file (starts with '@'), not BAM. A raw, unaligned FASTQ cannot be "
            f"used where an aligned BAM file is expected -- check that the correct file was supplied."
        )
        return result
    if fmt == "gzip_fastq":
        result.detected_format = "gzip_fastq"
        result.is_valid = False
        result.errors.append(
            f"'{path}' is gzip-compressed but not in BGZF format (BAM requires BGZF, a block-structured "
            f"variant of gzip) -- this looks like an ordinary gzipped text file (e.g. FASTQ.gz), not BAM."
        )
        return result
    if fmt != "bam":
        result.detected_format = fmt
        result.is_valid = False
        result.errors.append(
            f"'{path}' does not look like a valid BAM file (detected: {_HUMAN_FORMAT_NAMES.get(fmt, fmt)}). "
            f"A BAM file must be BGZF-compressed and begin with the 'BAM\\x01' signature once decompressed."
        )
        return result
    result.detected_format = "bam"

    try:
        with gzip.GzipFile(path, "rb") as fh:
            magic = fh.read(4)
            if len(magic) < 4:
                result.is_valid = False
                result.errors.append(f"'{path}' is truncated: the file ends before its BAM signature could even be read.")
                return result
            if magic != _BAM_MAGIC:
                result.is_valid = False
                result.errors.append(
                    f"'{path}' is BGZF-compressed but its decompressed content does not start with the BAM "
                    f"signature (found {magic!r} instead of b'BAM\\x01'). The file is likely corrupt, or is a "
                    f"different BGZF-based format (e.g. BCF, a tabix-indexed VCF.gz)."
                )
                return result

            l_text = _read_int32(fh, f"'{path}': truncated before the BAM header-length field could be read.", result)
            if l_text is None:
                return result
            if l_text < 0:
                result.is_valid = False
                result.errors.append(f"'{path}' has a corrupt BAM header: negative header-text length ({l_text}).")
                return result

            header_text = fh.read(l_text)
            if len(header_text) < l_text:
                result.is_valid = False
                result.errors.append(
                    f"'{path}' is truncated: the BAM header declares {l_text} byte(s) of SAM header text but "
                    f"only {len(header_text)} byte(s) were readable before the file ended."
                )
                return result

            n_ref = _read_int32(fh, f"'{path}': truncated before the reference-sequence count could be read.", result)
            if n_ref is None:
                return result
            if n_ref < 0:
                result.is_valid = False
                result.errors.append(f"'{path}' has a corrupt BAM header: negative reference-sequence count ({n_ref}).")
                return result

            for i in range(n_ref):
                entry_error = f"'{path}' is truncated: reference-sequence entry {i + 1}/{n_ref} is incomplete."
                l_name = _read_int32(fh, entry_error, result)
                if l_name is None:
                    return result
                name = fh.read(l_name)
                if len(name) < l_name:
                    result.is_valid = False
                    result.errors.append(entry_error)
                    return result
                l_ref = _read_int32(fh, entry_error, result)
                if l_ref is None:
                    return result
    except EOFError:
        result.is_valid = False
        result.errors.append(
            f"'{path}' is a truncated BGZF/gzip file -- the compressed stream ends abruptly before its "
            f"declared end. The file was likely cut off during transfer or writing."
        )
        return result
    except (OSError, zlib.error) as exc:
        result.is_valid = False
        result.errors.append(f"'{path}' could not be decompressed -- the BGZF/gzip compressed data is corrupt: {exc}")
        return result

    _validate_bai_index(path, index_path, result)
    return result


def _read_int32(fh, error_message: str, result: ValidationResult) -> Optional[int]:
    raw = fh.read(4)
    if len(raw) < 4:
        result.is_valid = False
        result.errors.append(error_message)
        return None
    (value,) = struct.unpack("<i", raw)
    return value


def _validate_bai_index(bam_path: str, index_path: Optional[str], result: ValidationResult) -> None:
    """
    BAM index (.bai) check: file exists, is non-empty, is readable, and
    starts with the correct 'BAI\\x01' magic literal. This does not
    parse the index's internal bin/chunk/linear-index structure (see
    this module's docstring for the pysam/htslib scope boundary) --
    it catches a missing, empty, truncated, or wrong-format index file,
    which is what "unreadable index files" in practice means for a
    pre-flight gate.
    """
    resolved = index_path or f"{bam_path}.bai"
    if not os.path.exists(resolved):
        result.warnings.append(
            f"No BAM index found at '{resolved}' -- not required for every use, but random-access "
            f"(region) queries will fail without one."
        )
        return

    if os.path.getsize(resolved) == 0:
        result.is_valid = False
        result.errors.append(f"BAM index '{resolved}' is empty (0 bytes) -- it is corrupt or was never finished writing.")
        return

    try:
        with open(resolved, "rb") as fh:
            magic = fh.read(4)
    except OSError as exc:
        result.is_valid = False
        result.errors.append(f"BAM index '{resolved}' could not be read: {exc}")
        return

    if magic != _BAI_MAGIC:
        result.is_valid = False
        result.errors.append(
            f"BAM index '{resolved}' does not start with the expected 'BAI\\x01' signature (found {magic!r}) "
            f"-- it is corrupt, truncated, or not actually a .bai file. Re-index the BAM file (e.g. "
            f"`samtools index`)."
        )


# ---------------------------------------------------------------------------
# CRAM
# ---------------------------------------------------------------------------

# CRAM's file-definition structure (magic + version + file ID) is a
# stable, foundational part of the CRAM spec. Deeper container/block-
# level structure is NOT parsed here -- see this module's docstring for
# why (that level of detail is squarely pysam/htslib's job). This is
# disclosed rather than silently implied to be a full CRAM parser.
_CRAM_FILE_DEFINITION_LEN = 4 + 1 + 1 + 20  # magic + major + minor + 20-byte file ID


def validate_cram(path: str, index_path: Optional[str] = None) -> ValidationResult:
    """
    Validates CRAM's file-definition header only: the 'CRAM' magic
    literal, a plausible major/minor version, and that the file is at
    least long enough to contain the fixed-size file-definition block.
    Does NOT parse CRAM's container/block/slice structure (see this
    module's docstring and the constant above) -- a file that passes
    this check has a structurally sound file-definition header but
    could still have corruption in its container data.
    """
    result = ValidationResult(path=path, detected_format=None, is_valid=True)

    if not os.path.exists(path):
        result.is_valid = False
        result.errors.append(f"File does not exist: '{path}'.")
        return result
    if os.path.getsize(path) == 0:
        result.detected_format = "empty"
        result.is_valid = False
        result.errors.append(f"File '{path}' is empty (0 bytes) -- there is no alignment data to process.")
        return result

    fmt = sniff_format(path)
    if fmt == "bam":
        result.detected_format = "bam"
        result.is_valid = False
        result.errors.append(
            f"'{path}' is a BAM file (detected the BGZF/BAM signature), not CRAM. If BAM was intended, use "
            f"validate_bam() instead; if CRAM was intended, the wrong file was supplied."
        )
        return result
    if fmt == "fastq":
        result.detected_format = "fastq"
        result.is_valid = False
        result.errors.append(
            f"'{path}' looks like a FASTQ file (starts with '@'), not CRAM. A raw, unaligned FASTQ cannot be "
            f"used where an aligned CRAM file is expected -- check that the correct file was supplied."
        )
        return result
    if fmt != "cram":
        result.detected_format = fmt
        result.is_valid = False
        result.errors.append(
            f"'{path}' does not look like a valid CRAM file (detected: {_HUMAN_FORMAT_NAMES.get(fmt, fmt)}). "
            f"A CRAM file must begin with the 'CRAM' signature."
        )
        return result
    result.detected_format = "cram"

    size = os.path.getsize(path)
    if size < _CRAM_FILE_DEFINITION_LEN:
        result.is_valid = False
        result.errors.append(
            f"'{path}' is truncated: only {size} byte(s) long, too short to even contain a complete CRAM "
            f"file-definition header ({_CRAM_FILE_DEFINITION_LEN} bytes required). The file was likely cut "
            f"off during transfer or writing."
        )
        return result

    with open(path, "rb") as fh:
        header = fh.read(_CRAM_FILE_DEFINITION_LEN)
    major, minor = header[4], header[5]
    if major not in (2, 3, 4):
        result.warnings.append(
            f"'{path}' declares CRAM format version {major}.{minor}, which this validator does not "
            f"recognize (expected major version 2, 3, or 4) -- it may be a newer format than this check knows "
            f"about, or the version field itself may be corrupt."
        )

    _validate_crai_index(path, index_path, result)
    return result


def _validate_crai_index(cram_path: str, index_path: Optional[str], result: ValidationResult) -> None:
    """
    CRAM index (.crai) check: file exists, is non-empty, readable, and
    is a valid (not corrupt) gzip stream -- .crai is a gzipped text
    listing of slice offsets. This is a shallower check than
    `_validate_bai_index`'s (no magic-literal equivalent to check for
    .crai): it catches missing/empty/truncated-compression index
    files, but does not validate the decompressed listing's own
    structure. Honest gap: a .crai that decompresses cleanly but has a
    scrambled listing (e.g. valid gzip, wrong content) would not be
    caught here -- see this module's docstring for the pysam/htslib
    scope boundary.
    """
    resolved = index_path or f"{cram_path}.crai"
    if not os.path.exists(resolved):
        result.warnings.append(
            f"No CRAM index found at '{resolved}' -- not required for every use, but random-access "
            f"(region) queries will fail without one."
        )
        return

    if os.path.getsize(resolved) == 0:
        result.is_valid = False
        result.errors.append(f"CRAM index '{resolved}' is empty (0 bytes) -- it is corrupt or was never finished writing.")
        return

    try:
        with gzip.GzipFile(resolved, "rb") as fh:
            fh.read(1)
    except (OSError, EOFError, zlib.error) as exc:
        result.is_valid = False
        result.errors.append(f"CRAM index '{resolved}' is not a valid, complete gzip stream (corrupt or truncated): {exc}")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_VALIDATORS = {"fastq": validate_fastq, "bam": validate_bam, "cram": validate_cram}


def validate_raw_input(path: str, expected_format: Optional[str] = None, index_path: Optional[str] = None) -> ValidationResult:
    """
    Convenience dispatcher: validates `path` as `expected_format`
    ("fastq" | "bam" | "cram"), or infers the expected format from the
    file's extension when not given. Either way, the actual format
    check is always content-based (`sniff_format`), never
    extension-trusting -- this is what catches "wrong file type
    entirely" (e.g. a FASTQ file named 'sample.bam').
    """
    fmt = expected_format or _guess_expected_format_from_extension(path)
    if fmt not in _VALIDATORS:
        result = ValidationResult(path=path, detected_format=None, is_valid=False)
        result.errors.append(
            f"Could not determine what kind of file '{path}' is supposed to be from its extension, and no "
            f"expected_format was given. Pass expected_format='fastq'|'bam'|'cram' explicitly."
        )
        return result
    if fmt == "bam" or fmt == "cram":
        return _VALIDATORS[fmt](path, index_path=index_path)
    return _VALIDATORS[fmt](path)


def _guess_expected_format_from_extension(path: str) -> Optional[str]:
    lower = path.lower()
    if lower.endswith((".fastq", ".fastq.gz", ".fq", ".fq.gz")):
        return "fastq"
    if lower.endswith(".bam"):
        return "bam"
    if lower.endswith(".cram"):
        return "cram"
    return None
