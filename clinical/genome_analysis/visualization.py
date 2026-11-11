"""
clinical/genome_analysis/visualization.py
─────────────────────────────────────────
Pure builder for the Chromosome Explorer's data: one JSON-ready map per genome
assembly, with a track ("layer") per evidence type on each chromosome.

LAYERS AND WHAT THEIR STATUSES MEAN
    small_variants   from the Phase 1 summary: variants_present /
                     no_variants_reported / not_assessed
    cnv, sv          findings_present  at least one call lands on this chromosome
                     no_calls_reported a call set covers this assembly and has no
                                       call here -- NOT "normal": a caller's
                                       callable regions are not recorded
                     not_assessed      no call set for this assembly exists

There is no "normal", "diploid" or "no abnormality" value anywhere in the output.
`karyotype` is always `not_assessed`: a karyotype is a cytogenetic result and is
not derivable from variant data of any kind.

CALL SETS ARE KEPT PER ASSEMBLY. Coordinates from a call set whose assembly differs
from the map's are never drawn on it; they are listed in `excluded_call_sets` with
the reason. There is no liftover.

WHAT IS DRAWN, honestly:
    * a CNV is a segment [start, end] with a lane, so overlapping segments stack
      instead of hiding each other (they are also listed in `overlaps`);
    * an uncertain end is a RANGE (`*_ci_x0`..`*_ci_x1`), never a point;
    * a breakend with a placed partner yields a link; one without yields a lone
      marker and says why (`partner_status`);
    * a link between two ends is drawn between them, and is NOT labelled a
      translocation -- only the source's own TRA label is carried through.
Positions are given both as bp and as a 0..1 fraction of the chromosome so a
renderer needs no reference table.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .reference import chromosomes_for
from .structural import (
    STATUS_NOT_ASSESSED,
    Breakpoint,
    CopyNumberVariant,
    StructuralVariant,
    assign_lanes,
    pair_breakends,
    relate_cnvs,
)
from .structural_vcf import RECORD_CLASS_CNV, RECORD_CLASS_SV, CallSet
from .summary import AnalysisSummary

LAYER_FINDINGS = "findings_present"
LAYER_NO_CALLS = "no_calls_reported"
LAYER_NOT_ASSESSED = "not_assessed"

MAP_LIMITATIONS: Tuple[str, ...] = (
    "Only calls from the attached call sets are drawn. A chromosome with no call is NOT shown as normal: "
    "the regions a caller could assess are not recorded.",
    "Copy number is shown only where the source reported it; a missing value is not a diploid value.",
    "Breakpoints marked as a range are uncertain; the range is the source's, not a confirmation.",
    "A link between two breakends is not by itself a translocation.",
    "No karyotype is determined or implied, and sex is not inferred from the sex chromosomes.",
    "Coordinates are valid only in the stated assembly; there is no liftover.",
)

LEGEND: Tuple[Dict[str, str], ...] = (
    {"kind": "small_variant", "label": "Small variant", "glyph": "tick"},
    {"kind": "deletion", "label": "Deletion", "glyph": "segment-below"},
    {"kind": "duplication", "label": "Duplication", "glyph": "segment-above"},
    {"kind": "copy_number_change", "label": "Copy-number change (direction not stated)", "glyph": "segment-hatched"},
    {"kind": "insertion", "label": "Insertion", "glyph": "triangle"},
    {"kind": "inversion", "label": "Inversion", "glyph": "bracket"},
    {"kind": "breakend", "label": "Breakend", "glyph": "diamond"},
    {"kind": "link", "label": "Breakend pair / partner", "glyph": "arc"},
)

_SV_KIND = {
    "DEL": "deletion",
    "DUP": "duplication",
    "INS": "insertion",
    "INV": "inversion",
    "BND": "breakend",
    "TRA": "translocation",
}


def _frac(pos: int, length: int) -> float:
    return round(min(max(pos / length, 0.0), 1.0), 9)


def _bp_json(bp: Breakpoint, length: Optional[int]) -> Dict[str, Any]:
    out = bp.to_dict()
    if length:
        out["frac"] = _frac(bp.pos, length)
        out["ci_low_frac"] = _frac(bp.ci_low, length)
        out["ci_high_frac"] = _frac(bp.ci_high, length)
    return out


def _in_region(start: int, end: int, region: Optional[Tuple[int, int]]) -> bool:
    return region is None or (start <= region[1] and region[0] <= end)


def build_chromosome_map(
    *,
    assembly: str,
    summary: Optional[AnalysisSummary],
    call_sets: Sequence[CallSet] = (),
    include_sex_chromosomes: bool = True,
    only_chromosome: Optional[str] = None,
    region: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    """
    `summary` is the Phase 1 small-variant summary for THIS assembly (or None);
    a summary of another assembly is refused rather than drawn. `region`
    (start, end) restricts every layer to findings overlapping it and requires
    `only_chromosome`.
    """
    if summary is not None and summary.assembly not in (None, assembly):
        raise ValueError("the small-variant summary is for a different assembly than the map")
    if region is not None and only_chromosome is None:
        raise ValueError("a region needs a chromosome")

    matching = [cs for cs in call_sets if cs.assembly == assembly]
    excluded = [
        {
            "call_set_id": cs.call_set_id,
            "assembly": cs.assembly,
            "record_class": cs.record_class,
            "reason": f"call set is {cs.assembly}; this map is {assembly}; coordinates are not converted",
        }
        for cs in call_sets
        if cs.assembly != assembly
    ]
    cnv_sets = [cs for cs in matching if cs.record_class == RECORD_CLASS_CNV]
    sv_sets = [cs for cs in matching if cs.record_class == RECORD_CLASS_SV]
    cnvs: List[Tuple[str, CopyNumberVariant]] = []
    for i, cs in enumerate(cnv_sets):
        cnvs += [(f"cnv:{cs.call_set_id or i}:{c.line_no}", c) for c in cs.cnvs]
    svs: List[Tuple[str, StructuralVariant]] = []
    for i, cs in enumerate(sv_sets):
        svs += [(f"sv:{cs.call_set_id or i}:{s.line_no}", s) for s in cs.svs]

    lengths = {c.name: c.length_bp for c in chromosomes_for(assembly)}
    refs = [c for c in chromosomes_for(assembly) if include_sex_chromosomes or c.name not in ("X", "Y")]
    if only_chromosome is not None:
        refs = [c for c in refs if c.name == only_chromosome]

    small_by_chrom = {c.chromosome.name: c for c in summary.chromosomes} if summary is not None else {}
    chromosomes: List[Dict[str, Any]] = []
    for ref in refs:
        length = ref.length_bp
        # small variants
        sm = small_by_chrom.get(ref.name)
        if sm is None:
            small_layer: Dict[str, Any] = {"status": LAYER_NOT_ASSESSED, "markers": []}
        else:
            markers = [
                {
                    "id": v["variant_key"],
                    "pos": v["pos"],
                    "frac": _frac(v["pos"], length),
                    "variant_type": v["variant_type"],
                }
                for v in sm.variants
                if _in_region(v["pos"], v["pos"], region)
            ]
            small_layer = {"status": sm.status, "count": len(sm.variants), "markers": markers}

        # CNV
        here = [(fid, c) for fid, c in cnvs if c.chrom == ref.name and _in_region(c.start, c.end, region)]
        if not cnv_sets:
            cnv_layer: Dict[str, Any] = {"status": LAYER_NOT_ASSESSED, "segments": [], "overlaps": []}
        else:
            lanes = assign_lanes([(c.start, c.end) for _, c in here])
            segs = []
            for (fid, c), lane in zip(here, lanes):
                d = c.to_dict()
                d.update(id=fid, lane=lane, kind_class=c.cnv_type, x0=_frac(c.start, length), x1=_frac(c.end, length))
                for key, bp in (("start_breakpoint", c.start_breakpoint), ("end_breakpoint", c.end_breakpoint)):
                    if bp is not None:
                        d[key] = _bp_json(bp, length)
                segs.append(d)
            overlaps = [
                {"a": segs[r["a"]]["id"], "b": segs[r["b"]]["id"], "relation": r["relation"]}
                for r in relate_cnvs([c for _, c in here])
            ]
            cnv_layer = {"status": LAYER_FINDINGS if segs else LAYER_NO_CALLS, "segments": segs, "overlaps": overlaps}

        # SV
        sv_layer: Dict[str, Any]
        if not sv_sets:
            sv_layer = {"status": LAYER_NOT_ASSESSED, "intervals": [], "breakends": []}
        else:
            intervals: List[Dict[str, Any]] = []
            breakends: List[Dict[str, Any]] = []
            for fid, sv in svs:
                kind = _SV_KIND[sv.sv_type]
                if sv.sv_type in ("DEL", "DUP", "INV"):
                    a, b = sv.breakpoints
                    if a.chrom == ref.name and _in_region(a.pos, b.pos, region):
                        intervals.append(
                            {
                                "id": fid,
                                "sv_type": sv.sv_type,
                                "kind_class": kind,
                                "status": sv.status,
                                "start": _bp_json(a, length),
                                "end": _bp_json(b, length),
                                "x0": _frac(a.pos, length),
                                "x1": _frac(b.pos, length),
                                "sv_length": sv.sv_length,
                                "caller": sv.caller,
                                "warnings": list(sv.warnings),
                                "source_id": sv.source_id,
                            }
                        )
                else:
                    for idx, bp in enumerate(sv.breakpoints):
                        if bp.chrom == ref.name and _in_region(bp.ci_low, bp.ci_high, region):
                            breakends.append(
                                {
                                    "id": fid,
                                    "sv_type": sv.sv_type,
                                    "kind_class": kind,
                                    "status": sv.status,
                                    "end": "own" if idx == 0 else "partner",
                                    "breakpoint": _bp_json(bp, length),
                                    "partner_status": sv.partner_status,
                                    "mate_id": sv.mate_id,
                                    "event_id": sv.event_id,
                                    "inserted_length": sv.sv_length if sv.sv_type == "INS" else None,
                                    "caller": sv.caller,
                                    "warnings": list(sv.warnings),
                                    "source_id": sv.source_id,
                                }
                            )
            # keep deterministic order: by position
            intervals.sort(key=lambda r: (r["start"]["pos"], r["id"]))
            breakends.sort(key=lambda r: (r["breakpoint"]["pos"], r["id"], r["end"]))
            sv_layer = {
                "status": LAYER_FINDINGS if (intervals or breakends) else LAYER_NO_CALLS,
                "intervals": intervals,
                "breakends": breakends,
            }

        chromosomes.append(
            {
                "name": ref.name,
                "kind": ref.kind,
                "length_bp": length,
                "assembly": assembly,
                "refseq_accession": ref.refseq_accession,
                "layers": {"small_variants": small_layer, "cnv": cnv_layer, "sv": sv_layer},
            }
        )

    # links between two placed ends (both on chromosomes 1-22/X/Y/MT)
    ordered = [sv for _, sv in svs]
    pairing = pair_breakends(ordered)
    paired_idx = {i for pair in pairing.paired for i in pair}
    links: List[Dict[str, Any]] = []
    shown = {c["name"] for c in chromosomes}
    for idx, (fid, sv) in enumerate(svs):
        if sv.sv_type not in ("BND", "TRA") or len(sv.breakpoints) != 2:
            continue
        a, b = sv.breakpoints
        if a.chrom is None or b.chrom is None or not ({a.chrom, b.chrom} & shown):
            continue
        if region is not None and not (
            (a.chrom == only_chromosome and _in_region(a.ci_low, a.ci_high, region))
            or (b.chrom == only_chromosome and _in_region(b.ci_low, b.ci_high, region))
        ):
            continue
        links.append(
            {
                "id": fid,
                "sv_type": sv.sv_type,
                "status": sv.status,
                "partner_status": sv.partner_status,
                "a": _bp_json(a, lengths.get(a.chrom)),
                "b": _bp_json(b, lengths.get(b.chrom)),
                "is_interchromosomal": sv.is_interchromosomal,
                "mate_paired": idx in paired_idx,
                "caller": sv.caller,
                "warnings": list(sv.warnings),
            }
        )

    return {
        "assembly": assembly,
        "coordinate_system": "1-based, inclusive (VCF); positions also given as a 0..1 fraction of the chromosome",
        "chromosomes": chromosomes,
        "links": links,
        "breakend_pairing": {
            "paired_pairs": len(pairing.paired),
            "mate_missing": len(pairing.mate_missing),
            "inconsistent_pairs": len(pairing.inconsistent),
            "no_mate_named": len(pairing.unpaired),
        },
        "call_sets": [cs.to_dict() for cs in matching],
        "excluded_call_sets": excluded,
        "karyotype": {
            "status": STATUS_NOT_ASSESSED,
            "reason": "No validated cytogenetic result is attached; a karyotype cannot be derived from variant data.",
        },
        "legend": [dict(item) for item in LEGEND],
        "limitations": list(MAP_LIMITATIONS),
    }
