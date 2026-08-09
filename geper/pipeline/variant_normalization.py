"""
Variant normalization: reference-free parsimony trimming and
reference-guided left-alignment of indels -- the standard "the same
variant can be written multiple valid ways in a VCF" problem (Tan et
al. 2015, "Unified representation of genetic variants," Bioinformatics
-- the paper documenting the algorithm this module implements, the same
one bcftools norm / vt normalize use).

What already existed before this module: multi-allelic site SPLITTING
(one `Variant` per ALT allele) already happens in
`pipeline/vcf_parser.py::VCFParser.iter_variants` -- this module does
not re-derive that; it operates on the already-split, single-allele
`Variant` records that stage produces. What did NOT exist: parsimony
trimming and left-alignment of indels (confirmed by search before
writing this module -- no normalization logic of any kind existed
anywhere in the VCF parsing/annotation stages).

Left-alignment needs real reference-genome sequence (to walk left
through a repeat region and confirm no further shift is possible) --
this module never fetches it itself; a `fetch_base` callback is
injected by the caller. `pipeline/orchestrator.py` wires in
`SequenceContextGenerator.fetch_reference_sequence` (the same
Ensembl-backed source every other stage already uses) as the
production reference source. Without one, this module still performs
the reference-free parsimony trim (right-trim + common-prefix trim),
which is often sufficient by itself -- modern variant callers (GATK,
DeepVariant) already left-align their output by convention, so
left-alignment here is usually a no-op safety net rather than a
routine transformation.

Scope, disclosed: left-alignment applies to clean insertions/deletions
(one allele becomes empty after trimming) -- a complex delins of
unequal, non-empty length after trimming is trimmed but not
left-aligned (there is no single well-defined "slide left" operation
for that case the way there is for a clean indel).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Tuple

FetchBase = Callable[[str, int], str]  # (chrom, 1-based genomic pos) -> single reference base


@dataclass
class NormalizedVariant:
    """Result of normalizing one already-single-allele variant. `changed` tells a caller whether anything actually moved."""

    chrom: str
    pos: int
    ref: str
    alt: str
    original_pos: int
    original_ref: str
    original_alt: str
    was_trimmed: bool
    was_left_aligned: bool
    left_align_skipped_reason: Optional[str] = None

    @property
    def changed(self) -> bool:
        return (self.pos, self.ref, self.alt) != (self.original_pos, self.original_ref, self.original_alt)

    def to_dict(self) -> dict:
        return {
            "chrom": self.chrom,
            "pos": self.pos,
            "ref": self.ref,
            "alt": self.alt,
            "original_pos": self.original_pos,
            "original_ref": self.original_ref,
            "original_alt": self.original_alt,
            "changed": self.changed,
            "was_trimmed": self.was_trimmed,
            "was_left_aligned": self.was_left_aligned,
            "left_align_skipped_reason": self.left_align_skipped_reason,
        }


def trim_variant(pos: int, ref: str, alt: str) -> Tuple[int, str, str, bool]:
    """
    Reference-free parsimony trimming: common suffix, then common prefix
    (Tan et al. 2015's first step). Public (not `_`-prefixed) because
    `database/clinvar_client.py` and `database/dbsnp_client.py` also call
    it directly, on the *candidate* record's SPDI-derived (pos, ref, alt)
    -- not just on the query variant -- to bring both sides to the same
    minimal representation before comparing. This matters because a
    ClinVar/dbSNP `canonical_spdi` is not guaranteed to already be
    parsimony-trimmed: confirmed live for VHL c.422dup (VCV000411979),
    whose canonical_spdi is `NC_000003.12:10146593:AA:AAA` -- a
    non-minimal 2-base window (trims to `A:AA`) -- while GEPER's own
    query variant reaches this point already minimally trimmed by
    `normalize_variant` below. Comparing the two representations without
    trimming both first missed this exact genuine match (see
    `database/clinvar_client.py::ClinVarClient._variant_match`'s
    docstring for the full account).
    """
    changed = False

    while len(ref) > 1 and len(alt) > 1 and ref[-1] == alt[-1]:
        ref, alt = ref[:-1], alt[:-1]
        changed = True

    while len(ref) > 1 and len(alt) > 1 and ref[0] == alt[0]:
        ref, alt = ref[1:], alt[1:]
        pos += 1
        changed = True

    return pos, ref, alt, changed


def bare_spdi(pos: int, ref: str, alt: str) -> Tuple[int, str, str]:
    """
    Trim to SPDI's bare form: unlike `trim_variant`, no single-base
    anchor is kept -- `ref`/`alt` may end up fully empty (a pure
    insertion or pure deletion). VCF representations cannot go this far
    (a VCF record always keeps at least one anchor base), but
    comparing two *different sources'* indel representations for
    equivalence needs a form neither source's own convention biases,
    so this is comparison-only, never used to produce a variant this
    module (or any VCF-emitting code) would treat as valid on its own.

    Confirmed live this resolves a second, distinct mismatch shape from
    the one `trim_variant` alone closes: BRCA1 c.1232_1233del
    (VCV000054169)'s canonical_spdi is `NC_000017.11:43094297:AT:`
    (fully bare -- empty inserted sequence) while GEPER's own anchored
    VCF-style representation at that locus is `43094297 CAT>C`.
    `trim_variant` cannot reduce `alt="C"` any further (it must keep at
    least 1 base), so the two never converge under it alone; both
    become `(43094298, 'AT', '')` under this function.
    """
    while ref and alt and ref[-1] == alt[-1]:
        ref, alt = ref[:-1], alt[:-1]

    while ref and alt and ref[0] == alt[0]:
        ref, alt = ref[1:], alt[1:]
        pos += 1

    return pos, ref, alt


def _left_align(
    chrom: str, pos: int, ref: str, alt: str, fetch_base: FetchBase, max_shift_bp: int = 200
) -> Tuple[int, str, str, bool]:
    """
    Reference-guided left-alignment for a clean indel (VCF's mandatory
    single-base anchor convention: for a deletion, `alt` is a strict
    prefix of `ref` -- the anchor base -- with the deleted bases
    following it; for an insertion, `ref` is a strict prefix of `alt`
    the same way).

    Shifts the anchor one base left exactly when doing so describes
    the identical variant: the base being "dropped" off the end of the
    longer allele must equal the real reference base immediately
    preceding the current anchor. If they match, shifting changes
    nothing about which bases are actually deleted/inserted -- only
    where the (arbitrary) anchor sits -- so it's performed; if they
    differ, shifting further would describe a *different* variant, so
    the leftmost valid anchor has been found. This is what resolves the
    classic ambiguity of an indel inside a homopolymer/repeat run (e.g.
    deleting one 'A' from "...AAAA..." is valid at several different
    POS values; left-alignment picks the leftmost, canonical one).

    Not eligible (returns unchanged, `shifted=False`) when `ref`/`alt`
    don't have this prefix relationship at all -- a complex delins of
    unequal, non-empty length has no single well-defined "slide left"
    operation the way a clean indel does (see module docstring).
    """
    is_deletion = len(ref) > len(alt) and ref.startswith(alt)
    is_insertion = len(alt) > len(ref) and alt.startswith(ref)
    if not (is_deletion or is_insertion):
        return pos, ref, alt, False

    shifted = False
    shifts = 0
    while pos > 1 and shifts < max_shift_bp:
        compare_char = ref[-1] if is_deletion else alt[-1]
        prev_base = fetch_base(chrom, pos - 1)
        if not prev_base or prev_base.upper() != compare_char.upper():
            break
        if is_deletion:
            ref, alt = prev_base.upper() + ref[:-1], prev_base.upper()
        else:
            alt, ref = prev_base.upper() + alt[:-1], prev_base.upper()
        pos -= 1
        shifted = True
        shifts += 1
    return pos, ref, alt, shifted


def normalize_variant(
    chrom: str, pos: int, ref: str, alt: str, fetch_base: Optional[FetchBase] = None, max_shift_bp: int = 200
) -> NormalizedVariant:
    """
    Normalize one single-allele variant. Symbolic/structural alleles
    ('*', '<DEL>', etc.), monomorphic calls (ref == alt), and anything
    that isn't plain A/C/G/T(/N) text are passed through unchanged --
    `pipeline/vcf_parser.py` already filters those out before a
    `Variant` is even constructed, but this function stays defensive
    in case it's called from elsewhere.
    """
    original_pos = pos
    original_ref, original_alt = ref.upper(), alt.upper()
    ref, alt = original_ref, original_alt

    if not ref or not alt or ref == alt or not ref.isalpha() or not alt.isalpha():
        return NormalizedVariant(
            chrom=chrom,
            pos=pos,
            ref=ref,
            alt=alt,
            original_pos=original_pos,
            original_ref=original_ref,
            original_alt=original_alt,
            was_trimmed=False,
            was_left_aligned=False,
        )

    pos, ref, alt, was_trimmed = trim_variant(pos, ref, alt)

    was_left_aligned = False
    left_align_skipped_reason = None
    if len(ref) != len(alt):  # a real indel remains after trimming -- eligible for left-alignment
        if fetch_base is not None:
            pos, ref, alt, was_left_aligned = _left_align(chrom, pos, ref, alt, fetch_base, max_shift_bp=max_shift_bp)
        else:
            left_align_skipped_reason = (
                "no reference sequence source provided; only reference-free trimming was applied"
            )

    return NormalizedVariant(
        chrom=chrom,
        pos=pos,
        ref=ref,
        alt=alt,
        original_pos=original_pos,
        original_ref=original_ref,
        original_alt=original_alt,
        was_trimmed=was_trimmed,
        was_left_aligned=was_left_aligned,
        left_align_skipped_reason=left_align_skipped_reason,
    )
