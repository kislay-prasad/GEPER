"""
Data model for gnomAD evidence.

Kept deliberately separate from `pipeline/vcf_parser.py::Variant` (the
input-side variant record): this module models the *evidence gnomAD
returns*, not the variant itself, mirroring how AlphaMissense/MMSplice
each get their own small, self-contained result shape rather than
mutating `Variant`.

All dataclasses are plain, JSON-serializable (via `to_dict()`), and
have no network/IO/torch dependency whatsoever -- exactly like
`pipeline/models/mmsplice/models.py`.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# The 7 continental populations gnomAD reports today, plus a catch-all
# "remaining" bucket for individuals gnomAD does not assign to one of
# the 7 (gnomAD v4's own terminology: "Remaining individuals").
POPULATIONS = ("afr", "amr", "asj", "eas", "fin", "nfe", "sas", "remaining")

POPULATION_LABELS: Dict[str, str] = {
    "afr": "African/African American",
    "amr": "Admixed American",
    "asj": "Ashkenazi Jewish",
    "eas": "East Asian",
    "fin": "European (Finnish)",
    "nfe": "European (non-Finnish)",
    "sas": "South Asian",
    "remaining": "Remaining",
}


@dataclass
class PopulationFrequency:
    """Allele frequency/count evidence for a single gnomAD population."""

    population: str  # one of POPULATIONS
    ac: Optional[int] = None  # allele count
    an: Optional[int] = None  # allele number
    af: Optional[float] = None  # allele frequency (ac/an; gnomAD also reports this directly)
    hom: Optional[int] = None  # homozygote count
    hemi: Optional[int] = None  # hemizygote count (X/Y non-PAR only; None elsewhere)

    def __post_init__(self) -> None:
        # Derive AF from AC/AN when gnomAD's own af field is absent but
        # both raw counts are present, rather than leaving it as None
        # when it's trivially computable -- never overwrites an af
        # gnomAD itself already reported.
        if self.af is None and self.ac is not None and self.an:
            self.af = self.ac / self.an if self.an > 0 else 0.0

    @property
    def label(self) -> str:
        return POPULATION_LABELS.get(self.population, self.population)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "population": self.population,
            "label": self.label,
            "ac": self.ac,
            "an": self.an,
            "af": self.af,
            "hom": self.hom,
            "hemi": self.hemi,
        }


@dataclass
class GnomadAnnotation:
    """
    Complete gnomAD evidence record for one variant, as returned by a
    provider (local indexed lookup or GraphQL fallback) after parsing.
    """

    chrom: str
    pos: int
    ref: str
    alt: str
    build: str  # "GRCh38" or "GRCh37"
    source: str  # "local_index" | "graphql" | "cache"
    found: bool = False

    genome_af: Optional[float] = None
    exome_af: Optional[float] = None
    ac: Optional[int] = None
    an: Optional[int] = None
    hom: Optional[int] = None
    hemi: Optional[int] = None

    population_breakdown: Dict[str, PopulationFrequency] = field(default_factory=dict)

    # Populated by `annotate_highest_population()` -- kept as an
    # explicit, separately-set field (rather than a computed property)
    # so callers can see at a glance whether it has been resolved yet.
    highest_population: Optional[str] = None

    error: Optional[str] = None

    @property
    def global_af(self) -> Optional[float]:
        """
        Best single "how common is this allele overall" figure.
        gnomAD reports genome and exome call sets somewhat separately;
        when both exist, the genome AF is preferred (larger, more
        representative sample in gnomAD v4's combined design), falling
        back to exome AF, then to whatever `self.ac`/`self.an` (an
        already-merged figure some providers return directly) implies.
        """
        if self.genome_af is not None:
            return self.genome_af
        if self.exome_af is not None:
            return self.exome_af
        if self.ac is not None and self.an:
            return self.ac / self.an if self.an > 0 else 0.0
        return None

    def annotate_highest_population(self) -> None:
        """Resolve `highest_population` from `population_breakdown`, if not already set."""
        if self.highest_population is not None or not self.population_breakdown:
            return
        best_pop, best_af = None, -1.0
        for pop, freq in self.population_breakdown.items():
            if freq.af is not None and freq.af > best_af:
                best_af, best_pop = freq.af, pop
        self.highest_population = best_pop

    def to_dict(self) -> Dict[str, Any]:
        self.annotate_highest_population()
        return {
            "found": self.found,
            "build": self.build,
            "source": self.source,
            "chrom": self.chrom,
            "pos": self.pos,
            "ref": self.ref,
            "alt": self.alt,
            "genome_af": self.genome_af,
            "exome_af": self.exome_af,
            "global_af": self.global_af,
            "ac": self.ac,
            "an": self.an,
            "hom": self.hom,
            "hemi": self.hemi,
            "highest_population": self.highest_population,
            "population_breakdown": {pop: freq.to_dict() for pop, freq in self.population_breakdown.items()},
            "error": self.error,
        }

    @staticmethod
    def not_found(chrom: str, pos: int, ref: str, alt: str, build: str, source: str) -> "GnomadAnnotation":
        return GnomadAnnotation(chrom=chrom, pos=pos, ref=ref, alt=alt, build=build, source=source, found=False)

    @staticmethod
    def from_error(chrom: str, pos: int, ref: str, alt: str, build: str, error: str) -> "GnomadAnnotation":
        return GnomadAnnotation(
            chrom=chrom, pos=pos, ref=ref, alt=alt, build=build, source="error", found=False, error=error
        )
