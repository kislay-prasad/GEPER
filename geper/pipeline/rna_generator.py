"""
RNA sequence generation.

Produces the RNA-equivalent (coding-strand transcription: DNA T -> RNA
U) of both the reference and alternate DNA sequence contexts, for
input into RNA-FM. GEPER does not perform full splicing simulation
(that requires transcript/exon annotation beyond a bare VCF); it
transcribes the coding-strand window as-is, which is the standard
input RNA-FM expects for local RNA-structure/function embedding.
"""

from dataclasses import dataclass

from pipeline.sequence_context import SequenceContext
from utils.exceptions import SequenceGenerationError
from utils.logger import get_logger

logger = get_logger(__name__)

_VALID_DNA_BASES = set("ACGTN")


@dataclass
class RNAContext:
    ref_rna: str
    alt_rna: str


class RNAGenerator:
    """Transcribes DNA sequence context into RNA sequence context."""

    def generate(self, context: SequenceContext) -> RNAContext:
        ref_rna = self._transcribe(context.ref_sequence)
        alt_rna = self._transcribe(context.alt_sequence)
        return RNAContext(ref_rna=ref_rna, alt_rna=alt_rna)

    @staticmethod
    def _transcribe(dna_sequence: str) -> str:
        sequence = dna_sequence.upper()
        invalid = set(sequence) - _VALID_DNA_BASES
        if invalid:
            raise SequenceGenerationError(
                f"Cannot transcribe DNA sequence containing invalid base(s): "
                f"{sorted(invalid)}."
            )
        return sequence.replace("T", "U")
