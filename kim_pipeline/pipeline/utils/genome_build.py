"""
pipeline/utils/genome_build.py
────────────────────────────────
Genome build (GRCh37 / GRCh38) detection from a VCF file.

GEPER's bundled resources (gnomAD GraphQL datasets `gnomad_r3`/`gnomad_r4`)
are GRCh38-only as of this version. This module does NOT fabricate GRCh37
coordinate data — that would risk silently wrong clinical calls, which is
worse than no support at all.

Instead it:
  1. Detects the input VCF's genome build with reasonable confidence using
     three signals, in order of reliability:
       a. ##reference / ##assembly header lines (explicit, most reliable)
       b. ##contig length= metadata (chr1 length is build-specific:
          249,250,621 = GRCh37; 248,956,422 = GRCh38)
       c. contig naming convention ("chr1" vs "1") — weak signal only,
          used as a last resort and never alone for a build call.
  2. Returns a GenomeBuildDetection with a build label and confidence, so
     callers can emit a clear, actionable warning instead of silently
     producing coordinate-mismatched ACMG calls when the detected
     build doesn't match what GEPER's resources support.
"""

from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("geper.pipeline.utils.genome_build")

# GEPER's bundled gnomAD-GraphQL resources are GRCh38-only.
SUPPORTED_BUILD = "GRCh38"

# chr1 length is one of the most reliable single discriminators between
# GRCh37 and GRCh38 reference assemblies.
_CHR1_LENGTH_BY_BUILD = {
    249250621: "GRCh37",
    248956422: "GRCh38",
}


@dataclass
class GenomeBuildDetection:
    build: Optional[str]  # "GRCh37", "GRCh38", or None if undetermined
    confidence: str  # "high", "medium", "low", "none"
    source: str  # what signal produced the call
    chr_prefix: bool = False  # True if contigs are named "chr1" not "1"


def detect_genome_build(vcf_path: str, max_header_lines: int = 5000) -> GenomeBuildDetection:
    """Inspect a VCF's header to determine its genome build.

    Never raises — on any read error, returns an "undetermined" result so
    callers can degrade gracefully (matching the rest of GEPER's
    fail-soft-with-a-warning philosophy) rather than crash.
    """
    try:
        path = Path(vcf_path)
        open_fn = gzip.open if str(path).endswith(".gz") else open
        ref_hint: Optional[str] = None
        chr1_length: Optional[int] = None
        chr_prefix = False
        saw_contig = False

        with open_fn(path, "rt") as fh:  # type: ignore[call-overload]
            for i, line in enumerate(fh):
                if i >= max_header_lines:
                    break
                if not line.startswith("#"):
                    break
                if line.startswith("##reference") or line.startswith("##assembly"):
                    low = line.lower()
                    if "grch38" in low or "hg38" in low:
                        ref_hint = "GRCh38"
                    elif "grch37" in low or "hg19" in low or "b37" in low:
                        ref_hint = "GRCh37"
                elif line.startswith("##contig"):
                    saw_contig = True
                    if "ID=chr" in line:
                        chr_prefix = True
                    if (
                        "ID=1," in line
                        or "ID=chr1," in line
                        or line.rstrip().endswith("ID=1>")
                        or line.rstrip().endswith("ID=chr1>")
                    ):
                        for token in line.strip().strip("<>").split(","):
                            if token.startswith("length="):
                                try:
                                    chr1_length = int(token.split("=", 1)[1].rstrip(">\n"))
                                except ValueError:
                                    pass

        # 1) Explicit header assembly metadata — most reliable.
        if ref_hint:
            return GenomeBuildDetection(
                build=ref_hint,
                confidence="high",
                source="##reference/##assembly header",
                chr_prefix=chr_prefix,
            )

        # 2) chr1 contig length — reliable, build-specific constant.
        if chr1_length and chr1_length in _CHR1_LENGTH_BY_BUILD:
            return GenomeBuildDetection(
                build=_CHR1_LENGTH_BY_BUILD[chr1_length],
                confidence="high",
                source="##contig chr1 length",
                chr_prefix=chr_prefix,
            )

        # 3) Contig naming alone — weak signal, cannot distinguish build.
        if saw_contig:
            return GenomeBuildDetection(
                build=None,
                confidence="low",
                source="contig naming only (chr-prefix=%s) — insufficient to determine build"
                % chr_prefix,
                chr_prefix=chr_prefix,
            )

        return GenomeBuildDetection(
            build=None, confidence="none", source="no header metadata found"
        )

    except Exception as exc:
        logger.debug("Genome build detection failed for %s: %s", vcf_path, exc)
        return GenomeBuildDetection(build=None, confidence="none", source=f"detection error: {exc}")


def check_vcf_gff3_build_consistency(vcf_path: str, gff_path: str) -> None:
    """Refuse the run when the VCF and the GFF3 state DIFFERENT builds.

    *** THIS IS THE PAIR NEITHER EXISTING CHECK COMPARES, AND IT IS THE
    PAIR THAT REACHES THE COORDINATE. *** `warn_if_unsupported_build`
    above compares the VCF to `SUPPORTED_BUILD`, a constant.
    `annotation/codon_provider.py::_check_genome_build_consistency`
    compares the FASTA to the GFF3 and says in its own docstring that
    the VCF is deliberately not checked. So a GRCh38 VCF with a GRCh37
    FASTA and a GRCh37 GFF3 satisfies both -- the VCF equals the
    constant, and the other two agree with each other -- and nothing
    warns at all.

    It matters because `annotation/stage.py` builds the HGVS `c.`
    position by mapping the VCF's genomic position through the
    transcript model loaded from the GFF3: the position comes from one
    file and the structure it is interpreted against comes from the
    other. A disagreement does not produce a missing value or an error.
    It produces a confidently wrong coordinate, printed unqualified.

    REFUSES ONLY A DEFINITE DISAGREEMENT -- both sides state a build and
    the builds differ. An undetectable build on either side is NOT
    refused: "cannot confirm" is not "confirmed inconsistent", which is
    the existing philosophy of both checks named above. Widening this to
    the indeterminate case is a separate decision with a separate cost.

    Raises `ConfigValidationError` rather than a new exception type:
    `main.py` already handles it as a fatal, operator-facing
    preflight failure, and the remedy here IS a configuration change
    (point `rna_analysis.refseq_gff` at a GFF3 matching the input).
    *** THE NAME IS NARROWER THAN THE CAUSE -- the VCF is an input, not
    configuration -- but one exception the callers already handle beats
    a second one they do not. ***
    """
    if not gff_path:
        return

    # Imported inside the function deliberately: `codon_provider` imports
    # this module at module level, so a top-level import here would be a
    # cycle. The GFF3 pragma parser lives there because that is where the
    # GFF3 is otherwise read, and existing tests import it from there.
    from pipeline.annotation.codon_provider import _detect_gff_chr1_length

    from pipeline.config_validator import ConfigValidationError

    gff_length = _detect_gff_chr1_length(gff_path)
    gff_build = _CHR1_LENGTH_BY_BUILD.get(gff_length) if gff_length else None
    if gff_build is None:
        return

    vcf_build = detect_genome_build(vcf_path).build
    if vcf_build is None or vcf_build == gff_build:
        return

    # ConfigValidationError takes (field_path, message) pairs, not a
    # string -- it formats them into a bulleted list. Matching that
    # contract rather than bending it is part of the cost of reusing an
    # exception the callers already handle.
    raise ConfigValidationError(
        [
            (
                "rna_analysis.refseq_gff",
                f"genome build mismatch with the input VCF: {Path(vcf_path).name} declares "
                f"{vcf_build}, but {Path(gff_path).name} declares {gff_build} "
                f"(chr1 length {gff_length}). HGVS c. coordinates are computed by mapping "
                f"the VCF's genomic positions through this GFF3's transcript model, so every "
                f"c. position in this run would be resolved against the wrong assembly and "
                f"printed with no indication that it is wrong. Supply a GFF3 matching the "
                f"VCF's build, or re-call the variants against {gff_build}.",
            )
        ]
    )


def warn_if_unsupported_build(vcf_path: str, sample_id: str = "SAMPLE") -> GenomeBuildDetection:
    """Detect the build and log a clear warning if it doesn't match
    SUPPORTED_BUILD, or if it could not be determined at all. Does not
    raise and does not block the pipeline — callers proceed either way,
    but the resulting ACMG calls should be treated as build-mismatched
    (and therefore unreliable) when this returns build != SUPPORTED_BUILD.
    """
    detection = detect_genome_build(vcf_path)

    if detection.build == SUPPORTED_BUILD:
        logger.info(
            "[%s] Detected genome build: %s (%s, source=%s)",
            sample_id,
            detection.build,
            detection.confidence,
            detection.source,
        )
    elif detection.build is not None:
        logger.warning(
            "[%s] Detected genome build %s, but GEPER's bundled gnomAD "
            "resources are %s-only. ClinVar/gnomAD coordinate lookups WILL "
            "be mismatched and results should not be trusted until the VCF is "
            "lifted over to %s (e.g. with Picard LiftoverVcf / CrossMap) or "
            "build-specific resources are configured. (source=%s)",
            sample_id,
            detection.build,
            SUPPORTED_BUILD,
            SUPPORTED_BUILD,
            detection.source,
        )
    else:
        logger.warning(
            "[%s] Could not determine genome build from VCF header (%s). "
            "Assuming %s (GEPER's only supported build) — if this VCF is "
            "actually GRCh37/hg19, ClinVar/gnomAD coordinate lookups will "
            "silently produce wrong results. Add a '##reference=' header line "
            "to make the build explicit.",
            sample_id,
            detection.source,
            SUPPORTED_BUILD,
        )

    return detection
