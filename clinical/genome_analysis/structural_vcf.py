"""
clinical/genome_analysis/structural_vcf.py
──────────────────────────────────────────
Ingestion interface for CNV / SV VCFs produced by an EXTERNAL, validated caller.

THIS DETECTS NOTHING. It parses and validates records someone else called. The
repository's FASTQ pipeline (kim_pipeline: QC -> alignment -> FreeBayes) emits
small variants only, and geper/pipeline/vcf_parser.py deliberately DISCARDS
symbolic structural ALTs ('<DEL>', ...) and counts them in a log line. This module
does not touch either of those; it is a separate input path for a separate file.

THE OPERATOR DECLARES, THE FILE IS CHECKED. Three things are required from the
caller and never guessed from the file:

  * `assembly`      GRCh37 or GRCh38. The header is then CHECKED against it: any
                    ##contig length that disagrees with the declared build's
                    primary-chromosome length, or a ##reference naming the other
                    build, rejects the WHOLE call set (CallSetRejected). A header
                    that carries no such evidence is accepted and recorded as
                    `header_check.status == "unverified"`.
  * `record_class`  "cnv" or "sv". Many callers label copy-number calls DEL/DUP,
                    so the class is a declaration about the file, not an inference
                    from SVTYPE. A record of the wrong class is `unsupported`.
  * (optional) `caller`, `source_id`, `call_set_id`, kept as provenance next to
                    whatever the header says (`##source`).

SUPPORTED INPUT, STATED PLAINLY
  VCF 4.x text; one record per ALT (a comma in a structural ALT is `unsupported`).
  CNV class : <DEL> <DUP> <CNV> <CNn>   with END or SVLEN, optional CN / LOG2RATIO /
              CIPOS / CIEND / IMPRECISE, CN also from a single sample's FORMAT.
  SV class  : <DEL> <DUP> <INV> <INS> <BND> <TRA> (and ':' subtypes such as
              <DUP:TANDEM>), VCF bracket breakends (t[p[ t]p] ]p]t [p[t), single
              breakends, literal-sequence insertions with SVTYPE=INS.
  Everything else is `unsupported` with a reason, or `invalid` with a reason.
  NOT SUPPORTED: liftover/assembly conversion; contigs outside 1-22, X, Y, MT
  (a BND partner on such a contig is kept, unplaced); gVCF; compressed input
  (decompress first); multi-sample genotype interpretation (FORMAT is preserved,
  not interpreted).

RECORDS ARE NEVER MERGED, REORDERED OR "FIXED". Every input record yields exactly
one outcome, and the original record text is preserved in `evidence.raw_record`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from .normalization import normalize_contig
from .reference import SUPPORTED_ASSEMBLIES, canonical_assembly, chromosomes_for
from .structural import (
    CNV_CHANGE,
    CNV_DELETION,
    CNV_DUPLICATION,
    PARTNER_GIVEN,
    PARTNER_MISSING,
    PARTNER_SINGLE,
    PARTNER_UNPLACED,
    PRECISION_INTERVAL,
    PRECISION_PRECISE,
    PRECISION_UNQUANTIFIED,
    STATUS_DETECTED,
    STATUS_INVALID,
    STATUS_UNCERTAIN,
    STATUS_UNSUPPORTED,
    Breakpoint,
    CopyNumberVariant,
    StructuralValidationError,
    StructuralVariant,
)

#: The ruleset that produced a stored document. Bump it when parsing or validation semantics change, so a stored
#: document can be re-derived with the SAME rules (see verify_document).
CALLSET_PARSER_VERSION = "structural_vcf/1"
CALLSET_DOCUMENT_SCHEMA_VERSION = "1"

RECORD_CLASS_CNV = "cnv"
RECORD_CLASS_SV = "sv"
_RAW_RECORD_LIMIT = 4000

_SYMBOLIC = re.compile(r"^<([A-Za-z0-9_]+)(?::[^<>]*)?>$")
_CN_ALT = re.compile(r"^CN(\d+)$")
_BND_PREFIX = re.compile(r"^([ACGTNacgtn]+)([\[\]])(.+):(\d+)\2$")  # t[p[   t]p]
_BND_SUFFIX = re.compile(r"^([\[\]])(.+):(\d+)\1([ACGTNacgtn]+)$")  # ]p]t   [p[t
_SINGLE_BREAKEND = re.compile(r"^(?:[ACGTNacgtn]+\.|\.[ACGTNacgtn]+)$")
_LITERAL_SEQUENCE = re.compile(r"^[ACGTNacgtn]+$")
_CONTIG_LINE = re.compile(r"^##contig=<(.*)>\s*$")


class CallSetRejected(ValueError):
    """The call set as a whole cannot be accepted (e.g. its header contradicts the declared assembly)."""


@dataclass(frozen=True)
class RecordOutcome:
    """Exactly one per data line. `record` is set only for detected/uncertain."""

    line_no: int
    status: str
    source_id: Optional[str]
    reason: Optional[str] = None
    record: Union[CopyNumberVariant, StructuralVariant, None] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"line_no": self.line_no, "status": self.status, "source_id": self.source_id, "reason": self.reason}


@dataclass(frozen=True)
class CallSet:
    assembly: str
    record_class: str
    caller: Optional[str]
    caller_in_header: Optional[str]
    source_id: Optional[str]
    call_set_id: Optional[str]
    header_check: Dict[str, Any]
    samples: Tuple[str, ...]
    outcomes: Tuple[RecordOutcome, ...]
    cnvs: Tuple[CopyNumberVariant, ...] = ()
    svs: Tuple[StructuralVariant, ...] = ()

    @property
    def rejected(self) -> Tuple[RecordOutcome, ...]:
        return tuple(o for o in self.outcomes if o.record is None)

    def counts(self) -> Dict[str, int]:
        out = {STATUS_DETECTED: 0, STATUS_UNCERTAIN: 0, STATUS_UNSUPPORTED: 0, STATUS_INVALID: 0}
        for o in self.outcomes:
            out[o.status] += 1
        out["records"] = len(self.outcomes)
        return out

    def to_document(self) -> Dict[str, Any]:
        """
        The COMPACT stored representation: one entry per input record (status, reason, and the validated record
        WITHOUT its `evidence` object). The original bytes remain the single authoritative copy of the source
        text; this document is derived and re-derivable (see verify_document).
        """
        records: List[Dict[str, Any]] = []
        for o in self.outcomes:
            entry: Dict[str, Any] = {
                "line_no": o.line_no,
                "status": o.status,
                "source_id": o.source_id,
                "reason": o.reason,
                "record": None,
            }
            if o.record is not None:
                body = o.record.to_dict()
                body.pop("evidence", None)
                entry["record"] = body
            records.append(entry)
        return {
            "schema_version": CALLSET_DOCUMENT_SCHEMA_VERSION,
            "assembly": self.assembly,
            "record_class": self.record_class,
            "counts": self.counts(),
            "header_check": dict(self.header_check),
            "samples": list(self.samples),
            "caller_in_header": self.caller_in_header,
            "records": records,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assembly": self.assembly,
            "record_class": self.record_class,
            "caller": self.caller,
            "caller_in_header": self.caller_in_header,
            "source_id": self.source_id,
            "call_set_id": self.call_set_id,
            "header_check": dict(self.header_check),
            "samples": list(self.samples),
            "counts": self.counts(),
            "rejected": [o.to_dict() for o in self.rejected],
        }


# ── header ────────────────────────────────────────────────────────────────────


def _parse_kv_list(body: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in body.split(","):
        key, sep, value = part.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _check_header(assembly: str, header: List[str]) -> Dict[str, Any]:
    lengths = {c.name: c.length_bp for c in chromosomes_for(assembly)}
    other = [a for a in SUPPORTED_ASSEMBLIES if a != assembly][0]
    other_lengths = {c.name: c.length_bp for c in chromosomes_for(other)}
    checked = 0
    for line in header:
        m = _CONTIG_LINE.match(line)
        if not m:
            continue
        kv = _parse_kv_list(m.group(1))
        name, length = normalize_contig(kv.get("ID", "")), kv.get("length", "")
        if name is None or not length.isdigit():
            continue
        checked += 1
        if int(length) != lengths[name]:
            hint = f" (that is the {other} length)" if int(length) == other_lengths[name] else ""
            raise CallSetRejected(
                f"header ##contig {kv.get('ID')} length {length} does not match {assembly} ({lengths[name]}){hint}; "
                "the call set is not accepted"
            )
    named = None
    tokens = (
        ("grch37", "GRCh37"),
        ("hg19", "GRCh37"),
        ("b37", "GRCh37"),
        ("grch38", "GRCh38"),
        ("hg38", "GRCh38"),
        ("b38", "GRCh38"),
    )
    for line in header:
        low = line.lower()
        if not (low.startswith("##reference") or low.startswith("##assembly")):
            continue
        for token, build in tokens:
            if token in low:
                named = build
                if build != assembly:
                    raise CallSetRejected(
                        f"header names {build} but the call set was declared {assembly}; not accepted"
                    )
                break
    if checked == 0 and named is None:
        return {
            "status": "unverified",
            "contigs_checked": 0,
            "reference_named": None,
            "note": "the header carries no contig length or reference naming; the declared assembly is unverified",
        }
    return {"status": "verified", "contigs_checked": checked, "reference_named": named, "note": None}


# ── record helpers ────────────────────────────────────────────────────────────


def _parse_info(field_: str) -> Dict[str, str]:
    info: Dict[str, str] = {}
    if field_ in (".", ""):
        return info
    for entry in field_.split(";"):
        if not entry:
            continue
        key, sep, value = entry.partition("=")
        info[key] = value if sep else "true"
    return info


def _first(value: Optional[str]) -> Optional[str]:
    return None if value is None else value.split(",")[0]


def _opt_float(text: Optional[str], name: str) -> Optional[float]:
    if text is None or text in (".", ""):
        return None
    try:
        value = float(text)
    except ValueError:
        raise StructuralValidationError(f"{name} {text!r} is not a number") from None
    if not math.isfinite(value):
        raise StructuralValidationError(f"{name} {text!r} is not finite")
    return value


def _opt_int(text: Optional[str], name: str) -> Optional[int]:
    if text is None or text in (".", ""):
        return None
    try:
        return int(_first(text))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise StructuralValidationError(f"{name} {text!r} is not an integer") from None


def _ci(info: Dict[str, str], key: str) -> Optional[Tuple[int, int]]:
    raw = info.get(key)
    if raw is None:
        return None
    parts = raw.split(",")
    try:
        lo, hi = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        raise StructuralValidationError(f"{key} {raw!r} is not 'low,high'") from None
    if lo > hi:
        raise StructuralValidationError(f"{key} {raw!r}: low exceeds high")
    return lo, hi


def _breakpoint(
    chrom_raw: str, pos: int, ci: Optional[Tuple[int, int]], imprecise: bool, notation: Optional[str] = None
) -> Breakpoint:
    canon = normalize_contig(chrom_raw)
    if ci is not None and ci != (0, 0):
        lo, hi = max(1, pos + ci[0]), pos + ci[1]
        return Breakpoint(canon, chrom_raw, pos, min(lo, pos), max(hi, pos), PRECISION_INTERVAL, notation)
    if imprecise:
        return Breakpoint(canon, chrom_raw, pos, pos, pos, PRECISION_UNQUANTIFIED, notation)
    return Breakpoint(canon, chrom_raw, pos, pos, pos, PRECISION_PRECISE, notation)


def _format_blocks(samples: Tuple[str, ...], fields: List[str]) -> Dict[str, Dict[str, str]]:
    if len(fields) < 10:
        return {}
    keys = fields[8].split(":")
    out: Dict[str, Dict[str, str]] = {}
    for name, column in zip(samples, fields[9:]):
        values = column.split(":")
        out[name] = {k: values[i] for i, k in enumerate(keys) if i < len(values)}
    return out


@dataclass
class _Ctx:
    assembly: str
    record_class: str
    caller: Optional[str]
    call_set_id: Optional[str]
    samples: Tuple[str, ...]


def _status(filter_: Optional[str], warnings: List[str]) -> str:
    return STATUS_DETECTED if filter_ == "PASS" and not warnings else STATUS_UNCERTAIN


def _end_from(info: Dict[str, str], pos: int, warnings: List[str], what: str) -> int:
    end = _opt_int(info.get("END"), "END")
    if end is not None:
        return end
    svlen = _opt_int(info.get("SVLEN"), "SVLEN")
    if svlen is None or svlen == 0:
        raise StructuralValidationError(f"{what} has neither END nor a usable SVLEN; the extent is not stated")
    warnings.append("END was not given; derived from POS + |SVLEN|")
    return pos + abs(svlen)


def _evidence(fields: List[str], info: Dict[str, str], ctx: _Ctx, alt: str, line: str) -> Dict[str, Any]:
    return {
        "ref": fields[3][:200],
        "alt": alt,
        "info": dict(info),
        "format": _format_blocks(ctx.samples, fields),
        "raw_record": line[:_RAW_RECORD_LIMIT],
    }


# ── CNV record ────────────────────────────────────────────────────────────────


def _parse_cnv(fields: List[str], info: Dict[str, str], ctx: _Ctx, line_no: int, line: str) -> RecordOutcome:
    chrom_raw, pos = fields[0], int(fields[1])
    vid = fields[2] if fields[2] != "." else None
    alt = fields[4]
    if "," in alt:
        return RecordOutcome(line_no, STATUS_UNSUPPORTED, vid, "multi-allelic structural ALT")
    m = _SYMBOLIC.match(alt)
    if not m:
        return RecordOutcome(line_no, STATUS_UNSUPPORTED, vid, "not a symbolic copy-number allele")
    alt_class = m.group(1).upper()
    cn_from_alt: Optional[int] = None
    cn_match = _CN_ALT.match(alt_class)
    if cn_match:
        alt_class, cn_from_alt = "CNV", int(cn_match.group(1))
    svtype = (info.get("SVTYPE") or alt_class).upper()
    if info.get("SVTYPE") and svtype != alt_class:
        raise StructuralValidationError(f"SVTYPE={svtype} contradicts ALT {alt}")
    types = {"DEL": CNV_DELETION, "DUP": CNV_DUPLICATION, "CNV": CNV_CHANGE}
    if svtype not in types:
        return RecordOutcome(
            line_no, STATUS_UNSUPPORTED, vid, f"{svtype} is not a copy-number class (call set declared 'cnv')"
        )
    canon = normalize_contig(chrom_raw)
    if canon is None:
        return RecordOutcome(
            line_no, STATUS_UNSUPPORTED, vid, f"contig {chrom_raw!r} is not chromosome 1-22, X, Y or MT"
        )

    warnings: List[str] = []
    end = _end_from(info, pos, warnings, "copy-number record")
    imprecise = "IMPRECISE" in info
    start_bp = _breakpoint(chrom_raw, pos, _ci(info, "CIPOS"), imprecise)
    end_bp = _breakpoint(chrom_raw, end, _ci(info, "CIEND"), imprecise)

    cn: Optional[float] = _opt_float(info.get("CN"), "CN")
    cn_source = "INFO:CN" if cn is not None else None
    fmt = _format_blocks(ctx.samples, fields)
    if cn is None and len(fmt) == 1:
        only = next(iter(fmt.values()))
        if "CN" in only:
            cn = _opt_float(only["CN"], "FORMAT CN")
            cn_source = "FORMAT:CN" if cn is not None else None
    if cn_from_alt is not None:
        if cn is not None and cn != cn_from_alt:
            raise StructuralValidationError(f"ALT {alt} states copy number {cn_from_alt} but CN={cn:g}")
        if cn is None:
            cn, cn_source = float(cn_from_alt), "ALT"
    log2 = _opt_float(info.get("LOG2RATIO", info.get("LOG2")), "LOG2RATIO")
    qual = _opt_float(fields[5], "QUAL")
    filt = None if fields[6] == "." else fields[6]
    if svtype == "CNV" and cn is None and log2 is None:
        warnings.append("copy-number change reported with neither CN nor a log2 ratio")

    cnv = CopyNumberVariant(
        assembly=ctx.assembly,
        chrom=canon,
        start=pos,
        end=end,
        cnv_type=types[svtype],
        status=_status(filt, warnings),
        caller=ctx.caller,
        source_id=vid,
        call_set_id=ctx.call_set_id,
        copy_number=cn,
        copy_number_source=cn_source,
        log2_ratio=log2,
        quality=qual,
        filter=filt,
        start_breakpoint=start_bp,
        end_breakpoint=end_bp,
        evidence=_evidence(fields, info, ctx, alt, line),
        line_no=line_no,
        warnings=tuple(warnings),
    )
    return RecordOutcome(line_no, cnv.status, vid, None, cnv)


# ── SV record ─────────────────────────────────────────────────────────────────


def _parse_sv(fields: List[str], info: Dict[str, str], ctx: _Ctx, line_no: int, line: str) -> RecordOutcome:
    chrom_raw, pos = fields[0], int(fields[1])
    vid = fields[2] if fields[2] != "." else None
    alt = fields[4]
    if "," in alt:
        return RecordOutcome(line_no, STATUS_UNSUPPORTED, vid, "multi-allelic structural ALT")
    declared = info.get("SVTYPE", "").upper() or None

    sv_type: Optional[str] = None
    notation: Optional[str] = None
    partner: Optional[Tuple[str, int]] = None
    single = False
    inserted: Optional[str] = None
    sym = _SYMBOLIC.match(alt)
    if sym:
        base = sym.group(1).upper()
        if _CN_ALT.match(base) or base == "CNV":
            return RecordOutcome(
                line_no, STATUS_UNSUPPORTED, vid, f"{alt} is a copy-number allele (call set declared 'sv')"
            )
        if base not in ("DEL", "DUP", "INS", "INV", "BND", "TRA"):
            return RecordOutcome(line_no, STATUS_UNSUPPORTED, vid, f"structural class {base} is not supported")
        sv_type = base
    else:
        m1, m2 = _BND_PREFIX.match(alt), _BND_SUFFIX.match(alt)
        if m1:
            sv_type, notation, partner = "BND", f"t{m1.group(2)}p{m1.group(2)}", (m1.group(3), int(m1.group(4)))
        elif m2:
            sv_type, notation, partner = "BND", f"{m2.group(1)}p{m2.group(1)}t", (m2.group(2), int(m2.group(3)))
        elif _SINGLE_BREAKEND.match(alt):
            sv_type, single = "BND", True
        elif declared == "INS" and _LITERAL_SEQUENCE.match(alt):
            sv_type, inserted = "INS", alt.upper()
        else:
            return RecordOutcome(
                line_no, STATUS_UNSUPPORTED, vid, "not a structural allele (looks like a small variant)"
            )
    if declared and declared != sv_type:
        raise StructuralValidationError(f"SVTYPE={declared} contradicts the allele {alt!r} ({sv_type})")

    canon = normalize_contig(chrom_raw)
    if canon is None:
        return RecordOutcome(
            line_no, STATUS_UNSUPPORTED, vid, f"contig {chrom_raw!r} is not chromosome 1-22, X, Y or MT"
        )

    warnings: List[str] = []
    imprecise = "IMPRECISE" in info
    own = _breakpoint(chrom_raw, pos, _ci(info, "CIPOS"), imprecise, notation if sv_type == "BND" else None)
    svlen = _opt_int(info.get("SVLEN"), "SVLEN")
    mate_raw, event = info.get("MATEID"), info.get("EVENT")
    if mate_raw and "," in mate_raw:
        warnings.append("more than one MATEID given; only the first is kept (all are in evidence)")
    breakpoints: Tuple[Breakpoint, ...]
    partner_status: Optional[str] = None

    if sv_type in ("DEL", "DUP", "INV"):
        chr2 = info.get("CHR2")
        if chr2 and normalize_contig(chr2) != canon:
            raise StructuralValidationError(f"{sv_type} cannot have CHR2={chr2} on a different chromosome")
        end = _end_from(info, pos, warnings, sv_type)
        if svlen is not None and abs(svlen) != end - pos:
            warnings.append(f"|SVLEN|={abs(svlen)} disagrees with END-POS={end - pos}")
        breakpoints = (own, _breakpoint(chrom_raw, end, _ci(info, "CIEND"), imprecise))
    elif sv_type == "INS":
        breakpoints = (own,)
    elif sv_type == "TRA":
        chr2, pos2 = info.get("CHR2"), _opt_int(info.get("POS2", info.get("END")), "POS2/END")
        if not chr2 or pos2 is None:
            raise StructuralValidationError(
                "TRA needs CHR2 and POS2 (or END); the partner is not stated and is not guessed"
            )
        breakpoints, partner_status = (own, _breakpoint(chr2, pos2, _ci(info, "CIEND"), imprecise)), PARTNER_GIVEN
    else:  # BND
        if partner is None and not single:  # symbolic <BND>
            chr2, pos2 = info.get("CHR2"), _opt_int(info.get("POS2"), "POS2")
            if chr2 and pos2 is not None:
                partner = (chr2, pos2)
        if single:
            breakpoints, partner_status = (own,), PARTNER_SINGLE
        elif partner is None:
            breakpoints, partner_status = (own,), PARTNER_MISSING
            warnings.append("breakend partner location not stated")
        else:
            pbp = _breakpoint(partner[0], partner[1], _ci(info, "CIEND"), imprecise)
            breakpoints = (own, pbp)
            partner_status = PARTNER_GIVEN if pbp.chrom is not None else PARTNER_UNPLACED
            if pbp.chrom is None:
                warnings.append(f"partner contig {partner[0]!r} is not placed on chromosomes 1-22, X, Y, MT")
            else:
                from .reference import get_chromosome

                if partner[1] > get_chromosome(ctx.assembly, pbp.chrom).length_bp:  # type: ignore[union-attr]
                    raise StructuralValidationError(
                        f"partner position {partner[1]} is beyond the end of {pbp.chrom} in {ctx.assembly}"
                    )

    qual = _opt_float(fields[5], "QUAL")
    filt = None if fields[6] == "." else fields[6]
    sv = StructuralVariant(
        assembly=ctx.assembly,
        sv_type=sv_type,
        status=_status(filt, warnings),
        breakpoints=breakpoints,
        partner_status=partner_status,
        caller=ctx.caller,
        source_id=vid,
        call_set_id=ctx.call_set_id,
        mate_id=_first(mate_raw),
        event_id=event,
        sv_length=svlen,
        inserted_sequence=inserted,
        quality=qual,
        filter=filt,
        evidence=_evidence(fields, info, ctx, alt, line),
        line_no=line_no,
        warnings=tuple(warnings),
    )
    return RecordOutcome(line_no, sv.status, vid, None, sv)


# ── entry point ───────────────────────────────────────────────────────────────


def parse_structural_vcf(
    source: Union[str, Iterable[str]],
    *,
    assembly: str,
    record_class: str,
    caller: Optional[str] = None,
    source_id: Optional[str] = None,
    call_set_id: Optional[str] = None,
    max_records: Optional[int] = None,
) -> CallSet:
    """
    Parse a CNV/SV VCF. Raises CallSetRejected for a file-level problem; per-record problems are outcomes.
    `max_records` rejects a file holding more data lines than the cap BEFORE any record is parsed.
    """
    canon_assembly = canonical_assembly(assembly)
    if canon_assembly is None:
        raise CallSetRejected(f"assembly {assembly!r} is not GRCh37 or GRCh38; no liftover is supported")
    if record_class not in (RECORD_CLASS_CNV, RECORD_CLASS_SV):
        raise CallSetRejected("record_class must be 'cnv' or 'sv'")
    lines = source.splitlines() if isinstance(source, str) else [ln.rstrip("\r\n") for ln in source]

    header: List[str] = []
    samples: Tuple[str, ...] = ()
    column_line_seen = False
    data: List[Tuple[int, str]] = []
    for no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if line.startswith("##"):
            header.append(line)
        elif line.startswith("#CHROM"):
            cols = line.lstrip("#").split("\t")
            if cols[:8] != ["CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO"]:
                raise CallSetRejected("the #CHROM header line does not have the VCF columns")
            samples, column_line_seen = tuple(cols[9:]), True
        elif line.startswith("#"):
            header.append(line)
        else:
            if not column_line_seen:
                raise CallSetRejected(f"line {no}: data before the #CHROM header line")
            data.append((no, line))
    if not column_line_seen:
        raise CallSetRejected("no #CHROM header line; not a VCF")
    if max_records is not None and len(data) > max_records:
        raise CallSetRejected(f"the file holds {len(data)} records; the configured limit is {max_records}")

    header_check = _check_header(canon_assembly, header)
    in_header = None
    for h in header:
        if h.lower().startswith("##source="):
            in_header = h.split("=", 1)[1][:200]
            break

    ctx = _Ctx(canon_assembly, record_class, caller, call_set_id, samples)
    outcomes: List[RecordOutcome] = []
    cnvs: List[CopyNumberVariant] = []
    svs: List[StructuralVariant] = []
    for no, line in data:
        fields = line.split("\t")
        vid = fields[2] if len(fields) > 2 and fields[2] != "." else None
        try:
            if len(fields) < 8:
                raise StructuralValidationError(f"{len(fields)} columns; a VCF record needs at least 8")
            if not fields[1].isdigit() or int(fields[1]) < 1:
                raise StructuralValidationError(f"POS {fields[1]!r} is not a positive integer")
            info = _parse_info(fields[7])
            outcome = (_parse_cnv if record_class == RECORD_CLASS_CNV else _parse_sv)(fields, info, ctx, no, line)
        except StructuralValidationError as exc:
            outcome = RecordOutcome(no, STATUS_INVALID, vid, str(exc))
        outcomes.append(outcome)
        if isinstance(outcome.record, CopyNumberVariant):
            cnvs.append(outcome.record)
        elif isinstance(outcome.record, StructuralVariant):
            svs.append(outcome.record)

    return CallSet(
        assembly=canon_assembly,
        record_class=record_class,
        caller=caller,
        caller_in_header=in_header,
        source_id=source_id,
        call_set_id=call_set_id,
        header_check=header_check,
        samples=samples,
        outcomes=tuple(outcomes),
        cnvs=tuple(cnvs),
        svs=tuple(svs),
    )


def callset_from_document(
    document: Dict[str, Any],
    *,
    caller: Optional[str] = None,
    source_id: Optional[str] = None,
    call_set_id: Optional[str] = None,
) -> CallSet:
    """
    Rebuild a CallSet from a stored COMPACT document through the validating constructors. A document that no
    longer satisfies the models raises StructuralValidationError instead of being drawn. Evidence is not stored,
    so it is empty on every rehydrated record.
    """
    if document.get("schema_version") != CALLSET_DOCUMENT_SCHEMA_VERSION:
        raise CallSetRejected("unsupported call-set document schema version")
    outcomes: List[RecordOutcome] = []
    cnvs: List[CopyNumberVariant] = []
    svs: List[StructuralVariant] = []
    for entry in document["records"]:
        record: Union[CopyNumberVariant, StructuralVariant, None] = None
        body = entry.get("record")
        if body is not None:
            body = {**body, "caller": caller, "call_set_id": call_set_id}
            if body.get("kind") == "cnv":
                record = CopyNumberVariant.from_dict(body)
                cnvs.append(record)
            else:
                record = StructuralVariant.from_dict(body)
                svs.append(record)
        outcomes.append(
            RecordOutcome(entry["line_no"], entry["status"], entry.get("source_id"), entry.get("reason"), record)
        )
    return CallSet(
        assembly=document["assembly"],
        record_class=document["record_class"],
        caller=caller,
        caller_in_header=document.get("caller_in_header"),
        source_id=source_id,
        call_set_id=call_set_id,
        header_check=dict(document.get("header_check") or {}),
        samples=tuple(document.get("samples") or ()),
        outcomes=tuple(outcomes),
        cnvs=tuple(cnvs),
        svs=tuple(svs),
    )


def verify_document(source: Union[str, bytes], document: Dict[str, Any]) -> bool:
    """
    Integrity check: re-parse the ORIGINAL bytes with the recorded rules and require the result to equal the stored
    derived document. A mismatch is a nonconformance to report, never something to repair silently.
    """
    text = source.decode("utf-8") if isinstance(source, bytes) else source
    try:
        again = parse_structural_vcf(text, assembly=document["assembly"], record_class=document["record_class"])
    except (CallSetRejected, KeyError):
        return False
    return again.to_document() == document


# ── parser rule-set registry ──────────────────────────────────────────────────
#
# A stored call set is IMMUTABLE EVIDENCE: its bytes, hash, derived document and the `parser_version` that produced
# it never change. So a parser change must never reach back into stored evidence, and it must not strand a call set
# that is still waiting for validation. The policy is therefore:
#
#   * every rule set that has ever WRITTEN a call set stays registered here, unchanged, for as long as any call set
#     recorded under it can still be validated or re-verified (a test pins that CALLSET_PARSER_VERSION is registered
#     and that every registered rule set rebuilds a document equal to the one it wrote);
#   * a new submission is parsed by the CURRENT rule set (CALLSET_PARSER_VERSION);
#   * validation re-derives the document with the RECORDED version's rule set, so a pending call set is validated
#     against exactly the evidence it was submitted with, never against a silently regenerated one;
#   * an old rule set is retired only by deciding what happens to the call sets still recorded under it, which is a
#     deliberate act, not a side effect of editing the parser.
#
# To change parsing semantics: add "structural_vcf/2" with its own function, bump CALLSET_PARSER_VERSION, keep the old
# entry. Do not edit the function registered under an existing version.
PARSER_RULESETS = {"structural_vcf/1": parse_structural_vcf}


def verify_document_for_version(
    parser_version: str, source: Union[str, bytes], document: Dict[str, Any]
) -> Optional[bool]:
    """
    Re-derive `document` from `source` with the rule set recorded as `parser_version`. Returns None when that rule set
    is not registered (the caller must treat it as not verifiable, never as verified), otherwise whether it matches.
    """
    parse = PARSER_RULESETS.get(parser_version)
    if parse is None:
        return None
    text = source.decode("utf-8") if isinstance(source, bytes) else source
    try:
        again = parse(text, assembly=document["assembly"], record_class=document["record_class"])
    except (CallSetRejected, KeyError):
        return False
    return again.to_document() == document
