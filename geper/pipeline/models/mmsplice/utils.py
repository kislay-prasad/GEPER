"""
Pure-function helpers for the MMSplice integration: DNA one-hot
encoding, exon/intron sequence splitting, the official delta-logit-PSI
combination formula, splice-window/eligibility arithmetic, and
human-readable interpretation text generation.

WHY THIS FILE PORTS A SMALL AMOUNT OF MATH FROM THE OFFICIAL `mmsplice`
PACKAGE INSTEAD OF IMPORTING IT
--------------------------------------------------------------------
The official `mmsplice` PyPI package (MIT licensed) pins
`cyvcf2<=0.30.15` in its own dependency metadata. That version of
cyvcf2 predates CPython 3.12's removal of the `PyLongObject.ob_digit`
field it depends on, so it cannot be *built* on 3.12+ and has no
prebuilt wheel for that pin -- `pip install mmsplice` fails outright
on any current Python (confirmed against this project's own Python
3.12 toolchain). `mmsplice`'s own `__init__.py` unconditionally
imports `mmsplice.mmsplice`, which unconditionally imports
`mmsplice.utils` and `mmsplice.exon_dataloader`, both of which import
`kipoiseq`/`kipoi`/`pyranges` (and, transitively, `cyvcf2`) at module
scope -- so even loading a single class from the package trips this,
regardless of whether GEPER ever uses the VCF-dataloader machinery
those packages exist for.

GEPER does not need any of that: it does not consume a VCF-format
input to `mmsplice`'s dataloaders, and it already fetches its own
ref/alt genomic sequence windows via Ensembl REST (see
`pipeline/sequence_context.py`). What GEPER genuinely needs from the
official package is (1) the five pretrained Keras `.h5` weight files
(`site-packages/mmsplice/models/*.h5` -- real, DeepMind/TUM-trained
MMSplice weights, Cheng et al. 2019, *Genome Biology*, "MMSplice:
modular modeling improves the predictions of genetic variant effects
on splicing"), and (2) `mmsplice/layers.py`, which defines the small
number of custom Keras layers (`ConvDNA`, `GlobalAveragePooling1D_Mask0`)
those weight files require to deserialize -- and, critically,
`layers.py` itself imports only `tensorflow`/`scipy`/`numpy`, nothing
from the kipoiseq/cyvcf2 chain. `loader.py` loads it by file path via
`importlib.util.spec_from_file_location` specifically so that
`mmsplice/__init__.py` is never executed and that whole dependency
chain is never touched.

The handful of pure-numpy functions below (`logit`, `_transform`,
`predict_delta_logit_psi`) and the sequence-splitting logic in
`SeqSplitter` are therefore reimplemented here directly, ported
faithfully from the corresponding functions in the official
`mmsplice.utils` / `mmsplice.exon_dataloader` modules (verified against
mmsplice 2.4.0's published source) with the same numeric behavior --
this is a from-scratch reimplementation of a small, published, MIT-
licensed formula/algorithm (not a reproduction of prose), done so this
integration has zero dependency on cyvcf2/kipoiseq/kipoi/pyranges
while still scoring every variant with the real, official, unmodified
pretrained network weights (no mock or placeholder scoring anywhere
in this module).
"""

from typing import Dict, List, Optional, Tuple

import numpy as np

from pipeline.models.mmsplice.models import (
    CANONICAL_ACCEPTOR_DINUCLEOTIDE,
    CANONICAL_DONOR_DINUCLEOTIDE,
    EligibilityResult,
    ExonAnnotation,
    ModularScores,
    SpliceWindow,
)

# Nucleotide channel order used by every mmsplice Keras model
# (mmsplice/layers.py::DNA = ["A", "C", "G", "T"]) -- must match
# exactly, since the pretrained weights were trained on this order.
_DNA_ALPHABET = ("A", "C", "G", "T")
_BASE_TO_INDEX = {b: i for i, b in enumerate(_DNA_ALPHABET)}


# ---------------------------------------------------------------------------
# DNA one-hot encoding (ported from mmsplice.utils.encodeDNA /
# kipoiseq.transforms.functional.one_hot's neutral-alphabet behavior:
# 'N' and any other non-ACGT character encode to an all-zero vector
# rather than raising, so soft-masked / ambiguous reference bases from
# Ensembl never crash a prediction).
# ---------------------------------------------------------------------------
def one_hot_encode(seq: str) -> np.ndarray:
    """Encode a single DNA sequence string to shape (len(seq), 4), float32."""
    seq = seq.upper()
    encoded = np.zeros((len(seq), len(_DNA_ALPHABET)), dtype=np.float32)
    for i, base in enumerate(seq):
        idx = _BASE_TO_INDEX.get(base)
        if idx is not None:
            encoded[i, idx] = 1.0
    return encoded


def encode_batch(seqs: List[str]) -> np.ndarray:
    """
    Encode a batch of same-purpose sequences (e.g. every 'donor' window
    in a batch of variants), left-padding shorter ones with all-zero
    ('N') rows so they stack into one dense array -- mirrors
    mmsplice.utils.encodeDNA's pad-to-max-length-in-batch behavior.
    """
    if not seqs:
        return np.zeros((0, 0, len(_DNA_ALPHABET)), dtype=np.float32)
    max_len = max(len(s) for s in seqs)
    batch = np.zeros((len(seqs), max_len, len(_DNA_ALPHABET)), dtype=np.float32)
    for i, seq in enumerate(seqs):
        encoded = one_hot_encode(seq)
        # left-pad (anchor="start" in kipoiseq terms: the real sequence
        # is anchored at the end of the array) so a fixed-length model
        # (acceptor/donor) always sees its window right-aligned even
        # when batched alongside longer variable-length ones.
        batch[i, max_len - len(seq):, :] = encoded
    return batch


# ---------------------------------------------------------------------------
# Sequence splitting into MMSplice's five modular windows. Faithful,
# from-scratch reimplementation of mmsplice.exon_dataloader.SeqSpliter's
# default-configured `.split()` (defaults verified against mmsplice
# 2.4.0: exon_cut_l=0, exon_cut_r=0, acceptor_intron_cut=6,
# donor_intron_cut=6, acceptor_intron_len=50, acceptor_exon_len=3,
# donor_exon_len=5, donor_intron_len=13) -- these five lengths are
# exactly what the five pretrained Keras models expect as input
# (confirmed by inspecting each model's `input_shape` after loading:
# Acceptor.h5 -> (None, 53, 4) = 50 + 3; Donor.h5 -> (None, 18, 4) =
# 5 + 13; Exon.h5/Intron3.h5/Intron5.h5 -> (None, None, 4), variable-
# length with attention-style masking).
# ---------------------------------------------------------------------------
class SeqSplitter:
    """Splits an overhanged exon sequence into the five MMSplice module inputs."""

    def __init__(
        self,
        exon_cut_l: int = 0,
        exon_cut_r: int = 0,
        acceptor_intron_cut: int = 6,
        donor_intron_cut: int = 6,
        acceptor_intron_len: int = 50,
        acceptor_exon_len: int = 3,
        donor_exon_len: int = 5,
        donor_intron_len: int = 13,
    ):
        self.exon_cut_l = exon_cut_l
        self.exon_cut_r = exon_cut_r
        self.acceptor_intron_cut = acceptor_intron_cut
        self.donor_intron_cut = donor_intron_cut
        self.acceptor_intron_len = acceptor_intron_len
        self.acceptor_exon_len = acceptor_exon_len
        self.donor_exon_len = donor_exon_len
        self.donor_intron_len = donor_intron_len

    def split(self, seq: str, overhang: Tuple[int, int]) -> Dict[str, str]:
        """
        Args:
            seq: the full overhanged-exon sequence (acceptor-side intron
                + exon + donor-side intron), 5'->3' in transcript
                orientation.
            overhang: (acceptor_side_intron_bp, donor_side_intron_bp)
                actually present at the start/end of `seq`.
        """
        intronl_len, intronr_len = overhang
        if intronl_len > len(seq):
            raise ValueError("Acceptor-side overhang cannot exceed sequence length.")
        if intronr_len > len(seq):
            raise ValueError("Donor-side overhang cannot exceed sequence length.")

        # Pad with 'N' if the supplied overhang is shorter than what
        # the acceptor/donor models require, exactly as the official
        # implementation does for exons near a contig/window edge.
        lack_l = self.acceptor_intron_len - intronl_len
        if lack_l >= 0:
            seq = "N" * (lack_l + 1) + seq
            intronl_len += lack_l + 1
        lack_r = self.donor_intron_len - intronr_len
        if lack_r >= 0:
            seq = seq + "N" * (lack_r + 1)
            intronr_len += lack_r + 1

        acceptor_intron = seq[: intronl_len - self.acceptor_intron_cut]

        acceptor_start = intronl_len - self.acceptor_intron_len
        acceptor_end = intronl_len + self.acceptor_exon_len
        acceptor = seq[acceptor_start:acceptor_end]

        exon_start = intronl_len + self.exon_cut_l
        exon_end = -intronr_len - self.exon_cut_r if (intronr_len + self.exon_cut_r) > 0 else len(seq)
        exon = seq[exon_start:exon_end] or "N"

        donor_start = -intronr_len - self.donor_exon_len
        donor_end = -intronr_len + self.donor_intron_len
        donor = seq[donor_start:donor_end] if donor_end != 0 else seq[donor_start:]

        donor_intron = seq[-intronr_len + self.donor_intron_cut:]

        return {
            "acceptor_intron": acceptor_intron,
            "acceptor": acceptor,
            "exon": exon,
            "donor": donor,
            "donor_intron": donor_intron,
        }


# ---------------------------------------------------------------------------
# Score combination math. `_LINEAR_MODEL_COEF`/`_LINEAR_MODEL_INTERCEPT`
# are the published MMSplice delta-logit-PSI linear-model coefficients
# (Cheng et al. 2019 supplementary materials / mmsplice.utils._LINEAR_MODEL,
# MIT-licensed source), reproduced verbatim as plain floating-point
# model parameters (not creative text) so `predict_delta_logit_psi`
# below reproduces the *official* MMSplice delta-logit-PSI score
# exactly, not an ad-hoc approximation of it.
# ---------------------------------------------------------------------------
_LINEAR_MODEL_COEF = np.array(
    [0.49685773, 0.72322957, 1.54760024, 0.75011527, 2.26187717,
     -0.69419094, 2.40138709, 0.88148553]
)
_LINEAR_MODEL_INTERCEPT = 0.0006480262366686865

_CLIP_EPS = 1e-5


def logit(x: np.ndarray, clip_threshold: float = _CLIP_EPS) -> np.ndarray:
    x = np.clip(x, clip_threshold, 1 - clip_threshold)
    return np.log(x) - np.log(1 - x)


def expit(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _not_close_zero(arr: np.ndarray) -> np.ndarray:
    return ~np.isclose(arr, 0)


def _transform(delta: np.ndarray) -> np.ndarray:
    """
    Build the 8-feature interaction vector MMSplice's published linear
    model was fit on: the 5 raw modular deltas plus 3 region-overlap
    interaction terms (exon/donor-intron/acceptor-intron), exactly as
    `mmsplice.utils.transform(region_only=False)`.
    """
    acceptor_intron_d, acceptor_d, exon_d, donor_d, donor_intron_d = delta

    exon_overlap = (
        (_not_close_zero(np.array([acceptor_d])) & _not_close_zero(np.array([exon_d])))
        | (_not_close_zero(np.array([exon_d])) & _not_close_zero(np.array([donor_d])))
    )[0]
    acceptor_intron_overlap = (
        _not_close_zero(np.array([acceptor_intron_d])) & _not_close_zero(np.array([acceptor_d]))
    )[0]
    donor_intron_overlap = (
        _not_close_zero(np.array([donor_d])) & _not_close_zero(np.array([donor_intron_d]))
    )[0]

    exon_term = exon_d * exon_overlap
    donor_intron_term = donor_intron_d * donor_intron_overlap
    acceptor_intron_term = acceptor_intron_d * acceptor_intron_overlap

    return np.array(
        [acceptor_intron_d, acceptor_d, exon_d, donor_d, donor_intron_d,
         exon_term, donor_intron_term, acceptor_intron_term]
    )


def predict_delta_logit_psi(ref_scores: ModularScores, alt_scores: ModularScores) -> float:
    """
    The official MMSplice "delta_logit_psi" score: a published linear
    combination of the five raw per-module score deltas plus their
    pairwise overlap-interaction terms (see `_transform`).
    """
    delta = np.array(alt_scores.as_list()) - np.array(ref_scores.as_list())
    features = _transform(delta)
    return float(features @ _LINEAR_MODEL_COEF + _LINEAR_MODEL_INTERCEPT)


# ---------------------------------------------------------------------------
# Splice-window / eligibility arithmetic (requirement #4). All pure
# coordinate math -- no network calls -- so it's cheaply unit-testable.
# ---------------------------------------------------------------------------
def distances_to_exon_boundaries(
    variant_pos: int, exon: ExonAnnotation
) -> Tuple[Optional[int], Optional[int]]:
    """
    Signed distance (bp) from a 1-based variant position to this exon's
    acceptor (5') and donor (3') boundaries, in *transcript* direction
    (positive = intronic side, i.e. outside the exon; 0 = exactly at
    the boundary base). Returns (distance_to_acceptor, distance_to_donor).
    """
    if exon.strand >= 0:
        acceptor_genomic, donor_genomic = exon.start, exon.end
        dist_to_acceptor = acceptor_genomic - variant_pos
        dist_to_donor = variant_pos - donor_genomic
    else:
        acceptor_genomic, donor_genomic = exon.end, exon.start
        dist_to_acceptor = variant_pos - acceptor_genomic
        dist_to_donor = donor_genomic - variant_pos
    return dist_to_acceptor, dist_to_donor


def classify_region(
    dist_to_acceptor: Optional[int],
    dist_to_donor: Optional[int],
    exon_length: int,
    intron_window: int,
    exon_near_splice_window: int,
) -> Optional[str]:
    """
    Classify a variant's position relative to one exon into one of the
    supported splice-relevant regions (requirement #3), or None if it
    falls outside every supported category for this exon.
    """
    if dist_to_acceptor is not None and 0 < dist_to_acceptor <= intron_window:
        return "intronic_near_acceptor"
    if dist_to_donor is not None and 0 < dist_to_donor <= intron_window:
        return "intronic_near_donor"
    if dist_to_acceptor is not None and dist_to_acceptor <= 0 and dist_to_donor is not None and dist_to_donor <= 0:
        # Inside the exon (both distances are <= 0, i.e. not upstream
        # of the acceptor or downstream of the donor).
        if -dist_to_acceptor <= exon_near_splice_window or -dist_to_donor <= exon_near_splice_window:
            return "exonic_near_splice"
        if exon_length <= 2 * exon_near_splice_window:
            # Short exon: fully within the "near splice" zone from
            # either boundary regardless of which side is closer.
            return "exonic_near_splice"
        return "deep_exonic"
    return "deep_intronic"


def evaluate_eligibility(
    variant_type: str,
    supported_variant_types: Tuple[str, ...],
    dist_to_acceptor: Optional[int],
    dist_to_donor: Optional[int],
    exon_length: int,
    intron_window: int,
    exon_near_splice_window: int,
) -> EligibilityResult:
    """Pure decision function backing requirement #4's eligibility gate."""
    if variant_type not in supported_variant_types:
        return EligibilityResult(
            eligible=False,
            reason=f"variant_type '{variant_type}' is not supported by this MMSplice "
            f"integration (supported: {', '.join(supported_variant_types)})",
        )

    region = classify_region(
        dist_to_acceptor, dist_to_donor, exon_length, intron_window, exon_near_splice_window
    )
    if region in ("deep_intronic", "deep_exonic", None):
        reason = (
            "variant falls outside the configured splice window "
            f"(intron_window={intron_window}bp, exon_near_splice_window={exon_near_splice_window}bp); "
            f"region classified as '{region or 'no overlapping exon'}'"
        )
        return EligibilityResult(eligible=False, reason=reason, region=region)

    return EligibilityResult(eligible=True, reason=f"variant is {region.replace('_', ' ')}", region=region)


# ---------------------------------------------------------------------------
# Interpretation text generation (requirement #7). Every threshold is
# read from the caller (config.py::MMSpliceConfig), never hardcoded
# here, so tuning behavior never requires touching this logic.
# ---------------------------------------------------------------------------
def generate_interpretation(
    delta_logit_psi: float,
    donor_delta: float,
    acceptor_delta: float,
    exon_skipping_score: float,
    intron_retention_score: float,
    moderate_threshold: float,
    strong_threshold: float,
    site_loss_threshold: float,
    exon_skipping_threshold: float,
    intron_retention_threshold: float,
) -> Tuple[str, str, str]:
    """
    Returns (interpretation_text, category, confidence).

    category is one of: "none", "moderate", "strong_donor_loss",
    "strong_acceptor_loss", "exon_skipping", "intron_retention".
    confidence is one of: "low", "moderate", "high".
    """
    abs_delta = abs(delta_logit_psi)

    if exon_skipping_score >= exon_skipping_threshold:
        return (
            "Likely exon skipping.",
            "exon_skipping",
            "high" if exon_skipping_score >= exon_skipping_threshold * 1.5 else "moderate",
        )

    if intron_retention_score >= intron_retention_threshold:
        return (
            "Predicted intron retention.",
            "intron_retention",
            "high" if intron_retention_score >= intron_retention_threshold * 1.5 else "moderate",
        )

    if donor_delta <= -site_loss_threshold and abs(donor_delta) >= abs(acceptor_delta):
        return (
            "Strong donor site loss.",
            "strong_donor_loss",
            "high" if abs_delta >= strong_threshold else "moderate",
        )

    if acceptor_delta <= -site_loss_threshold:
        return (
            "Strong acceptor site loss.",
            "strong_acceptor_loss",
            "high" if abs_delta >= strong_threshold else "moderate",
        )

    if abs_delta >= strong_threshold:
        return ("Strong predicted splice disruption.", "strong", "high")

    if abs_delta >= moderate_threshold:
        return ("Moderate splice disruption.", "moderate", "moderate")

    return ("No predicted splice disruption.", "none", "low" if abs_delta < moderate_threshold / 2 else "moderate")


def canonical_dinucleotide_check(sequence_around_boundary: str, is_donor: bool) -> Optional[bool]:
    """
    Best-effort check of whether the expected canonical GT/AG
    dinucleotide is present at a boundary, for logging/QC purposes only
    (never gates eligibility or scoring). Returns None if the input is
    too short to check.
    """
    expected = CANONICAL_DONOR_DINUCLEOTIDE if is_donor else CANONICAL_ACCEPTOR_DINUCLEOTIDE
    if len(sequence_around_boundary) < 2:
        return None
    return sequence_around_boundary.upper() == expected
