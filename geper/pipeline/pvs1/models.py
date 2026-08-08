"""
Data model for the PVS1 (null variant) evidence rule.

Kept deliberately separate from `pipeline/vcf_parser.py::Variant` and
from the per-variant evidence shapes already in this codebase, for the
same reason `pipeline/clingen/models.py` is separate: PVS1 is the one
ACMG/AMP criterion whose answer depends on *transcript structure* --
which exon the variant lands in, how much coding sequence lies 3' of
it, and where the last exon-exon junction is -- and none of GEPER's
existing evidence dicts carry that. `TranscriptContext` below is that
missing input, modelled once here and consumed by
`pipeline/pvs1/decision_tree.py`.

Everything in this module is pure: plain dataclasses, arithmetic, and
`to_dict()`. No network, no IO, no CONFIG-dependent behaviour -- so the
decision tree built on top of it is fully unit-testable against frozen
transcript structures (see `tests/fixtures/pvs1_transcripts.json`,
which holds real Ensembl MANE Select structures).

Coordinate conventions used throughout:
  - All genomic coordinates are 1-based, inclusive, and always stored
    with `start <= end` regardless of strand (Ensembl's convention).
  - "Transcript order" means 5'->3' *along the transcript*: ascending
    genomic coordinate on the + strand, descending on the - strand.
  - CDS coordinates are 1-based from the A of the initiator ATG, i.e.
    the `c.` numbering of HGVS for positions inside the CDS. The
    mapping implemented here was validated against five real ClinVar
    HGVS records on both strands (see `tests/test_pvs1.py`).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Qualifying "null variant" classes
# ---------------------------------------------------------------------------
#
# ACMG/AMP 2015 (Richards et al.) defines PVS1's scope as: "null
# variant (nonsense, frameshift, canonical +-1 or 2 splice sites,
# initiation codon, single or multiexon deletion) in a gene where LOF
# is a known mechanism of disease". These are those classes, named
# once here so the classifier, the decision tree and the rule engine
# all agree on the vocabulary.

NULL_NONSENSE = "nonsense"
NULL_FRAMESHIFT = "frameshift"
NULL_CANONICAL_SPLICE = "canonical_splice_site"
NULL_INITIATION_CODON = "initiation_codon"
NULL_EXON_DELETION = "exon_deletion"
NULL_WHOLE_GENE_DELETION = "whole_gene_deletion"

QUALIFYING_NULL_TYPES = (
    NULL_NONSENSE,
    NULL_FRAMESHIFT,
    NULL_CANONICAL_SPLICE,
    NULL_INITIATION_CODON,
    NULL_EXON_DELETION,
    NULL_WHOLE_GENE_DELETION,
)

# ---------------------------------------------------------------------------
# Evidence strengths
# ---------------------------------------------------------------------------
#
# The ClinGen SVI PVS1 recommendation (Abou Tayoun et al., Hum Mutat
# 2018, PMID 30192042) refines the single binary PVS1 of ACMG/AMP 2015
# into a decision tree whose leaves are graded strengths. These strings
# match the keys of `pipeline/acmg_rules.py::_POINTS` exactly, so a
# downgraded PVS1 contributes the right number of points to the
# combining rules without any translation layer.

STRENGTH_VERY_STRONG = "very_strong"
STRENGTH_STRONG = "strong"
STRENGTH_MODERATE = "moderate"
STRENGTH_SUPPORTING = "supporting"
STRENGTH_NOT_APPLICABLE = "not_applicable"

_STRENGTH_ORDER = (
    STRENGTH_NOT_APPLICABLE,
    STRENGTH_SUPPORTING,
    STRENGTH_MODERATE,
    STRENGTH_STRONG,
    STRENGTH_VERY_STRONG,
)

STRENGTH_LABELS = {
    STRENGTH_VERY_STRONG: "PVS1 (Very Strong)",
    STRENGTH_STRONG: "PVS1_Strong",
    STRENGTH_MODERATE: "PVS1_Moderate",
    STRENGTH_SUPPORTING: "PVS1_Supporting",
    STRENGTH_NOT_APPLICABLE: "PVS1 not applicable",
}


def strength_rank(strength: str) -> int:
    """Numeric ordering of PVS1 strengths, weakest first (0 = not applicable)."""
    try:
        return _STRENGTH_ORDER.index(strength)
    except ValueError:
        return 0


def cap_strength(strength: str, ceiling: str) -> str:
    """Return `strength`, downgraded to `ceiling` if it is stronger than the ceiling."""
    return strength if strength_rank(strength) <= strength_rank(ceiling) else ceiling


# ---------------------------------------------------------------------------
# Gene-level loss-of-function mechanism
# ---------------------------------------------------------------------------
#
# PVS1's precondition -- "in a gene where LOF is a known mechanism of
# disease" -- is a *gene-level* judgement, not a variant-level one.
# GEPER's integrated source for it is ClinGen's dosage-sensitivity
# curation (see `pipeline/clingen/`). These are the states that
# curation can put a gene in; "unknown" is a first-class value, never
# silently collapsed into "not established", because the two lead to
# different reporting (a gap to fill vs. a curated negative).

LOF_ESTABLISHED = "established"
LOF_ESTABLISHED_RECESSIVE = "established_autosomal_recessive"
LOF_NOT_ESTABLISHED = "not_established"
LOF_REFUTED = "refuted"
LOF_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExonSpan:
    """One exon of one transcript. `rank` is 1-based in transcript order."""

    start: int  # 1-based genomic, inclusive; always <= end, regardless of strand
    end: int
    rank: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def contains(self, pos: int) -> bool:
        return self.start <= pos <= self.end

    def to_dict(self) -> Dict[str, Any]:
        return {"start": self.start, "end": self.end, "rank": self.rank, "length": self.length}


@dataclass(frozen=True)
class CodingSpan:
    """
    The coding (CDS) portion of one exon: its genomic bounds clipped to
    the translated region, plus the 1-based CDS interval it occupies.
    `length` is 0 for wholly non-coding (UTR-only) exons, which is why
    the NMD arithmetic below filters on `length > 0` -- the ClinGen SVI
    rule counts *coding* exons, not transcribed ones.
    """

    exon_rank: int
    start: int  # genomic, clipped to the CDS; 0-length spans carry start > end
    end: int
    length: int
    cds_start: Optional[int]  # 1-based CDS coordinate of this exon's first coding base
    cds_end: Optional[int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exon_rank": self.exon_rank,
            "genomic_start": self.start,
            "genomic_end": self.end,
            "length": self.length,
            "cds_start": self.cds_start,
            "cds_end": self.cds_end,
        }


@dataclass
class TranscriptContext:
    """
    The transcript structure PVS1 needs, plus the derived arithmetic the
    ClinGen SVI decision tree asks for (NMD boundary, fraction of
    protein lost, exon-skipping frame effect).

    `biologically_relevant` encodes the SVI tree's "exon is present in
    a biologically-relevant transcript" node at the transcript level:
    True for a MANE Select / clinically-established transcript, False
    when a caller has positively determined the transcript is not the
    disease-relevant one, and None when unknown. None is *not* treated
    as False -- an unknown is reported as an unchecked caveat instead
    (see `decision_tree.py`).
    """

    transcript_id: str
    chrom: str
    strand: int  # +1 / -1
    exons: List[ExonSpan]  # any order on input; normalized to transcript order in __post_init__
    cds_genomic_start: int  # lower genomic coordinate of the CDS (NOT necessarily the ATG)
    cds_genomic_end: int  # higher genomic coordinate of the CDS
    gene_symbol: Optional[str] = None
    protein_length: Optional[int] = None  # amino acids, excluding the stop codon
    is_mane_select: bool = False
    is_canonical: bool = False
    biologically_relevant: Optional[bool] = None
    source: str = "unknown"
    # The transcript's CDS, in transcript orientation (already
    # reverse-complemented for minus-strand genes), when the lookup
    # fetched it. This is what lets a substitution's consequence be
    # called from the real reading frame instead of from
    # `pipeline/protein_translator.py`'s frame-unaware sequence window
    # -- see `pipeline/pvs1/utils.py::coding_consequence`.
    cds_sequence: Optional[str] = None

    def __post_init__(self):
        ordered = sorted(self.exons, key=lambda e: e.start, reverse=self.strand < 0)
        self.exons = [ExonSpan(start=e.start, end=e.end, rank=i) for i, e in enumerate(ordered, start=1)]

    # -- basic structure -------------------------------------------------

    @property
    def exon_count(self) -> int:
        return len(self.exons)

    def exon_at(self, pos: int) -> Optional[ExonSpan]:
        for exon in self.exons:
            if exon.contains(pos):
                return exon
        return None

    def exon_by_rank(self, rank: int) -> Optional[ExonSpan]:
        for exon in self.exons:
            if exon.rank == rank:
                return exon
        return None

    # -- coding structure ------------------------------------------------

    def coding_spans(self) -> List[CodingSpan]:
        """Per-exon CDS spans in transcript order (UTR-only exons get length 0)."""
        spans: List[CodingSpan] = []
        cumulative = 0
        for exon in self.exons:
            low = max(exon.start, self.cds_genomic_start)
            high = min(exon.end, self.cds_genomic_end)
            length = max(0, high - low + 1)
            if length == 0:
                spans.append(CodingSpan(exon.rank, low, high, 0, None, None))
                continue
            spans.append(
                CodingSpan(
                    exon_rank=exon.rank,
                    start=low,
                    end=high,
                    length=length,
                    cds_start=cumulative + 1,
                    cds_end=cumulative + length,
                )
            )
            cumulative += length
        return spans

    def coding_exon_sizes(self) -> List[int]:
        """Coding length of each coding exon, in transcript order (UTR-only exons dropped)."""
        return [span.length for span in self.coding_spans() if span.length > 0]

    @property
    def cds_length(self) -> int:
        """Total CDS length in nucleotides, including the stop codon."""
        return sum(self.coding_exon_sizes())

    @property
    def total_codons(self) -> Optional[int]:
        """
        Number of amino acids in the encoded protein, excluding the stop
        codon. Prefers the annotated `protein_length` when available and
        falls back to CDS arithmetic, so a transcript whose CDS is not a
        clean multiple of 3 (annotation edge cases) still yields a usable
        denominator for the ">10% of protein" rule rather than silently
        skewing it.
        """
        if self.protein_length:
            return self.protein_length
        cds = self.cds_length
        if cds < 6:
            return None
        return (cds // 3) - 1

    # -- coordinate mapping ----------------------------------------------

    def cds_position(self, pos: int) -> Optional[int]:
        """1-based CDS (`c.`) coordinate of a genomic position, or None if it is intronic/UTR."""
        for span in self.coding_spans():
            if span.length == 0 or not (span.start <= pos <= span.end):
                continue
            offset = (pos - span.start) if self.strand > 0 else (span.end - pos)
            return span.cds_start + offset
        return None

    def codon_at(self, pos: int) -> Optional[int]:
        """1-based codon (amino acid) number of a genomic position inside the CDS."""
        cds_pos = self.cds_position(pos)
        return None if cds_pos is None else (cds_pos + 2) // 3

    def first_affected_codon(self, pos: int, ref: str, alt: str) -> Optional[int]:
        """
        1-based codon number for an indel's reported "affected codon",
        or None if none of the REF/ALT span lands in the CDS at all.

        `pos` itself (the VCF REF/ALT anchor base) is used whenever it
        maps into the CDS -- unchanged from plain `codon_at(pos)`, so
        the codon reported for an ordinary indel is exactly what it
        always was. `pos` alone breaks down only for an indel whose
        breakpoints sit exactly on an exon boundary (e.g. a clean
        whole-exon deletion): the anchor base can fall in the
        neighbouring intron even though the deletion itself removes a
        large, genuinely coding stretch, and `codon_at(pos)` then
        silently returns None for a variant that plainly has an
        affected codon. Only in that fallback case do we scan forward
        through the rest of the REF/ALT span for the first base that
        does map into the CDS -- scanning unconditionally (picking
        whichever scanned base maps to the numerically smallest CDS
        coordinate) would instead risk *changing* the reported codon
        for ordinary indels on a minus-strand transcript, where a
        larger genomic offset maps to an earlier CDS coordinate.
        """
        cds_pos = self.cds_position(pos)
        if cds_pos is None:
            span = max(len(ref), len(alt))
            for offset in range(1, span):
                cds_pos = self.cds_position(pos + offset)
                if cds_pos is not None:
                    break
        if cds_pos is None:
            return None
        return (cds_pos + 2) // 3

    def genomic_position_for_cds(self, cds_pos: int) -> Optional[int]:
        """Inverse of `cds_position`: the genomic position of a 1-based CDS coordinate, or None if out of range."""
        for span in self.coding_spans():
            if span.length == 0 or not (span.cds_start <= cds_pos <= span.cds_end):
                continue
            offset = cds_pos - span.cds_start
            return (span.start + offset) if self.strand > 0 else (span.end - offset)
        return None

    def genomic_positions_for_codon(self, codon_number: int) -> List[int]:
        """
        The (up to 3) genomic positions of one codon, each mapped
        independently via `genomic_position_for_cds` -- deliberately
        *not* assumed contiguous. A "split codon" whose bases straddle
        an exon-exon junction (its first base the last base of one
        exon, its other bases the first bases of the next, with a
        whole intron between them -- not rare; ~10% of codons in a
        typical multi-exon gene split this way) has genomic positions
        that are nowhere near each other, and code that assumed
        `[low, low+1, low+2]` would silently query the wrong locus for
        two of its three bases. Returns fewer than 3 positions only if
        the codon number is out of range for this transcript.
        """
        first_cds = (codon_number - 1) * 3 + 1
        positions = [self.genomic_position_for_cds(first_cds + i) for i in range(3)]
        return [p for p in positions if p is not None]

    def distance_to_nearest_exon_boundary(self, pos: int) -> Optional[int]:
        """
        Distance in bp from an exonic position to the nearest exon-intron
        junction of *its own* exon (0 = the terminal exonic base itself,
        adjacent to the canonical splice dinucleotide). None if `pos`
        is not exonic at all (intronic/UTR/outside the transcript).

        Exists for the same reason `pipeline/pvs1/decision_tree.py`
        checks `splice_site_at` before trusting a truncation's NMD
        call: an exonic base within a few bp of a junction is
        "splice-region" territory (VEP's own `splice_region_variant`
        consequence uses this exact 3-bp exonic convention) even though
        it is not itself one of the canonical +-1/+-2 intronic bases
        `splice_site_at` detects. A missense substitution there can
        plausibly disrupt splicing *in addition to* changing the amino
        acid -- exactly the ambiguity `pipeline/ps1_pm5/`'s PS1/PM5
        splice-proximity caveat exists to catch.
        """
        for exon in self.exons:
            if not (exon.start <= pos <= exon.end):
                continue
            return min(pos - exon.start, exon.end - pos)
        return None

    def coding_exon_rank_at(self, pos: int) -> Optional[int]:
        for span in self.coding_spans():
            if span.length > 0 and span.start <= pos <= span.end:
                return span.exon_rank
        return None

    def splice_site_at(self, pos: int) -> Optional[Tuple[int, str, int]]:
        """
        Classify a position as a canonical (+-1/+-2) splice site.

        Returns `(exon_rank, "donor" | "acceptor", offset)` where the
        exon is the one whose splice site is disrupted and `offset` is 1
        or 2 (distance into the intron), or None if the position is not
        a canonical splice dinucleotide of this transcript. The first
        exon has no acceptor and the last no donor -- there is no intron
        on those sides -- so both are excluded.
        """
        for exon in self.exons:
            # In transcript orientation the donor follows the exon's 3'
            # end and the acceptor precedes its 5' start; on the minus
            # strand those are the lower and higher genomic coordinates
            # respectively.
            if self.strand > 0:
                donor_bases = (exon.end + 1, exon.end + 2)
                acceptor_bases = (exon.start - 1, exon.start - 2)
            else:
                donor_bases = (exon.start - 1, exon.start - 2)
                acceptor_bases = (exon.end + 1, exon.end + 2)

            if exon.rank < self.exon_count and pos in donor_bases:
                return exon.rank, "donor", donor_bases.index(pos) + 1
            if exon.rank > 1 and pos in acceptor_bases:
                return exon.rank, "acceptor", acceptor_bases.index(pos) + 1
        return None

    def is_initiation_codon(self, pos: int) -> bool:
        """True if the position falls in the first coding codon (c.1-c.3)."""
        cds_pos = self.cds_position(pos)
        return cds_pos is not None and cds_pos <= 3

    # -- ClinGen SVI arithmetic ------------------------------------------

    def nmd_cutoff_cds(self, nmd_penultimate_window: int = 50) -> Optional[int]:
        """
        The last CDS nucleotide position at which a premature termination
        codon is still predicted to trigger nonsense-mediated decay.

        Implements the ClinGen SVI rule ("NMD is not predicted to occur
        if the premature termination codon occurs in the 3'-most exon or
        within the 3'-most 50 nucleotides of the penultimate exon") as
        the equivalent CDS-coordinate cutoff:

            cutoff = (CDS length up to the end of the penultimate exon)
                     - min(50, penultimate coding exon length)

        `min(...)` matters for genes whose penultimate coding exon is
        shorter than 50 nt -- without it the cutoff would spill back into
        the antepenultimate exon and wrongly call NMD-escape there.

        Returns None for single-coding-exon transcripts: NMD requires a
        downstream exon-exon junction, so those escape it entirely and
        the caller must not compare against a cutoff at all.
        """
        sizes = self.coding_exon_sizes()
        if len(sizes) <= 1:
            return None
        return sum(sizes[:-1]) - min(nmd_penultimate_window, sizes[-2])

    def is_nmd_predicted(self, termination_codon: int, nmd_penultimate_window: int = 50) -> bool:
        """
        Whether a premature termination codon at `termination_codon`
        (1-based amino acid position) is predicted to trigger NMD.
        Single-coding-exon transcripts always return False.
        """
        cutoff = self.nmd_cutoff_cds(nmd_penultimate_window)
        if cutoff is None:
            return False
        return termination_codon * 3 <= cutoff

    def is_in_last_exon(self, pos: int) -> Optional[bool]:
        rank = self.coding_exon_rank_at(pos)
        if rank is None:
            return None
        coding_ranks = [s.exon_rank for s in self.coding_spans() if s.length > 0]
        return bool(coding_ranks) and rank == coding_ranks[-1]

    def fraction_of_protein_lost(self, first_lost_codon: int) -> Optional[float]:
        """
        Fraction of the protein removed by a truncation whose first lost
        residue is `first_lost_codon`. Feeds the SVI tree's ">10% of the
        protein" node.
        """
        total = self.total_codons
        if not total or first_lost_codon <= 0:
            return None
        lost = max(0, total - first_lost_codon + 1)
        return lost / total

    def exon_skip_preserves_frame(self, exon_rank: int) -> Optional[bool]:
        """
        Whether skipping the coding portion of one exon leaves the
        reading frame intact -- the SVI tree's first question for a
        canonical splice-site variant. None if the exon has no coding
        sequence to skip.
        """
        for span in self.coding_spans():
            if span.exon_rank == exon_rank:
                return None if span.length == 0 else (span.length % 3 == 0)
        return None

    def nmd_after_exon_skip(self, exon_rank: int, nmd_penultimate_window: int = 50) -> Optional[Dict[str, Any]]:
        """
        NMD prediction for the frameshifted transcript that results from
        skipping one coding exon (the canonical-splice-site branch of the
        SVI tree).

        Skipping an exon changes the exon-exon junction structure, so NMD
        must be judged against the *post-skip* transcript, not the
        reference one -- evaluating it against the reference junctions is
        the single most common way to get a terminal-exon splice variant
        wrong (it makes the variant look NMD-competent when the new stop
        codon actually lands in the final exon).

        The exact position of the new stop codon is not computable
        without translating the spliced sequence, so this returns the
        earliest position it can occupy -- the junction where the frame
        shifts -- together with the margin between that lower bound and
        the NMD cutoff. A large margin means the NMD call is robust (a
        random frameshift meets a stop codon after ~20 codons on
        average); a small one is reported as uncertain by the caller.

        Returns None if the exon has no coding sequence or the post-skip
        transcript has fewer than two coding exons.
        """
        spans = [s for s in self.coding_spans() if s.length > 0]
        target = next((s for s in spans if s.exon_rank == exon_rank), None)
        if target is None:
            return None

        post_skip_sizes = [s.length for s in spans if s.exon_rank != exon_rank]
        if len(post_skip_sizes) <= 1:
            return {
                "nmd_predicted": False,
                "cutoff_cds": None,
                "ptc_lower_bound_cds": None,
                "margin_codons": None,
                "reason": "post-skip transcript has a single coding exon; NMD requires a downstream junction.",
            }

        cutoff = sum(post_skip_sizes[:-1]) - min(nmd_penultimate_window, post_skip_sizes[-2])
        # Coding sequence 5' of the skipped exon is unchanged by the
        # skip, so its length is both the skipped exon's reference
        # cds_start - 1 and the post-skip coordinate at which the new
        # reading frame begins.
        ptc_lower_bound = target.cds_start - 1
        return {
            "nmd_predicted": ptc_lower_bound <= cutoff,
            "cutoff_cds": cutoff,
            "ptc_lower_bound_cds": ptc_lower_bound,
            "margin_codons": (cutoff - ptc_lower_bound) / 3.0,
            "reason": None,
        }

    def coding_span_for_rank(self, exon_rank: int) -> Optional[CodingSpan]:
        for span in self.coding_spans():
            if span.exon_rank == exon_rank:
                return span
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "transcript_id": self.transcript_id,
            "gene_symbol": self.gene_symbol,
            "chrom": self.chrom,
            "strand": self.strand,
            "exon_count": self.exon_count,
            "coding_exon_count": len(self.coding_exon_sizes()),
            "cds_genomic_start": self.cds_genomic_start,
            "cds_genomic_end": self.cds_genomic_end,
            "cds_length": self.cds_length,
            "protein_length": self.total_codons,
            "is_mane_select": self.is_mane_select,
            "is_canonical": self.is_canonical,
            "biologically_relevant": self.biologically_relevant,
            "source": self.source,
            "nmd_cutoff_cds": self.nmd_cutoff_cds(),
            "cds_sequence": self.cds_sequence,
            "exons": [e.to_dict() for e in self.exons],
        }


@dataclass
class PVS1Evaluation:
    """
    The result of one PVS1 evaluation: the strength that applies, the
    SVI decision-tree leaf that produced it, and the full audit trail of
    every caveat that was checked (with its answer) plus every one that
    could not be checked because GEPER has no integrated source for it.

    `provisional_strength` is what the decision tree returned *before*
    the gene-level LOF-mechanism gate was applied. It is reported even
    when PVS1 does not apply, so a reviewer can see "this would have
    been Very Strong had the gene's mechanism been established" rather
    than just a bare negative.
    """

    applies: bool
    strength: str
    criterion_code: str  # SVI/AutoPVS1-style leaf code, e.g. "NF1", "SS3", "IC4", "DEL2"
    null_variant_type: Optional[str]
    rationale: str
    provisional_strength: Optional[str] = None
    lof_mechanism: str = LOF_UNKNOWN
    decision_path: List[str] = field(default_factory=list)
    caveats_checked: List[str] = field(default_factory=list)
    unchecked_caveats: List[str] = field(default_factory=list)
    supporting_evidence: List[str] = field(default_factory=list)
    conflicting_evidence: List[str] = field(default_factory=list)
    evidence_sources: List[str] = field(default_factory=list)
    confidence: Optional[str] = None
    transcript_id: Optional[str] = None
    termination_codon: Optional[int] = None
    nmd_predicted: Optional[bool] = None

    @property
    def strength_label(self) -> str:
        return STRENGTH_LABELS.get(self.strength, self.strength)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "applies": self.applies,
            "strength": self.strength,
            "strength_label": self.strength_label,
            "provisional_strength": self.provisional_strength,
            "criterion_code": self.criterion_code,
            "null_variant_type": self.null_variant_type,
            "lof_mechanism": self.lof_mechanism,
            "rationale": self.rationale,
            "decision_path": self.decision_path,
            "caveats_checked": self.caveats_checked,
            "unchecked_caveats": self.unchecked_caveats,
            "supporting_evidence": self.supporting_evidence,
            "conflicting_evidence": self.conflicting_evidence,
            "evidence_sources": self.evidence_sources,
            "confidence": self.confidence,
            "transcript_id": self.transcript_id,
            "termination_codon": self.termination_codon,
            "nmd_predicted": self.nmd_predicted,
        }
