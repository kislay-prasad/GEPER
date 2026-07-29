"""
Data model for the PS1 and PM5 (established-pathogenic-position)
evidence rules.

Both criteria answer variations on one question -- "has ClinVar
already established a pathogenic missense change at this codon, and
how does it relate to the amino acid change this variant makes?" -- so
they share one evidence-gathering step (`ClinVarCodonMatch`, one
qualifying ClinVar record found at the query variant's codon) and
split only at the final comparison: PS1 wants an anchor with the
*same* resulting amino acid as the query (different nucleotide
change); PM5 wants a *different* resulting amino acid at the same
codon. See `pipeline/ps1_pm5/decision.py` for that comparison.

Kept as a separate package from `pipeline/pvs1/`, mirroring why that
package is itself separate from `acmg_rules.py`: this evidence needs a
different external input (a codon-neighborhood ClinVar search) that no
other rule needs, and the confidence-tier / protein-change-parsing
logic it requires is substantial enough to deserve its own module
rather than being folded into `pipeline/acmg_rules.py` directly.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ClinVar's own review-status star-rating tiers (see
# https://www.ncbi.nlm.nih.gov/clinvar/docs/review_status/), most
# authoritative first. Matched against `germline_classification.
# review_status` strings verbatim -- these are ClinVar's own text, not
# GEPER's invention, so a new review-status string ClinVar introduces
# later fails safe (unmatched -> lowest tier) rather than crashing.
STAR_PRACTICE_GUIDELINE = 4
STAR_EXPERT_PANEL = 3
STAR_MULTIPLE_SUBMITTERS_NO_CONFLICTS = 2
STAR_SINGLE_SUBMITTER = 1
STAR_NONE = 0

_REVIEW_STATUS_STARS = {
    "practice guideline": STAR_PRACTICE_GUIDELINE,
    "reviewed by expert panel": STAR_EXPERT_PANEL,
    "criteria provided, multiple submitters, no conflicts": STAR_MULTIPLE_SUBMITTERS_NO_CONFLICTS,
    "criteria provided, single submitter": STAR_SINGLE_SUBMITTER,
    "criteria provided, conflicting classifications": STAR_NONE,
    "no assertion criteria provided": STAR_NONE,
    "no classification provided": STAR_NONE,
    "no classification for the individual variant": STAR_NONE,
}


def star_rating(review_status: Optional[str]) -> int:
    """ClinVar star rating for a `review_status` string; unrecognized/None -> 0 (fails safe, never guesses upward)."""
    if not review_status:
        return STAR_NONE
    return _REVIEW_STATUS_STARS.get(review_status.strip().lower(), STAR_NONE)


def is_conflicting(review_status: Optional[str]) -> bool:
    """Whether ClinVar itself flags this record's classification as disputed among submitters."""
    return bool(review_status) and "conflicting" in review_status.strip().lower()


# Classifications this codebase accepts as "established pathogenic" for
# PS1/PM5 purposes. "Pathogenic/Likely pathogenic" is ClinVar's own
# combined label when multiple submitters agree on the pathogenic side
# without being unanimous on which of the two exact terms -- excluding
# it would silently drop a large share of real 2-star/expert-panel
# records (see e.g. TP53 p.Met237Ile's c.711G>T, "Pathogenic/Likely
# pathogenic", multiple submitters no conflicts -- a real, solid PS1
# anchor). Bare "Likely pathogenic" is deliberately included too: nothing
# in the ACMG/AMP PS1/PM5 wording restricts the anchor to "Pathogenic"
# only, and ClinGen's own SVI recommendations use "established
# pathogenic variant" to mean the P/LP boundary, not P alone.
PATHOGENIC_CLASSIFICATIONS = (
    "Pathogenic",
    "Pathogenic/Likely pathogenic",
    "Likely pathogenic",
)


@dataclass(frozen=True)
class ClinVarCodonMatch:
    """
    One ClinVar missense record found at a query variant's codon,
    already parsed into the fields PS1/PM5 comparison needs.

    `pos`/`ref`/`alt` are on the genomic forward strand (VCF/SPDI
    convention) so a match can be checked for exact self-identity
    against the query variant without a strand-aware comparison.
    """

    uid: str
    accession: Optional[str]
    title: str
    pos: int
    ref: str
    alt: str
    ref_aa: str  # single-letter
    alt_aa: str  # single-letter
    codon_number: int
    clinical_significance: str
    review_status: Optional[str]
    star_rating: int
    is_conflicting: bool
    condition: List[str] = field(default_factory=list)

    @property
    def meets_confidence(self) -> bool:
        return (
            not self.is_conflicting
            and self.clinical_significance in PATHOGENIC_CLASSIFICATIONS
        )

    def is_same_variant_as(self, pos: int, ref: str, alt: str) -> bool:
        return self.pos == pos and self.ref.upper() == ref.upper() and self.alt.upper() == alt.upper()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "accession": self.accession,
            "title": self.title,
            "pos": self.pos,
            "ref": self.ref,
            "alt": self.alt,
            "protein_change": f"{self.ref_aa}{self.codon_number}{self.alt_aa}",
            "codon_number": self.codon_number,
            "clinical_significance": self.clinical_significance,
            "review_status": self.review_status,
            "star_rating": self.star_rating,
            "is_conflicting": self.is_conflicting,
            "condition": self.condition,
        }


@dataclass
class PS1PM5Evaluation:
    """
    Result of evaluating PS1 or PM5 for one variant. Shape mirrors
    `pipeline/pvs1/models.py::PVS1Evaluation` (decision_path /
    caveats_checked / unchecked_caveats / supporting_evidence) so
    `pipeline/acmg_rules.py` adapts both the same way.
    """

    code: str  # "PS1" | "PM5"
    applies: bool
    rationale: str
    # Plain-dict form (`ClinVarCodonMatch.to_dict()`'s shape), not the
    # dataclass itself: `pipeline/ps1_pm5/lookup.py::ClinVarCodonLookup
    # .query_codon` returns matches as dicts for both the fresh-fetch
    # and cache-hit paths (see that method's own comment for why a
    # dataclass/dict split there was a real bug), so
    # `pipeline/ps1_pm5/decision.py` never reconstructs the dataclass
    # and this field matches what it actually has on hand.
    matched_anchors: List[Dict[str, Any]] = field(default_factory=list)
    rejected_anchors: List[Dict[str, Any]] = field(default_factory=list)  # found, but failed confidence/aa filters
    decision_path: List[str] = field(default_factory=list)
    caveats_checked: List[str] = field(default_factory=list)
    unchecked_caveats: List[str] = field(default_factory=list)
    supporting_evidence: List[str] = field(default_factory=list)
    conflicting_evidence: List[str] = field(default_factory=list)
    evidence_sources: List[str] = field(default_factory=list)
    confidence: Optional[str] = None
    query_codon_number: Optional[int] = None
    query_ref_aa: Optional[str] = None
    query_alt_aa: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "applies": self.applies,
            "rationale": self.rationale,
            "matched_anchors": self.matched_anchors,
            "rejected_anchors": self.rejected_anchors,
            "decision_path": self.decision_path,
            "caveats_checked": self.caveats_checked,
            "unchecked_caveats": self.unchecked_caveats,
            "supporting_evidence": self.supporting_evidence,
            "conflicting_evidence": self.conflicting_evidence,
            "evidence_sources": self.evidence_sources,
            "confidence": self.confidence,
            "query_codon_number": self.query_codon_number,
            "query_ref_aa": self.query_ref_aa,
            "query_alt_aa": self.query_alt_aa,
        }
