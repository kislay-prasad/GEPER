"""
tests/test_genome_build_consistency.py
─────────────────────────────────────────
FIX #8: nothing validated that the FASTA and GFF3 handed to
FastaCodonContextProvider actually describe the same genome build
(GRCh37 vs GRCh38) -- codon-level consequences would be silently wrong
for any transcript whose coordinates shifted between builds, with no
signal that anything was inconsistent.

Reuses the same discriminator pipeline/utils/genome_build.py already
established for VCF header detection (chr1's length is build-specific:
249,250,621 = GRCh37; 248,956,422 = GRCh38) -- applied here to:
  - the FASTA, via its .fai samtools index (or the in-memory-loaded
    sequence's own length, when samtools isn't available)
  - the GFF3, via its ##sequence-region pragma (part of the GFF3 spec
    itself, not a vendor convention)

Fail-soft, matching genome_build.py's own philosophy: a mismatch logs a
warning, never blocks the provider from loading -- either signal can be
legitimately absent (an older/minimal GFF3 without the pragma, a FASTA
never indexed with samtools), and "cannot confirm" must not read as
"confirmed consistent."

VCF is deliberately NOT checked here -- this class never sees the VCF
file at all (the orchestrator hands it already-parsed (chrom, pos, ref,
alt, transcript_id) tuples). THE VCF-vs-GFF3 CROSS-CHECK THIS DOCSTRING
FLAGGED AS A FOLLOW-UP NOW EXISTS (2026-09-10) as
`utils/genome_build.py::check_vcf_gff3_build_consistency`, which RAISES
rather than warns and is called from `runner.py`; it is tested by
`tests/test_vcf_gff3_build_disagreement_blocks.py`, not here. The
original note read: a VCF-vs-FASTA/GFF3 cross-check would need
runner.py's already-detected build (see its own
`checkpoint["detected_genome_build"]`, populated by
pipeline/utils/genome_build.py's `warn_if_unsupported_build`) threaded
into `make_codon_provider_from_cfg`. Flagged as a follow-up in the diff
report, out of this file's scope.

Fixture construction mirrors test_undetermined_aa_guard.py's
`_make_provider` convention: bypass __init__ via __new__() to exercise
internal logic without needing real multi-hundred-megabase FASTA files
(chr1's real length is the whole discriminator, and can't be
represented as a literal fixture string).
"""

from __future__ import annotations

import logging

from pipeline.annotation.codon_provider import (
    FastaCodonContextProvider,
    _FastaReader,
    _detect_gff_chr1_length,
)


def _make_provider_stub(fasta_chr1_length, gff_path):
    provider = FastaCodonContextProvider.__new__(FastaCodonContextProvider)
    provider._fasta_path = "/fake.fasta"
    provider._gff_path = str(gff_path)
    reader = _FastaReader.__new__(_FastaReader)
    reader._path = "/fake.fasta"
    reader._has_samtools = False
    reader._in_memory = {"chr1": "A" * fasta_chr1_length} if fasta_chr1_length else {}
    provider._fasta = reader
    return provider


class TestDetectGffChr1Length:
    def test_sequence_region_pragma_chr1(self, tmp_path):
        gff = tmp_path / "grch38.gff3"
        gff.write_text("##gff-version 3\n##sequence-region chr1 1 248956422\n")
        assert _detect_gff_chr1_length(str(gff)) == 248956422

    def test_sequence_region_pragma_bare_1(self, tmp_path):
        gff = tmp_path / "grch37.gff3"
        gff.write_text("##gff-version 3\n##sequence-region 1 1 249250621\n")
        assert _detect_gff_chr1_length(str(gff)) == 249250621

    def test_no_pragma_returns_none(self, tmp_path):
        gff = tmp_path / "no_pragma.gff3"
        gff.write_text("##gff-version 3\nchr1\t.\tgene\t1000\t1099\t.\t+\t.\tID=gene-G1\n")
        assert _detect_gff_chr1_length(str(gff)) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert _detect_gff_chr1_length(str(tmp_path / "nope.gff3")) is None


class TestBuildConsistencyWarning:
    def test_matching_grch38_no_mismatch_warning(self, tmp_path, caplog):
        gff = tmp_path / "match38.gff3"
        gff.write_text("##gff-version 3\n##sequence-region chr1 1 248956422\n")
        provider = _make_provider_stub(248956422, gff)

        with caplog.at_level(logging.WARNING, logger="geper.pipeline.annotation.codon_provider"):
            provider._check_genome_build_consistency()

        assert not any("mismatch" in r.message.lower() for r in caplog.records)
        assert provider.detected_fasta_build == "GRCh38"
        assert provider.detected_gff_build == "GRCh38"

    def test_mismatch_grch37_fasta_grch38_gff_warns(self, tmp_path, caplog):
        """THE RED-FIRST CASE: before this fix, nothing checked this at
        all -- codon translation would silently use GRCh38 GFF3
        coordinates against GRCh37 FASTA sequence with zero signal."""
        gff = tmp_path / "grch38.gff3"
        gff.write_text("##gff-version 3\n##sequence-region chr1 1 248956422\n")
        provider = _make_provider_stub(249250621, gff)  # FASTA looks like GRCh37

        with caplog.at_level(logging.WARNING, logger="geper.pipeline.annotation.codon_provider"):
            provider._check_genome_build_consistency()

        assert any("mismatch" in r.message.lower() for r in caplog.records), (
            "FASTA (GRCh37-length chr1) and GFF3 (GRCh38-length chr1) disagree "
            "and no warning was logged -- this is exactly the silent-mismatch "
            "gap FIX #8 closes"
        )
        assert provider.detected_fasta_build == "GRCh37"
        assert provider.detected_gff_build == "GRCh38"

    def test_undetermined_signal_does_not_warn_mismatch(self, tmp_path, caplog):
        """No ##sequence-region pragma at all -- must not be misread as
        a confirmed mismatch (or silently treated as a confirmed
        match)."""
        gff = tmp_path / "no_pragma.gff3"
        gff.write_text("##gff-version 3\n")
        provider = _make_provider_stub(248956422, gff)

        with caplog.at_level(logging.WARNING, logger="geper.pipeline.annotation.codon_provider"):
            provider._check_genome_build_consistency()

        assert not any("mismatch" in r.message.lower() for r in caplog.records)
        assert provider.detected_gff_build is None
