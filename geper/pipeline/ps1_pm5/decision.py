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
from typing import Any, Dict, List, Optional, Tuple

from pipeline.ps1_pm5.models import PS1PM5Evaluation
from pipeline.pvs1.models import TranscriptContext
from pipeline.pvs1.utils import CodingConsequenceDetail

_ACCEPTED_CLASSIFICATIONS = ("Pathogenic", "Pathogenic/Likely pathogenic", "Likely pathogenic")


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
    ) -> PS1PM5Evaluation:
        gate = self._prerequisite_gate("PS1", query, matches)
        if gate is not None:
            return gate

        qualifying, rejected = self._qualifying_anchors(query, matches, pos, ref, alt, same_amino_acid=True)
        path = self._opening_path(query, matches, qualifying, "the identical resulting amino acid")
        return self._finalize("PS1", "same amino acid change", query, qualifying, rejected, transcript, pos, path)

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
    ) -> PS1PM5Evaluation:
        gate = self._prerequisite_gate("PM5", query, matches)
        if gate is not None:
            return gate

        qualifying, rejected = self._qualifying_anchors(query, matches, pos, ref, alt, same_amino_acid=False)
        path = self._opening_path(query, matches, qualifying, "a DIFFERENT resulting amino acid")
        evaluation = self._finalize(
            "PM5", "different amino acid change at the same codon", query, qualifying, rejected, transcript, pos, path,
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
        query: CodingConsequenceDetail, matches: List[Dict[str, Any]], qualifying: List[Dict[str, Any]], direction_label: str
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
        code: str, query: Optional[CodingConsequenceDetail], matches: List[Dict[str, Any]]
    ) -> Optional[PS1PM5Evaluation]:
        """Not-evaluated/not-applicable short-circuits shared by both rules."""
        if query is None:
            return PS1PM5Evaluation(
                code=code, applies=False,
                rationale=(
                    f"{code} was not evaluated: this variant's amino-acid consequence could not be "
                    "determined in the transcript's reading frame (not a single-nucleotide substitution, "
                    "no CDS sequence available, or the position falls outside the coding sequence)."
                ),
                decision_path=["Qualifying missense substitution? -> Could not be determined."],
                unchecked_caveats=["Query variant's amino-acid change could not be computed."],
                confidence="Low",
            )
        if query.category != "missense":
            return PS1PM5Evaluation(
                code=code, applies=False,
                rationale=(
                    f"{code} applies only to missense substitutions; this variant's predicted consequence "
                    f"is '{query.category}'."
                ),
                decision_path=[f"Qualifying missense substitution? -> No ({query.category})."],
                query_codon_number=query.codon_number, query_ref_aa=query.ref_aa, query_alt_aa=query.alt_aa,
            )
        if not matches:
            return PS1PM5Evaluation(
                code=code, applies=False,
                rationale=(
                    f"{code} was not evaluated: no ClinVar record with a parseable missense protein change "
                    f"was found at codon {query.codon_number}."
                ),
                decision_path=[
                    f"Qualifying missense substitution at codon {query.codon_number}? -> Yes.",
                    "ClinVar records found at this codon? -> None.",
                ],
                evidence_sources=["ClinVar"],
                query_codon_number=query.codon_number, query_ref_aa=query.ref_aa, query_alt_aa=query.alt_aa,
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
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Split `matches` (every ClinVar record at this codon) into
        (qualifying, rejected) for one rule direction.

        `qualifying`: not this variant's own record, resulting amino
        acid matches the requested direction (same for PS1, different
        for PM5), meets the confidence bar (>= `min_star_rating`, not
        conflicting, classified Pathogenic/Likely pathogenic).
        `rejected`: found at this codon, right amino-acid direction,
        but failed the confidence bar -- kept so the rationale can
        name what was *seen but insufficient*, not just report zero.

        A record whose amino-acid direction doesn't match this rule at
        all (e.g. -- for PM5 -- one with the *same* amino acid as the
        query, which is PS1's territory) is excluded from both lists;
        it simply is not evidence for this rule.
        """
        qualifying, rejected = [], []
        for match in matches:
            if (
                pos is not None and ref is not None and alt is not None
                and match["pos"] == pos
                and match["ref"].upper() == ref.upper()
                and match["alt"].upper() == alt.upper()
            ):
                continue  # this variant's own ClinVar record, if it has one -- never its own anchor

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

            if (
                match["clinical_significance"] in _ACCEPTED_CLASSIFICATIONS
                and not match["is_conflicting"]
                and match["star_rating"] >= self.thresholds.min_star_rating
            ):
                qualifying.append(match)
            else:
                rejected.append(match)
        qualifying.sort(key=lambda m: m["star_rating"], reverse=True)
        return qualifying, rejected

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
    ) -> PS1PM5Evaluation:
        caveats_checked: List[str] = []
        unchecked: List[str] = []
        conflicting: List[str] = []

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
                code=code, applies=False,
                rationale=(
                    f"{code} does not apply: no ClinVar record at codon {query.codon_number} sharing "
                    f"{relation_label} met the confidence bar (>= {self.thresholds.min_star_rating}-star, "
                    "not conflicting, Pathogenic/Likely pathogenic)."
                    + (f" {len(rejected)} record(s) were seen but excluded; see caveats_checked." if rejected else "")
                ),
                rejected_anchors=rejected,
                decision_path=path, caveats_checked=caveats_checked, conflicting_evidence=conflicting,
                evidence_sources=["ClinVar"],
                query_codon_number=query.codon_number, query_ref_aa=query.ref_aa, query_alt_aa=query.alt_aa,
                confidence="Low",
            )

        # -- splice-proximity caveat (query's own position) ------------
        near_boundary = transcript.distance_to_nearest_exon_boundary(pos) if (transcript is not None and pos is not None) else None
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
                code=code, applies=False,
                rationale=(
                    f"{code} would otherwise apply based on {relation_label} vs. {anchor_summary}, but is "
                    f"withheld: the query variant sits within {self.thresholds.splice_proximity_exon_bp} bp "
                    "of an exon-intron junction and could have a distinct splicing consequence the known "
                    "pathogenic anchor does not share."
                ),
                matched_anchors=qualifying, rejected_anchors=rejected,
                decision_path=path, caveats_checked=caveats_checked, unchecked_caveats=unchecked,
                conflicting_evidence=conflicting, evidence_sources=["ClinVar"],
                query_codon_number=query.codon_number, query_ref_aa=query.ref_aa, query_alt_aa=query.alt_aa,
                confidence="Moderate",
            )

        if transcript is not None and pos is not None:
            caveats_checked.append(
                f"Splice-proximity caveat: query variant is {near_boundary} bp from the nearest "
                f"exon-intron junction -- outside the {self.thresholds.splice_proximity_exon_bp} bp "
                f"splice-region window, so {code} is not withheld on this basis."
            )
        else:
            unchecked.append("Splice-proximity caveat: transcript structure was unavailable, so this could not be checked.")

        path.append(f"{code} applies -> Yes, citing {len(qualifying)} qualifying ClinVar record(s).")
        supporting = [
            f"ClinVar: {m['accession'] or m['uid']} -- {m['title']} -- {m['clinical_significance']} "
            f"({m['review_status']}, {m['star_rating']}-star)"
            + (f", condition(s): {', '.join(m['condition'])}" if m.get("condition") else "")
            for m in qualifying
        ]
        return PS1PM5Evaluation(
            code=code, applies=True,
            rationale=(
                f"{code} applies: this variant produces {query.ref_aa}{query.codon_number}{query.alt_aa}, "
                f"{relation_label} as {len(qualifying)} ClinVar record(s) already classified at this codon "
                f"(strongest: {qualifying[0]['title']}, {qualifying[0]['clinical_significance']}, "
                f"{qualifying[0]['review_status']})."
            ),
            matched_anchors=qualifying, rejected_anchors=rejected,
            decision_path=path, caveats_checked=caveats_checked, unchecked_caveats=unchecked,
            supporting_evidence=supporting, conflicting_evidence=conflicting,
            evidence_sources=["ClinVar"],
            query_codon_number=query.codon_number, query_ref_aa=query.ref_aa, query_alt_aa=query.alt_aa,
            confidence="High" if qualifying[0]["star_rating"] >= 3 else "Moderate",
        )
