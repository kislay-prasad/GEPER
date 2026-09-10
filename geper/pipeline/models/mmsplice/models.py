"""
Typed data structures and constants shared across the MMSplice module.

Keeping these in one file (rather than scattering dicts/tuples across
loader/predictor/service) means the exact shape of every intermediate
and final MMSplice result is defined once, is fully typed, and matches
one canonical schema everywhere -- the same "one dataclass, everyone
imports it" shape `pipeline/sequence_context.py::SequenceContext` and
`pipeline/vcf_parser.py::Variant` already use elsewhere in GEPER.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# MMSplice's five modular sub-models, in the exact order the official
# implementation concatenates their scores (mmsplice.mmsplice.MMSplice.
# predict_modular_scores_on_batch): acceptor-intron, acceptor, exon,
# donor, donor-intron. Ported here (not imported from the `mmsplice`
# PyPI package) so this module never needs to import `mmsplice.utils`
# or `mmsplice.mmsplice` -- see loader.py's module docstring for why.
# ---------------------------------------------------------------------------
MODULE_NAMES: List[str] = ["acceptor_intron", "acceptor", "exon", "donor", "donor_intron"]

# The five official pretrained Keras weight files bundled inside the
# `mmsplice` PyPI package (site-packages/mmsplice/models/*.h5), in the
# same order as MODULE_NAMES. These are the *real*, DeepMind/TUM-trained
# MMSplice weights (Cheng et al. 2019, Genome Biology) -- GEPER never
# trains or fabricates its own splice model.
MODEL_FILENAMES: Dict[str, str] = {
    "acceptor_intron": "Intron3.h5",
    "acceptor": "Acceptor.h5",
    "exon": "Exon.h5",
    "donor": "Donor.h5",
    "donor_intron": "Intron5.h5",
}


@dataclass(frozen=True)
class ExonAnnotation:
    """
    One exon of one transcript, as returned by Ensembl's overlap/region
    endpoint, normalized to 1-based inclusive genomic coordinates
    (matching VCF/`Variant` convention used throughout GEPER).
    """

    chrom: str
    start: int
    end: int
    strand: int  # +1 or -1
    exon_id: str
    transcript_id: str
    gene_id: Optional[str] = None
    exon_rank: Optional[int] = None  # 1-based position of this exon within the transcript
    transcript_exon_count: Optional[int] = None

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def is_first_exon(self) -> bool:
        return self.exon_rank == 1

    @property
    def is_last_exon(self) -> bool:
        return (
            self.exon_rank is not None
            and self.transcript_exon_count is not None
            and self.exon_rank == self.transcript_exon_count
        )


@dataclass(frozen=True)
class SpliceWindow:
    """
    The genomic window MMSplice actually scores for one (variant, exon)
    pair: the exon itself plus its upstream (acceptor-side) and
    downstream (donor-side) intronic overhang, oriented in *transcript*
    (5'->3') direction -- i.e. "upstream_overhang" is always the
    acceptor side and "downstream_overhang" is always the donor side,
    regardless of genomic strand.
    """

    exon: ExonAnnotation
    window_genomic_start: int  # min(exon.start, exon.end) minus the larger overhang, clamped >= 1
    window_genomic_end: int
    upstream_overhang: int  # actual bp of acceptor-side intron included (<= configured window)
    downstream_overhang: int  # actual bp of donor-side intron included (<= configured window)
    variant_distance_to_acceptor: Optional[
        int
    ]  # bp from variant to this exon's acceptor boundary (None if not applicable)
    variant_distance_to_donor: Optional[int]  # bp from variant to this exon's donor boundary (None if not applicable)


@dataclass(frozen=True)
class EligibilityResult:
    """Outcome of the eligibility check (requirement #4)."""

    eligible: bool
    reason: str
    region: Optional[str] = None  # e.g. "intronic_near_acceptor", "exonic_near_donor", "deep_intronic"
    splice_window: Optional[SpliceWindow] = None


@dataclass(frozen=True)
class ModularScores:
    """Raw 5-value modular score vector for one allele (ref or alt)."""

    acceptor_intron: float
    acceptor: float
    exon: float
    donor: float
    donor_intron: float

    def as_list(self) -> List[float]:
        return [self.acceptor_intron, self.acceptor, self.exon, self.donor, self.donor_intron]

    @classmethod
    def from_list(cls, values: List[float]) -> "ModularScores":
        return cls(*[float(v) for v in values])


@dataclass
class MMSpliceRawPrediction:
    """
    Full raw numeric output of one MMSplice prediction, before the
    interpretation layer turns it into human-readable text. This is
    the internal contract between predictor.py and service.py.
    """

    ref_scores: ModularScores
    alt_scores: ModularScores
    delta_logit_psi: float
    donor_delta: float
    acceptor_delta: float
    exon_delta: float
    acceptor_intron_delta: float
    donor_intron_delta: float
    alt_donor_score: float
    alt_acceptor_score: float
    exon_skipping_score: float
    intron_retention_score: float
    model_version: str
    runtime_ms: float


# ---------------------------------------------------------------------------
# Standard genomic constant: canonical splice dinucleotides.
# ---------------------------------------------------------------------------
CANONICAL_DONOR_DINUCLEOTIDE = "GT"
CANONICAL_ACCEPTOR_DINUCLEOTIDE = "AG"
