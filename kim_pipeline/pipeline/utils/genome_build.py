"""
pipeline/utils/genome_build.py
────────────────────────────────
Genome build (GRCh37 / GRCh38) detection from a VCF file.

GEPER's bundled resources (PharmGKB-derived PGx star-allele coordinates,
gnomAD GraphQL datasets `gnomad_r3`/`gnomad_r4`) are GRCh38-only as of this
version. This module does NOT fabricate GRCh37 coordinate data — that would
risk silently wrong clinical calls, which is worse than no support at all.

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
     producing coordinate-mismatched ACMG/PGx calls when the detected
     build doesn't match what GEPER's resources support.
"""

from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("geper.pipeline.utils.genome_build")

# GEPER's bundled PGx/gnomAD-GraphQL resources are GRCh38-only.
SUPPORTED_BUILD = "GRCh38"

# chr1 length is one of the most reliable single discriminators between
# GRCh37 and GRCh38 reference assemblies.
_CHR1_LENGTH_BY_BUILD = {
    249250621: "GRCh37",
    248956422: "GRCh38",
}


@dataclass
class GenomeBuildDetection:
    build: Optional[str]       # "GRCh37", "GRCh38", or None if undetermined
    confidence: str            # "high", "medium", "low", "none"
    source: str                # what signal produced the call
    chr_prefix: bool = False   # True if contigs are named "chr1" not "1"


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
                    if "ID=1," in line or "ID=chr1," in line or line.rstrip().endswith("ID=1>") or line.rstrip().endswith("ID=chr1>"):
                        for token in line.strip().strip("<>").split(","):
                            if token.startswith("length="):
                                try:
                                    chr1_length = int(token.split("=", 1)[1].rstrip(">\n"))
                                except ValueError:
                                    pass

        # 1) Explicit header assembly metadata — most reliable.
        if ref_hint:
            return GenomeBuildDetection(
                build=ref_hint, confidence="high", source="##reference/##assembly header",
                chr_prefix=chr_prefix,
            )

        # 2) chr1 contig length — reliable, build-specific constant.
        if chr1_length and chr1_length in _CHR1_LENGTH_BY_BUILD:
            return GenomeBuildDetection(
                build=_CHR1_LENGTH_BY_BUILD[chr1_length], confidence="high",
                source="##contig chr1 length", chr_prefix=chr_prefix,
            )

        # 3) Contig naming alone — weak signal, cannot distinguish build.
        if saw_contig:
            return GenomeBuildDetection(
                build=None, confidence="low",
                source="contig naming only (chr-prefix=%s) — insufficient to determine build" % chr_prefix,
                chr_prefix=chr_prefix,
            )

        return GenomeBuildDetection(build=None, confidence="none", source="no header metadata found")

    except Exception as exc:
        logger.debug("Genome build detection failed for %s: %s", vcf_path, exc)
        return GenomeBuildDetection(build=None, confidence="none", source=f"detection error: {exc}")


def warn_if_unsupported_build(vcf_path: str, sample_id: str = "SAMPLE") -> GenomeBuildDetection:
    """Detect the build and log a clear warning if it doesn't match
    SUPPORTED_BUILD, or if it could not be determined at all. Does not
    raise and does not block the pipeline — callers proceed either way,
    but the resulting ACMG/PGx calls should be treated as build-mismatched
    (and therefore unreliable) when this returns build != SUPPORTED_BUILD.
    """
    detection = detect_genome_build(vcf_path)

    if detection.build == SUPPORTED_BUILD:
        logger.info(
            "[%s] Detected genome build: %s (%s, source=%s)",
            sample_id, detection.build, detection.confidence, detection.source,
        )
    elif detection.build is not None:
        logger.warning(
            "[%s] Detected genome build %s, but GEPER's bundled PGx and gnomAD "
            "resources are %s-only. ClinVar/gnomAD/PGx coordinate lookups WILL "
            "be mismatched and results should not be trusted until the VCF is "
            "lifted over to %s (e.g. with Picard LiftoverVcf / CrossMap) or "
            "build-specific resources are configured. (source=%s)",
            sample_id, detection.build, SUPPORTED_BUILD, SUPPORTED_BUILD, detection.source,
        )
    else:
        logger.warning(
            "[%s] Could not determine genome build from VCF header (%s). "
            "Assuming %s (GEPER's only supported build) — if this VCF is "
            "actually GRCh37/hg19, ClinVar/gnomAD/PGx coordinate lookups will "
            "silently produce wrong results. Add a '##reference=' header line "
            "to make the build explicit.",
            sample_id, detection.source, SUPPORTED_BUILD,
        )

    return detection
