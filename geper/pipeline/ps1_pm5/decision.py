"""
PS1 and PM5 evaluation logic.

ACMG/AMP 2015 (Richards et al., Genet Med 17:405) defines both as a
comparison against a previously established pathogenic variant at the
same codon:

  PS1 (Strong): "Same amino acid change as a previously established
  pathogenic variant regardless of nucleotide change" -- e.g. a known
  pathogenic Arg175His and a novel nucleotide change that also
  produces Arg175His.

  PM5 (Moderate): "Novel missense change at an amino acid residue
  where a different missense change determined to be pathogenic has
  been seen before" -- e.g. a known pathogenic Arg175His and a novel
  Arg175Cys at the same residue.

Both share one evidence-gathering step (every ClinVar record with a
parseable missense protein change at the query variant's codon --
see `pipeline/ps1_pm5/lookup.py`) and diverge only at the amino-acid
comparison: PS1 wants `alt_aa` to match; PM5 wants it to differ.
`_qualifying_anchors` implements the comparison both rules share
(confidence filtering, self-exclusion); `evaluate_ps1`/`evaluate_pm5`
each call it with the opposite `same_amino_acid` flag, then
`_finalize` applies the splice-proximity caveat and builds the
`PS1PM5Evaluation` both return.

This module is pure: no network, no CONFIG-dependent behaviour beyond
what is passed in via `PS1PM5Thresholds`, so it is unit-testable
against frozen ClinVar match lists exactly like
`pipeline/pvs1/decision_tree.py`.
"""

from dataclasses import dataclass
from typing import Any, Collection, Dict, List, Optional, Tuple

from pipeline.ps1_pm5.models import PS1PM5Evaluation
from pipeline.ps1_pm5.utils import GRCH38, canonical_build, submitter_note
from pipeline.pvs1.models import TranscriptContext
from pipeline.pvs1.utils import CodingConsequenceDetail

_ACCEPTED_CLASSIFICATIONS = ("Pathogenic", "Pathogenic/Likely pathogenic", "Likely pathogenic")

# ---------------------------------------------------------------------------
# Record identity (ClinVar audit C3)
#
# "Is this ClinVar record the variant being evaluated?" must be answered in the
# RUN'S assembly. A match's `pos` is its canonical-SPDI position, i.e. GRCh38
# (see `models.py::ClinVarCodonMatch`), while the query variant's position is in
# the input VCF's assembly; comparing them directly let a GRCh37 variant's own
# ClinVar record through as its own PS1 anchor. The decision is made here, per
# query variant, never while the (shared, cached) codon matches are built.
# ---------------------------------------------------------------------------

IDENTITY_OWN = "own"
IDENTITY_DIFFERENT = "different"
IDENTITY_REF_MISMATCH = "ref_mismatch"
IDENTITY_UNVERIFIED = "unverified"

REASON_OWN_VARIANT_LEVEL_UID = "own_record_confirmed_by_variant_level_lookup"
REASON_OWN_COORDINATES = "own_record_same_position_and_alleles"
REASON_REF_MISMATCH = "ref_mismatch_needs_review"
REASON_UNVERIFIED_ASSEMBLY = "identity_unverified_assembly_unresolved"
REASON_UNVERIFIED_NO_POSITION = "identity_unverified_no_position_in_assembly"
REASON_UNVERIFIED_LEGACY_SHAPE = "identity_unverified_legacy_match_shape"
REASON_UNVERIFIED_QUERY = "identity_unverified_query_coordinates_missing"
REASON_DIFFERENT = "different_variant"

_REASON_TEXT = {
    REASON_OWN_VARIANT_LEVEL_UID: "this variant's own ClinVar record (confirmed by the variant-level ClinVar lookup)",
    REASON_OWN_COORDINATES: "this variant's own ClinVar record (same position and alleles)",
    REASON_REF_MISMATCH: "same position and alternate allele as this variant but a different reference allele",
    REASON_UNVERIFIED_ASSEMBLY: "the genome assembly of this run could not be resolved",
    REASON_UNVERIFIED_NO_POSITION: "ClinVar gives no usable position for this record in this run's assembly",
    REASON_UNVERIFIED_LEGACY_SHAPE: "the stored ClinVar match carries no per-assembly positions",
    REASON_UNVERIFIED_QUERY: "this variant's own coordinates were not available",
}


@dataclass(frozen=True)
class RecordIdentity:
    verdict: str  # IDENTITY_*
    reason_code: str  # REASON_*

    @property
    def excludes(self) -> bool:
        return self.verdict != IDENTITY_DIFFERENT


def identify_record(
    match: Dict[str, Any],
    build: Optional[str],
    pos: Optional[int],
    ref: Optional[str],
    alt: Optional[str],
    own_uids: Optional[Collection[str]] = None,
) -> RecordIdentity:
    """
    Whether one codon match is the query variant itself, compared in `build` (a canonical build, see
    `utils.canonical_build`) against the query's `pos`/`ref`/`alt`.

    * `own_uids`: ClinVar UIDs the variant-level ClinVar lookup already confirmed as exact matches for this
      variant (assembly-aware, validated). A UID in it is the variant's own record -- decided first, so it
      holds even when the coordinates below cannot be compared.
    * GRCh38: compared on the record's `pos` (its SPDI position). Any other build: on the record's own
      `pos_by_build[build]`. A record with no position in that build, a match stored before `pos_by_build`
      existed, an unresolved build or missing query coordinates cannot be verified -> IDENTITY_UNVERIFIED.
    * Same position and ALT: REF equal -> own record; REF different -> IDENTITY_REF_MISMATCH (never lets
      the record through: the build/coordinate disagreement needs a human).
    * Anything else (another position, or another ALT at the same position) is a genuinely different
      variant. Neither rsID nor position proximity is ever used to call two records the same variant.

    Everything but IDENTITY_DIFFERENT excludes the record from the PS1/PM5 anchors.
    """
    if own_uids and str(match.get("uid")) in {str(u) for u in own_uids}:
        return RecordIdentity(IDENTITY_OWN, REASON_OWN_VARIANT_LEVEL_UID)
    if build is None:
        return RecordIdentity(IDENTITY_UNVERIFIED, REASON_UNVERIFIED_ASSEMBLY)
    if pos is None or not ref or not alt:
        return RecordIdentity(IDENTITY_UNVERIFIED, REASON_UNVERIFIED_QUERY)
    if build == GRCH38:
        match_pos = match.get("pos")
    else:
        by_build = match.get("pos_by_build")
        if by_build is None:
            return RecordIdentity(IDENTITY_UNVERIFIED, REASON_UNVERIFIED_LEGACY_SHAPE)
        match_pos = by_build.get(build)
    if not isinstance(match_pos, int) or isinstance(match_pos, bool):
        return RecordIdentity(IDENTITY_UNVERIFIED, REASON_UNVERIFIED_NO_POSITION)
    if match_pos != pos or str(match.get("alt", "")).upper() != alt.upper():
        return RecordIdentity(IDENTITY_DIFFERENT, REASON_DIFFERENT)
    if str(match.get("ref", "")).upper() != ref.upper():
        return RecordIdentity(IDENTITY_REF_MISMATCH, REASON_REF_MISMATCH)
    return RecordIdentity(IDENTITY_OWN, REASON_OWN_COORDINATES)


@dataclass(frozen=True)
class PS1PM5Thresholds:
    min_star_rating: int = 2
    splice_proximity_exon_bp: int = 3


class PS1PM5Evaluator:
    """Evaluates PS1 and PM5 for one variant against its codon's ClinVar neighborhood."""

    def __init__(self, thresholds: Optional[PS1PM5Thresholds] = None):
        self.thresholds = thresholds or PS1PM5Thresholds()

    # ------------------------------------------------------------------
    # PS1
    # ------------------------------------------------------------------

    def evaluate_ps1(
        self,
        query: Optional[CodingConsequenceDetail],
        matches: List[Dict[str, Any]],
        transcript: Optional[TranscriptContext],
        pos: Optional[int],
        ref: Optional[str],
        alt: Optional[str],
        assembly: Optional[str] = None,
        own_uids: Optional[Collection[str]] = None,
    ) -> PS1PM5Evaluation:
        """
        `assembly` is the run's genome assembly (any spelling `utils.canonical_build` accepts; None or an
        unsupported value is UNRESOLVED, never silently GRCh38): `pos`/`ref`/`alt` are in it, and a record's
        identity is judged there. `own_uids`: ClinVar UIDs the variant-level lookup confirmed as this variant.
        """
        gate = self._prerequisite_gate("PS1", query, matches, ref, alt)
        if gate is not None:
            return gate

        qualifying, rejected, excluded = self._qualifying_anchors(
            query, matches, pos, ref, alt, same_amino_acid=True, assembly=assembly, own_uids=own_uids
        )
        path = self._opening_path(query, matches, qualifying, "the identical resulting amino acid")
        return self._finalize(
            "PS1", "same amino acid change", query, qualifying, rejected, transcript, pos, path, excluded, assembly
        )

    # ------------------------------------------------------------------
    # PM5
    # ------------------------------------------------------------------

    def evaluate_pm5(
        self,
        query: Optional[CodingConsequenceDetail],
        matches: List[Dict[str, Any]],
        transcript: Optional[TranscriptContext],
        pos: Optional[int],
        ref: Optional[str],
        alt: Optional[str],
        assembly: Optional[str] = None,
        own_uids: Optional[Collection[str]] = None,
    ) -> PS1PM5Evaluation:
        gate = self._prerequisite_gate("PM5", query, matches, ref, alt)
        if gate is not None:
            return gate

        qualifying, rejected, excluded = self._qualifying_anchors(
            query, matches, pos, ref, alt, same_amino_acid=False, assembly=assembly, own_uids=own_uids
        )
        path = self._opening_path(query, matches, qualifying, "a DIFFERENT resulting amino acid")
        evaluation = self._finalize(
            "PM5",
            "different amino acid change at the same codon",
            query,
            qualifying,
            rejected,
            transcript,
            pos,
            path,
            excluded,
            assembly,
        )
        if evaluation.applies:
            evaluation.unchecked_caveats.append(
                "Substitution-similarity caveat (not fully modeled): PM5 is applied at the codon-position "
                "level only, per the base ACMG/AMP definition. GEPER does not evaluate whether this "
                "variant's specific substitution is physicochemically similar to the anchor's (e.g. "
                "Grantham distance, BLOSUM score, conservative vs. non-conservative) or whether the "
                "position is known to tolerate some substitutions but not others -- ACMG/AMP guidance "
                "and subsequent ClinGen SVI recommendations note this can matter (a position pathogenic "
                "only for a bulky/charged substitution may not behave the same way for a small/neutral "
                "one). A curator should confirm the two substitutions are mechanistically comparable "
                "before accepting this evidence at face value."
            )
        return evaluation

    # ------------------------------------------------------------------
    # Shared
    # ------------------------------------------------------------------

    @staticmethod
    def _opening_path(
        query: CodingConsequenceDetail,
        matches: List[Dict[str, Any]],
        qualifying: List[Dict[str, Any]],
        direction_label: str,
    ) -> List[str]:
        return [
            f"Qualifying missense substitution at codon {query.codon_number} "
            f"({query.ref_aa}{query.codon_number}{query.alt_aa})? -> Yes.",
            f"ClinVar records found at this codon: {len(matches)}.",
            f"Records with {direction_label} ({query.alt_aa}), passing the confidence filter and "
            f"excluding this variant's own record: {len(qualifying)}.",
        ]

    @staticmethod
    def _prerequisite_gate(
        code: str,
        query: Optional[CodingConsequenceDetail],
        matches: List[Dict[str, Any]],
        ref: Optional[str] = None,
        alt: Optional[str] = None,
    ) -> Optional[PS1PM5Evaluation]:
        """
        Not-evaluated/not-applicable short-circuits shared by both
        rules.

        `query is None` collapses several distinct causes in
        `pipeline/pvs1/utils.py::coding_consequence_detail` (see its own
        docstring: not a substitution, no CDS fetched, position outside
        the CDS, or a build/transcript mismatch) -- report review round
        4, I8, split the one cause that is a genuine variant-class
        inapplicability, not a gap: an indel (`ref`/`alt` present but
        not both single bases). PS1/PM5 are ACMG/AMP-defined exclusively
        for missense *substitutions* at a codon, the identical class
        restriction the "wrong consequence" branch below already
        reports as `not_triggered` for a SNV whose consequence is
        nonsense/synonymous/etc. -- an indel is "checked, and the
        answer is no" by the same logic, not "could not be checked".
        Every other `query is None` cause (missing transcript/CDS, a
        position genuinely outside the CDS, ref/alt not supplied at
        all) still reports the genuine `not_evaluated` gap below.
        """
        if query is None:
            if ref and alt and (len(ref) != 1 or len(alt) != 1):
                return PS1PM5Evaluation(
                    code=code,
                    applies=False,
                    rationale=(
                        f"{code} applies only to missense substitutions; this variant is not a "
                        "single-nucleotide substitution (an insertion/deletion), so it is not a "
                        "candidate consequence class for this criterion."
                    ),
                    decision_path=["Qualifying missense substitution? -> No (not a single-nucleotide substitution)."],
                )
            return PS1PM5Evaluation(
                code=code,
                applies=False,
                rationale=(
                    f"{code} was not evaluated: this variant's amino-acid consequence could not be "
                    "determined in the transcript's reading frame (no CDS sequence available, the "
                    "position falls outside the coding sequence, or ref/alt were not supplied)."
                ),
                decision_path=["Qualifying missense substitution? -> Could not be determined."],
                unchecked_caveats=["Query variant's amino-acid change could not be computed."],
                confidence="Low",
            )
        if query.category != "missense":
            return PS1PM5Evaluation(
                code=code,
                applies=False,
                rationale=(
                    f"{code} applies only to missense substitutions; this variant's predicted consequence "
                    f"is '{query.category}'."
                ),
                decision_path=[f"Qualifying missense substitution? -> No ({query.category})."],
                query_codon_number=query.codon_number,
                query_ref_aa=query.ref_aa,
                query_alt_aa=query.alt_aa,
            )
        if not matches:
            return PS1PM5Evaluation(
                code=code,
                applies=False,
                rationale=(
                    f"{code} was not evaluated: no ClinVar record with a parseable missense protein change "
                    f"was found at codon {query.codon_number}."
                ),
                decision_path=[
                    f"Qualifying missense substitution at codon {query.codon_number}? -> Yes.",
                    "ClinVar records found at this codon? -> None.",
                ],
                evidence_sources=["ClinVar"],
                query_codon_number=query.codon_number,
                query_ref_aa=query.ref_aa,
                query_alt_aa=query.alt_aa,
                confidence="Low",
            )
        return None

    def _qualifying_anchors(
        self,
        query: CodingConsequenceDetail,
        matches: List[Dict[str, Any]],
        pos: Optional[int],
        ref: Optional[str],
        alt: Optional[str],
        same_amino_acid: bool,
        assembly: Optional[str] = None,
        own_uids: Optional[Collection[str]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Split `matches` (every ClinVar record at this codon) into
        (qualifying, rejected, excluded) for one rule direction.

        `qualifying`: not this variant's own record, resulting amino
        acid matches the requested direction (same for PS1, different
        for PM5), meets the confidence bar (>= `min_star_rating`, not
        conflicting, classified Pathogenic/Likely pathogenic).
        `rejected`: found at this codon, right amino-acid direction,
        but failed the confidence bar -- kept so the rationale can
        name what was *seen but insufficient*, not just report zero.
        `excluded` (C3): right amino-acid direction, but NOT used because of the identity check --
        this variant's own record, a position+ALT match with a different REF, or a record whose
        identity could not be verified in the run's assembly. Each entry carries a structured reason,
        and none of these is ever in `qualifying` or `rejected`.

        A record whose amino-acid direction doesn't match this rule at
        all (e.g. -- for PM5 -- one with the *same* amino acid as the
        query, which is PS1's territory) is excluded from every list;
        it simply is not evidence for this rule.
        """
        build = canonical_build(assembly)
        qualifying: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        excluded: List[Dict[str, Any]] = []
        for match in matches:
            match_ref_aa, match_alt_aa = match["protein_change"][0], match["protein_change"][-1]
            if match_ref_aa == match_alt_aa or "*" in (match_ref_aa, match_alt_aa):
                # Not a missense change at all (synonymous, or a
                # nonsense/stop-lost record that also happens to sit at
                # this codon) -- PS1/PM5 both explicitly compare
                # missense-to-missense; a nonsense change at the same
                # residue is neither "the same amino acid change" nor
                # a "different missense change" in the ACMG sense, so
                # it is not evidence for either rule and must not be
                # silently counted as a PM5 anchor just because its
                # resulting residue happens to differ from the query's.
                continue

            is_same_aa = match_alt_aa == query.alt_aa
            if is_same_aa != same_amino_acid:
                continue

            identity = identify_record(match, build, pos, ref, alt, own_uids)
            if identity.excludes:
                excluded.append(
                    {
                        "uid": match.get("uid"),
                        "accession": match.get("accession"),
                        "title": match.get("title"),
                        "verdict": identity.verdict,
                        "reason_code": identity.reason_code,
                        "reason": _REASON_TEXT.get(identity.reason_code, identity.reason_code),
                    }
                )
                continue

            if (
                match["clinical_significance"] in _ACCEPTED_CLASSIFICATIONS
                and not match["is_conflicting"]
                and match["star_rating"] >= self.thresholds.min_star_rating
            ):
                qualifying.append(match)
            else:
                rejected.append(match)
        qualifying.sort(key=lambda m: m["star_rating"], reverse=True)
        return qualifying, rejected, excluded

    @staticmethod
    def _identity_notes(
        code: str, excluded: List[Dict[str, Any]], build: Optional[str]
    ) -> Tuple[List[str], List[str], List[str]]:
        """(caveats_checked, unchecked_caveats, conflicting_evidence) text for the identity exclusions."""

        def names(records: List[Dict[str, Any]]) -> str:
            shown = [str(r.get("accession") or r.get("uid")) for r in records[:5]]
            return ", ".join(shown) + (f" and {len(records) - 5} more" if len(records) > 5 else "")

        own = [e for e in excluded if e["verdict"] == IDENTITY_OWN]
        mismatch = [e for e in excluded if e["verdict"] == IDENTITY_REF_MISMATCH]
        unverified = [e for e in excluded if e["verdict"] == IDENTITY_UNVERIFIED]
        checked: List[str] = []
        unchecked: List[str] = []
        conflicting: List[str] = []
        if own:
            checked.append(
                f"Self-exclusion: ClinVar record {names(own)} {'is' if len(own) == 1 else 'are'} this variant's own "
                f"record and was not used as evidence for {code}."
            )
        for e in mismatch:
            note = (
                f"ClinVar record {e.get('accession') or e.get('uid')} has the same position and alternate allele as "
                f"this variant but a different reference allele; it was not used as {code} evidence and the "
                "mismatch needs review."
            )
            unchecked.append(note)
            conflicting.append(note)
        by_reason: Dict[str, List[Dict[str, Any]]] = {}
        for e in unverified:
            by_reason.setdefault(e["reason_code"], []).append(e)
        for reason_code, records in by_reason.items():
            reason = _REASON_TEXT.get(reason_code, reason_code)
            note = (
                f"{len(records)} ClinVar record(s) at this codon could not be confirmed as different from this "
                f"variant ({reason}) and were not used as {code} evidence: {names(records)}."
            )
            unchecked.append(note)
            conflicting.append(note)
        return checked, unchecked, conflicting

    def _finalize(
        self,
        code: str,
        relation_label: str,
        query: CodingConsequenceDetail,
        qualifying: List[Dict[str, Any]],
        rejected: List[Dict[str, Any]],
        transcript: Optional[TranscriptContext],
        pos: Optional[int],
        path: List[str],
        excluded: Optional[List[Dict[str, Any]]] = None,
        assembly: Optional[str] = None,
    ) -> PS1PM5Evaluation:
        excluded = excluded or []
        caveats_checked, unchecked, conflicting = self._identity_notes(code, excluded, canonical_build(assembly))
        identity_blocked = [e for e in excluded if e["verdict"] != IDENTITY_OWN]

        if rejected:
            summary = "; ".join(
                f"{m['title']} ({m['clinical_significance']}, {m['review_status'] or 'no review status'})"
                for m in rejected[:5]
            )
            caveats_checked.append(
                f"Confidence filter: {len(rejected)} record(s) at this codon shared {relation_label} but "
                f"did not meet the confidence bar and were excluded: {summary}."
            )
            conflicting.append(
                f"{len(rejected)} ClinVar record(s) at this codon were seen but excluded by the confidence "
                "filter (see caveats_checked)."
            )

        if not qualifying:
            return PS1PM5Evaluation(
                code=code,
                applies=False,
                rationale=(
                    f"{code} does not apply: no ClinVar record at codon {query.codon_number} sharing "
                    f"{relation_label} met the confidence bar (>= {self.thresholds.min_star_rating}-star, "
                    "not conflicting, Pathogenic/Likely pathogenic)."
                    + (f" {len(rejected)} record(s) were seen but excluded; see caveats_checked." if rejected else "")
                    + (
                        f" {len(identity_blocked)} record(s) were not used because their identity could not be "
                        "verified or needs review; see the caveats."
                        if identity_blocked
                        else ""
                    )
                ),
                rejected_anchors=rejected,
                decision_path=path,
                caveats_checked=caveats_checked,
                unchecked_caveats=unchecked,
                conflicting_evidence=conflicting,
                evidence_sources=["ClinVar"],
                query_codon_number=query.codon_number,
                query_ref_aa=query.ref_aa,
                query_alt_aa=query.alt_aa,
                confidence="Low",
                excluded_records=excluded,
            )

        # -- splice-proximity caveat (query's own position) ------------
        near_boundary = (
            transcript.distance_to_nearest_exon_boundary(pos) if (transcript is not None and pos is not None) else None
        )
        blocked_by_splice = near_boundary is not None and near_boundary <= self.thresholds.splice_proximity_exon_bp

        anchor_summary = "; ".join(
            f"{m['title']} ({m['clinical_significance']}, {m['review_status']}, {m['star_rating']}-star)"
            for m in qualifying
        )

        if blocked_by_splice:
            path.append(
                f"Query variant within {self.thresholds.splice_proximity_exon_bp} bp of an exon-intron "
                f"junction (distance={near_boundary} bp)? -> Yes -- {code} withheld."
            )
            caveats_checked.append(
                f"Splice-proximity caveat: the query variant's position is {near_boundary} bp from the "
                f"nearest exon-intron junction of its own exon, within the "
                f"{self.thresholds.splice_proximity_exon_bp} bp splice-region window. It could plausibly "
                "disrupt splicing in addition to changing the amino acid -- a mechanism the established "
                f"pathogenic anchor(s) below are not known to share -- so {code} is not applied even "
                "though the amino-acid comparison matches."
            )
            return PS1PM5Evaluation(
                code=code,
                applies=False,
                rationale=(
                    f"{code} would otherwise apply based on {relation_label} vs. {anchor_summary}, but is "
                    f"withheld: the query variant sits within {self.thresholds.splice_proximity_exon_bp} bp "
                    "of an exon-intron junction and could have a distinct splicing consequence the known "
                    "pathogenic anchor does not share."
                ),
                matched_anchors=qualifying,
                rejected_anchors=rejected,
                decision_path=path,
                caveats_checked=caveats_checked,
                unchecked_caveats=unchecked,
                conflicting_evidence=conflicting,
                evidence_sources=["ClinVar"],
                query_codon_number=query.codon_number,
                query_ref_aa=query.ref_aa,
                query_alt_aa=query.alt_aa,
                confidence="Moderate",
                excluded_records=excluded,
            )

        if transcript is not None and pos is not None:
            caveats_checked.append(
                f"Splice-proximity caveat: query variant is {near_boundary} bp from the nearest "
                f"exon-intron junction -- outside the {self.thresholds.splice_proximity_exon_bp} bp "
                f"splice-region window, so {code} is not withheld on this basis."
            )
        else:
            unchecked.append(
                "Splice-proximity caveat: transcript structure was unavailable, so this could not be checked."
            )

        path.append(f"{code} applies -> Yes, citing {len(qualifying)} qualifying ClinVar record(s).")
        supporting = [
            f"ClinVar: {m['accession'] or m['uid']} -- {m['title']} -- {m['clinical_significance']} "
            f"({m['review_status']}, {m['star_rating']}-star){submitter_note(m.get('submitters'))}"
            + (f", condition(s): {', '.join(m['condition'])}" if m.get("condition") else "")
            for m in qualifying
        ]
        return PS1PM5Evaluation(
            code=code,
            applies=True,
            rationale=(
                f"{code} applies: this variant produces {query.ref_aa}{query.codon_number}{query.alt_aa}, "
                f"{relation_label} as {len(qualifying)} ClinVar record(s) already classified at this codon "
                f"(strongest: {qualifying[0]['title']}, {qualifying[0]['clinical_significance']}, "
                f"{qualifying[0]['review_status']})."
            ),
            matched_anchors=qualifying,
            rejected_anchors=rejected,
            decision_path=path,
            caveats_checked=caveats_checked,
            unchecked_caveats=unchecked,
            supporting_evidence=supporting,
            conflicting_evidence=conflicting,
            evidence_sources=["ClinVar"],
            query_codon_number=query.codon_number,
            query_ref_aa=query.ref_aa,
            query_alt_aa=query.alt_aa,
            confidence="High" if qualifying[0]["star_rating"] >= 3 else "Moderate",
            excluded_records=excluded,
        )
