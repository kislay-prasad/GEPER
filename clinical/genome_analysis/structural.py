"""
clinical/genome_analysis/structural.py
──────────────────────────────────────
Typed, validated CNV and structural-variant records. Pure data and pure
functions: no I/O, no database, no third-party imports.

WHAT THESE ARE. Representations of calls that an EXTERNAL, separately validated
caller produced. GEPER's own FASTQ pipeline calls small variants only (FreeBayes);
nothing in this repository detects copy-number or structural variants, and
nothing here does either. See structural_vcf.py for how records arrive.

STATUS VOCABULARY (shared by both models and by the ingest outcome):

    detected     the source marked the call PASS and nothing in it was in doubt
    uncertain    the source did not mark it PASS (including an unset FILTER '.'),
                 or validation had to note a discrepancy (see `warnings`).
                 Never promoted to `detected`.
    not_assessed no call set covers this (used by the visualization layer; a
                 record itself is never "not_assessed")
    unsupported  syntactically valid but outside what is supported here
    invalid      malformed or inconsistent with the declared assembly

Positional imprecision is NOT uncertainty of the call: a PASS call with a
CIPOS interval is `detected`, and its interval travels in `Breakpoint`.

COORDINATES are the source's, verbatim: 1-based VCF POS / END. For a symbolic
DEL the base at POS is the anchor that precedes the deleted bases; it is not
shifted. No liftover exists in this module: a record is valid only in the
assembly it was declared in.

NO ABSENCE IS EVER READ AS NORMAL. A missing copy number stays None (never 2).
A missing call is not a diploid call. Two overlapping records are both kept.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .reference import SUPPORTED_ASSEMBLIES, get_chromosome

STATUS_DETECTED = "detected"
STATUS_UNCERTAIN = "uncertain"
STATUS_NOT_ASSESSED = "not_assessed"
STATUS_UNSUPPORTED = "unsupported"
STATUS_INVALID = "invalid"

RECORD_STATUSES = frozenset({STATUS_DETECTED, STATUS_UNCERTAIN})  # statuses a stored finding may carry
OUTCOME_STATUSES = frozenset({STATUS_DETECTED, STATUS_UNCERTAIN, STATUS_UNSUPPORTED, STATUS_INVALID})

CNV_DELETION = "deletion"
CNV_DUPLICATION = "duplication"
#: SVTYPE=CNV: the source reports a copy-number change without saying which way
#: it points. Direction is never inferred from CN, because "loss" is relative to
#: a baseline (2 on autosomes; not on X/Y/MT, and unknown for the patient).
CNV_CHANGE = "copy_number_change"
CNV_TYPES = frozenset({CNV_DELETION, CNV_DUPLICATION, CNV_CHANGE})

SV_TYPES = ("DEL", "DUP", "INS", "INV", "BND", "TRA")

PRECISION_PRECISE = "precise"
PRECISION_INTERVAL = "interval"
PRECISION_UNQUANTIFIED = "imprecise_unquantified"

PARTNER_PAIRED = "paired"  # filled in only by pair_breakends' result, never by the parser
PARTNER_GIVEN = "partner_given"
PARTNER_MISSING = "partner_not_specified"
PARTNER_SINGLE = "single_breakend"
PARTNER_UNPLACED = "partner_on_unplaced_contig"


class StructuralValidationError(ValueError):
    """A record violates an invariant of the model (coordinates, assembly, values)."""


def _finite(value: Optional[float], name: str) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
    ):
        raise StructuralValidationError(f"{name} must be a finite number, got {value!r}")


def _check_assembly(assembly: str) -> None:
    if assembly not in SUPPORTED_ASSEMBLIES:
        raise StructuralValidationError(
            f"unsupported assembly {assembly!r}; supported: {', '.join(SUPPORTED_ASSEMBLIES)}"
        )


def _check_position(assembly: str, chrom: str, pos: int, what: str) -> None:
    ref = get_chromosome(assembly, chrom)
    if ref is None:
        raise StructuralValidationError(f"{what}: {chrom!r} is not a primary chromosome of {assembly}")
    if isinstance(pos, bool) or not isinstance(pos, int) or pos < 1 or pos > ref.length_bp:
        raise StructuralValidationError(
            f"{what}: position {pos!r} is outside 1..{ref.length_bp} on {chrom} ({assembly})"
        )


@dataclass(frozen=True)
class Breakpoint:
    """
    One end of an event. `chrom` is the canonical name, or None when the end lies
    on a contig this module does not place (then `chrom_raw` is all that is known).
    `ci_low`/`ci_high` are ABSOLUTE 1-based positions bounding where the source says
    the end may be (`pos` when it quantified nothing). `precision` says which.
    """

    chrom: Optional[str]
    chrom_raw: str
    pos: int
    ci_low: int
    ci_high: int
    precision: str = PRECISION_PRECISE
    notation: Optional[str] = None  # BND bracket form verbatim, e.g. "t[p["; never reinterpreted

    def __post_init__(self) -> None:
        if isinstance(self.pos, bool) or not isinstance(self.pos, int) or self.pos < 1:
            raise StructuralValidationError(f"breakpoint position must be a positive integer, got {self.pos!r}")
        if not (self.ci_low <= self.pos <= self.ci_high):
            raise StructuralValidationError("breakpoint interval must contain its position")
        if self.precision not in (PRECISION_PRECISE, PRECISION_INTERVAL, PRECISION_UNQUANTIFIED):
            raise StructuralValidationError(f"unknown precision {self.precision!r}")
        if self.precision == PRECISION_PRECISE and (self.ci_low != self.pos or self.ci_high != self.pos):
            raise StructuralValidationError("a precise breakpoint has no interval")

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Breakpoint":
        """Rebuild through the validating constructor, so a stored document is re-validated on every read."""
        return cls(
            d["chrom"],
            d["chrom_raw"],
            d["pos"],
            d["ci_low"],
            d["ci_high"],
            d.get("precision", PRECISION_PRECISE),
            d.get("notation"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chrom": self.chrom,
            "chrom_raw": self.chrom_raw,
            "pos": self.pos,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "precision": self.precision,
            "notation": self.notation,
        }


@dataclass(frozen=True)
class CopyNumberVariant:
    assembly: str
    chrom: str
    start: int
    end: int
    cnv_type: str
    status: str
    caller: Optional[str] = None
    source_id: Optional[str] = None  # the VCF ID column
    call_set_id: Optional[str] = None
    copy_number: Optional[float] = None  # as measured by the source; None = not reported (NOT 2)
    copy_number_source: Optional[str] = None
    log2_ratio: Optional[float] = None
    quality: Optional[float] = None
    filter: Optional[str] = None
    start_breakpoint: Optional[Breakpoint] = None
    end_breakpoint: Optional[Breakpoint] = None
    evidence: Dict[str, Any] = field(default_factory=dict)
    line_no: Optional[int] = None
    warnings: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_assembly(self.assembly)
        if self.status not in RECORD_STATUSES:
            raise StructuralValidationError(f"a CNV record's status must be one of {sorted(RECORD_STATUSES)}")
        if self.cnv_type not in CNV_TYPES:
            raise StructuralValidationError(f"unsupported CNV type {self.cnv_type!r}")
        _check_position(self.assembly, self.chrom, self.start, "CNV start")
        _check_position(self.assembly, self.chrom, self.end, "CNV end")
        if self.end < self.start:
            raise StructuralValidationError(f"CNV end {self.end} precedes start {self.start}")
        _finite(self.copy_number, "copy_number")
        _finite(self.log2_ratio, "log2_ratio")
        _finite(self.quality, "quality")
        if self.copy_number is not None and self.copy_number < 0:
            raise StructuralValidationError("copy_number cannot be negative")

    @property
    def length_bp(self) -> int:
        return self.end - self.start + 1

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CopyNumberVariant":
        def bp(key: str) -> Optional[Breakpoint]:
            return Breakpoint.from_dict(d[key]) if d.get(key) else None

        return cls(
            assembly=d["assembly"],
            chrom=d["chrom"],
            start=d["start"],
            end=d["end"],
            cnv_type=d["cnv_type"],
            status=d["status"],
            caller=d.get("caller"),
            source_id=d.get("source_id"),
            call_set_id=d.get("call_set_id"),
            copy_number=d.get("copy_number"),
            copy_number_source=d.get("copy_number_source"),
            log2_ratio=d.get("log2_ratio"),
            quality=d.get("quality"),
            filter=d.get("filter"),
            start_breakpoint=bp("start_breakpoint"),
            end_breakpoint=bp("end_breakpoint"),
            evidence=dict(d.get("evidence") or {}),
            line_no=d.get("line_no"),
            warnings=tuple(d.get("warnings") or ()),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "cnv",
            "assembly": self.assembly,
            "chrom": self.chrom,
            "start": self.start,
            "end": self.end,
            "length_bp": self.length_bp,
            "cnv_type": self.cnv_type,
            "status": self.status,
            "copy_number": self.copy_number,
            "copy_number_source": self.copy_number_source,
            "log2_ratio": self.log2_ratio,
            "quality": self.quality,
            "filter": self.filter,
            "start_breakpoint": self.start_breakpoint.to_dict() if self.start_breakpoint else None,
            "end_breakpoint": self.end_breakpoint.to_dict() if self.end_breakpoint else None,
            "caller": self.caller,
            "source_id": self.source_id,
            "call_set_id": self.call_set_id,
            "evidence": dict(self.evidence),
            "line_no": self.line_no,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class StructuralVariant:
    """
    `breakpoints[0]` is the record's own position. `breakpoints[1]` is the other
    end when the source gave one: END for DEL/DUP/INV, the partner for BND. It is
    None for INS, for a single breakend, and for a BND whose partner the source
    did not state -- never fabricated.
    """

    assembly: str
    sv_type: str
    status: str
    breakpoints: Tuple[Breakpoint, ...]
    partner_status: Optional[str] = None
    caller: Optional[str] = None
    source_id: Optional[str] = None
    call_set_id: Optional[str] = None
    mate_id: Optional[str] = None
    event_id: Optional[str] = None
    sv_length: Optional[int] = None  # SVLEN as reported (DEL is usually negative)
    inserted_sequence: Optional[str] = None
    quality: Optional[float] = None
    filter: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)
    line_no: Optional[int] = None
    warnings: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_assembly(self.assembly)
        if self.status not in RECORD_STATUSES:
            raise StructuralValidationError(f"an SV record's status must be one of {sorted(RECORD_STATUSES)}")
        if self.sv_type not in SV_TYPES:
            raise StructuralValidationError(f"unsupported SV type {self.sv_type!r}")
        if not 1 <= len(self.breakpoints) <= 2:
            raise StructuralValidationError("an SV has one or two breakpoints")
        for bp in self.breakpoints:
            if bp.chrom is not None:
                _check_position(self.assembly, bp.chrom, bp.pos, "SV breakpoint")
        if self.sv_type in ("DEL", "DUP", "INV"):
            if len(self.breakpoints) != 2 or self.breakpoints[0].chrom != self.breakpoints[1].chrom:
                raise StructuralValidationError(f"{self.sv_type} needs two breakpoints on one chromosome")
            if self.breakpoints[1].pos < self.breakpoints[0].pos:
                raise StructuralValidationError(f"{self.sv_type} end precedes its start")
        if self.sv_type == "INS" and len(self.breakpoints) != 1:
            raise StructuralValidationError("INS has exactly one breakpoint")
        _finite(self.quality, "quality")

    @property
    def is_interchromosomal(self) -> Optional[bool]:
        """True/False only when BOTH ends are placed; None otherwise. Not a claim of translocation."""
        if len(self.breakpoints) != 2:
            return None
        a, b = self.breakpoints
        if a.chrom is None or b.chrom is None:
            return None
        return a.chrom != b.chrom

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StructuralVariant":
        return cls(
            assembly=d["assembly"],
            sv_type=d["sv_type"],
            status=d["status"],
            breakpoints=tuple(Breakpoint.from_dict(b) for b in d["breakpoints"]),
            partner_status=d.get("partner_status"),
            caller=d.get("caller"),
            source_id=d.get("source_id"),
            call_set_id=d.get("call_set_id"),
            mate_id=d.get("mate_id"),
            event_id=d.get("event_id"),
            sv_length=d.get("sv_length"),
            inserted_sequence=d.get("inserted_sequence"),
            quality=d.get("quality"),
            filter=d.get("filter"),
            evidence=dict(d.get("evidence") or {}),
            line_no=d.get("line_no"),
            warnings=tuple(d.get("warnings") or ()),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": "sv",
            "assembly": self.assembly,
            "sv_type": self.sv_type,
            "status": self.status,
            "breakpoints": [bp.to_dict() for bp in self.breakpoints],
            "partner_status": self.partner_status,
            "is_interchromosomal": self.is_interchromosomal,
            "mate_id": self.mate_id,
            "event_id": self.event_id,
            "sv_length": self.sv_length,
            "inserted_sequence": self.inserted_sequence,
            "quality": self.quality,
            "filter": self.filter,
            "caller": self.caller,
            "source_id": self.source_id,
            "call_set_id": self.call_set_id,
            "evidence": dict(self.evidence),
            "line_no": self.line_no,
            "warnings": list(self.warnings),
        }


# ── pure helpers: overlaps, lanes, breakend pairing ──────────────────────────


def intervals_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end


def relate_cnvs(cnvs: Sequence[CopyNumberVariant]) -> List[Dict[str, Any]]:
    """
    Every pair of CNVs on one chromosome (same assembly) that overlap or abut,
    WITHOUT merging or altering either. `relation` is "overlap" or "adjacent"
    (end + 1 == start). Indexes refer to the input sequence.
    """
    out: List[Dict[str, Any]] = []
    for i in range(len(cnvs)):
        for j in range(i + 1, len(cnvs)):
            a, b = cnvs[i], cnvs[j]
            if a.chrom != b.chrom or a.assembly != b.assembly:
                continue
            if intervals_overlap(a.start, a.end, b.start, b.end):
                out.append({"a": i, "b": j, "relation": "overlap"})
            elif a.end + 1 == b.start or b.end + 1 == a.start:
                out.append({"a": i, "b": j, "relation": "adjacent"})
    return out


def assign_lanes(intervals: Sequence[Tuple[int, int]]) -> List[int]:
    """
    Greedy lane (row) assignment so overlapping intervals are drawn on different
    rows instead of hiding one another. Returns a lane index per input interval,
    in input order. Deterministic. Adjacent intervals may share a lane.
    """
    order = sorted(range(len(intervals)), key=lambda k: (intervals[k][0], intervals[k][1], k))
    lane_end: List[int] = []
    lanes = [0] * len(intervals)
    for k in order:
        start, end = intervals[k]
        for lane, last in enumerate(lane_end):
            if start > last:
                lane_end[lane] = end
                lanes[k] = lane
                break
        else:
            lane_end.append(end)
            lanes[k] = len(lane_end) - 1
    return lanes


@dataclass(frozen=True)
class BreakendPairing:
    """Indexes into the input sequence. Nothing is merged or mutated."""

    paired: Tuple[Tuple[int, int], ...]
    mate_missing: Tuple[int, ...]  # names a mate that is not in the set
    inconsistent: Tuple[Tuple[int, int], ...]  # mates that do not agree with each other
    unpaired: Tuple[int, ...]  # BND records that name no mate at all


def pair_breakends(svs: Sequence[StructuralVariant]) -> BreakendPairing:
    """
    Pair BND records by RECIPROCAL MATEID (a.mate_id == b.source_id and
    b.mate_id == a.source_id) and verify that each states the other's location.
    A pair that disagrees is reported, not fixed.
    """
    by_id: Dict[str, int] = {}
    for idx, sv in enumerate(svs):
        if sv.sv_type == "BND" and sv.source_id and sv.source_id != ".":
            by_id.setdefault(sv.source_id, idx)
    paired: List[Tuple[int, int]] = []
    inconsistent: List[Tuple[int, int]] = []
    missing: List[int] = []
    unpaired: List[int] = []
    seen: set = set()
    for idx, sv in enumerate(svs):
        if sv.sv_type != "BND" or idx in seen:
            continue
        if not sv.mate_id:
            unpaired.append(idx)
            continue
        j = by_id.get(sv.mate_id)
        if j is None:
            missing.append(idx)
            continue
        mate = svs[j]
        if j == idx or mate.mate_id != sv.source_id:
            inconsistent.append((min(idx, j), max(idx, j)))
            seen.update({idx, j})
            continue
        a_own, a_partner = sv.breakpoints[0], (sv.breakpoints[1] if len(sv.breakpoints) == 2 else None)
        b_own, b_partner = mate.breakpoints[0], (mate.breakpoints[1] if len(mate.breakpoints) == 2 else None)
        agrees = (
            a_partner is not None
            and b_partner is not None
            and (a_partner.chrom_raw, a_partner.pos) == (b_own.chrom_raw, b_own.pos)
            and (b_partner.chrom_raw, b_partner.pos) == (a_own.chrom_raw, a_own.pos)
        )
        (paired if agrees else inconsistent).append((min(idx, j), max(idx, j)))
        seen.update({idx, j})
    return BreakendPairing(
        tuple(sorted(set(paired))), tuple(missing), tuple(sorted(set(inconsistent))), tuple(unpaired)
    )
