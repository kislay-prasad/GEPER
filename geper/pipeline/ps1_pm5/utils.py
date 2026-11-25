"""
Adapters for PS1/PM5: parsing ClinVar's HGVS.p protein-change notation
out of its esummary `title` field, and turning a raw esummary entry
into a `ClinVarCodonMatch`.

Nothing here makes a clinical judgement -- that is
`pipeline/ps1_pm5/decision.py`'s job. This module only reshapes data,
matching the division of responsibility `pipeline/pvs1/utils.py`
already establishes for that package.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pipeline.ps1_pm5.models import ClinVarCodonMatch, is_conflicting, star_rating
from utils.clinvar_records import is_single_allele_record

# Standard three-letter -> one-letter amino acid code, plus ClinVar's
# stop-codon notations ("Ter", "*", "X" all appear across ClinVar's
# title history). PS1/PM5 both compare single amino-acid substitutions
# only (see `decision.py`), so a stop-codon "protein change" is parsed
# here (for completeness/negative-testing) but the decision layer
# rejects it -- a nonsense match at the same codon is PVS1/PM5-null
# territory, not PS1/PM5's missense-vs-missense comparison.
_THREE_TO_ONE = {
    "Ala": "A",
    "Arg": "R",
    "Asn": "N",
    "Asp": "D",
    "Cys": "C",
    "Gln": "Q",
    "Glu": "E",
    "Gly": "G",
    "His": "H",
    "Ile": "I",
    "Leu": "L",
    "Lys": "K",
    "Met": "M",
    "Phe": "F",
    "Pro": "P",
    "Ser": "S",
    "Thr": "T",
    "Trp": "W",
    "Tyr": "Y",
    "Val": "V",
    "Ter": "*",
    "Sec": "U",
    "Pyl": "O",
}

# ClinVar HGVS.p titles look like "...(p.Arg175His)" for a substitution,
# "...(p.Arg175=)" for a synonymous change (ClinVar's own notation for
# "no change"), or "...(p.Arg175Ter)"/"(p.Arg175*)" for a nonsense
# change. Frameshift/del/dup/ins consequences use a different suffix
# ("fs", "del", "dup", "ins") this pattern intentionally does not
# match -- PS1/PM5 only ever compare single-residue substitutions, so a
# non-substitution title is correctly treated as "not parseable" here
# rather than partially matched.
_PROTEIN_CHANGE_RE = re.compile(r"\(p\.([A-Za-z]{3})(\d+)(=|Ter|\*|[A-Za-z]{3})\)")


GRCH37 = "GRCh37"
GRCH38 = "GRCh38"

# Spellings the PS1/PM5 path accepts for each build (matched case-insensitively, whole string; surrounding
# whitespace ignored). Approved policy: GRCh37/GRCH37/hg19/b37 -> GRCh37, GRCh38/hg38/b38 -> GRCh38. Anything
# else -- "37", "38", "GRCh38.p13", "GRCh37.p13", "GRCh-37", "grch_37", "GRCh36", "" -- is UNRESOLVED, never
# silently GRCh38 (ClinVar audit C3). Lower-case/whitespace tolerance is kept from the original C3
# implementation; whether to narrow it to the literal list is an open policy question (see the Phase 0 audit).
_BUILD_ALIASES = {
    "grch37": GRCH37,
    "hg19": GRCH37,
    "b37": GRCH37,
    "grch38": GRCH38,
    "hg38": GRCH38,
    "b38": GRCH38,
}


def canonical_build(assembly: Any) -> Optional[str]:
    """
    "GRCh37" or "GRCh38" for a supported assembly spelling (GRCh37/GRCH37/hg19/b37, GRCh38/hg38/b38), else None
    (unresolved: None, "", "37", "GRCh38.p14", ...). Scope: the PS1/PM5 path only; the rest of the pipeline
    normalises assemblies on its own.
    """
    if not isinstance(assembly, str):
        return None
    return _BUILD_ALIASES.get(assembly.strip().lower())


def _positions_by_build(variation_loc: Any, spdi_grch38_pos: int) -> Dict[str, int]:
    """
    ClinVar's own placement of one component in each build, from `variation_loc[]` (`assembly_name`, 1-based
    `start`). Only GRCh37/GRCh38 entries with a numeric start are kept. If ClinVar's GRCh38 start disagrees
    with the SPDI position the record's coordinates are internally inconsistent and nothing is trusted
    (empty result): a later identity check then reports "cannot be verified" instead of guessing.
    """
    found: Dict[str, int] = {}
    for loc in variation_loc if isinstance(variation_loc, list) else []:
        if not isinstance(loc, dict):
            continue
        name = loc.get("assembly_name")
        start = str(loc.get("start") if loc.get("start") is not None else "").strip()
        if name in (GRCH37, GRCH38) and start.isdigit() and name not in found:
            found[name] = int(start)
    if GRCH38 in found and found[GRCH38] != spdi_grch38_pos:
        return {}
    return found


def parse_protein_change(title: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Parse `(p.Arg175His)`-style HGVS.p notation out of a ClinVar
    esummary `title`. Returns `{"ref_aa", "alt_aa", "codon_number"}`
    (single-letter amino acids) or None if the title has no
    parseable substitution/synonymous/nonsense protein change (e.g. a
    frameshift, an intronic-only variant with no `(p....)` suffix at
    all, or an unrecognized three-letter code).
    """
    if not title:
        return None
    match = _PROTEIN_CHANGE_RE.search(title)
    if not match:
        return None
    ref_three, codon_str, alt_token = match.groups()
    ref_aa = _THREE_TO_ONE.get(ref_three)
    if ref_aa is None:
        return None
    alt_aa = ref_aa if alt_token == "=" else _THREE_TO_ONE.get(alt_token)
    if alt_aa is None:
        return None
    return {"ref_aa": ref_aa, "alt_aa": alt_aa, "codon_number": int(codon_str)}


def clinvar_codon_match_from_esummary(uid: str, entry: Dict[str, Any]) -> Optional[ClinVarCodonMatch]:
    """
    Build a `ClinVarCodonMatch` from one ClinVar esummary entry (the
    same shape `database/clinvar_client.py::ClinVarClient._esummary`
    already parses `clinical_significance`/`review_status`/`condition`
    out of), plus the genomic pos/ref/alt from its `variation_set`.

    Returns None when the entry lacks a parseable single-residue
    protein change or a `canonical_spdi` genomic position -- e.g. an
    intronic-only ClinVar record with no molecular consequence at the
    protein level, which is simply not usable evidence for PS1/PM5 (not
    an error; every other qualifying record at the same codon is
    unaffected).
    """
    # C2: only a recognised single-allele record can be a codon anchor. Haplotype, diplotype and
    # other multi-variant records (and unrecognised or missing record types) are never anchors.
    # Their titles happened not to parse as a protein change, which was accidental protection;
    # `esummary` also carries a `protein_change` field on them.
    variation_set_entry = entry.get("variation_set")
    if not is_single_allele_record(
        entry.get("obj_type"), len(variation_set_entry) if isinstance(variation_set_entry, list) else None
    ):
        return None

    title = entry.get("title")
    parsed = parse_protein_change(title)
    if parsed is None:
        return None

    variation_set = entry.get("variation_set") or []
    pos = ref = alt = None
    pos_by_build: Dict[str, int] = {}
    for v in variation_set:
        spdi = v.get("canonical_spdi") if isinstance(v, dict) else None
        if not spdi or spdi.count(":") < 3:
            continue
        _, spdi_pos, spdi_ref, spdi_alt = spdi.split(":", 3)
        try:
            pos = int(spdi_pos) + 1  # NCBI SPDI position is 0-based/interbase; VCF-style positions are 1-based.
        except ValueError:
            continue
        ref, alt = spdi_ref, spdi_alt
        # `pos` above is a GRCh38 coordinate (canonical SPDI is GRCh38-only). Keep ClinVar's own per-build
        # placement of the SAME component next to it, so a query in another build can be compared in its
        # own frame (C3).
        pos_by_build = _positions_by_build(v.get("variation_loc"), pos)
        break
    if pos is None or not ref or not alt:
        return None

    germline = entry.get("germline_classification") or {}
    review_status = germline.get("review_status")
    return ClinVarCodonMatch(
        uid=uid,
        accession=entry.get("accession"),
        title=title,
        pos=pos,
        ref=ref,
        alt=alt,
        ref_aa=parsed["ref_aa"],
        alt_aa=parsed["alt_aa"],
        codon_number=parsed["codon_number"],
        clinical_significance=germline.get("description") or "",
        review_status=review_status,
        star_rating=star_rating(review_status),
        is_conflicting=is_conflicting(review_status),
        condition=[trait.get("trait_name") for trait in germline.get("trait_set", []) if isinstance(trait, dict)],
        pos_by_build=pos_by_build,
    )


def matches_from_clinvar_codon_result(clinvar_codon_result: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Extract the plain-dict match list out of the dict
    `pipeline/ps1_pm5/lookup.py::ClinVarCodonLookup.query_codon`
    returns, or `[]` when the stage was skipped/errored/found nothing
    -- the neutral value `pipeline/ps1_pm5/decision.py` treats as "no
    ClinVar evidence to compare against" rather than a guess.
    """
    if not clinvar_codon_result or clinvar_codon_result.get("skipped") or clinvar_codon_result.get("error") is not None:
        return []
    return clinvar_codon_result.get("matches") or []


def submitter_note(submitters: Optional[List[Dict[str, Any]]]) -> str:
    """
    Format ` (submitted by X, Y)` for an evidence-trail string citing a
    ClinVar accession, or `""` when submitter identity wasn't captured
    (`submitters` is `None` -- lookup failed or hasn't run) or ClinVar
    genuinely lists none (`[]`). Mirrors
    `pipeline/acmg_rules.py::ACMGRuleEngine._submitter_note` (BP6's own
    copy) -- round 30 retrospective-study leakage control: naming the
    submitter in PS1/PM5's own evidence trail, not filtering on it. See
    this repo's `ROUND_CANDIDATES.md` Round 30 entry.
    """
    if not submitters:
        return ""
    names = [s.get("name") for s in submitters if isinstance(s, dict) and s.get("name")]
    if not names:
        return ""
    return f" (submitted by {', '.join(dict.fromkeys(names))})"


def dedupe_by_uid(matches: List[ClinVarCodonMatch]) -> List[ClinVarCodonMatch]:
    """A codon's (up to 3) genomic positions are queried separately (see `lookup.py`); a record spanning more than one base would otherwise appear twice."""
    seen = set()
    out = []
    for m in matches:
        if m.uid in seen:
            continue
        seen.add(m.uid)
        out.append(m)
    return out
