"""
tests/test_rna.py
──────────────────
Unit tests for pipeline.rna.transcript.
"""

from pipeline.rna.transcript import RnaTranscriptAnalyser, TranscriptRecord


# ── GFF3 fixture helpers ───────────────────────────────────────────────────────


def _write_gff(path, lines):
    path.write_text("\n".join(lines) + "\n")


def _make_analyser(gff_path: str) -> RnaTranscriptAnalyser:
    return RnaTranscriptAnalyser(cfg={"rna_analysis": {"refseq_gff": gff_path}})


# GFF3 columns: seqname source feature start end score strand frame attributes
_GFF_HEADER = "##gff-version 3"


def _mrna_line(chrom, start, end, strand, transcript_id, gene_id):
    return (
        f"{chrom}\tRefSeq\tmRNA\t{start}\t{end}\t.\t{strand}\t.\t"
        f"ID={transcript_id};gene_id={gene_id};Parent={gene_id}"
    )


def _exon_line(chrom, start, end, strand, parent_id):
    return f"{chrom}\tRefSeq\texon\t{start}\t{end}\t.\t{strand}\t.\tParent={parent_id}"


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_transcript_loaded(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 500, "+", "TX1", "GENE1"),
            _exon_line("chr1", 100, 200, "+", "TX1"),
            _exon_line("chr1", 300, 500, "+", "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    rec = analyser.get_transcript("TX1")
    assert rec is not None
    assert isinstance(rec, TranscriptRecord)
    assert len(rec.exons) == 2


def test_exons_sorted(tmp_path):
    gff = tmp_path / "test.gff3"
    # Write exons out of order
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 500, "+", "TX1", "GENE1"),
            _exon_line("chr1", 300, 500, "+", "TX1"),
            _exon_line("chr1", 100, 200, "+", "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    rec = analyser.get_transcript("TX1")
    assert rec is not None
    starts = [e[0] for e in rec.exons]
    assert starts == sorted(starts)


def test_is_splice_region_true(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 500, "+", "TX1", "GENE1"),
            _exon_line("chr1", 100, 200, "+", "TX1"),
            _exon_line("chr1", 300, 500, "+", "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    # Position 201 is 1 base after exon end (200) → within window=2
    assert analyser.is_splice_region("chr1", 201, "TX1", window=2) is True


def test_is_splice_region_false(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 500, "+", "TX1", "GENE1"),
            _exon_line("chr1", 100, 200, "+", "TX1"),
            _exon_line("chr1", 300, 500, "+", "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    # Position 150 is in the middle of exon 1 — not near any boundary
    assert analyser.is_splice_region("chr1", 150, "TX1", window=2) is False


def test_get_exon_number_exonic(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 500, "+", "TX1", "GENE1"),
            _exon_line("chr1", 100, 200, "+", "TX1"),
            _exon_line("chr1", 300, 500, "+", "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    # Position 350 is in exon 2 (300-500)
    assert analyser.get_exon_number("chr1", 350, "TX1") == 2


def test_get_exon_number_intronic(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 500, "+", "TX1", "GENE1"),
            _exon_line("chr1", 100, 200, "+", "TX1"),
            _exon_line("chr1", 300, 500, "+", "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    # Position 250 is intronic (between exon 1 and exon 2)
    assert analyser.get_exon_number("chr1", 250, "TX1") is None


def test_classify_region_exonic(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 500, "+", "TX1", "GENE1"),
            _exon_line("chr1", 100, 200, "+", "TX1"),
            _exon_line("chr1", 300, 500, "+", "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    assert analyser.classify_region("chr1", 150, "TX1") == "exonic"


def test_classify_region_intronic(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 500, "+", "TX1", "GENE1"),
            _exon_line("chr1", 100, 200, "+", "TX1"),
            _exon_line("chr1", 300, 500, "+", "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    assert analyser.classify_region("chr1", 250, "TX1") == "intronic"


def test_classify_region_splice_donor(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 500, "+", "TX1", "GENE1"),
            _exon_line("chr1", 100, 200, "+", "TX1"),
            _exon_line("chr1", 300, 500, "+", "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    # Position 201 is 1 base after exon end → intronic splice donor
    result = analyser.classify_region("chr1", 201, "TX1")
    assert result == "splice_donor"


def test_no_gff_returns_none(tmp_path):
    analyser = RnaTranscriptAnalyser(cfg={"rna_analysis": {"refseq_gff": "/nonexistent.gff"}})
    assert analyser.get_transcript("TX1") is None


def test_get_transcripts_for_gene(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 500, "+", "TX1", "GENE1"),
            _exon_line("chr1", 100, 200, "+", "TX1"),
            _mrna_line("chr1", 100, 500, "+", "TX2", "GENE1"),
            _exon_line("chr1", 100, 250, "+", "TX2"),
        ],
    )
    analyser = _make_analyser(str(gff))
    results = analyser.get_transcripts_for_gene("GENE1")
    assert len(results) == 2
    ids = {r.transcript_id for r in results}
    assert ids == {"TX1", "TX2"}


def test_get_transcripts_for_gene_no_match_returns_empty(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 500, "+", "TX1", "GENE1"),
            _exon_line("chr1", 100, 200, "+", "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    assert analyser.get_transcripts_for_gene("NO_SUCH_GENE") == []


# ── CDS/transcript coordinate mapping (ISSUE 1 support) ───────────────────────


def _cds_line(chrom, start, end, strand, phase, parent_id):
    return f"{chrom}\tRefSeq\tCDS\t{start}\t{end}\t.\t{strand}\t{phase}\tParent={parent_id}"


def test_genomic_to_cds_pos_plus_strand_single_exon(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 500, "+", "TX1", "GENE1"),
            _exon_line("chr1", 100, 500, "+", "TX1"),
            _cds_line("chr1", 100, 500, "+", 0, "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    # First base of CDS → position 1
    assert analyser.genomic_to_cds_pos("chr1", 100, "TX1") == 1
    assert analyser.genomic_to_cds_pos("chr1", 150, "TX1") == 51


def test_genomic_to_cds_pos_plus_strand_multi_exon(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 600, "+", "TX1", "GENE1"),
            _exon_line("chr1", 100, 200, "+", "TX1"),
            _exon_line("chr1", 300, 600, "+", "TX1"),
            _cds_line("chr1", 100, 200, "+", 0, "TX1"),
            _cds_line("chr1", 300, 600, "+", 2, "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    # CDS exon 1 is 101 bases (100..200 inclusive); first base of exon 2 CDS
    # is CDS position 102.
    assert analyser.genomic_to_cds_pos("chr1", 300, "TX1") == 102
    # Intronic position between the two CDS segments has no CDS coordinate.
    assert analyser.genomic_to_cds_pos("chr1", 250, "TX1") is None


def test_genomic_to_cds_pos_minus_strand(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 500, "-", "TX1", "GENE1"),
            _exon_line("chr1", 100, 500, "-", "TX1"),
            _cds_line("chr1", 100, 500, "-", 0, "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    # On minus strand, CDS position 1 is at the highest genomic coordinate.
    assert analyser.genomic_to_cds_pos("chr1", 500, "TX1") == 1
    assert analyser.genomic_to_cds_pos("chr1", 499, "TX1") == 2


def test_genomic_to_cds_pos_utr_returns_none(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 500, "+", "TX1", "GENE1"),
            _exon_line("chr1", 100, 500, "+", "TX1"),
            # CDS starts at 150 → 100-149 is 5' UTR
            _cds_line("chr1", 150, 500, "+", 0, "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    assert analyser.genomic_to_cds_pos("chr1", 120, "TX1") is None
    assert analyser.genomic_to_cds_pos("chr1", 150, "TX1") == 1


def test_genomic_to_transcript_pos_noncoding(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 300, "+", "TX1", "GENE1"),
            _exon_line("chr1", 100, 200, "+", "TX1"),
            _exon_line("chr1", 250, 300, "+", "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    assert analyser.genomic_to_transcript_pos("chr1", 100, "TX1") == 1
    # First base of exon 2 is transcript position 102 (exon1 is 101 bases)
    assert analyser.genomic_to_transcript_pos("chr1", 250, "TX1") == 102
    # Intronic
    assert analyser.genomic_to_transcript_pos("chr1", 220, "TX1") is None


def test_genomic_to_cds_pos_no_cds_data_returns_none(tmp_path):
    gff = tmp_path / "test.gff3"
    _write_gff(
        gff,
        [
            _GFF_HEADER,
            _mrna_line("chr1", 100, 500, "+", "TX1", "GENE1"),
            _exon_line("chr1", 100, 500, "+", "TX1"),
        ],
    )
    analyser = _make_analyser(str(gff))
    assert analyser.genomic_to_cds_pos("chr1", 150, "TX1") is None
