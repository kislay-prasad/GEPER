"""
HGVS (Human Genome Variation Society) notation: generation of g./c./p.
strings from a normalized variant, and validation of an HGVS string
(caller-supplied or internally generated) against real reference data.

Nothing in GEPER generated or validated HGVS notation before this
module (confirmed by search: every existing "HGVS" reference in this
codebase -- `pipeline/ps1_pm5/utils.py::parse_protein_change`,
`pipeline/pvs1/utils.py::termination_codon_from_hgvs_p` -- only PARSES
HGVS.p notation already published in a ClinVar record; none of it
generates or validates a string against reference sequence).

Scope, disclosed rather than implied away:
  - g. (genomic) notation: substitution, deletion, insertion, and
    delins are covered -- not every rare HGVS description form (repeat
    expansions, complex rearrangements, uncertain/unknown-length
    variants) is.
  - c. (coding) notation: only exonic, CDS-relative positions are
    generated, reusing `pipeline/pvs1/models.py::TranscriptContext
    .cds_position()` (already-existing, already-tested transcript
    structure) rather than re-deriving exon/CDS mapping. An
    intronic or UTR position returns None (not fabricated) -- see
    `to_hgvs_c`'s docstring.
  - p. (protein) notation: only generated when the transcript's CDS
    sequence is available (`TranscriptContext.cds_sequence`, which its
    own docstring notes is not always populated); reuses
    `CONFIG.CODON_TABLE` (the same standard genetic code
    `pipeline/protein_translator.py` already uses) rather than a new
    codon table, and the 3-letter amino acid names already defined in
    `pipeline/ps1_pm5/utils.py::_THREE_TO_ONE` (inverted), rather than
    a duplicate table.
  - Validation covers RefSeq accession *format* (NC_/NM_/NP_/NG_ prefix
    + version) and a real reference-base cross-check when a fetcher is
    supplied -- it is a syntax + cross-check validator for the common
    description forms above, not a full implementation of the HGVS
    specification (which additionally covers uncertainty notation,
    mosaicism, RNA-level description, and many rarer forms).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from config import CONFIG
from pipeline.ps1_pm5.utils import _THREE_TO_ONE
from pipeline.pvs1.models import TranscriptContext

FetchBase = Callable[[str, int], str]  # (chrom, 1-based genomic pos) -> single reference base

_ONE_TO_THREE: Dict[str, str] = {one: three for three, one in _THREE_TO_ONE.items() if one != "*"}
_ONE_TO_THREE["*"] = "Ter"

_COMPLEMENT = {"A": "T", "T": "A", "C": "G", "G": "C", "N": "N"}

# RefSeq chromosome accessions for GRCh38/GRCh37 (primary assembly,
# chromosomes 1-22 + X/Y/MT). Stable, versioned NCBI identifiers --
# verify against NCBI's own assembly report (GCF_000001405.* for
# GRCh38, GCF_000001405.25 for GRCh37) before relying on this table for
# a build/version this hasn't been checked against.
_REFSEQ_CHROM_ACCESSIONS: Dict[str, Dict[str, str]] = {
    "GRCh38": {
        "1": "NC_000001.11",
        "2": "NC_000002.12",
        "3": "NC_000003.12",
        "4": "NC_000004.12",
        "5": "NC_000005.10",
        "6": "NC_000006.12",
        "7": "NC_000007.14",
        "8": "NC_000008.11",
        "9": "NC_000009.12",
        "10": "NC_000010.11",
        "11": "NC_000011.10",
        "12": "NC_000012.12",
        "13": "NC_000013.11",
        "14": "NC_000014.9",
        "15": "NC_000015.10",
        "16": "NC_000016.10",
        "17": "NC_000017.11",
        "18": "NC_000018.10",
        "19": "NC_000019.10",
        "20": "NC_000020.11",
        "21": "NC_000021.9",
        "22": "NC_000022.11",
        "X": "NC_000023.11",
        "Y": "NC_000024.10",
        "MT": "NC_012920.1",
    },
    "GRCh37": {
        "1": "NC_000001.10",
        "2": "NC_000002.11",
        "3": "NC_000003.11",
        "4": "NC_000004.11",
        "5": "NC_000005.9",
        "6": "NC_000006.11",
        "7": "NC_000007.13",
        "8": "NC_000008.10",
        "9": "NC_000009.11",
        "10": "NC_000010.10",
        "11": "NC_000011.9",
        "12": "NC_000012.11",
        "13": "NC_000013.10",
        "14": "NC_000014.8",
        "15": "NC_000015.9",
        "16": "NC_000016.9",
        "17": "NC_000017.10",
        "18": "NC_000018.9",
        "19": "NC_000019.9",
        "20": "NC_000020.10",
        "21": "NC_000021.8",
        "22": "NC_000022.10",
        "X": "NC_000023.10",
        "Y": "NC_000024.9",
        "MT": "NC_012920.1",
    },
}


def _strip_chr(chrom: str) -> str:
    normalized = chrom.replace("chr", "").replace("Chr", "").replace("CHR", "")
    return "MT" if normalized in ("M", "mt", "Mt") else normalized


# The rCRS mitochondrial accession every build maps "MT" to in
# `_REFSEQ_CHROM_ACCESSIONS` above -- MT numbering is invariant across
# GRCh37/GRCh38 (both use the same NC_012920.1-derived contig), which is
# exactly why a VCF's CHROM column sometimes carries the accession
# itself rather than a build-relative name like "MT".
_MITOCHONDRIAL_ACCESSION_RE = re.compile(r"^NC_012920(\.\d+)?$")
_MITOCHONDRIAL_CHROM_NAMES = frozenset({"MT", "M"})


def is_mitochondrial_chrom(chrom: Optional[str]) -> bool:
    """
    True for any spelling of the human mitochondrial contig this
    codebase is known to encounter in a VCF's CHROM column: 'MT', 'M',
    'chrM', 'chrMT' (any case), or the rCRS RefSeq accession itself,
    'NC_012920.<version>' (see `_REFSEQ_CHROM_ACCESSIONS['...']['MT']`
    above -- the same accession every build maps 'MT' to, and the one
    `_HGVS_KIND_PATTERNS["m"]`/`_parse_hgvs`'s "m" kind already
    recognizes for HGVS.m notation).

    This is the compartment gate `pipeline/orchestrator.py::
    GeperPipeline._process_variant` uses to skip mitochondrial variants
    entirely (report review round 8, option (a): reject at the top of
    per-variant processing, not silently). It is intentionally a single
    boolean, not a per-evidence-source NOT_APPLICABLE distinction --
    see ROUND_CANDIDATES.md for the larger per-criterion compartment
    gate (option (b)) this narrower check defers, and for why (a)
    doesn't foreclose (b) later.
    """
    if not chrom:
        return False
    stripped = chrom.strip()
    if _MITOCHONDRIAL_ACCESSION_RE.match(stripped):
        return True
    upper = stripped.upper()
    if upper.startswith("CHR"):
        upper = upper[3:]
    return upper in _MITOCHONDRIAL_CHROM_NAMES


def _revcomp(seq: str) -> str:
    return "".join(_COMPLEMENT.get(b, "N") for b in reversed(seq.upper()))


# ---------------------------------------------------------------------------
# Shared del/ins/delins string formatting (identical logic for g. and c.
# once a caller has resolved a single 1-based numbering system to use)
# ---------------------------------------------------------------------------


def _format_variant_description(pos: int, ref: str, alt: str) -> str:
    if len(ref) == 1 and len(alt) == 1:
        return f"{pos}{ref}>{alt}"

    if len(alt) < len(ref) and ref.startswith(alt):
        # Deletion, VCF anchor-base convention (alt is a prefix of ref).
        start = pos + len(alt)
        end = pos + len(ref) - 1
        return f"{start}del" if start == end else f"{start}_{end}del"

    if len(alt) > len(ref) and alt.startswith(ref):
        # Insertion, VCF anchor-base convention (ref is a prefix of alt).
        inserted = alt[len(ref) :]
        left = pos + len(ref) - 1
        return f"{left}_{left + 1}ins{inserted}"

    # Complex delins: neither a clean deletion nor a clean insertion.
    end = pos + len(ref) - 1
    return f"{pos}delins{alt}" if pos == end else f"{pos}_{end}delins{alt}"


# ---------------------------------------------------------------------------
# g. (genomic) notation
# ---------------------------------------------------------------------------


def refseq_chrom_accession(chrom: str, assembly: str) -> Optional[str]:
    return _REFSEQ_CHROM_ACCESSIONS.get(assembly, {}).get(_strip_chr(chrom))


def to_hgvs_g(chrom: str, pos: int, ref: str, alt: str, assembly: str = "GRCh38") -> Optional[str]:
    """Genomic HGVS notation, e.g. 'NC_000017.11:g.43106487T>G'. None if `assembly`/`chrom` has no known RefSeq accession."""
    accession = refseq_chrom_accession(chrom, assembly)
    if accession is None:
        return None
    return f"{accession}:g.{_format_variant_description(pos, ref.upper(), alt.upper())}"


# ---------------------------------------------------------------------------
# c. (coding) notation
# ---------------------------------------------------------------------------


def to_hgvs_c(transcript_context: TranscriptContext, genomic_pos: int, ref: str, alt: str) -> Optional[str]:
    """
    Coding HGVS notation, e.g. 'NM_007294.4:c.181T>G', reusing
    `TranscriptContext.cds_position()` for the genomic->CDS coordinate
    mapping (real, already-tested transcript structure -- not
    re-derived here). Returns None when the variant's anchor position
    falls in an intron or UTR -- this function generates exonic,
    CDS-relative notation only (see module docstring); it does not
    generate the '+N'/'-N' intronic-offset or '-N'/'*N' UTR forms of
    HGVS c. notation.
    """
    ref, alt = ref.upper(), alt.upper()
    strand = transcript_context.strand

    # The transcript-5'-most genomic base of the ref allele (its "first"
    # base in transcript/coding orientation) -- the highest genomic
    # coordinate on a minus-strand transcript, the lowest on a plus-strand one.
    first_genomic = (genomic_pos + len(ref) - 1) if strand < 0 else genomic_pos

    cds_pos = transcript_context.cds_position(first_genomic)
    if cds_pos is None:
        return None  # intronic/UTR -- honestly out of scope, not fabricated

    ref_t = _revcomp(ref) if strand < 0 else ref
    alt_t = _revcomp(alt) if strand < 0 else alt
    return f"{transcript_context.transcript_id}:c.{_format_variant_description(cds_pos, ref_t, alt_t)}"


# ---------------------------------------------------------------------------
# p. (protein) notation
# ---------------------------------------------------------------------------


def _translate_codon(codon_dna: str) -> Optional[str]:
    if len(codon_dna) != 3:
        return None
    rna_codon = codon_dna.upper().replace("T", "U")
    return CONFIG.CODON_TABLE.get(rna_codon)


def to_hgvs_p(transcript_context: TranscriptContext, codon_number: int) -> Optional[str]:
    """
    Predicted protein HGVS notation for one substitution, e.g.
    'p.(Cys61Gly)' -- parentheses per HGVS convention for a consequence
    *deduced* from the DNA change rather than directly observed at the
    protein level. Requires `transcript_context.cds_sequence` (the
    transcript's own CDS, in coding orientation -- not always
    populated, see `TranscriptContext`'s docstring); returns None
    (not fabricated) when it is unavailable, or when `codon_number` is
    out of range for this transcript's CDS.

    Reuses `CONFIG.CODON_TABLE` (the standard genetic code
    `pipeline/protein_translator.py` already uses) and the 3-letter
    amino acid names already defined for parsing ClinVar's HGVS.p
    notation (`pipeline/ps1_pm5/utils.py::_THREE_TO_ONE`, inverted) --
    this module does not need a variant/alternate-allele argument: it
    reads the reference codon straight from the transcript's own CDS,
    which is what "translate this codon" means without a substitution
    already applied; callers wanting the ALT-allele protein
    consequence pass the already-substituted CDS via
    `to_hgvs_p_for_substitution` below instead.
    """
    ref_codon = _codon_from_cds(transcript_context, codon_number)
    if ref_codon is None:
        return None
    ref_aa = _translate_codon(ref_codon)
    if ref_aa is None:
        return None
    ref_three = _ONE_TO_THREE.get(ref_aa)
    if ref_three is None:
        return None
    return f"p.({ref_three}{codon_number}=)"


def to_hgvs_p_for_substitution(
    transcript_context: TranscriptContext, codon_number: int, alt_cds_sequence: str
) -> Optional[str]:
    """Same as `to_hgvs_p`, but translates `alt_cds_sequence` (the transcript's CDS with the variant's ALT allele already substituted in) at `codon_number` and reports the resulting amino-acid change (or '=' if synonymous, 'Ter' if it creates a stop)."""
    ref_codon = _codon_from_cds(transcript_context, codon_number)
    alt_codon = _codon_from_cds_string(alt_cds_sequence, codon_number)
    if ref_codon is None or alt_codon is None:
        return None
    ref_aa, alt_aa = _translate_codon(ref_codon), _translate_codon(alt_codon)
    if ref_aa is None or alt_aa is None:
        return None
    ref_three, alt_three = _ONE_TO_THREE.get(ref_aa), _ONE_TO_THREE.get(alt_aa)
    if ref_three is None or alt_three is None:
        return None
    if ref_aa == alt_aa:
        return f"p.({ref_three}{codon_number}=)"
    return f"p.({ref_three}{codon_number}{alt_three})"


def _codon_from_cds(transcript_context: TranscriptContext, codon_number: int) -> Optional[str]:
    return _codon_from_cds_string(transcript_context.cds_sequence, codon_number)


def _codon_from_cds_string(cds_sequence: Optional[str], codon_number: int) -> Optional[str]:
    if not cds_sequence or codon_number < 1:
        return None
    start = (codon_number - 1) * 3
    codon = cds_sequence[start : start + 3]
    return codon if len(codon) == 3 else None


# ---------------------------------------------------------------------------
# HGVS validation
# ---------------------------------------------------------------------------

_ACCESSION_PATTERNS = {
    "g": re.compile(r"^NC_\d{6}\.\d+$"),
    "c": re.compile(r"^NM_\d+\.\d+$"),
    "n": re.compile(r"^NR_\d+\.\d+$"),  # non-coding RNA transcripts
    "p": re.compile(r"^NP_\d+\.\d+$"),
    "m": re.compile(r"^NC_012920\.\d+$"),  # mitochondrial genome, its own accession
}

# Matches "<accession>:<kind>.<description>", e.g.
# "NM_007294.4:c.181T>G" or "NC_000017.11:g.43106487T>G". Captures the
# accession and kind separately from the description so each can be
# validated independently.
_HGVS_RE = re.compile(r"^(?P<accession>[A-Za-z0-9_.]+):(?P<kind>[gcnmp])\.(?P<description>.+)$")

_SUBSTITUTION_RE = re.compile(r"^(?P<pos>-?\*?\d+)(?P<ref>[ACGTUacgtu]+)>(?P<alt>[ACGTUacgtu]+)$")
_DELETION_RE = re.compile(r"^(?P<start>-?\*?\d+)(?:_(?P<end>-?\*?\d+))?del$")
_INSERTION_RE = re.compile(r"^(?P<start>-?\*?\d+)_(?P<end>-?\*?\d+)ins(?P<seq>[ACGTUacgtu]+)$")
_DELINS_RE = re.compile(r"^(?P<start>-?\*?\d+)(?:_(?P<end>-?\*?\d+))?delins(?P<seq>[ACGTUacgtu]+)$")


@dataclass
class HGVSValidationResult:
    hgvs_string: str
    is_valid: bool
    kind: Optional[str] = None  # "g" | "c" | "n" | "m" | "p"
    accession: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "hgvs_string": self.hgvs_string,
            "is_valid": self.is_valid,
            "kind": self.kind,
            "accession": self.accession,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def validate_hgvs(
    hgvs_string: str,
    fetch_reference_base: Optional[FetchBase] = None,
    chrom_hint: Optional[str] = None,
) -> HGVSValidationResult:
    """
    Validates an HGVS string's:
      1. Overall shape ('<accession>:<kind>.<description>').
      2. Accession format (RefSeq prefix + version matching `kind`:
         NC_/g., NM_/c., NR_/n., NP_/p.).
      3. Description syntax (substitution / deletion / insertion /
         delins -- see module docstring for the scope boundary).
      4. For g. notation with `fetch_reference_base` supplied: the
         stated reference base(s) are cross-checked against the real
         reference genome at the stated position(s) -- a mismatch is a
         hard error, not a warning (a wrong ref base means either the
         position is wrong or the string describes a different
         variant than intended, either of which is unsafe to act on).

    `chrom_hint` lets a caller supply the actual chromosome for the
    reference cross-check when the accession itself isn't in
    `_REFSEQ_CHROM_ACCESSIONS` (e.g. a non-primary contig) -- without
    it, the cross-check is skipped for an unrecognized accession
    (not silently assumed to have failed).
    """
    result = HGVSValidationResult(hgvs_string=hgvs_string, is_valid=True)

    match = _HGVS_RE.match(hgvs_string.strip())
    if not match:
        result.is_valid = False
        result.errors.append(
            f"'{hgvs_string}' does not match the expected HGVS shape '<accession>:<kind>.<description>' "
            f"(e.g. 'NM_007294.4:c.181T>G'). Check for a missing colon, a missing 'g./c./n./p.' prefix, "
            f"or stray whitespace."
        )
        return result

    accession, kind, description = match.group("accession"), match.group("kind"), match.group("description")
    result.accession = accession
    result.kind = kind

    accession_pattern = _ACCESSION_PATTERNS.get(kind)
    if accession_pattern is None:
        result.is_valid = False
        result.errors.append(
            f"Unrecognized HGVS notation kind '{kind}.' in '{hgvs_string}' (expected one of g./c./n./m./p.)."
        )
        return result
    if not accession_pattern.match(accession):
        expected = {"g": "NC_######.#", "c": "NM_#.#", "n": "NR_#.#", "p": "NP_#.#", "m": "NC_012920.#"}[kind]
        result.is_valid = False
        result.errors.append(
            f"'{accession}' is not a valid RefSeq accession for '{kind}.' notation (expected the form "
            f"'{expected}', e.g. a version number after a dot -- a bare 'NM_007294' without '.4' is not "
            f"a complete accession)."
        )
        return result

    if kind == "p":
        # Protein description syntax (Xxx###Yyy / Xxx###= / Xxx###Ter)
        # is a different grammar from the nucleotide forms below --
        # this validator focuses on the nucleotide (g./c.) forms (see
        # module docstring); a p. accession/shape that reached this
        # point is accepted as syntactically well-formed at the level
        # already checked (accession + overall shape) without a deeper
        # protein-description parse.
        return result

    _validate_description(description, result, fetch_reference_base, kind, accession, chrom_hint)
    return result


def _validate_description(
    description: str,
    result: HGVSValidationResult,
    fetch_reference_base: Optional[FetchBase],
    kind: str,
    accession: str,
    chrom_hint: Optional[str],
) -> None:
    sub = _SUBSTITUTION_RE.match(description)
    if sub:
        _cross_check_substitution(sub, result, fetch_reference_base, kind, accession, chrom_hint)
        return
    if _DELETION_RE.match(description) or _INSERTION_RE.match(description) or _DELINS_RE.match(description):
        # Structurally recognized -- see module docstring: this
        # validator does not additionally cross-check the reference
        # sequence for del/ins/delins forms (unlike the substitution
        # case above), only their syntax.
        return

    result.is_valid = False
    result.errors.append(
        f"'{description}' does not match any recognized HGVS description form (substitution 'N#>N', "
        f"deletion '#[_#]del', insertion '#_#insN', or delins '#[_#]delinsN'). It may use an HGVS form "
        f"this validator does not cover (see module docstring), or it may be malformed."
    )


def _cross_check_substitution(
    match: "re.Match",
    result: HGVSValidationResult,
    fetch_reference_base: Optional[FetchBase],
    kind: str,
    accession: str,
    chrom_hint: Optional[str],
) -> None:
    pos_str, stated_ref = match.group("pos"), match.group("ref").upper()
    if pos_str.startswith(("*", "-")) or "+" in pos_str or "-" in pos_str[1:]:
        result.warnings.append(
            f"Position '{pos_str}' looks like a UTR/intronic-offset coordinate ('-', '*', or a '+N'/'-N' "
            f"offset) -- this validator's reference cross-check only supports plain CDS/genomic integer "
            f"positions, so it was skipped for this position."
        )
        return
    if fetch_reference_base is None:
        result.warnings.append(
            "No reference sequence source was supplied -- the stated reference base was not cross-checked against real sequence."
        )
        return
    if kind != "g":
        result.warnings.append(
            f"Reference-base cross-check is only implemented for g. notation in this validator -- '{kind}.' was not checked against sequence (see module docstring)."
        )
        return

    chrom = chrom_hint or _chrom_for_accession(accession)
    if chrom is None:
        result.warnings.append(
            f"Accession '{accession}' is not in this validator's known chromosome table and no chrom_hint was given -- reference cross-check skipped."
        )
        return

    pos = int(pos_str)
    try:
        real_base = fetch_reference_base(chrom, pos)
    except Exception as exc:  # noqa: BLE001 - a fetch failure must not be mistaken for "reference base confirmed"
        result.warnings.append(
            f"Could not fetch the real reference base at {chrom}:{pos} to cross-check ({exc}) -- cross-check skipped."
        )
        return

    if real_base.upper() != stated_ref[: len(real_base)]:
        result.is_valid = False
        result.errors.append(
            f"'{result.hgvs_string}' states reference base '{stated_ref}' at position {pos}, but the real "
            f"reference genome has '{real_base.upper()}' there. Either the position is wrong, or this string "
            f"describes a different variant than intended."
        )


def _chrom_for_accession(accession: str) -> Optional[str]:
    for assembly_table in _REFSEQ_CHROM_ACCESSIONS.values():
        for chrom, acc in assembly_table.items():
            if acc == accession:
                return chrom
    return None
