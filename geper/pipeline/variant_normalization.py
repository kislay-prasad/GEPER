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

Correctness invariant (C1, ClinVar audit): a normalised variant must
describe the SAME sequence change as its input. One left shift moves
(pos, ref, alt) to (pos-1, p+ref[:-1], p+alt[:-1]), where `p` is the
reference base at pos-1. That is sequence-preserving if and only if the
two alleles end in the same base (`ref[-1] == alt[-1]`): the base that
stays behind after the shift is then the same on both sides. An earlier
version tested `ref[-1]` against the reference base at pos-1 (the base
before the anchor) instead. In a homopolymer all of those bases are
equal, so the mistake was invisible there; in any other repeat it
shifted indels into DIFFERENT variants: 9 of 80 real BRCA1/BRCA2 ClinVar
indels in the audit, and 22 of the same 80 once each is given in its
already left-aligned (caller) form. The test is `ref[-1] == alt[-1]` and
nothing else. The leftmost anchor of an indel inside a run is the
reference base before the run (what bcftools norm / vt normalize emit);
the old code stopped one base short, with the anchor inside the run.

Reference allele check: when a reference source is supplied, the REF
allele is compared with the reference before anything is changed. A
mismatch (typically a genome-build mix-up) is reported on the result
(`reference_check == "mismatch"`) and the variant is returned exactly as
given, never trimmed or shifted against a reference it does not match.
If the reference cannot be read, the variant is trimmed only and the
result says the check was not performed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

FetchBase = Callable[[str, int], str]  # (chrom, 1-based genomic pos) -> single reference base
FetchSequence = Callable[[str, int, int], str]  # (chrom, 1-based start, 1-based inclusive end) -> bases

# Backward walk granularity when a window source is available: one request
# per this many bases instead of one request per base.
_WINDOW_CHUNK_BP = 64

REFERENCE_CHECK_MATCHED = "matched"
REFERENCE_CHECK_MISMATCH = "mismatch"
REFERENCE_CHECK_NOT_CHECKED = "not_checked"


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
    # "matched" | "mismatch" | "not_checked" -- see the module docstring.
    reference_check: str = REFERENCE_CHECK_NOT_CHECKED
    reference_check_detail: Optional[str] = None

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
            "reference_check": self.reference_check,
            "reference_check_detail": self.reference_check_detail,
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


class _ReferenceReader:
    """
    Reads reference bases for one variant from whichever source the
    caller supplied: a window source (`fetch_sequence`, preferred -- one
    request covers many bases) and/or a single-base source (`fetch_base`,
    kept for existing callers). Bases are cached for the lifetime of one
    `normalize_variant` call.
    """

    def __init__(self, fetch_base: Optional[FetchBase], fetch_sequence: Optional[FetchSequence]):
        self._fetch_base = fetch_base
        self._fetch_sequence = fetch_sequence
        self._bases: Dict[Tuple[str, int], str] = {}

    @property
    def available(self) -> bool:
        return self._fetch_base is not None or self._fetch_sequence is not None

    def _remember(self, chrom: str, start: int, sequence: str) -> None:
        for offset, base in enumerate(sequence.upper()):
            self._bases[(chrom, start + offset)] = base

    def window(self, chrom: str, start: int, end: int) -> str:
        """Reference bases for [start, end] inclusive (1-based), upper-case."""
        if end < start:
            return ""
        missing = [p for p in range(start, end + 1) if (chrom, p) not in self._bases]
        if missing:
            if self._fetch_sequence is not None:
                lo, hi = missing[0], missing[-1]
                sequence = self._fetch_sequence(chrom, lo, hi)
                if sequence is None or len(sequence) != hi - lo + 1:
                    raise ValueError(
                        f"reference source returned {0 if sequence is None else len(sequence)} base(s) "
                        f"for a {hi - lo + 1}-base window"
                    )
                self._remember(chrom, lo, sequence)
            else:
                assert self._fetch_base is not None
                for p in missing:
                    base = self._fetch_base(chrom, p)
                    if not base or len(base) != 1:
                        raise ValueError("reference source returned no base for a position")
                    self._bases[(chrom, p)] = base.upper()
        return "".join(self._bases[(chrom, p)] for p in range(start, end + 1))

    def base(self, chrom: str, pos: int) -> str:
        """The single reference base at `pos`; walks backwards a chunk at a time when a window source exists."""
        if (chrom, pos) not in self._bases and self._fetch_sequence is not None:
            self.window(chrom, max(1, pos - _WINDOW_CHUNK_BP + 1), pos)
        return self.window(chrom, pos, pos)


def _left_align(
    chrom: str, pos: int, ref: str, alt: str, fetch_base: FetchBase, max_shift_bp: int = 200
) -> Tuple[int, str, str, bool]:
    """
    Reference-guided left-alignment (Tan et al. 2015) of an already
    trimmed variant.

    One shift moves (pos, ref, alt) to (pos-1, p+ref[:-1], p+alt[:-1])
    with `p` the reference base at pos-1. It describes the identical
    sequence change exactly when `ref[-1] == alt[-1]` (the base left
    behind is then the same on both sides), so that is the only test
    applied. Shifting stops when the alleles' last bases differ, at
    position 1, or after `max_shift_bp` shifts; every state reached is
    sequence-equivalent to the input, so stopping early still yields a
    valid (just not fully left-aligned) description.

    Works for any trimmed allele pair, including the VCF
    single-base-anchor insertions and deletions and the less common
    non-anchored forms; a pair whose last bases differ (a substitution
    or a complex delins) is returned unchanged.
    """
    shifted = False
    shifts = 0
    while pos > 1 and shifts < max_shift_bp and ref and alt and ref[-1] == alt[-1]:
        prev_base = (fetch_base(chrom, pos - 1) or "").upper()
        if len(prev_base) != 1:
            raise ValueError("reference source returned no base for a position")
        ref, alt = prev_base + ref[:-1], prev_base + alt[:-1]
        pos -= 1
        shifted = True
        shifts += 1
    return pos, ref, alt, shifted


def _reference_mismatch(chrom: str, pos: int, ref: str, reader: _ReferenceReader) -> Optional[str]:
    """
    None when `ref` equals the reference at [pos, pos+len(ref)-1];
    otherwise a short description of the first difference. A reference
    'N' is not treated as a difference (nothing can be verified there).
    """
    observed = reader.window(chrom, pos, pos + len(ref) - 1)
    for offset, (given, found) in enumerate(zip(ref, observed)):
        if found != "N" and given != found:
            return (
                f"VCF REF '{_shorten(ref)}' does not match the reference '{_shorten(observed)}' at "
                f"{chrom}:{pos} (first difference at {chrom}:{pos + offset})"
            )
    return None


def _shorten(sequence: str, limit: int = 24) -> str:
    return sequence if len(sequence) <= limit else sequence[:limit] + "..."


def normalize_variant(
    chrom: str,
    pos: int,
    ref: str,
    alt: str,
    fetch_base: Optional[FetchBase] = None,
    max_shift_bp: int = 200,
    fetch_sequence: Optional[FetchSequence] = None,
) -> NormalizedVariant:
    """
    Normalize one single-allele variant. Symbolic/structural alleles
    ('*', '<DEL>', etc.), monomorphic calls (ref == alt), and anything
    that isn't plain A/C/G/T(/N) text are passed through unchanged --
    `pipeline/vcf_parser.py` already filters those out before a
    `Variant` is even constructed, but this function stays defensive
    in case it's called from elsewhere.

    `fetch_sequence(chrom, start, end)` (preferred) and/or
    `fetch_base(chrom, pos)` supply the reference. With one, the REF
    allele is checked against the reference first (see the module
    docstring) and indels are left-aligned; without one, only
    reference-free trimming is done. Reference reads that fail are
    reported on the result, never raised.
    """
    original_pos = pos
    original_ref, original_alt = ref.upper(), alt.upper()
    ref, alt = original_ref, original_alt

    def result(
        *,
        was_trimmed: bool = False,
        was_left_aligned: bool = False,
        left_align_skipped_reason: Optional[str] = None,
        reference_check: str = REFERENCE_CHECK_NOT_CHECKED,
        reference_check_detail: Optional[str] = None,
    ) -> NormalizedVariant:
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
            reference_check=reference_check,
            reference_check_detail=reference_check_detail,
        )

    if not ref or not alt or ref == alt or not ref.isalpha() or not alt.isalpha():
        return result()

    reader = _ReferenceReader(fetch_base, fetch_sequence)
    reference_check = REFERENCE_CHECK_NOT_CHECKED
    reference_detail: Optional[str] = None
    if reader.available:
        try:
            mismatch = _reference_mismatch(chrom, pos, ref, reader)
        except Exception as exc:  # noqa: BLE001 -- a reference outage must degrade, never abort the variant
            detail = str(exc) or type(exc).__name__
            reference_detail = f"reference sequence unavailable ({type(exc).__name__}: {_shorten(detail, 120)})"
            reader = _ReferenceReader(None, None)
        else:
            if mismatch is not None:
                # Do not trim or shift against a reference this REF does not match.
                return result(
                    reference_check=REFERENCE_CHECK_MISMATCH,
                    reference_check_detail=mismatch,
                    left_align_skipped_reason="reference allele mismatch; variant left exactly as given",
                )
            reference_check = REFERENCE_CHECK_MATCHED

    pos, ref, alt, was_trimmed = trim_variant(pos, ref, alt)
    trimmed_pos = pos

    was_left_aligned = False
    left_align_skipped_reason = None
    if len(ref) != len(alt):  # a real indel remains after trimming -- eligible for left-alignment
        if reader.available:
            try:
                pos, ref, alt, was_left_aligned = _left_align(
                    chrom, pos, ref, alt, lambda c, p: reader.base(c, p), max_shift_bp=max_shift_bp
                )
            except Exception as exc:  # noqa: BLE001 -- keep the trimmed (equivalent) form
                pos, ref, alt, _ = trim_variant(original_pos, original_ref, original_alt)
                was_left_aligned = False
                detail = str(exc) or type(exc).__name__
                reference_detail = f"reference sequence unavailable ({type(exc).__name__}: {_shorten(detail, 120)})"
                left_align_skipped_reason = (
                    "reference sequence could not be read; only reference-free trimming was applied"
                )
            else:
                if pos > 1 and ref and alt and ref[-1] == alt[-1] and trimmed_pos - pos >= max_shift_bp:
                    left_align_skipped_reason = (
                        f"left-alignment stopped at the {max_shift_bp} bp shift limit; the variant is equivalent "
                        "to its input but may not be fully left-aligned"
                    )
        elif reference_detail is not None:
            left_align_skipped_reason = "reference sequence could not be read; only reference-free trimming was applied"
        else:
            left_align_skipped_reason = (
                "no reference sequence source provided; only reference-free trimming was applied"
            )

    return result(
        was_trimmed=was_trimmed,
        was_left_aligned=was_left_aligned,
        left_align_skipped_reason=left_align_skipped_reason,
        reference_check=reference_check,
        reference_check_detail=reference_detail,
    )
