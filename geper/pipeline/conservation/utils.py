"""
Shared, dependency-free helpers for the conservation-score
integration: build normalization/UCSC-genome-id mapping and cache-key
construction. Mirrors `pipeline/gnomad/utils.py`'s shape.
"""

from typing import Optional

_GRCH37_MARKERS = ("grch37", "hg19", "b37")
_GRCH38_MARKERS = ("grch38", "hg38", "b38")

# UCSC's own genome identifiers -- both the bigWig files and the REST
# API use these, not "GRCh37"/"GRCh38".
_UCSC_GENOME_ID = {"GRCh38": "hg38", "GRCh37": "hg19"}


def normalize_build(assembly: Optional[str]) -> str:
    """Same accepted spellings / same GRCh38 default as
    `pipeline.gnomad.utils.normalize_build` -- kept as its own copy
    (not a shared import) matching this codebase's existing
    convention of each evidence-source package owning its own small
    utils module (see pipeline/gnomad/utils.py, pipeline/clingen/utils.py)."""
    if not assembly:
        return "GRCh38"
    label = assembly.strip().lower().replace("-", "").replace("_", "")
    if label in _GRCH37_MARKERS:
        return "GRCh37"
    if label in _GRCH38_MARKERS:
        return "GRCh38"
    return "GRCh38"


def ucsc_genome_id(build: str) -> str:
    """ "GRCh38"/"GRCh37" -> "hg38"/"hg19" -- the identifier both the
    local bigWig filename convention and UCSC's REST API expect."""
    return _UCSC_GENOME_ID.get(build, "hg38")


def normalize_chrom(chrom: str) -> str:
    """UCSC (both its bigWig files and its REST API) always uses
    'chr'-prefixed contig names, for both hg19 and hg38 -- unlike
    gnomAD, which varies by build (see
    pipeline.gnomad.utils.normalize_chrom).

    The mitochondrial contig needs its own case: UCSC's own name for it
    is 'chrM', never 'chrMT' -- a bare strip-and-reprefix maps an
    Ensembl-style 'MT' input to the non-existent 'chrMT' contig (round
    15 finding; same M-family special case already handled correctly in
    pipeline.hgvs_utils._strip_chr and models.alphamissense.
    _normalize_chrom_for_catalogue)."""
    bare = chrom[3:] if chrom.lower().startswith("chr") else chrom
    if bare.upper() in ("M", "MT"):
        return "chrM"
    return f"chr{bare}"


def position_key(chrom: str, pos: int, build: str) -> str:
    """Stable cache/dedup key -- build-qualified, position-only (not
    allele-qualified, see models.py's own docstring for why)."""
    bare_chrom = chrom[3:] if chrom.lower().startswith("chr") else chrom
    return f"{build}:{bare_chrom}:{pos}"
