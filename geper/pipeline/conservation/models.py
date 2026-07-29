"""
Data model for conservation-score evidence.

Position-keyed, not allele-keyed: unlike gnomAD (population frequency
of a specific ALT allele) or ClinVar (curated per-allele
significance), PhyloP/PhastCons/GERP++ are properties of a genomic
*position* in a multi-species alignment -- the score does not depend
on which alternate base is observed there. `ConservationAnnotation`
is still constructed per-variant (chrom+pos+ref+alt, same as every
other evidence dict this pipeline produces) for a uniform calling
convention across `pipeline/orchestrator.py`'s stage helpers, but the
lookup key/cache key only ever needs chrom+pos+build.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ConservationAnnotation:
    """Complete conservation evidence record for one variant's position."""

    chrom: str
    pos: int
    ref: str
    alt: str
    build: str
    source: str
    found: bool
    phylop_score: Optional[float] = None
    phastcons_score: Optional[float] = None
    gerp_score: Optional[float] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chrom": self.chrom,
            "pos": self.pos,
            "ref": self.ref,
            "alt": self.alt,
            "build": self.build,
            "source": self.source,
            "found": self.found,
            "phylop_score": self.phylop_score,
            "phastcons_score": self.phastcons_score,
            "gerp_score": self.gerp_score,
            "error": self.error,
        }

    @classmethod
    def not_found(cls, chrom: str, pos: int, ref: str, alt: str, build: str, source: str) -> "ConservationAnnotation":
        return cls(chrom=chrom, pos=pos, ref=ref, alt=alt, build=build, source=source, found=False)

    @classmethod
    def from_error(cls, chrom: str, pos: int, ref: str, alt: str, build: str, error: str) -> "ConservationAnnotation":
        return cls(chrom=chrom, pos=pos, ref=ref, alt=alt, build=build, source="error", found=False, error=error)
