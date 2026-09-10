"""
Genome assembly / build validation.

GEPER fetches reference sequence context from Ensembl (see
pipeline/sequence_context.py), which requires knowing the correct
genome build (e.g. GRCh37 vs GRCh38). Using the wrong build doesn't
raise a loud error -- it silently returns *plausible-looking* sequence
for the wrong coordinates, which then quietly corrupts every
downstream DNA/RNA/protein model input and BLAST/ClinVar/dbSNP lookup
for that variant.

This module does a lightweight, best-effort preflight check *before*
any variant is processed:

  1. Try to detect the assembly the VCF itself claims (from ##reference
     / ##contig header lines, or contig-length fingerprinting as a
     fallback).
  2. If the caller also supplied --assembly and it definitely
     disagrees with what the VCF declares, stop the run with an
     informative error (AssemblyMismatchError) rather than silently
     mis-fetching reference sequence for millions of variants.
  3. If detection succeeds but the caller supplied nothing, return the
     detected build so the orchestrator can pass it on to Ensembl
     automatically instead of relying on Ensembl's current default.
  4. If detection is inconclusive, the outcome depends on whether the
     CALLER established the build:
       - `--assembly` given: warn and proceed. An informal header is
         not itself fatal; the build has been established, on purpose,
         by the caller.
       - `--assembly` absent: STOP (AssemblyMismatchError). Nobody has
         established the build, and proceeding means Ensembl's default
         silently becomes the answer.

     THIS REVERSES THIS MODULE'S ORIGINAL ITEM 4, which read: "If
     detection is inconclusive, warn and proceed -- we should never
     block a run just because header metadata was informal or missing;
     sequence_context.py's per-variant REF-mismatch warning remains as
     a second line of defense." Quoted rather than deleted because the
     reasoning was sound and the trade was deliberate. Ruled the other
     way by the human on 2026-09-10: that second line of defense is
     real (`sequence_context.py` logs "Reference mismatch for ..." per
     variant) but it is a WARNING, PER VARIANT, ON A RUN THAT HAS
     ALREADY FETCHED THE WRONG BUILD -- it reports the symptom after
     the decision that caused it. In his words, warning but never
     blocking here "is the exact failure class the build check exists
     to prevent, moved one layer down".
"""

import re
from typing import List, Optional

from utils.exceptions import AssemblyMismatchError
from utils.logger import get_logger

logger = get_logger(__name__)

_GRCH37_MARKERS = ("grch37", "hg19", "b37")
_GRCH38_MARKERS = ("grch38", "hg38", "b38")

# Well-known chromosome-1 lengths, keyed by build. Used only as a
# fallback fingerprint when the header doesn't explicitly name a build
# (e.g. contig lines with a `length=` attribute but no `assembly=`).
_CHR1_LENGTH_BY_BUILD = {
    249_250_621: "GRCh37",
    248_956_422: "GRCh38",
}

_CONTIG_LENGTH_RE = re.compile(r"length=(\d+)", re.IGNORECASE)
_CONTIG_ID_RE = re.compile(r"ID=(chr)?1,", re.IGNORECASE)


def detect_vcf_assembly(header_lines: List[str]) -> Optional[str]:
    """
    Best-effort detection of the genome build a VCF's header declares.
    Returns 'GRCh37', 'GRCh38', or None if undeterminable.
    """
    joined = "\n".join(header_lines).lower()

    for marker in _GRCH38_MARKERS:
        if marker in joined:
            return "GRCh38"
    for marker in _GRCH37_MARKERS:
        if marker in joined:
            return "GRCh37"

    # No explicit build tag anywhere in the header -- fingerprint via
    # the chr1 contig length, which differs between GRCh37 and GRCh38.
    for line in header_lines:
        if not line.startswith("##contig"):
            continue
        if not _CONTIG_ID_RE.search(line):
            continue
        length_match = _CONTIG_LENGTH_RE.search(line)
        if length_match:
            return _CHR1_LENGTH_BY_BUILD.get(int(length_match.group(1)))

    return None


def _normalize_build(build: str) -> str:
    """Collapse assorted spellings ('hg19', 'GRCh37', 'b37', ...) to one token."""
    token = build.lower().replace("-", "").replace("_", "").replace(".", "")
    if "37" in token or "hg19" in token:
        return "grch37"
    if "38" in token or "hg38" in token:
        return "grch38"
    return token


def validate_assembly(header_lines: List[str], cli_assembly: Optional[str]) -> Optional[str]:
    """
    Validate the requested --assembly (if any) against what the VCF
    header declares, before any variant is processed.

    Returns the assembly GEPER should actually use for Ensembl lookups
    (the CLI value if given and consistent, otherwise the detected
    value, otherwise None to let Ensembl fall back to its default).

    Raises AssemblyMismatchError if the VCF and --assembly definitely
    disagree.
    """
    detected = detect_vcf_assembly(header_lines)

    if detected is None:
        if cli_assembly is None:
            # Nobody has established the build: not the VCF, not the
            # caller. Falling through here returned None, which
            # `SequenceContextGenerator` documents as "lets Ensembl use
            # its default" -- so a GRCh37 VCF would be annotated against
            # GRCh38 transcript structure and the resulting HGVS c.
            # coordinate would be confidently wrong rather than absent.
            # Refused for the same reason the mismatch below is refused;
            # the only difference is that there the wrong build is
            # stated and here it is assumed.
            raise AssemblyMismatchError(
                "Could not determine the input VCF's genome assembly/build "
                "from its header (no ##reference/##contig assembly tag and "
                "no recognizable chr1 contig length), and no --assembly was "
                "given. Reference-sequence and transcript lookups would be "
                "fetched from whichever build Ensembl defaults to, silently "
                "corrupting every downstream model input and every HGVS c. "
                "coordinate for every variant in this run. Re-run with "
                "--assembly GRCh38 or --assembly GRCh37 to state the build "
                "explicitly, or add a ##reference line to the VCF header so "
                "it can be detected."
            )
        logger.warning(
            "Could not determine the input VCF's genome assembly/build from "
            "its header (no ##reference/##contig assembly tag and no "
            f"recognizable chr1 contig length). Proceeding with --assembly={cli_assembly}; "
            "if results look wrong, double-check the VCF was generated "
            "against the build you expect (GRCh37 vs GRCh38)."
        )
        return cli_assembly

    logger.info(f"Detected VCF genome assembly/build: {detected} (from header).")

    if cli_assembly and _normalize_build(cli_assembly) != _normalize_build(detected):
        raise AssemblyMismatchError(
            f"Assembly mismatch: the VCF header indicates '{detected}' but "
            f"--assembly was set to '{cli_assembly}'. Reference-sequence "
            "lookups would be fetched from the wrong genome build, silently "
            "corrupting every downstream model input for every variant in "
            f"this run. Re-run with --assembly {detected} to match the VCF, "
            "or omit --assembly entirely to let GEPER use the detected "
            "build automatically, or correct the VCF's header if this "
            "detection is itself wrong."
        )

    return cli_assembly or detected
