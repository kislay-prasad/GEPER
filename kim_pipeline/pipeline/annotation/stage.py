"""
pipeline/annotation/stage.py
──────────────────────────────
AnnotationStage — filtered VCF → annotated variant list (Task 4 + 5).

Phase 1 fixes applied:
  FIX 1.1 — Real functional consequence annotation using RnaTranscriptAnalyser.
             CodonContextProvider interface added (placeholder; SNV codon-context
             requires a CDS FASTA source — see KNOWN LIMITATION below).
  FIX 1.3 — ZygosityExtractor wired in; richer fields (ab, gq, phase_set,
             hemizygous) surfaced on AnnotatedVariant alongside existing fields.

KNOWN LIMITATION (FIX 1.1):
  Distinguishing missense / synonymous / stop-gained for an exonic SNV requires
  reading the reference codon from a CDS FASTA.  Until a CDS FASTA source is
  configured, exonic SNVs are annotated as "coding_sequence_variant".  Supply a
  concrete CodonContextProvider implementation to lift this limitation.
"""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .gff_index import GffIndex
from pipeline.zygosity.extractor import ZygosityExtractor

logger = logging.getLogger("geper.pipeline.annotation.stage")


# ─── CodonContextProvider interface ──────────────────────────────────────────
# A pluggable interface for resolving the codon change of an exonic SNV.
# Without a CDS FASTA source, the only concrete implementation raises
# NotImplementedError for SNVs.  Indel frame-shift and splice detection do
# NOT need this provider and are fully implemented below.


class CodonContextProvider(ABC):
    """Abstract provider: given a variant position, return codon-level context."""

    @abstractmethod
    def get_codon_change(
        self,
        chrom: str,
        pos: int,
        ref: str,
        alt: str,
        transcript_id: str,
    ) -> Optional[str]:
        """Return one of 'missense', 'synonymous', 'stop_gained', 'stop_lost',
        'start_lost', or None if the change cannot be determined.

        Raises:
            NotImplementedError: if the concrete class has no CDS data source.
        """


class NoCdsCodonContextProvider(CodonContextProvider):
    """Placeholder: raises NotImplementedError — no CDS FASTA configured.

    KNOWN LIMITATION: to resolve missense/synonymous/stop calls for SNVs,
    replace this with a CDS-FASTA-backed implementation.
    """

    def get_codon_change(
        self,
        chrom: str,
        pos: int,
        ref: str,
        alt: str,
        transcript_id: str,
    ) -> Optional[str]:
        raise NotImplementedError(
            "CDS FASTA not configured — cannot resolve codon change for SNV at "
            f"{chrom}:{pos} {ref}>{alt}. "
            "Set a concrete CodonContextProvider to enable missense/synonymous annotation."
        )


# ─── Consequence mapping ──────────────────────────────────────────────────────

# Explicit not-determined sentinel — distinct from any real SO consequence
# term, so a downstream reader (rendering, ACMG evidence logic) can tell
# "we tried and could not resolve a codon-level effect" apart from an
# actual classification. Never returned by `_map_consequence` for the MNV/
# complex case (situation 3): that path always has a real, if coarse,
# answer (`coding_sequence_variant`) and giving it this sentinel would
# misrepresent a real answer as a failure to answer.
CONSEQUENCE_NOT_DETERMINED = "consequence_not_determined"


def _map_consequence(
    region: str,
    ref: str,
    alt: str,
    codon_provider: Optional[CodonContextProvider],
    chrom: str,
    pos: int,
    transcript_id: str,
) -> str:
    """Map a classify_region() result + variant shape to a VEP-style SO term.

    Indel frameshift detection and splice donor/acceptor detection are fully
    implemented.  Exonic SNV codon-level consequences require a CDS data source
    (see CodonContextProvider); absent that, they return "coding_sequence_variant".

    Args:
        region:        Output of RnaTranscriptAnalyser.classify_region().
        ref, alt:      Reference and alternate allele strings.
        codon_provider: Optional provider for codon-level context.
        chrom, pos, transcript_id: Passed through to the codon provider.

    Returns:
        A Sequence Ontology consequence string.
    """
    is_snv = len(ref) == 1 and len(alt) == 1
    is_indel = len(ref) != len(alt)
    length_diff = abs(len(ref) - len(alt))
    is_frameshift = is_indel and (length_diff % 3 != 0)
    is_inframe = is_indel and (length_diff % 3 == 0)

    if region in ("splice_donor", "splice_donor_variant"):
        return "splice_donor_variant"
    if region in ("splice_acceptor", "splice_acceptor_variant"):
        return "splice_acceptor_variant"
    if region == "intronic":
        return "intron_variant"
    if region == "intergenic":
        return "intergenic_variant"
    # FIX 4: proper UTR consequence terms (not intergenic)
    if region == "utr5":
        return "5_prime_UTR_variant"
    if region == "utr3":
        return "3_prime_UTR_variant"

    # region == "exonic" (coding)
    if is_frameshift:
        return "frameshift_variant"
    if is_inframe:
        if len(alt) > len(ref):
            return "inframe_insertion"
        return "inframe_deletion"

    # Exonic SNV — try codon context provider
    if is_snv and codon_provider is not None:
        try:
            codon_change = codon_provider.get_codon_change(chrom, pos, ref, alt, transcript_id)
            if codon_change == "missense":
                return "missense_variant"
            if codon_change == "synonymous":
                return "synonymous_variant"
            if codon_change == "stop_gained":
                return "stop_gained"
            if codon_change == "stop_lost":
                return "stop_lost"
            if codon_change == "start_lost":
                return "start_lost"
        except NotImplementedError:
            pass  # No CDS data — fall through to coding_sequence_variant
        except Exception as exc:
            logger.debug("CodonContextProvider failed for %s:%d: %s", chrom, pos, exc)

    if is_snv:
        # No codon context available, or this variant's codon could not be
        # determined despite a configured provider -- an explicit
        # not-determined sentinel, never a guessed classification.
        return CONSEQUENCE_NOT_DETERMINED

    # Multi-nucleotide variant / complex: a real, if coarse, classification
    # -- keep the honest SO term. Giving this the sentinel would misrepresent
    # a real answer as a failure to answer (situation 3, confirmed by the
    # human: keeps coding_sequence_variant).
    return "coding_sequence_variant"


# ─── Zygosity helpers (Task 5) — preserved for backward-compat exports ────────


def _parse_zygosity(gt: str) -> str:
    """Convert a VCF GT string to a human-readable zygosity label.

    Examples::
        "0/1"  → "Heterozygous"
        "1/1"  → "Homozygous_alt"
        "0/0"  → "Homozygous_ref"
        "./."  → "No_call"
        "0/1/2" → "Multi_allelic"

    NOTE: preserved for backward compatibility with test imports.
    The annotation stage now uses ZygosityExtractor internally.
    """
    alleles = [a for a in re.split(r"[/|]", gt) if a != "."]
    if not alleles:
        return "No_call"
    unique = set(alleles)
    if len(unique) == 1:
        if "0" in unique:
            return "Homozygous_ref"
        return "Homozygous_alt"
    if len(unique) == 2 and "0" in unique:
        return "Heterozygous"
    return "Multi_allelic"


def _parse_format_fields(format_str: str, sample_str: str) -> Dict[str, str]:
    """Return a dict of FORMAT key → sample value."""
    keys = format_str.split(":")
    vals = sample_str.split(":")
    return dict(zip(keys, vals))


def _extract_genotype(format_str: str, sample_str: str) -> Dict[str, Optional[str]]:
    """Extract GT, AD, DP, Genotype, and Zygosity from a VCF record.

    NOTE: preserved for backward compatibility with test imports.
    The annotation stage now uses ZygosityExtractor internally.
    """
    fields = _parse_format_fields(format_str, sample_str)
    gt = fields.get("GT", "./.")
    ad = fields.get("AD")
    dp = fields.get("DP")

    zygosity = _parse_zygosity(gt)
    genotype = gt

    return {
        "GT": gt,
        "AD": ad,
        "DP": dp,
        "Genotype": genotype,
        "Zygosity": zygosity,
    }


# ─── HGVS notation ───────────────────────────────────────────────────────────

_COMPLEMENT = {
    "A": "T",
    "T": "A",
    "G": "C",
    "C": "G",
    "N": "N",
    "a": "t",
    "t": "a",
    "g": "c",
    "c": "g",
    "n": "n",
}


def _revcomp(seq: str) -> str:
    return "".join(_COMPLEMENT.get(b, "N") for b in reversed(seq))


def _build_hgvs(
    chrom: str,
    pos: int,
    ref: str,
    alt: str,
    transcript_id: Optional[str],
    cds_pos: Optional[int] = None,
    end_cds_pos: Optional[int] = None,
    strand: Optional[str] = None,
) -> str:
    """Generate HGVS notation for a variant.

    ISSUE 1 FIX: ``c.``/``n.`` notation is only emitted when a verified
    CDS- or transcript-relative coordinate (``cds_pos``) has been supplied
    by the caller (see RnaTranscriptAnalyser.genomic_to_cds_pos /
    genomic_to_transcript_pos). Without it, this function has no way to
    know the real coding-relative position, so it MUST fall back to
    genomic (g.) notation — pairing a c./n. prefix with the raw genomic
    position is invalid HGVS and is never produced.

    Args:
        cds_pos: 1-based CDS-relative (for c.) or transcript-relative
            (for n.) coordinate of *pos*, already computed by the caller.
            None means "could not be determined" → fall back to g.
        end_cds_pos: same, for the last base of a multi-base ref (indels).
            Only used on the plus strand, where genomic and transcript
            coordinates increase in the same direction so the arithmetic
            is unambiguous; minus-strand indels fall back to g. notation.
        strand: "+" or "-". On a minus-strand transcript, c./n. notation
            describes the variant on the transcript (sense) strand, so
            ref/alt must be reverse-complemented relative to the genomic
            (reference-strand) ref/alt passed in.
    """
    is_snv = len(ref) == 1 and len(alt) == 1
    is_insertion = len(ref) == 1 and len(alt) > 1
    minus = strand == "-"

    use_transcript_coords = (
        transcript_id is not None
        and cds_pos is not None
        and (is_snv or (not minus and (is_insertion or end_cds_pos is not None)))
    )

    if use_transcript_coords:
        tid_upper = transcript_id.upper()
        coord_prefix = "n." if tid_upper.startswith(("NR_", "XR_")) else "c."
        ref_prefix = f"{transcript_id}:{coord_prefix}"
        disp_ref = _revcomp(ref) if minus else ref
        disp_alt = _revcomp(alt) if minus else alt
        use_pos = cds_pos
        end_pos = end_cds_pos
    else:
        # No verified transcript-relative coordinate — safe genomic
        # fallback. NEVER pair a c./n. prefix with a raw genomic position.
        ref_prefix = f"{chrom}:g."
        disp_ref, disp_alt = ref, alt
        use_pos = pos
        end_pos = pos + len(ref) - 1 if len(ref) > 1 else None

    if len(disp_ref) == 1 and len(disp_alt) == 1:
        return f"{ref_prefix}{use_pos}{disp_ref}>{disp_alt}"

    if len(ref) == 1 and len(alt) > 1:
        inserted = disp_alt[1:]
        return f"{ref_prefix}{use_pos}_{use_pos + 1}ins{inserted}"

    if len(ref) > 1 and len(alt) == 1:
        del_start = use_pos + 1
        del_end = end_pos if end_pos is not None else use_pos + len(ref) - 1
        if del_start == del_end:
            return f"{ref_prefix}{del_start}del"
        return f"{ref_prefix}{del_start}_{del_end}del"

    del_end = end_pos if end_pos is not None else use_pos + len(ref) - 1
    return f"{ref_prefix}{use_pos}_{del_end}delins{disp_alt}"


# ─── Annotated variant record ─────────────────────────────────────────────────


@dataclass
class AnnotatedVariant:
    """A single annotated variant from the filtered VCF."""

    chrom: str = ""
    pos: int = 0
    ref: str = ""
    alt: str = ""
    qual: Optional[float] = None
    filter_field: str = ""
    # Annotation fields (Task 4)
    gene_name: Optional[str] = None
    transcript_id: Optional[str] = None
    hgvs: str = ""
    is_inframe_indel: bool = False
    # FIX 1.1: functional consequence (SO term)
    consequence: str = ""
    # Genotype fields (Task 5) — original fields preserved
    gt: Optional[str] = None
    ad: Optional[str] = None
    dp: Optional[str] = None
    genotype: Optional[str] = None
    zygosity: Optional[str] = None
    # FIX 1.3: richer zygosity fields from ZygosityExtractor
    gq: Optional[int] = None
    ab: Optional[float] = None
    phase_set: Optional[str] = None
    hemizygous: bool = False
    # Raw INFO
    info: str = ""
    # FIX 13: VEP HGVS — propagated from VEP annotation when available.
    # These take precedence over the locally-generated hgvs field.
    vep_hgvs_c: str = ""  # VEP HGVSc (e.g. NM_000059.4:c.5266dup)
    vep_hgvs_p: str = ""  # VEP HGVSp (e.g. NP_000050.3:p.Gln1756fs)
    # Annotation scores — populated from INFO field when present
    cadd_phred: Optional[float] = None
    revel_score: Optional[float] = None
    spliceai_score: Optional[float] = None
    alphamissense_score: Optional[float] = None
    # AI engine inputs — populated by codon provider when possible
    ref_sequence: Optional[str] = None  # DNA context window (ref allele centred)
    alt_sequence: Optional[str] = None  # DNA context window (alt allele centred)
    wildtype_aa: Optional[str] = None  # Wild-type amino-acid sequence
    mutant_aa: Optional[str] = None  # Mutant amino-acid sequence
    # PS1/PM5 codon-level ClinVar evidence — populated in orchestration stage 4b
    same_aa_pathogenic: Optional[bool] = None
    novel_aa_at_known_pathogenic_codon: Optional[bool] = None
    # Repeat-region annotation — populated from annotation INFO or BED lookup
    in_repeat_region: Optional[bool] = None

    def to_dict(self) -> Dict:
        return asdict(self)


# ─── LoF / in-frame indel helpers ─────────────────────────────────────────────


def _compute_is_inframe_indel(ref: str, alt: str, is_lof: bool = False) -> bool:
    """Determine whether a variant is an in-frame insertion/deletion.

    NOTE: This helper is used before consequence annotation is available
    (during VCF parsing).  Once consequence is resolved, is_inframe_indel is
    re-derived from the consequence value in AnnotationStage.run().
    """
    if is_lof:
        return False
    is_indel = len(ref) != len(alt)
    if not is_indel:
        return False
    length_diff = abs(len(ref) - len(alt))
    return length_diff % 3 == 0


# ─── Consequence → is_inframe_indel re-derivation ────────────────────────────


def _consequence_to_is_inframe_indel(consequence: str) -> bool:
    """Re-derive is_inframe_indel from a resolved consequence SO term.

    FIX 1.1: is_inframe_indel must match the consequence annotation so that
    PM4 in classifier.py always uses a consistent value.
    """
    return consequence in {"inframe_insertion", "inframe_deletion"}


# ─── VCF parser ───────────────────────────────────────────────────────────────


def _parse_csq_header(line: str) -> List[str]:
    """Extract CSQ field names from a VEP ##INFO=<ID=CSQ,...> header line.

    FIX 1: VEP embeds scores in the CSQ tag, not as standalone INFO keys.
    This function parses the Format descriptor so they can be extracted.
    """
    import re as _re

    m = _re.search(r'Format:\s*([^"]+)', line)
    if not m:
        return []
    fmt_part = m.group(1).strip().rstrip('">').strip()
    return [f.strip() for f in fmt_part.split("|")] if fmt_part else []


def _extract_csq_scores(
    info: str,
    csq_fields: List[str],
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Extract (cadd_phred, revel, spliceai_max, am_pathogenicity) from VEP CSQ INFO.

    The `spliceai_max` position always returns `None` now (2026-08-22):
    SpliceAI parsing was removed for licence reasons (Illumina's
    pretrained models are CC BY-NC 4.0, the plugin is no longer
    requested -- see `pipeline/vep/stage.py`). The tuple shape is kept
    unchanged so callers don't need to change their unpacking.

    FIX 1: Parses the CSQ tag to retrieve pre-computed scores that VEP
    embeds inside the structured CSQ annotation rather than as plain INFO keys.
    Uses the most-severe consequence entry (lowest severity rank).
    Returns (None, None, None, None) when CSQ is absent or has no scores.
    """

    def _f(v: str) -> Optional[float]:
        try:
            return float(v) if v and v not in (".", "") else None
        except ValueError:
            return None

    csq_raw = ""
    for part in info.split(";"):
        if part.startswith("CSQ="):
            csq_raw = part[4:]
            break
    if not csq_raw or not csq_fields:
        return None, None, None, None

    best_rank = len(_SEVERITY_ORDER) + 1
    best_cadd: Optional[float] = None
    best_revel: Optional[float] = None
    best_spliceai: Optional[float] = None
    best_am: Optional[float] = None

    for entry_str in csq_raw.split(","):
        values = entry_str.split("|")
        entry: Dict[str, str] = {}
        for i, val in enumerate(values):
            if i < len(csq_fields):
                entry[csq_fields[i]] = val

        consequences = entry.get("Consequence", "").split("&")
        rank = min(
            (_SEVERITY_RANK.get(c, len(_SEVERITY_ORDER)) for c in consequences),
            default=len(_SEVERITY_ORDER),
        )
        if rank < best_rank:
            best_rank = rank
            best_cadd = _f(entry.get("CADD_PHRED", ""))
            best_revel = _f(entry.get("REVEL", ""))
            # SpliceAI parsing REMOVED (licence, same class as OMIM --
            # Illumina's plugin is no longer requested by
            # pipeline/vep/stage.py, so this field is never present in
            # VEP's CSQ output). best_spliceai stays None; kept in the
            # return tuple's shape so callers don't need to change their
            # unpacking.
            best_am = _f(entry.get("AM_PATHOGENICITY", "") or entry.get("am_pathogenicity", ""))

    return best_cadd, best_revel, best_spliceai, best_am


# Severity ranking needed by _extract_csq_scores above — import from top level
_SEVERITY_ORDER = [
    "transcript_ablation",
    "splice_acceptor_variant",
    "splice_donor_variant",
    "stop_gained",
    "frameshift_variant",
    "stop_lost",
    "start_lost",
    "transcript_amplification",
    "inframe_insertion",
    "inframe_deletion",
    "missense_variant",
    "protein_altering_variant",
    "splice_region_variant",
    "incomplete_terminal_codon_variant",
    "start_retained_variant",
    "stop_retained_variant",
    "synonymous_variant",
    "coding_sequence_variant",
    "mature_miRNA_variant",
    "5_prime_UTR_variant",
    "3_prime_UTR_variant",
    "non_coding_transcript_exon_variant",
    "intron_variant",
    "NMD_transcript_variant",
    "non_coding_transcript_variant",
    "upstream_gene_variant",
    "downstream_gene_variant",
    "TFBS_ablation",
    "TFBS_amplification",
    "TF_binding_site_variant",
    "regulatory_region_ablation",
    "regulatory_region_amplification",
    "feature_elongation",
    "regulatory_region_variant",
    "feature_truncation",
    "intergenic_variant",
]
_SEVERITY_RANK = {c: i for i, c in enumerate(_SEVERITY_ORDER)}


def _extract_csq_hgvs(
    info: str,
    csq_fields: List[str],
) -> Tuple[str, str]:
    """FIX 13: Extract HGVSc and HGVSp from the VEP CSQ INFO tag.

    Returns (hgvs_c, hgvs_p) strings from the most-severe CSQ entry.
    Returns ("", "") when CSQ is absent or has no HGVS fields.
    Never generates NM_xxx:g. — VEP always emits valid c./p. notation.
    """
    csq_raw = ""
    for part in info.split(";"):
        if part.startswith("CSQ="):
            csq_raw = part[4:]
            break
    if not csq_raw or not csq_fields:
        return "", ""

    best_rank = len(_SEVERITY_ORDER) + 1
    best_hgvs_c = ""
    best_hgvs_p = ""

    for entry_str in csq_raw.split(","):
        values = entry_str.split("|")
        entry: Dict[str, str] = {
            csq_fields[i]: values[i] for i in range(min(len(csq_fields), len(values)))
        }
        consequences = entry.get("Consequence", "").split("&")
        rank = min(
            (_SEVERITY_RANK.get(c, len(_SEVERITY_ORDER)) for c in consequences),
            default=len(_SEVERITY_ORDER),
        )
        if rank < best_rank:
            best_rank = rank
            hc = entry.get("HGVSc", "") or ""
            hp = entry.get("HGVSp", "") or ""
            # Filter out invalid g. notations on transcript IDs
            if (
                hc
                and ":g." in hc
                and any(hc.upper().startswith(p) for p in ("NM_", "XM_", "NR_", "XR_", "ENST"))
            ):
                hc = ""  # invalid — VEP should not emit NM_:g. but guard anyway
            best_hgvs_c = hc
            best_hgvs_p = hp

    return best_hgvs_c, best_hgvs_p


def _iter_vcf(vcf_path: str, skipped: Optional[List[Dict]] = None):
    """Stream-parse a plain or gzip-compressed VCF file, yielding one
    AnnotatedVariant per ALT allele (multi-allelic records expanded).

    This is a generator (FIX: large-VCF scalability) — records are produced
    one at a time instead of being collected into a single in-memory list,
    so callers that consume it incrementally (e.g. writing annotation
    output as a streaming JSON array) do not hold the whole VCF's worth of
    AnnotatedVariant objects in RAM simultaneously.

    FIX 1: CSQ header is tracked so VEP-embedded scores can be extracted.
    FORMAT and SAMPLE columns are parsed for genotype information when present.
    Uses ZygosityExtractor for richer genotype parsing (FIX 1.3).

    FIX (Issue 5): symbolic spanning-deletion placeholder alleles
    (ALT == "*", per the VCF spec — used by some callers/gVCF-derived
    VCFs to represent "this position overlaps a deletion called at an
    earlier record") carry no ref/alt sequence of their own and must
    never be annotated or sent into ACMG classification. They are
    filtered out here and, if *skipped* is provided, recorded with a
    reason so callers (AnnotationStage.run) can surface the skip count
    and reason in the annotation summary / report rather than silently
    dropping them.
    """
    if not Path(vcf_path).exists():
        raise FileNotFoundError(f"VCF not found: {vcf_path!r}")

    import gzip as _gzip

    open_fn = _gzip.open if vcf_path.endswith(".gz") else open

    csq_fields: List[str] = []  # FIX 1: populated from ##INFO=<ID=CSQ header

    with open_fn(vcf_path, "rt") as fh:  # type: ignore[call-overload]
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("##"):
                # FIX 1: capture CSQ format from VEP header
                if "ID=CSQ" in line:
                    csq_fields = _parse_csq_header(line)
                continue
            if line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 5:
                continue

            chrom = cols[0]
            pos_s = cols[1]
            ref = cols[3]
            alt_field = cols[4]

            # FIX 12: expand multi-allelic records, one AnnotatedVariant per ALT
            alt_alleles = alt_field.split(",") if "," in alt_field else [alt_field]

            qual_s = cols[5] if len(cols) > 5 else "."
            filt = cols[6] if len(cols) > 6 else "."
            info = cols[7] if len(cols) > 7 else "."
            format_s = cols[8] if len(cols) > 8 else ""
            sample_s = cols[9] if len(cols) > 9 else ""

            try:
                pos = int(pos_s)
            except ValueError:
                continue

            try:
                qual = float(qual_s) if qual_s not in (".", "") else None
            except ValueError:
                qual = None

            # FIX 1: extract VEP-embedded scores from CSQ tag once per record
            csq_cadd, csq_revel, csq_spliceai, csq_am = (
                _extract_csq_scores(info, csq_fields) if csq_fields else (None, None, None, None)
            )
            # FIX 13: extract VEP HGVSc and HGVSp from CSQ tag
            vep_hgvs_c, vep_hgvs_p = _extract_csq_hgvs(info, csq_fields) if csq_fields else ("", "")

            for allele_idx, alt in enumerate(alt_alleles):
                alt = alt.strip()
                if not alt or alt == ".":
                    continue

                # FIX (Issue 5): filter symbolic ALT=* spanning-deletion
                # placeholder alleles. These are not real variants — they
                # mark "this position falls inside a deletion reported at
                # an earlier VCF record" and have no ref/alt sequence to
                # classify. Never annotate or classify them.
                if alt == "*":
                    logger.info(
                        "Skipped symbolic spanning-deletion placeholder "
                        "(ALT=*) at %s:%s — not a classifiable variant; "
                        "the real deletion is reported at an earlier VCF record.",
                        chrom,
                        pos_s,
                    )
                    if skipped is not None:
                        skipped.append(
                            {
                                "chrom": chrom,
                                "pos": pos,
                                "ref": ref,
                                "alt": alt,
                                "reason": (
                                    "Symbolic VCF placeholder (ALT=*) — spanning "
                                    "deletion overlap marker, not a classifiable variant"
                                ),
                            }
                        )
                    continue

                is_inframe_indel = _compute_is_inframe_indel(ref, alt)

                var = AnnotatedVariant(
                    chrom=chrom,
                    pos=pos,
                    ref=ref,
                    alt=alt,
                    qual=qual,
                    filter_field=filt,
                    info=info,
                    is_inframe_indel=is_inframe_indel,
                    vep_hgvs_c=vep_hgvs_c,
                    vep_hgvs_p=vep_hgvs_p,
                )

                # FIX 1: populate scores from CSQ (prefer CSQ over plain INFO)
                var.cadd_phred = (
                    csq_cadd if csq_cadd is not None else _parse_info_float(info, "CADD_PHRED")
                )
                var.revel_score = (
                    csq_revel if csq_revel is not None else _parse_info_float(info, "REVEL")
                )
                var.spliceai_score = csq_spliceai
                if var.spliceai_score is None:
                    val = _parse_info_float(info, "SpliceAI")
                    if val is None:
                        ds_ag = _parse_info_float(info, "DS_AG")
                        ds_al = _parse_info_float(info, "DS_AL")
                        ds_dg = _parse_info_float(info, "DS_DG")
                        ds_dl = _parse_info_float(info, "DS_DL")
                        sc = [x for x in [ds_ag, ds_al, ds_dg, ds_dl] if x is not None]
                        val = max(sc) if sc else None
                    var.spliceai_score = val
                var.alphamissense_score = (
                    csq_am if csq_am is not None else _parse_info_float(info, "AM_PATHOGENICITY")
                )

                if format_s and sample_s:
                    fmt_keys = format_s.split(":")
                    fmt_vals = sample_s.split(":")
                    gt_raw = dict(zip(fmt_keys, fmt_vals)).get("GT", "./.")

                    # FIX 1.3: Use ZygosityExtractor for richer extraction
                    try:
                        zy_result = ZygosityExtractor.extract(
                            gt_raw, fmt_keys, fmt_vals, alt_index=allele_idx + 1
                        )
                        _zy_map = {
                            "heterozygous": "Heterozygous",
                            "homozygous_alt": "Homozygous_alt",
                            "homozygous_ref": "Homozygous_ref",
                            "no_call": "No_call",
                            "hemizygous": "Hemizygous",
                            "multi_allelic": "Multi_allelic",
                            "unknown": "Unknown",
                        }
                        var.gt = zy_result.gt
                        var.ad = (
                            ",".join(str(x) for x in zy_result.ad)
                            if zy_result.ad is not None
                            else None
                        )
                        var.dp = str(zy_result.dp) if zy_result.dp is not None else None
                        var.genotype = zy_result.gt
                        var.zygosity = _zy_map.get(zy_result.zygosity, zy_result.zygosity.title())
                        var.gq = zy_result.gq
                        var.ab = zy_result.ab
                        var.phase_set = zy_result.phase_set
                        var.hemizygous = zy_result.zygosity == "hemizygous"
                    except Exception as exc:
                        logger.warning(
                            "ZygosityExtractor failed for %s:%s: %s — falling back",
                            chrom,
                            pos_s,
                            exc,
                        )
                        geno = _extract_genotype(format_s, sample_s)
                        var.gt = geno["GT"]
                        var.ad = geno["AD"]
                        var.dp = geno["DP"]
                        var.genotype = geno["Genotype"]
                        var.zygosity = geno["Zygosity"]

                yield var


def _parse_vcf(vcf_path: str, skipped: Optional[List[Dict]] = None) -> List[AnnotatedVariant]:
    """List-returning compatibility wrapper around _iter_vcf().

    Existing callers (and tests) that need the full list still work
    unchanged; AnnotationStage.run() now uses _iter_vcf() directly to
    stream-process records for the large-VCF scalability fix.
    """
    return list(_iter_vcf(vcf_path, skipped=skipped))


def _parse_info_float(info: str, key: str) -> Optional[float]:
    """Extract a float value from a VCF INFO string by key.

    Handles both ``KEY=VALUE`` and flag-style (bare key) fields.
    Returns None if key absent or value is not numeric.
    """
    for part in info.split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            if k.strip() == key:
                try:
                    return float(v.strip().split(",")[0])
                except (ValueError, IndexError):
                    return None
    return None


# ─── Stage result ─────────────────────────────────────────────────────────────


@dataclass
class AnnotationResult:
    sample_id: str = ""
    annotated_vcf_path: str = ""
    annotation_json_path: str = ""
    total_variants: int = 0
    annotated_count: int = 0
    unannotated_count: int = 0
    gff3_source: str = ""
    elapsed_seconds: float = 0.0
    variants: List[AnnotatedVariant] = field(default_factory=list)
    # FIX (Issue 5): symbolic ALT=* spanning-deletion placeholders that were
    # filtered out before annotation/ACMG. Each entry documents why the
    # record was skipped, so reports never silently drop variants.
    skipped_symbolic: List[Dict] = field(default_factory=list)
    # Run-level fact for the not-determined sentinel: whether a real
    # FastaCodonContextProvider was available for this run (vs. the
    # NoCdsCodonContextProvider placeholder). When False, every exonic SNV
    # in `variants` was annotated CONSEQUENCE_NOT_DETERMINED rather than a
    # specific molecular effect -- a run-level disclosure, not a per-variant
    # one, since it applies uniformly to every such variant in this run.
    codon_resolution_available: bool = True

    def to_dict(self) -> Dict:
        return {
            "sample_id": self.sample_id,
            "annotated_vcf_path": self.annotated_vcf_path,
            "annotation_json_path": self.annotation_json_path,
            "total_variants": self.total_variants,
            "annotated_count": self.annotated_count,
            "unannotated_count": self.unannotated_count,
            "gff3_source": self.gff3_source,
            "elapsed_seconds": self.elapsed_seconds,
            "skipped_symbolic_count": len(self.skipped_symbolic),
            "skipped_symbolic": self.skipped_symbolic,
            "codon_resolution_available": self.codon_resolution_available,
            "variants": [v.to_dict() for v in self.variants],
        }


# ─── Stage ────────────────────────────────────────────────────────────────────


def _write_annotation_json_streaming(path: str, result: "AnnotationResult") -> None:
    """Write annotation.json incrementally (FIX: large-VCF scalability).

    Produces the same JSON structure as
    ``json.dumps(result.to_dict(), indent=2)`` — same keys, same
    "variants" array of the same per-variant dicts — but never builds one
    giant string holding every variant's serialized JSON simultaneously.
    Peak memory is ~one variant dict at a time instead of the whole file.
    """
    header = {
        "sample_id": result.sample_id,
        "annotated_vcf_path": result.annotated_vcf_path,
        "annotation_json_path": result.annotation_json_path,
        "total_variants": result.total_variants,
        "annotated_count": result.annotated_count,
        "unannotated_count": result.unannotated_count,
        "gff3_source": result.gff3_source,
        "elapsed_seconds": result.elapsed_seconds,
        "skipped_symbolic_count": len(result.skipped_symbolic),
        "skipped_symbolic": result.skipped_symbolic,
        "codon_resolution_available": result.codon_resolution_available,
    }
    with open(path, "w") as fh:
        fh.write("{\n")
        for key, value in header.items():
            fh.write(f"  {json.dumps(key)}: {json.dumps(value)},\n")
        fh.write('  "variants": [\n')
        n = len(result.variants)
        for i, var in enumerate(result.variants):
            line = json.dumps(var.to_dict(), indent=2)
            # re-indent the per-variant block by 4 spaces to match the
            # nested array formatting json.dumps(..., indent=2) would use
            indented = "\n".join("    " + ln for ln in line.split("\n"))
            fh.write(indented)
            fh.write(",\n" if i < n - 1 else "\n")
        fh.write("  ]\n")
        fh.write("}\n")


class AnnotationStage:
    """
    Annotate PASS-filtered variants with gene, transcript, HGVS, genotype,
    and functional consequence information.

    FIX 1.1: Consequence annotation via RnaTranscriptAnalyser.classify_region().
    FIX 1.3: Zygosity extracted via ZygosityExtractor (hemizygous-aware).

    Configuration (under cfg["annotation"]):
        refseq_gff   Path to GFF3 annotation file.
        require_gff  bool, default True.
    """

    def __init__(self, cfg: Optional[Dict] = None) -> None:
        self._cfg = (cfg or {}).get("annotation", {}) or {}
        rna_cfg = (cfg or {}).get("rna_analysis", {}) or {}
        self._raw_cfg = cfg or {}
        self._gff_path: Optional[str] = (
            self._cfg.get("refseq_gff") or rna_cfg.get("refseq_gff") or None
        )
        # Provenance of `self._gff_path`: "configured" if the caller set
        # rna_analysis.refseq_gff / annotation.refseq_gff explicitly;
        # resolved lazily to "mt_bootstrap" or "unavailable" the first
        # time `_get_gff_index()` runs (see mt_gff3_bootstrap.py) if no
        # explicit path was given.
        self._gff_provenance: str = "configured" if self._gff_path else "unresolved"
        self._require_gff: bool = bool(self._cfg.get("require_gff", False))
        self._gff_index: Optional[GffIndex] = None

        # FIX 1.1: RnaTranscriptAnalyser for classify_region()
        self._rna_analyser = None
        # Phase 2: use FastaCodonContextProvider when reference_fasta is configured;
        # otherwise fall back to NoCdsCodonContextProvider (no-op placeholder).
        try:
            from pipeline.annotation.codon_provider import make_codon_provider_from_cfg

            self._codon_provider: CodonContextProvider = make_codon_provider_from_cfg(cfg)
        except Exception as _cp_exc:
            logger.debug("CodonContextProvider init failed, using placeholder: %s", _cp_exc)
            self._codon_provider = NoCdsCodonContextProvider()

    def _get_gff_index(self) -> Optional[GffIndex]:
        """Load (once) the GFF3 index.  Fails loudly if required but missing."""
        if self._gff_index is not None:
            return self._gff_index

        if not self._gff_path:
            # No explicit rna_analysis.refseq_gff / annotation.refseq_gff —
            # try the opt-in, mitochondrial-only auto-fetch bootstrap
            # before giving up. Disabled by default; see
            # mt_gff3_bootstrap.py for scope/limits. This never makes a
            # network call unless the caller explicitly opted in via
            # config or GEPER_AUTO_FETCH_MT_GFF3=1, so existing
            # offline/default-config behavior is unchanged.
            from pipeline.annotation.mt_gff3_bootstrap import resolve_gff3_source

            bootstrap_path, provenance = resolve_gff3_source(self._raw_cfg)
            self._gff_provenance = provenance
            if bootstrap_path:
                self._gff_path = bootstrap_path
                logger.info(
                    "Using auto-fetched mitochondrial-only GFF3 bootstrap annotation "
                    "('%s'). This covers MT/chrM variants only — set "
                    "'rna_analysis.refseq_gff' to a full GRCh38 GFF3 for nuclear-genome "
                    "gene/transcript/HGVS annotation.",
                    bootstrap_path,
                )
            elif self._require_gff:
                raise FileNotFoundError(
                    "Annotation stage requires a GFF3 annotation file but none is "
                    "configured.\nSet 'rna_analysis.refseq_gff' in your config YAML "
                    "to the path of a RefSeq or Ensembl GFF3 file.\n\n"
                    "Download RefSeq GRCh38 GFF3:\n"
                    "  wget https://ftp.ncbi.nlm.nih.gov/refseq/H_sapiens/annotation/"
                    "GRCh38_latest/refseq_identifiers/GRCh38_latest_genomic.gff.gz\n"
                    "  # then set: rna_analysis.refseq_gff: /path/to/file.gff.gz"
                )
            else:
                logger.warning(
                    "No GFF3 annotation configured (rna_analysis.refseq_gff is empty). "
                    "Running in DEGRADED mode: gene/transcript/HGVS-c annotation will be "
                    "skipped and variants will only carry genomic (g.) coordinates. "
                    "Set 'rna_analysis.refseq_gff' to a RefSeq/Ensembl GFF3 file for full "
                    "annotation, set 'rna_analysis.auto_fetch_mt_gff3: true' for MT-only "
                    "variants, or set 'annotation.require_gff: true' to make this a hard error."
                )
                return None

        self._gff_index = GffIndex.from_file(self._gff_path)
        return self._gff_index

    @property
    def gff_provenance(self) -> str:
        """ "configured" | "mt_bootstrap" | "unavailable" | "unresolved" (before first use)."""
        return self._gff_provenance

    def _get_rna_analyser(self):
        """Lazy-load RnaTranscriptAnalyser using same GFF3 path as GffIndex."""
        if self._rna_analyser is not None:
            return self._rna_analyser
        if not self._gff_path:
            return None
        try:
            from pipeline.rna.transcript import RnaTranscriptAnalyser

            # RnaTranscriptAnalyser reads cfg["rna_analysis"]["refseq_gff"]
            rna_cfg = {"rna_analysis": {"refseq_gff": self._gff_path}}
            self._rna_analyser = RnaTranscriptAnalyser(cfg=rna_cfg)
        except Exception as exc:
            logger.warning("Could not initialise RnaTranscriptAnalyser: %s", exc)
            self._rna_analyser = None
        return self._rna_analyser

    def run(
        self,
        filtered_vcf_path: str,
        output_dir: str,
        sample_id: str = "SAMPLE",
    ) -> AnnotationResult:
        """Annotate all PASS variants in filtered_vcf_path.

        Returns AnnotationResult containing annotated variant list and paths.
        """
        t0 = time.time()
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        annotation_json = str(out / "annotation.json")

        gff_index = self._get_gff_index()
        rna_analyser = self._get_rna_analyser()

        logger.info(
            "[%s] AnnotationStage: vcf=%s gff=%s",
            sample_id,
            filtered_vcf_path,
            self._gff_path or "<none>",
        )

        # FIX (Issue 5): collects symbolic ALT=* records filtered by _iter_vcf
        skipped_symbolic: List[Dict] = []
        variants_iter = _iter_vcf(filtered_vcf_path, skipped=skipped_symbolic)

        annotated = 0
        unannotated = 0
        variants: List[AnnotatedVariant] = []

        for var in variants_iter:
            if gff_index is not None:
                gene, transcript = gff_index.lookup(var.chrom, var.pos)
            else:
                gene, transcript = None, None

            var.gene_name = gene
            var.transcript_id = transcript
            # FIX 13: use VEP HGVSc when available (already validated by VEP).
            # Fall back to locally-generated HGVS only when VEP has not run.
            if var.vep_hgvs_c:
                var.hgvs = var.vep_hgvs_c
            else:
                # ISSUE 1 FIX: compute a verified CDS-/transcript-relative
                # coordinate before allowing _build_hgvs to emit c./n.
                # notation; without one it always falls back to genomic
                # (g.) notation, which is never invalid HGVS.
                cds_pos = end_cds_pos = tx_strand = None
                if rna_analyser is not None and transcript:
                    tx_rec = rna_analyser.get_transcript(transcript)
                    tx_strand = tx_rec.strand if tx_rec else None
                    tid_upper = transcript.upper()
                    if tid_upper.startswith(("NR_", "XR_")):
                        cds_pos = rna_analyser.genomic_to_transcript_pos(
                            var.chrom, var.pos, transcript
                        )
                        if len(var.ref) > 1:
                            end_cds_pos = rna_analyser.genomic_to_transcript_pos(
                                var.chrom, var.pos + len(var.ref) - 1, transcript
                            )
                    else:
                        cds_pos = rna_analyser.genomic_to_cds_pos(var.chrom, var.pos, transcript)
                        if len(var.ref) > 1:
                            end_cds_pos = rna_analyser.genomic_to_cds_pos(
                                var.chrom, var.pos + len(var.ref) - 1, transcript
                            )
                var.hgvs = _build_hgvs(
                    var.chrom,
                    var.pos,
                    var.ref,
                    var.alt,
                    transcript,
                    cds_pos=cds_pos,
                    end_cds_pos=end_cds_pos,
                    strand=tx_strand,
                )

            # FIX 1.1: Determine functional consequence
            tx_known_to_rna_analyser = (
                rna_analyser is not None
                and transcript
                and rna_analyser.get_transcript(transcript) is not None
            )
            if tx_known_to_rna_analyser:
                region = rna_analyser.classify_region(var.chrom, var.pos, transcript)
                var.consequence = _map_consequence(
                    region=region,
                    ref=var.ref,
                    alt=var.alt,
                    codon_provider=self._codon_provider,
                    chrom=var.chrom,
                    pos=var.pos,
                    transcript_id=transcript,
                )
                # Re-derive is_inframe_indel from consequence (FIX 1.1)
                var.is_inframe_indel = _consequence_to_is_inframe_indel(var.consequence)
            elif gene is None:
                # No transcript resolved → intergenic
                var.consequence = "intergenic_variant"
            elif gene:
                # GffIndex resolved a gene (and possibly a transcript-like
                # feature ID — e.g. a tRNA/rRNA/CDS record from a
                # mitochondrial-style GFF3), but RnaTranscriptAnalyser's
                # exon/CDS model only understands mRNA/transcript records,
                # so it cannot classify this position relative to that
                # feature. Calling it "intergenic_variant" here would be
                # actively wrong (it IS inside a real, named gene) — use an
                # honest, non-committal term instead of a misleading one.
                var.consequence = "gene_region_variant"
            # else: gff_index=None and gene=None → consequence stays ""

            if gene:
                annotated += 1
            else:
                unannotated += 1

            # ── Annotation scores from INFO ───────────────────────────────
            if var.info and var.info != ".":
                if var.cadd_phred is None:
                    var.cadd_phred = _parse_info_float(var.info, "CADD_PHRED")
                if var.revel_score is None:
                    var.revel_score = _parse_info_float(var.info, "REVEL")
                if var.spliceai_score is None:
                    val = _parse_info_float(var.info, "SpliceAI")
                    if val is None:
                        val = _parse_info_float(var.info, "DS_AG")
                        ds_al = _parse_info_float(var.info, "DS_AL")
                        ds_dg = _parse_info_float(var.info, "DS_DG")
                        ds_dl = _parse_info_float(var.info, "DS_DL")
                        scores = [x for x in [val, ds_al, ds_dg, ds_dl] if x is not None]
                        val = max(scores) if scores else None
                    var.spliceai_score = val
                if var.alphamissense_score is None:
                    var.alphamissense_score = _parse_info_float(var.info, "AM_PATHOGENICITY")
                # RepeatMasker flag in INFO (e.g. from VEP REPEAT_REGION=1)
                if var.in_repeat_region is None:
                    rm = _parse_info_float(var.info, "REPEAT_REGION")
                    if rm is not None:
                        var.in_repeat_region = bool(rm > 0)

            # ── AI engine sequence inputs from codon provider ─────────────
            if (
                var.consequence == "missense_variant"
                and hasattr(self._codon_provider, "_fasta")
                and hasattr(self._codon_provider, "_cds_map")
                and getattr(self._codon_provider, "_available", False)
                and var.transcript_id
            ):
                try:
                    consequence, ref_codon, alt_codon, ref_aa, alt_aa = (
                        self._codon_provider.get_codon_and_aa(
                            var.chrom, var.pos, var.ref, var.alt, var.transcript_id
                        )
                    )
                    if ref_aa and alt_aa:
                        var.wildtype_aa = ref_aa
                        var.mutant_aa = alt_aa
                except Exception:
                    pass  # best-effort only

            # DNA context windows for DNABERT-2 (50 bp flanks)
            if (
                var.ref_sequence is None
                and hasattr(self._codon_provider, "_fasta")
                and getattr(self._codon_provider, "_available", False)
            ):
                try:
                    fasta = self._codon_provider._fasta  # type: ignore[attr-defined]
                    flank = 50
                    start = max(1, var.pos - flank)
                    end = var.pos + flank
                    ref_ctx = fasta.fetch(var.chrom, start, end)
                    if ref_ctx and len(ref_ctx) > 0:
                        # Replace centre base with alt to build alt context
                        centre = var.pos - start  # 0-based index in the fetched string
                        if 0 <= centre < len(ref_ctx):
                            alt_ctx = ref_ctx[:centre] + var.alt + ref_ctx[centre + len(var.ref) :]
                            var.ref_sequence = ref_ctx
                            var.alt_sequence = alt_ctx
                except Exception:
                    pass

            variants.append(var)

        result = AnnotationResult(
            sample_id=sample_id,
            annotated_vcf_path=filtered_vcf_path,
            annotation_json_path=annotation_json,
            total_variants=len(variants),
            annotated_count=annotated,
            unannotated_count=unannotated,
            gff3_source=self._gff_path or "",
            elapsed_seconds=round(time.time() - t0, 3),
            variants=variants,
            skipped_symbolic=skipped_symbolic,
            codon_resolution_available=bool(getattr(self._codon_provider, "_available", False)),
        )
        _write_annotation_json_streaming(annotation_json, result)

        logger.info(
            "[%s] Annotation complete: %d/%d variants gene-resolved in %.2fs "
            "(%d symbolic ALT=* record(s) skipped)",
            sample_id,
            annotated,
            len(variants),
            result.elapsed_seconds,
            len(skipped_symbolic),
        )
        return result
