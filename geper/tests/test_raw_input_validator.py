"""
Tests for pipeline/raw_input_validator.py.

No real FASTQ/BAM/CRAM file exists anywhere in this repository to
reuse (confirmed by search before writing this module -- GEPER's own
pipeline never ingests these formats; see that module's docstring).
"Valid input" fixtures here are therefore constructed by this test
file itself, to the real, published FASTQ/BAM/BGZF binary
specifications (not simplified stand-ins) -- see `_make_valid_bam_bytes`
for the exact BGZF block/BAM header layout this constructs by hand.
Every "malformed" fixture is a real, deliberately-corrupted mutation of
one of these valid fixtures (truncation, header corruption, length
mismatch), exercised through the actual validator functions -- nothing
here mocks the validator itself.
"""

import gzip
import os
import struct
import tempfile
import unittest
import zlib

from pipeline.raw_input_validator import (
    ValidationResult,
    sniff_format,
    validate_bam,
    validate_cram,
    validate_fastq,
    validate_raw_input,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

_VALID_FASTQ_TEXT = (
    "@SRR000001.1 FCA:1:1:1:1 length=8\n"
    "ACGTACGT\n"
    "+SRR000001.1 FCA:1:1:1:1 length=8\n"
    "IIIIIIII\n"
    "@SRR000001.2 FCA:1:1:1:2 length=8\n"
    "TTGGCCAA\n"
    "+\n"
    "FFFFF:::\n"
)

# BGZF's fixed 28-byte empty EOF marker block, verbatim per the SAM
# spec's BGZF section (the same constant every BGZF writer, e.g.
# htslib, appends to close a well-formed file).
_BGZF_EOF_MARKER = bytes.fromhex("1f8b08040000000000ff0600424302001b0003000000000000000000")[:28]


def _bgzf_block(payload: bytes) -> bytes:
    """One BGZF block wrapping `payload` -- see the SAM spec's BGZF section for the exact 18-byte header/extra-field layout this constructs."""
    co = zlib.compressobj(6, zlib.DEFLATED, -15)  # raw deflate, no zlib header/trailer
    compressed = co.compress(payload) + co.flush()
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    isize = len(payload) & 0xFFFFFFFF
    bsize = 18 + len(compressed) + 8 - 1  # total block size - 1, per spec
    header = bytes([0x1F, 0x8B, 0x08, 0x04]) + struct.pack("<I", 0) + bytes([0, 0xFF]) + struct.pack("<H", 6)
    extra = bytes([ord("B"), ord("C")]) + struct.pack("<H", 2) + struct.pack("<H", bsize)
    trailer = struct.pack("<II", crc, isize)
    return header + extra + compressed + trailer


def _bgzf_compress(data: bytes) -> bytes:
    return _bgzf_block(data) + _BGZF_EOF_MARKER


def _make_valid_bam_bytes() -> bytes:
    """A minimal, structurally valid, header-only BAM (real GRCh38 chr1 length, zero alignment records -- itself a valid BAM)."""
    header_text = b"@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:248956422\n"
    payload = b"BAM\x01"
    payload += struct.pack("<i", len(header_text))
    payload += header_text
    payload += struct.pack("<i", 1)  # n_ref = 1
    ref_name = b"chr1\x00"
    payload += struct.pack("<i", len(ref_name))
    payload += ref_name
    payload += struct.pack("<i", 248956422)  # l_ref
    return _bgzf_compress(payload)


def _make_valid_bai_bytes() -> bytes:
    return b"BAI\x01" + b"\x01\x00\x00\x00" + b"\x00" * 4  # magic + minimal (unparsed-by-us) body


def _make_valid_cram_bytes() -> bytes:
    return b"CRAM" + bytes([3, 0]) + b"\x00" * 20  # magic + v3.0 + 20-byte file ID


def _write(tmpdir: str, name: str, data) -> str:
    path = os.path.join(tmpdir, name)
    mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
    with open(path, mode) as fh:
        fh.write(data)
    return path


class _TmpDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir_ctx.name

    def tearDown(self):
        self._tmpdir_ctx.cleanup()


# ---------------------------------------------------------------------------
# FASTQ
# ---------------------------------------------------------------------------

class TestValidateFastqValid(_TmpDirTestCase):
    def test_valid_plain_fastq_passes(self):
        path = _write(self.tmpdir, "valid.fastq", _VALID_FASTQ_TEXT)
        result = validate_fastq(path)
        self.assertTrue(result.is_valid, msg=result.errors)
        self.assertEqual(result.detected_format, "fastq")
        self.assertEqual(result.errors, [])

    def test_valid_gzipped_fastq_passes(self):
        path = os.path.join(self.tmpdir, "valid.fastq.gz")
        with gzip.open(path, "wt") as fh:
            fh.write(_VALID_FASTQ_TEXT)
        result = validate_fastq(path)
        self.assertTrue(result.is_valid, msg=result.errors)


class TestValidateFastqEmptyAndMissing(_TmpDirTestCase):
    def test_empty_file_fails_with_clear_message(self):
        path = _write(self.tmpdir, "empty.fastq", "")
        result = validate_fastq(path)
        self.assertFalse(result.is_valid)
        self.assertIn("empty", result.errors[0].lower())

    def test_missing_file_fails_with_clear_message(self):
        result = validate_fastq(os.path.join(self.tmpdir, "does_not_exist.fastq"))
        self.assertFalse(result.is_valid)
        self.assertIn("does not exist", result.errors[0])


class TestValidateFastqTruncated(_TmpDirTestCase):
    def test_truncated_mid_record_fails(self):
        # Real corruption: cut the valid fixture off mid-4-line-block
        # (after the header + sequence line of the second record, before
        # its '+' and quality lines) -- simulating a transfer that died partway.
        truncated = _VALID_FASTQ_TEXT.rsplit("\n", 3)[0] + "\n"  # drop the last '+' and quality lines
        path = _write(self.tmpdir, "truncated.fastq", truncated)
        result = validate_fastq(path)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("truncated" in e.lower() for e in result.errors), msg=result.errors)


class TestValidateFastqBadHeader(_TmpDirTestCase):
    def test_missing_at_sign_header_fails(self):
        # Corrupt the SECOND record's header, not the first -- byte 0
        # of the file must stay '@' so sniff_format() still correctly
        # recognizes the file as FASTQ at all, exercising the deeper
        # per-record header check this test actually targets (a header
        # corrupted partway through a multi-record file, the realistic
        # case -- a file whose very first byte isn't '@' is instead,
        # correctly, caught earlier as "not a FASTQ file at all").
        corrupted = _VALID_FASTQ_TEXT.replace("@SRR000001.2", "SRR000001.2", 1)
        path = _write(self.tmpdir, "bad_header.fastq", corrupted)
        result = validate_fastq(path)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("expected a FASTQ header starting with '@'" in e for e in result.errors), msg=result.errors)


class TestValidateFastqLengthMismatch(_TmpDirTestCase):
    def test_sequence_quality_length_mismatch_fails(self):
        # Real corruption: quality line shortened by one character relative
        # to its sequence line (the single most common real FASTQ corruption).
        corrupted = _VALID_FASTQ_TEXT.replace("IIIIIIII\n", "IIIIIII\n", 1)
        path = _write(self.tmpdir, "length_mismatch.fastq", corrupted)
        result = validate_fastq(path)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("must always match" in e for e in result.errors), msg=result.errors)
        self.assertTrue(any("8 character(s)" in e and "7 character(s)" in e for e in result.errors), msg=result.errors)


class TestValidateFastqInvalidCharacters(_TmpDirTestCase):
    def test_invalid_sequence_character_fails(self):
        corrupted = _VALID_FASTQ_TEXT.replace("ACGTACGT", "ACGTAXGT", 1)  # 'X' is not a valid base
        path = _write(self.tmpdir, "bad_base.fastq", corrupted)
        result = validate_fastq(path)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("invalid character 'X'" in e for e in result.errors), msg=result.errors)

    def test_invalid_quality_character_fails(self):
        # A raw NUL byte in the quality string (ASCII 0) is well outside the legal Phred range.
        corrupted = _VALID_FASTQ_TEXT.replace("IIIIIIII", "IIII\x00III", 1)
        path = _write(self.tmpdir, "bad_qual.fastq", corrupted)
        result = validate_fastq(path)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("outside the valid Phred quality range" in e for e in result.errors), msg=result.errors)


class TestValidateFastqWrongFileType(_TmpDirTestCase):
    def test_bam_file_passed_as_fastq_fails_clearly(self):
        path = _write(self.tmpdir, "actually_a.bam_named.fastq", _make_valid_bam_bytes())
        result = validate_fastq(path)
        self.assertFalse(result.is_valid)
        self.assertIn("does not look like a FASTQ file", result.errors[0])


# ---------------------------------------------------------------------------
# BAM
# ---------------------------------------------------------------------------

class TestValidateBamValid(_TmpDirTestCase):
    def test_valid_bam_without_index_passes_with_warning(self):
        path = _write(self.tmpdir, "valid.bam", _make_valid_bam_bytes())
        result = validate_bam(path)
        self.assertTrue(result.is_valid, msg=result.errors)
        self.assertEqual(result.detected_format, "bam")
        self.assertTrue(any("no bam index found" in w.lower() for w in result.warnings))

    def test_valid_bam_with_valid_index_passes_no_warning(self):
        path = _write(self.tmpdir, "valid.bam", _make_valid_bam_bytes())
        _write(self.tmpdir, "valid.bam.bai", _make_valid_bai_bytes())
        result = validate_bam(path)
        self.assertTrue(result.is_valid, msg=result.errors)
        self.assertEqual(result.warnings, [])


class TestValidateBamEmptyAndMissing(_TmpDirTestCase):
    def test_empty_bam_fails(self):
        path = _write(self.tmpdir, "empty.bam", b"")
        result = validate_bam(path)
        self.assertFalse(result.is_valid)
        self.assertIn("empty", result.errors[0].lower())

    def test_missing_bam_fails(self):
        result = validate_bam(os.path.join(self.tmpdir, "nope.bam"))
        self.assertFalse(result.is_valid)
        self.assertIn("does not exist", result.errors[0])


class TestValidateBamTruncated(_TmpDirTestCase):
    def test_truncated_bam_fails(self):
        full = _make_valid_bam_bytes()
        truncated = full[: len(full) // 2]  # cut the BGZF stream itself in half
        path = _write(self.tmpdir, "truncated.bam", truncated)
        result = validate_bam(path)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("truncated" in e.lower() or "corrupt" in e.lower() for e in result.errors), msg=result.errors)

    def test_bam_truncated_inside_header_text_fails(self):
        # Corrupt the header more surgically: rebuild the BAM payload
        # but chop it off partway through the declared SAM header text,
        # so the BGZF/gzip layer is intact but the BAM header itself is incomplete.
        header_text = b"@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:248956422\n"
        payload = b"BAM\x01" + struct.pack("<i", len(header_text)) + header_text[:10]  # declare full length, provide only 10 bytes
        path = _write(self.tmpdir, "truncated_header.bam", _bgzf_compress(payload))
        result = validate_bam(path)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("truncated" in e.lower() for e in result.errors), msg=result.errors)


class TestValidateBamCorruptHeader(_TmpDirTestCase):
    def test_corrupt_bam_magic_fails(self):
        # A wrong magic literal is caught by sniff_format() itself
        # (it decompresses and checks the same 4 bytes before
        # validate_bam's own deeper header parsing ever runs) --
        # still a clear, correct rejection, just via that earlier check.
        payload = b"BAX\x01" + struct.pack("<i", 0)  # wrong magic, matching real corruption of the signature byte
        path = _write(self.tmpdir, "bad_magic.bam", _bgzf_compress(payload))
        result = validate_bam(path)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("does not look like a valid BAM file" in e for e in result.errors), msg=result.errors)

    def test_negative_header_length_fails(self):
        payload = b"BAM\x01" + struct.pack("<i", -1)
        path = _write(self.tmpdir, "negative_len.bam", _bgzf_compress(payload))
        result = validate_bam(path)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("negative header-text length" in e for e in result.errors), msg=result.errors)


class TestValidateBamWrongFileType(_TmpDirTestCase):
    def test_fastq_file_passed_as_bam_fails_clearly(self):
        path = _write(self.tmpdir, "actually.fastq_named.bam", _VALID_FASTQ_TEXT)
        result = validate_bam(path)
        self.assertFalse(result.is_valid)
        self.assertIn("looks like a FASTQ file", result.errors[0])

    def test_cram_file_passed_as_bam_fails_clearly(self):
        path = _write(self.tmpdir, "actually.cram_named.bam", _make_valid_cram_bytes())
        result = validate_bam(path)
        self.assertFalse(result.is_valid)
        self.assertIn("is a CRAM file", result.errors[0])


class TestValidateBaiIndex(_TmpDirTestCase):
    def test_corrupt_bai_magic_fails(self):
        path = _write(self.tmpdir, "valid.bam", _make_valid_bam_bytes())
        _write(self.tmpdir, "valid.bam.bai", b"XXXX" + b"\x00" * 8)
        result = validate_bam(path)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("does not start with the expected 'BAI" in e for e in result.errors), msg=result.errors)

    def test_empty_bai_fails(self):
        path = _write(self.tmpdir, "valid.bam", _make_valid_bam_bytes())
        _write(self.tmpdir, "valid.bam.bai", b"")
        result = validate_bam(path)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("index" in e.lower() and "empty" in e.lower() for e in result.errors), msg=result.errors)


# ---------------------------------------------------------------------------
# CRAM
# ---------------------------------------------------------------------------

class TestValidateCramValid(_TmpDirTestCase):
    def test_valid_cram_file_definition_passes(self):
        path = _write(self.tmpdir, "valid.cram", _make_valid_cram_bytes())
        result = validate_cram(path)
        self.assertTrue(result.is_valid, msg=result.errors)
        self.assertEqual(result.detected_format, "cram")


class TestValidateCramTruncated(_TmpDirTestCase):
    def test_too_short_cram_fails(self):
        path = _write(self.tmpdir, "truncated.cram", b"CRAM" + bytes([3, 0]))  # missing the 20-byte file ID entirely
        result = validate_cram(path)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("truncated" in e.lower() for e in result.errors), msg=result.errors)

    def test_empty_cram_fails(self):
        path = _write(self.tmpdir, "empty.cram", b"")
        result = validate_cram(path)
        self.assertFalse(result.is_valid)
        self.assertIn("empty", result.errors[0].lower())


class TestValidateCramWrongFileType(_TmpDirTestCase):
    def test_bam_file_passed_as_cram_fails_clearly(self):
        path = _write(self.tmpdir, "actually.bam_named.cram", _make_valid_bam_bytes())
        result = validate_cram(path)
        self.assertFalse(result.is_valid)
        self.assertIn("is a BAM file", result.errors[0])

    def test_fastq_file_passed_as_cram_fails_clearly(self):
        path = _write(self.tmpdir, "actually.fastq_named.cram", _VALID_FASTQ_TEXT)
        result = validate_cram(path)
        self.assertFalse(result.is_valid)
        self.assertIn("looks like a FASTQ file", result.errors[0])


# ---------------------------------------------------------------------------
# sniff_format + dispatcher
# ---------------------------------------------------------------------------

class TestSniffFormat(_TmpDirTestCase):
    def test_sniffs_each_real_fixture_correctly(self):
        fastq_path = _write(self.tmpdir, "a.fastq", _VALID_FASTQ_TEXT)
        bam_path = _write(self.tmpdir, "a.bam", _make_valid_bam_bytes())
        cram_path = _write(self.tmpdir, "a.cram", _make_valid_cram_bytes())
        self.assertEqual(sniff_format(fastq_path), "fastq")
        self.assertEqual(sniff_format(bam_path), "bam")
        self.assertEqual(sniff_format(cram_path), "cram")

    def test_sniffs_gzipped_fastq_distinctly_from_bam(self):
        gz_fastq_path = os.path.join(self.tmpdir, "a.fastq.gz")
        with gzip.open(gz_fastq_path, "wt") as fh:
            fh.write(_VALID_FASTQ_TEXT)
        self.assertEqual(sniff_format(gz_fastq_path), "gzip_fastq")


class TestValidateRawInputDispatcher(_TmpDirTestCase):
    def test_infers_format_from_extension(self):
        path = _write(self.tmpdir, "sample.fastq", _VALID_FASTQ_TEXT)
        result = validate_raw_input(path)
        self.assertTrue(result.is_valid)
        self.assertEqual(result.detected_format, "fastq")

    def test_unrecognized_extension_without_expected_format_fails_clearly(self):
        path = _write(self.tmpdir, "sample.data", _VALID_FASTQ_TEXT)
        result = validate_raw_input(path)
        self.assertFalse(result.is_valid)
        self.assertIn("Pass expected_format=", result.errors[0])

    def test_raise_if_invalid_raises_with_clear_message(self):
        from utils.exceptions import RawInputValidationError

        path = _write(self.tmpdir, "empty.fastq", "")
        result = validate_fastq(path)
        with self.assertRaises(RawInputValidationError) as ctx:
            result.raise_if_invalid()
        self.assertIn("failed pre-flight validation", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
