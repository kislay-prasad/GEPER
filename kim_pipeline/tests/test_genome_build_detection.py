"""Tests for pipeline/utils/genome_build.py (item 2: genome build detection)."""

from pipeline.utils.genome_build import (
    detect_genome_build,
    warn_if_unsupported_build,
    SUPPORTED_BUILD,
)


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    return str(p)


def test_detects_grch38_from_reference_header(tmp_path):
    vcf = _write(tmp_path, "a.vcf", "##fileformat=VCFv4.2\n##reference=GRCh38\n#CHROM\tPOS\n")
    result = detect_genome_build(vcf)
    assert result.build == "GRCh38"
    assert result.confidence == "high"


def test_detects_grch37_from_hg19_reference_header(tmp_path):
    vcf = _write(tmp_path, "b.vcf", "##fileformat=VCFv4.2\n##reference=hg19\n#CHROM\tPOS\n")
    result = detect_genome_build(vcf)
    assert result.build == "GRCh37"


def test_detects_grch37_from_chr1_contig_length(tmp_path):
    vcf = _write(
        tmp_path,
        "c.vcf",
        "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=249250621>\n#CHROM\tPOS\n",
    )
    result = detect_genome_build(vcf)
    assert result.build == "GRCh37"


def test_detects_grch38_from_chr1_contig_length(tmp_path):
    vcf = _write(
        tmp_path,
        "d.vcf",
        "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=248956422>\n#CHROM\tPOS\n",
    )
    result = detect_genome_build(vcf)
    assert result.build == "GRCh38"


def test_undetermined_when_no_metadata(tmp_path):
    vcf = _write(tmp_path, "e.vcf", "##fileformat=VCFv4.2\n#CHROM\tPOS\n")
    result = detect_genome_build(vcf)
    assert result.build is None


def test_never_raises_on_missing_file():
    result = detect_genome_build("/nonexistent/path.vcf")
    assert result.build is None
    assert result.confidence == "none"


def test_warn_if_unsupported_build_does_not_raise(tmp_path, caplog):
    vcf = _write(tmp_path, "f.vcf", "##fileformat=VCFv4.2\n##reference=hg19\n#CHROM\tPOS\n")
    result = warn_if_unsupported_build(vcf, sample_id="t")
    assert result.build == "GRCh37"
    assert result.build != SUPPORTED_BUILD


def test_mismatched_build_warning_does_not_claim_pgx_is_bundled(tmp_path, caplog):
    """The PGx package was deleted 2026-09-10 (clinical-disclosure ruling).
    This warning is printed to a user at runtime, so it must not assert a
    resource exists that no longer does. It should still name gnomAD,
    which IS still a real, bundled, build-sensitive resource."""
    vcf = _write(tmp_path, "g.vcf", "##fileformat=VCFv4.2\n##reference=hg19\n#CHROM\tPOS\n")
    with caplog.at_level("WARNING"):
        warn_if_unsupported_build(vcf, sample_id="t")
    warning_text = " ".join(r.getMessage() for r in caplog.records)
    assert "PGx" not in warning_text
    assert "gnomAD" in warning_text


def test_undetermined_build_warning_does_not_claim_pgx_is_bundled(tmp_path, caplog):
    """Same claim, the other branch of warn_if_unsupported_build (no header
    metadata at all -- the 'assuming SUPPORTED_BUILD' warning)."""
    vcf = _write(tmp_path, "h.vcf", "##fileformat=VCFv4.2\n#CHROM\tPOS\n")
    with caplog.at_level("WARNING"):
        warn_if_unsupported_build(vcf, sample_id="t")
    warning_text = " ".join(r.getMessage() for r in caplog.records)
    assert "PGx" not in warning_text
    assert "gnomAD" in warning_text
