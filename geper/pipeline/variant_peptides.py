"""
Variant-aware reference/alternate peptides for ESM-2.

`pipeline/protein_translator.py` translates a flat +/-500 bp genomic window from
the first AUG it finds. It has no transcript, exon or strand awareness, so for a
variant that is not inside that arbitrary open reading frame the "reference" and
"alternate" peptides are identical and unrelated to the variant (Run 5: BRCA1
17:43093449 G>T, p.Ser694Arg, got the same 24-residue peptide on both sides).

This module builds the peptides from the transcript's verified coding sequence
instead, reusing `pipeline/pvs1/utils.py::coding_consequence_detail` for the
genomic-to-CDS mapping (exon splicing and strand handling, including the
complementing of REF/ALT on minus-strand transcripts) and the REF-allele
cross-check. It **fails closed**: whenever a variant-specific peptide cannot be
trusted it returns a `PeptideSkip` with a structured reason code, and the caller
must not embed any peptide.

Pure functions only: no network, no model loading.
"""

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Union

from config import CONFIG
from pipeline.pvs1.models import TranscriptContext
from pipeline.pvs1.utils import (
    CONSEQUENCE_MISSENSE,
    CONSEQUENCE_NONSENSE,
    CONSEQUENCE_STOP_LOST,
    CONSEQUENCE_SYNONYMOUS,
    coding_consequence_detail,
)

# ---------------------------------------------------------------------------
# Structured skip reason codes
# ---------------------------------------------------------------------------
SKIP_NO_TRANSCRIPT = "no_trusted_transcript"
SKIP_NO_CDS = "no_cds_sequence"
SKIP_NOT_SNV = "not_single_nucleotide_substitution"
SKIP_CDS_NOT_MULTIPLE_OF_3 = "cds_length_not_multiple_of_3"
SKIP_CDS_INVALID_BASES = "cds_invalid_bases"
SKIP_CDS_NO_START = "cds_missing_start_codon"
SKIP_CDS_NO_STOP = "cds_missing_stop_codon"
SKIP_CDS_INTERNAL_STOP = "cds_internal_stop"
SKIP_CDS_LENGTH_DISAGREES = "cds_length_disagrees_with_exons"
SKIP_PROTEIN_LENGTH_DISAGREES = "cds_protein_length_mismatch"
SKIP_NOT_IN_CDS = "variant_not_in_cds"
SKIP_REF_MISMATCH = "ref_allele_mismatch"
SKIP_UNDETERMINED = "consequence_undetermined"
SKIP_RESIDUE_DISAGREES = "translated_residue_disagrees"
SKIP_STOP_LOST = "stop_lost_extension_unavailable"
SKIP_START_ALTERED = "start_codon_altered"
SKIP_EMPTY_ALT = "empty_alternate_peptide"
SKIP_INVARIANT = "peptide_invariant_failed"
SKIP_PROTEIN_UNCHANGED = "protein_unchanged"  # synonymous: peptides recorded, ESM-2 deliberately not run
SKIP_CONSTRUCTION_ERROR = "peptide_construction_error"
SKIP_BAD_LIMIT = "invalid_residue_limit"

_COMPLEMENT = {"A": "T", "C": "G", "G": "C", "T": "A"}

# ESM-2 adds <cls> and <eos>; the residue budget is the token limit minus these.
ESM2_SPECIAL_TOKENS = 2


@dataclass(frozen=True)
class PeptideSkip:
    """The variant-specific peptide could not be trusted; ESM-2 must not run."""

    code: str
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class VariantPeptides:
    """Reference/alternate peptide windows around the variant's own codon."""

    transcript_id: str
    gene_symbol: Optional[str]
    codon_number: int
    ref_aa: str
    alt_aa: str  # "*" for a stop-gain
    category: str  # synonymous | missense | nonsense
    ref_peptide: str  # window, no stop symbol
    alt_peptide: str  # window, no stop symbol (truncated before the stop for nonsense)
    window_start: int  # 1-based protein residue number of the window's first residue
    window_end: int  # 1-based protein residue number of the window's last REFERENCE residue
    variant_index: int  # 0-based offset of the variant codon within the window
    protein_length: int  # residues in the full reference protein, excluding the stop
    residue_limit: int  # the ESM-2 residue budget this window was fitted to

    @property
    def protein_changed(self) -> bool:
        return self.ref_peptide != self.alt_peptide

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["protein_changed"] = self.protein_changed
        return out


def _translate_cds(cds: str) -> List[str]:
    table = CONFIG.CODON_TABLE  # RNA-keyed
    return [table.get(cds[i : i + 3].replace("T", "U"), "?") for i in range(0, len(cds), 3)]


def _validate_cds(transcript: TranscriptContext, cds: str) -> Union[List[str], PeptideSkip]:
    """Translate the whole CDS in frame 0 and verify it is a complete, clean reading frame."""
    if len(cds) % 3 != 0:
        return PeptideSkip(SKIP_CDS_NOT_MULTIPLE_OF_3, f"CDS length {len(cds)} is not a multiple of 3.")
    if set(cds) - set("ACGT"):
        return PeptideSkip(SKIP_CDS_INVALID_BASES, "CDS contains bases other than A/C/G/T.")
    residues = _translate_cds(cds)
    if "?" in residues:
        return PeptideSkip(SKIP_CDS_INVALID_BASES, "CDS contains a codon outside the standard table.")
    if cds[:3] != "ATG":
        return PeptideSkip(SKIP_CDS_NO_START, "CDS does not begin with an ATG start codon.")
    if residues[-1] != "*":
        return PeptideSkip(SKIP_CDS_NO_STOP, "CDS does not end with a stop codon.")
    if "*" in residues[:-1]:
        return PeptideSkip(SKIP_CDS_INTERNAL_STOP, "CDS contains an in-frame stop before its final codon.")
    if transcript.cds_length != len(cds):
        return PeptideSkip(
            SKIP_CDS_LENGTH_DISAGREES,
            f"CDS sequence length {len(cds)} disagrees with the exon/CDS arithmetic ({transcript.cds_length}).",
        )
    protein_length = len(residues) - 1
    if transcript.protein_length is not None and int(transcript.protein_length) != protein_length:
        return PeptideSkip(
            SKIP_PROTEIN_LENGTH_DISAGREES,
            f"Translated protein is {protein_length} residues but the transcript annotates {transcript.protein_length}.",
        )
    return residues


def _window(codon_number: int, protein_length: int, limit: int) -> "tuple[int, int]":
    """1-based inclusive [start, end] of at most `limit` residues containing `codon_number`."""
    if protein_length <= limit:
        return 1, protein_length
    half = (limit - 1) // 2
    start = max(1, codon_number - half)
    end = start + limit - 1
    if end > protein_length:
        end = protein_length
        start = max(1, end - limit + 1)
    return start, end


def build_variant_peptides(
    transcript: Optional[TranscriptContext],
    pos: int,
    ref: str,
    alt: str,
    max_residues: int,
) -> Union[VariantPeptides, PeptideSkip]:
    """
    Reference and alternate peptide windows for a single-nucleotide substitution,
    taken from the transcript's own coding sequence, or a `PeptideSkip`.

    `max_residues` is the number of amino acids ESM-2 can take (its token limit
    minus the two special tokens). A protein longer than that is cut to a window
    centred on the variant codon, so the variant is never truncated away.
    """
    if max_residues is None or max_residues < 3:
        return PeptideSkip(SKIP_BAD_LIMIT, f"Residue limit {max_residues!r} is too small for a variant-centred window.")
    if transcript is None:
        return PeptideSkip(SKIP_NO_TRANSCRIPT, "No trusted transcript structure was resolved for this variant.")
    if not transcript.cds_sequence:
        return PeptideSkip(SKIP_NO_CDS, f"No CDS sequence is available for transcript {transcript.transcript_id}.")
    ref_u, alt_u = (ref or "").upper(), (alt or "").upper()
    if len(ref_u) != 1 or len(alt_u) != 1 or ref_u not in "ACGT" or alt_u not in "ACGT" or ref_u == alt_u:
        return PeptideSkip(
            SKIP_NOT_SNV, "Only single-nucleotide substitutions (A/C/G/T) have a variant-specific peptide here."
        )

    cds = transcript.cds_sequence.upper()
    residues = _validate_cds(transcript, cds)
    if isinstance(residues, PeptideSkip):
        return residues
    protein = "".join(residues[:-1])
    protein_length = len(protein)

    cds_pos = transcript.cds_position(int(pos))
    if cds_pos is None:
        return PeptideSkip(
            SKIP_NOT_IN_CDS, f"Position {pos} is not inside the coding sequence of {transcript.transcript_id}."
        )
    expected = cds[cds_pos - 1]
    genomic_expected = _COMPLEMENT.get(expected, expected) if transcript.strand < 0 else expected
    if genomic_expected != ref_u:
        return PeptideSkip(
            SKIP_REF_MISMATCH,
            f"VCF REF {ref_u} disagrees with the transcript base {genomic_expected} at position {pos}; "
            "transcript and variant do not describe the same locus/build.",
        )

    detail = coding_consequence_detail(transcript, int(pos), ref_u, alt_u)
    if detail is None:
        return PeptideSkip(SKIP_UNDETERMINED, "The coding consequence could not be determined from the transcript.")
    codon_number = detail.codon_number
    if codon_number < 1 or codon_number > len(residues) or residues[codon_number - 1] != detail.ref_aa:
        return PeptideSkip(
            SKIP_RESIDUE_DISAGREES,
            f"Translated residue at codon {codon_number} does not match the consequence call ({detail.ref_aa}).",
        )

    category = detail.category
    if category == CONSEQUENCE_STOP_LOST:
        return PeptideSkip(SKIP_STOP_LOST, "Stop-loss: the extended protein lies beyond the CDS sequence available.")
    if codon_number == 1 and detail.alt_aa != "M":
        return PeptideSkip(SKIP_START_ALTERED, "The initiation codon is altered; the protein product is not defined.")
    if category not in (CONSEQUENCE_SYNONYMOUS, CONSEQUENCE_MISSENSE, CONSEQUENCE_NONSENSE):
        return PeptideSkip(SKIP_UNDETERMINED, f"Unsupported consequence category {category!r}.")

    start, end = _window(codon_number, protein_length, max_residues)
    ref_window = protein[start - 1 : end]
    variant_index = codon_number - start

    if category == CONSEQUENCE_NONSENSE:
        alt_window = protein[start - 1 : codon_number - 1]  # ends right before the new stop
    else:
        alt_window = ref_window[:variant_index] + detail.alt_aa + ref_window[variant_index + 1 :]

    if not alt_window:
        return PeptideSkip(SKIP_EMPTY_ALT, "The alternate peptide window is empty.")

    # Defence in depth: the windows must agree with the consequence just derived.
    ok = ref_window[variant_index] == detail.ref_aa and len(ref_window) <= max_residues
    if category == CONSEQUENCE_MISSENSE:
        ok = ok and alt_window[variant_index] == detail.alt_aa and alt_window != ref_window
        ok = ok and alt_window[:variant_index] == ref_window[:variant_index]
        ok = ok and alt_window[variant_index + 1 :] == ref_window[variant_index + 1 :]
    elif category == CONSEQUENCE_SYNONYMOUS:
        ok = ok and alt_window == ref_window
    else:  # nonsense
        ok = ok and alt_window == ref_window[:variant_index] and len(alt_window) == variant_index
    if not ok or "*" in ref_window or "*" in alt_window:
        return PeptideSkip(SKIP_INVARIANT, "The constructed peptide windows failed the internal consistency check.")

    return VariantPeptides(
        transcript_id=transcript.transcript_id,
        gene_symbol=transcript.gene_symbol,
        codon_number=codon_number,
        ref_aa=detail.ref_aa,
        alt_aa=detail.alt_aa,
        category=category,
        ref_peptide=ref_window,
        alt_peptide=alt_window,
        window_start=start,
        window_end=end,
        variant_index=variant_index,
        protein_length=protein_length,
        residue_limit=max_residues,
    )
