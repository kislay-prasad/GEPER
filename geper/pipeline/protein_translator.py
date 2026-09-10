"""
Protein sequence translation.

Translates an RNA coding sequence into a protein (amino acid) sequence
using the standard genetic code, for input into ESM-2. Translation
starts at the first in-frame ATG/AUG (standard start codon) found in
the window and proceeds until a stop codon or the end of the sequence,
which is the correct behavior for a short flanking window that may not
begin exactly at a transcript's annotated CDS start.
"""

from dataclasses import dataclass
from typing import Optional

from config import CONFIG
from pipeline.rna_generator import RNAContext
from utils.exceptions import SequenceGenerationError
from utils.logger import get_logger

logger = get_logger(__name__)

_START_CODON = "AUG"
_STOP_SYMBOL = "*"


@dataclass
class ProteinContext:
    ref_protein: Optional[str]
    alt_protein: Optional[str]
    ref_orf_found: bool
    alt_orf_found: bool


class ProteinTranslator:
    """Translates RNA sequences to protein using the standard codon table."""

    def __init__(self):
        self.codon_table = CONFIG.CODON_TABLE

    def translate_context(self, rna_context: RNAContext) -> ProteinContext:
        ref_protein, ref_found = self._translate_orf(rna_context.ref_rna)
        alt_protein, alt_found = self._translate_orf(rna_context.alt_rna)
        return ProteinContext(
            ref_protein=ref_protein,
            alt_protein=alt_protein,
            ref_orf_found=ref_found,
            alt_orf_found=alt_found,
        )

    def _translate_orf(self, rna_sequence: str) -> "tuple[Optional[str], bool]":
        """Find the first start codon and translate until a stop codon / sequence end."""
        start_index = rna_sequence.find(_START_CODON)
        if start_index == -1:
            logger.debug("No start codon (AUG) found in RNA window; skipping translation.")
            return None, False

        protein_residues = []
        for i in range(start_index, len(rna_sequence) - 2, 3):
            codon = rna_sequence[i : i + 3]
            amino_acid = self.codon_table.get(codon)
            if amino_acid is None:
                # Ambiguous base (N) or malformed codon at window edge.
                logger.debug(f"Unrecognized codon '{codon}' encountered; stopping translation.")
                break
            if amino_acid == _STOP_SYMBOL:
                protein_residues.append(_STOP_SYMBOL)
                break
            protein_residues.append(amino_acid)

        if not protein_residues:
            return None, False
        return "".join(protein_residues), True

    def translate_single(self, rna_sequence: str) -> str:
        """Translate a single RNA sequence starting at its first base (frame 0)."""
        if len(rna_sequence) % 3 != 0:
            logger.debug("RNA sequence length is not a multiple of 3; trailing bases ignored.")
        residues = []
        for i in range(0, len(rna_sequence) - 2, 3):
            codon = rna_sequence[i : i + 3]
            amino_acid = self.codon_table.get(codon, "X")
            residues.append(amino_acid)
        if not residues:
            raise SequenceGenerationError("RNA sequence too short to translate a single codon.")
        return "".join(residues)
