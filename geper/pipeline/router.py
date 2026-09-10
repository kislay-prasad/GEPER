"""
Sequence router.

Decides which DNA foundation model(s) should analyze a given variant's
sequence context. A variant may be routed to more than one model when
it satisfies multiple criteria (e.g. a long AND structurally complex
region) -- the orchestrator runs every model the router returns and
merges their outputs.

Routing rules (see config.RoutingConfig for the underlying thresholds):
    - length >= HYENADNA_MIN_LEN                       -> HyenaDNA
    - variant_type in COMPLEX_CONTEXT_VARIANT_TYPES     -> Evo 2
    - otherwise (typical SNV/indel, short window)       -> HyenaDNA
      (universal default; DNABERT-2, the former default here, has
      been removed from GEPER entirely -- see LICENSE_AUDIT.md)
"""

from typing import Any, Dict, List, Optional

from config import CONFIG
from pipeline.pvs1.utils import protein_effect_flags, transcript_from_result
from pipeline.sequence_context import SequenceContext
from pipeline.vcf_parser import Variant
from utils.exceptions import RoutingError
from utils.logger import get_logger

logger = get_logger(__name__)

HYENADNA = "hyenadna"
EVO2 = "evo2"


class SequenceRouter:
    """Intelligently selects which DNA model(s) should process a variant."""

    def route(self, variant: Variant, context: SequenceContext) -> List[str]:
        """Return an ordered list of model registry keys to run for this variant."""
        selected: List[str] = []
        routing_cfg = CONFIG.routing

        is_long = context.length >= routing_cfg.HYENADNA_MIN_LEN
        is_complex = self._is_complex_context(variant)

        if is_long:
            selected.append(HYENADNA)
        if is_complex:
            selected.append(EVO2)
        if not selected:
            # Universal default for the typical short/standard SNV or
            # indel window: HyenaDNA has no fixed context-length wall
            # (unlike a transformer-attention model), so it is a safe
            # default for both short and long windows. This replaces
            # DNABERT-2, which used to fill this role -- DNABERT-2 has
            # been removed from GEPER entirely (see LICENSE_AUDIT.md).
            selected.append(HYENADNA)

        if not selected:
            # Defensive fallback -- should be unreachable given the
            # logic above, but routing must never return an empty list.
            raise RoutingError(f"Router failed to select any model for variant {variant.chrom}:{variant.pos}.")

        # Preserve first-seen order while de-duplicating.
        deduped = list(dict.fromkeys(selected))
        logger.info(
            f"Routed {variant.chrom}:{variant.pos} ({variant.variant_type}, "
            f"context length {context.length}) -> {deduped}"
        )
        return deduped

    @staticmethod
    def _is_complex_context(variant: Variant) -> bool:
        """
        Flags a variant as needing Evo 2's multi-species context. Uses
        VCF INFO annotations when present (e.g. a prior annotation
        step tagging repeat regions or splice proximity), falling back
        to the variant_type itself for structural calls.
        """
        info_flags = {k.lower() for k in variant.info.keys()}
        complex_types = set(CONFIG.routing.COMPLEX_CONTEXT_VARIANT_TYPES)

        if variant.variant_type == "MNV" and len(variant.ref) >= 20:
            return True
        if info_flags & complex_types:
            return True
        if variant.info.get("SVTYPE"):
            return True
        return False

    @staticmethod
    def recommended_flank_size(variant: Variant) -> int:
        """
        Suggest a flank size for SequenceContextGenerator before routing
        is even known -- structural-variant-tagged records get the long
        HyenaDNA-scale window up front so we don't have to re-fetch.
        """
        if variant.info.get("SVTYPE"):
            return CONFIG.routing.LONG_RANGE_FLANK_SIZE
        return CONFIG.routing.DEFAULT_FLANK_SIZE

    def requires_rna_analysis(self, variant: Variant) -> bool:
        """
        Decide whether RNA-FM should run for this variant. Runs when the
        VCF annotates the variant as exonic/transcribed/splice-relevant,
        or as a conservative default for SNVs/indels where transcript
        impact hasn't been pre-annotated (RNA-FM is cheap relative to
        the DNA models, so default-on avoids missing real signal).
        """
        info = {k.upper(): v for k, v in variant.info.items()}
        region_tag = info.get("REGION", "").lower()
        if region_tag in ("exonic", "splicing", "utr3", "utr5", "ncrna_exonic"):
            return True
        if "GENEINFO" in info or "GENE" in info:
            return True
        # Default: allow RNA-level analysis for point mutations/small
        # indels, since these most commonly fall in or near transcripts.
        return variant.variant_type in ("SNV", "insertion", "deletion")

    def requires_protein_analysis(self, variant: Variant) -> bool:
        """
        Decide whether translation + ESM-2 should run. Runs when the
        VCF marks the variant as coding (CDS/exonic + coding
        consequence), or defaults to True for SNVs so missense/nonsense
        effects are always considered unless explicitly ruled non-coding.
        """
        info = {k.upper(): v for k, v in variant.info.items()}
        consequence = info.get("CONSEQUENCE", info.get("ANN", "")).lower()
        non_coding_markers = ("intron", "intergenic", "utr", "upstream", "downstream")
        if any(marker in consequence for marker in non_coding_markers):
            return False
        return True

    def is_missense_eligible(self, variant: Variant, transcript_result: Optional[Dict[str, Any]]) -> bool:
        """
        Decide whether AlphaMissense should run for this variant.
        AlphaMissense scores exactly one thing: a single amino-acid
        substitution caused by a genomic missense change. This
        deliberately excludes every other consequence class rather
        than trying to make AlphaMissense degrade gracefully on input
        it was never designed for.

        Consequence is classified via `pipeline/pvs1/utils.py::
        protein_effect_flags` -- the same transcript-CDS-frame machinery
        BP7/BP1 use -- NOT `pipeline/protein_translator.py`'s local
        window translation (flat +/-500bp, no splicing, no reverse-
        complement for minus-strand transcripts). That window was
        already shown to call frame essentially at random for any
        variant more than a few dozen bases from whatever AUG it
        happens to find in the window, which made this gate a coin
        flip rather than a real missense/synonymous call. See
        `ProteinEffectFlags`'s docstring for the full history.

        Excludes:
          - non-SNV VCF records (insertion/deletion/MNV) -- covers
            frameshift-causing indels and multi-base substitutions.
          - symbolic or structural ALT alleles (e.g. '<DEL>', '*') or
            an `SVTYPE`-tagged record -- never a simple substitution.
          - anything `protein_effect_flags` could not classify from
            real transcript/CDS data (`determined=False`): no
            transcript structure fetched, no CDS sequence, a
            build/transcript mismatch at this codon, or a multi-
            nucleotide same-length substitution. Skipping is the safe
            default here -- see this method's caller in
            `pipeline/orchestrator.py::_run_alphamissense_stage` for
            why running AlphaMissense on an unclassified consequence
            (rather than skipping) would risk scoring a synonymous,
            nonsense, or frameshift change as if it were a clean
            missense substitution.
          - synonymous, nonsense/stop-loss, and frameshift/in-frame-
            indel calls -- AlphaMissense's catalogue is keyed on
            exactly one missense substitution per entry.
        """
        if variant.variant_type != "SNV":
            return False
        if variant.info.get("SVTYPE"):
            return False
        if variant.alt.startswith("<") or variant.alt in (".", "*") or len(variant.alt) != 1:
            return False

        transcript = transcript_from_result(transcript_result)
        flags = protein_effect_flags(variant.to_dict(), transcript)
        if not flags.determined:
            return False
        return flags.is_missense
