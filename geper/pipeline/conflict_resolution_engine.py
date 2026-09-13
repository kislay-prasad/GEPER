"""
Phase 6 -- Conflict Resolution Engine.

Identifies, explains, and documents disagreements between evidence
sources GEPER already collected for a variant. This engine never
changes the ACMG classification (Phase 1) -- GEPER's ACMG combining
rules already treat ClinVar as a cross-reference only, not a direct
input (see `acmg_rules.ACMGRuleEngine._clinvar_crossref`), so nothing
here needs to "win" against that classification; this engine's job is
purely to surface conflicts for human review with an honest severity
rating and a plain-language explanation of how GEPER's existing design
already handles (or doesn't yet numerically account for) each one.

Every conflict is built only from fields already present on the
`InterpretationResult` this engine is given (its `raw_evidence`, ACMG
rules, `ai_consensus`, and the already-computed confidence/priority
conflict penalties) -- nothing is queried or invented. Where GEPER
genuinely has no comparable evidence to detect a conflict (Sequence:
BLAST returns a homology hit count with no direction, and Ensembl is
not exposed as its own per-variant evidence dict -- see
`interpretation_result.py`'s and `prioritization_engine.py`'s
docstrings for the same point), this engine says so explicitly rather
than fabricating a conflict.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Union

from config import CONFIG, ConflictConfig

_SEVERITY_WEIGHT_KEY = {
    "Minor": "MINOR_WEIGHT",
    "Moderate": "MODERATE_WEIGHT",
    "Major": "MAJOR_WEIGHT",
    "Critical": "CRITICAL_WEIGHT",
}

# ClinVar's own two highest-confidence review tiers (3- and 4-star
# review status, verbatim ClinVar API text, lowercased for comparison).
_AUTHORITATIVE_CLINVAR_REVIEW_STATUS = ("reviewed by expert panel", "practice guideline")
# The next tier down (1- and 2-star): a genuine, curated ClinVar
# assertion still exists, just not at expert-panel/practice-guideline
# confidence. Deliberately excludes "criteria provided, conflicting
# classifications" -- ClinVar's own submitters disagree with each
# other there, so GEPER disagreeing with a single (arbitrarily chosen)
# record from an already-split entry is much weaker evidence than
# disagreeing with a record multiple submitters agree on -- and
# excludes the 0-star tiers ("no assertion provided"/"no assertion
# criteria provided") entirely, which carry no curation weight at all.
_CURATED_CLINVAR_REVIEW_STATUS = (
    "criteria provided, single submitter",
    "criteria provided, multiple submitters, no conflicts",
)
_PATHOGENIC_LEANING_CLASSIFICATIONS = ("pathogenic", "likely pathogenic")
_BENIGN_LEANING_CLASSIFICATIONS = ("benign", "likely benign")
# ClinVar's own non-committal tiers -- a record can land here two ways:
# a single reviewed assertion of "Uncertain significance", or ClinVar's
# submitters disagreeing with each other ("Conflicting classifications
# of pathogenicity"/the older "Conflicting interpretations of
# pathogenicity" wording) -- either way, ClinVar itself is not staking
# out a pathogenic or benign direction, which is exactly the case
# `_clinvar_disagrees` (report review round 4, I4) needed a name for to
# stop silently treating it as agreement.
_UNCERTAIN_LEANING_CLASSIFICATIONS = (
    "uncertain significance",
    "conflicting classifications of pathogenicity",
    "conflicting interpretations of pathogenicity",
)


def _ai_directions(ai_consensus: Optional[List[Dict[str, Any]]]) -> Dict[str, str]:
    """Same direction-extraction rule used consistently in Phase 3/4."""
    out = {}
    for v in ai_consensus or []:
        pred = (v.get("prediction") or "").lower()
        source = v.get("source") or v.get("model") or "model"
        if "pathogenic" in pred or pred in (
            "strong_donor_loss",
            "strong_acceptor_loss",
            "exon_skipping",
            "intron_retention",
            "strong",
            "moderate",
        ):
            out[source] = "damaging"
        elif "benign" in pred:
            out[source] = "benign"
    return out


def _pct(penalty: float) -> str:
    return f"{penalty:.0%} conflict penalty applied"


@dataclass
class ConflictItem:
    conflict_type: str
    category: str  # "Clinical" | "Population" | "AI" | "Protein" | "Structure" | "Sequence" | "ACMG Criterion"
    evidence_a: Dict[str, str]  # {"source": ..., "statement": ...}
    evidence_b: Dict[str, str]
    severity: str  # "Critical" | "Major" | "Moderate" | "Minor" | "Not evaluable"
    resolution: str
    resolution_rationale: str
    confidence_impact: str
    priority_impact: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conflict_type": self.conflict_type,
            "category": self.category,
            "evidence_a": self.evidence_a,
            "evidence_b": self.evidence_b,
            "severity": self.severity,
            "resolution": self.resolution,
            "resolution_rationale": self.resolution_rationale,
            "confidence_impact": self.confidence_impact,
            "priority_impact": self.priority_impact,
        }


@dataclass
class ConflictResolutionResult:
    conflict_list: List[ConflictItem]
    conflict_summary: str
    conflict_score: float  # 0-100, higher = more/worse conflict
    conflict_severity: str  # "None" | "Minor" | "Moderate" | "Major" | "Critical"
    conflict_resolution: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conflict_list": [c.to_dict() for c in self.conflict_list],
            "conflict_summary": self.conflict_summary,
            "conflict_score": round(self.conflict_score, 1),
            "conflict_severity": self.conflict_severity,
            "conflict_resolution": self.conflict_resolution,
        }


class ConflictResolutionEngine:
    """
    Detects conflicts across the six categories in the Phase 6 spec
    (Clinical, Population, AI, Protein, Structure, Sequence) plus a
    seventh, "ACMG Criterion", which simply re-surfaces the per-
    criterion conflicting-evidence notes Phase 1 already recorded
    (e.g. PM1's "position is an estimate" caveat) rather than treating
    them as a second, separately-invented signal.
    """

    @staticmethod
    def has_critical_conflict(acmg_classification: Optional[str], clinvar_result: Optional[Dict[str, Any]]) -> bool:
        """
        Whether `_expert_panel_disagreement_conflict` -- the only
        Critical-tier detector this engine has -- would fire for this
        variant. Public (unlike the detector methods below, which stay
        `_`-prefixed since `detect()` is their only normal caller):
        `pipeline/interpretation.py` calls this BEFORE `detect()` runs
        (report review round 4, I2), so `PrioritizationEngine.score()`
        can floor a variant's review-priority category at "Critical"
        when GEPER's own classification disagrees with an expert-panel/
        practice-guideline ClinVar record -- the same floor shape
        `_overall_severity` below already applies to conflict severity
        itself. Deliberately re-runs the cheap, side-effect-free static
        check rather than restructuring Phase 4/Phase 6's run order
        (Phase 6 needs Phase 4's own conflict-penalty output as one of
        its *inputs*, so a hard swap would create a real circular
        dependency, not just a call-order preference).

        HIGH-1 narrowed that last claim, and it is worth stating
        precisely rather than leaving a comment that overstates its
        case: the cycle is real for `detect()` as a whole, but only
        because `_ai_conflict` renders the two penalties into display
        strings. Detection itself does not depend on them -- see
        `real_conflicts_for_scoring`, which hoists exactly that half.
        """
        return (
            ConflictResolutionEngine._expert_panel_disagreement_conflict(acmg_classification, clinvar_result)
            is not None
        )

    def _detect_conflict_items(
        self,
        *,
        acmg_classification: Optional[str],
        acmg_conflicting_evidence: Sequence[Union[str, Dict[str, Any]]],
        ai_consensus: List[Dict[str, Any]],
        confidence_conflict_penalty: float,
        confidence_conflict_explanation: str,
        priority_conflict_penalty: float,
        priority_conflict_explanation: str,
        clinvar_result: Optional[Dict[str, Any]] = None,
        clingen_result: Optional[Dict[str, Any]] = None,
        gnomad_result: Optional[Dict[str, Any]] = None,
        alphamissense_result: Optional[Dict[str, Any]] = None,
        mmsplice_result: Optional[Dict[str, Any]] = None,
        uniprot_result: Optional[Dict[str, Any]] = None,
        interpro_result: Optional[Dict[str, Any]] = None,
        alphafold_result: Optional[Dict[str, Any]] = None,
        blast_result: Optional[Dict[str, Any]] = None,
    ) -> List[ConflictItem]:
        """
        Runs every detector and returns the raw item list, before any
        scoring filter. Extracted from `detect()` so `detect()` and
        `real_conflicts_for_scoring()` share ONE detector orchestration
        -- two hand-maintained copies of this list would drift the
        moment a detector is added, and the scoring path would then
        silently stop seeing a conflict category the report still
        shows.
        """
        conflicts: List[ConflictItem] = []

        c = self._expert_panel_disagreement_conflict(acmg_classification, clinvar_result)
        if c:
            conflicts.append(c)
        c = self._curated_clinvar_disagreement_conflict(acmg_classification, clinvar_result)
        if c:
            conflicts.append(c)
        conflicts.extend(self._acmg_criterion_conflicts(acmg_conflicting_evidence))
        c = self._clinical_conflict(clinvar_result, clingen_result)
        if c:
            conflicts.append(c)
        c = self._population_conflict(acmg_classification, clinvar_result, gnomad_result)
        if c:
            conflicts.append(c)
        c = self._ai_conflict(
            ai_consensus,
            confidence_conflict_penalty,
            confidence_conflict_explanation,
            priority_conflict_penalty,
            priority_conflict_explanation,
        )
        if c:
            conflicts.append(c)
        c = self._protein_conflict(uniprot_result, interpro_result)
        if c:
            conflicts.append(c)
        c = self._structural_conflict(alphafold_result, ai_consensus)
        if c:
            conflicts.append(c)
        conflicts.append(self._sequence_conflict_note(blast_result))
        return conflicts

    @staticmethod
    def _real_conflicts(conflicts: List[ConflictItem]) -> List[ConflictItem]:
        # "Not evaluable" notes (Sequence) are excluded from scoring --
        # they are not detected conflicts, just an honest statement that
        # no check could be performed. "ACMG Criterion" items are also
        # excluded here for the same reason (D4, report review round
        # 3): `_acmg_criterion_conflicts`'s own docstring says these are
        # "not a disagreement between two independent sources", just a
        # caveat the rule engine already flagged on itself (e.g. an
        # estimated coordinate, partial evidence). Almost every finding
        # has at least one such caveat somewhere in its ACMG evaluation,
        # so counting them toward `conflict_severity` made the
        # "Conflicting evidence" Attention flag fire on effectively
        # every finding regardless of whether any evidence actually
        # disagreed -- exactly the signal-free flag A4 was meant to
        # remove. They remain in `conflict_list` (and therefore in the
        # full per-finding conflict detail) for reviewer visibility;
        # they just no longer drive the top-line severity/score/summary
        # or the Attention-column flag text.
        #
        # HIGH-1: this filter is now the SINGLE definition of "a real
        # conflict" for all three consumers -- the Attention flag (via
        # the severity/score below), the confidence engine's conflict
        # penalty and the prioritization engine's. Before this, the flag
        # read the filtered list while both penalties read the raw
        # per-criterion `conflicting_evidence`, so the two could never
        # agree by construction: on a five-variant run every one of the
        # eight items driving the penalties was an excluded "ACMG
        # Criterion" caveat, while the one genuine Clinical
        # disagreement contributed nothing to either penalty.
        return [
            c
            for c in conflicts
            if c.severity in ("Minor", "Moderate", "Major", "Critical") and c.category != "ACMG Criterion"
        ]

    def real_conflicts_for_scoring(
        self,
        *,
        acmg_classification: Optional[str],
        acmg_conflicting_evidence: Sequence[Union[str, Dict[str, Any]]],
        ai_consensus: List[Dict[str, Any]],
        clinvar_result: Optional[Dict[str, Any]] = None,
        clingen_result: Optional[Dict[str, Any]] = None,
        gnomad_result: Optional[Dict[str, Any]] = None,
        alphamissense_result: Optional[Dict[str, Any]] = None,
        mmsplice_result: Optional[Dict[str, Any]] = None,
        uniprot_result: Optional[Dict[str, Any]] = None,
        interpro_result: Optional[Dict[str, Any]] = None,
        alphafold_result: Optional[Dict[str, Any]] = None,
        blast_result: Optional[Dict[str, Any]] = None,
    ) -> List[ConflictItem]:
        """
        The real-conflict list Phases 3 and 4 score against, computed
        before either of them runs. Same hoist shape as
        `has_critical_conflict` (I2): a cheap, side-effect-free re-run
        of the detectors rather than a reordering of the phases.

        `detect()` cannot simply be moved ahead of Phases 3/4, because
        it takes their conflict penalties as inputs -- but ONLY
        `_ai_conflict` consumes those four parameters, and only to
        render its `confidence_impact`/`priority_impact` display
        strings. Nothing about which items exist, or their severities,
        depends on them. So this pass supplies placeholder penalties
        and DISCARDS every rendered string: its return value is counted,
        never shown. `detect()` re-runs afterwards with the real
        penalties, and its items are the ones that reach the report.
        """
        return self._real_conflicts(
            self._detect_conflict_items(
                acmg_classification=acmg_classification,
                acmg_conflicting_evidence=acmg_conflicting_evidence,
                ai_consensus=ai_consensus,
                # Placeholders: see the docstring above. These reach only
                # `_ai_conflict`'s two impact strings, which this method's
                # caller never reads.
                confidence_conflict_penalty=0.0,
                confidence_conflict_explanation="",
                priority_conflict_penalty=0.0,
                priority_conflict_explanation="",
                clinvar_result=clinvar_result,
                clingen_result=clingen_result,
                gnomad_result=gnomad_result,
                alphamissense_result=alphamissense_result,
                mmsplice_result=mmsplice_result,
                uniprot_result=uniprot_result,
                interpro_result=interpro_result,
                alphafold_result=alphafold_result,
                blast_result=blast_result,
            )
        )

    def detect(
        self,
        *,
        acmg_classification: Optional[str],
        acmg_conflicting_evidence: Sequence[Union[str, Dict[str, Any]]],
        ai_consensus: List[Dict[str, Any]],
        confidence_conflict_penalty: float,
        confidence_conflict_explanation: str,
        priority_conflict_penalty: float,
        priority_conflict_explanation: str,
        clinvar_result: Optional[Dict[str, Any]] = None,
        clingen_result: Optional[Dict[str, Any]] = None,
        gnomad_result: Optional[Dict[str, Any]] = None,
        alphamissense_result: Optional[Dict[str, Any]] = None,
        mmsplice_result: Optional[Dict[str, Any]] = None,
        uniprot_result: Optional[Dict[str, Any]] = None,
        interpro_result: Optional[Dict[str, Any]] = None,
        alphafold_result: Optional[Dict[str, Any]] = None,
        blast_result: Optional[Dict[str, Any]] = None,
    ) -> ConflictResolutionResult:
        cfg = CONFIG.conflict
        conflicts = self._detect_conflict_items(
            acmg_classification=acmg_classification,
            acmg_conflicting_evidence=acmg_conflicting_evidence,
            ai_consensus=ai_consensus,
            confidence_conflict_penalty=confidence_conflict_penalty,
            confidence_conflict_explanation=confidence_conflict_explanation,
            priority_conflict_penalty=priority_conflict_penalty,
            priority_conflict_explanation=priority_conflict_explanation,
            clinvar_result=clinvar_result,
            clingen_result=clingen_result,
            gnomad_result=gnomad_result,
            alphamissense_result=alphamissense_result,
            mmsplice_result=mmsplice_result,
            uniprot_result=uniprot_result,
            interpro_result=interpro_result,
            alphafold_result=alphafold_result,
            blast_result=blast_result,
        )
        real_conflicts = self._real_conflicts(conflicts)

        score = self._score(real_conflicts, cfg)
        severity = self._overall_severity(score, real_conflicts, cfg)
        summary = self._summary(real_conflicts, severity)
        resolution = self._overall_resolution(real_conflicts, acmg_classification)

        return ConflictResolutionResult(
            conflict_list=conflicts,
            conflict_summary=summary,
            conflict_score=score,
            conflict_severity=severity,
            conflict_resolution=resolution,
        )

    # ------------------------------------------------------------------
    # Category detectors
    # ------------------------------------------------------------------

    @staticmethod
    def _acmg_criterion_conflicts(
        acmg_conflicting_evidence: Sequence[Union[str, Dict[str, Any]]],
    ) -> List[ConflictItem]:
        # Re-surfaces Phase 1/2's already-computed, already-tagged
        # per-criterion caveats (e.g. PM1's position-estimate caveat,
        # PVS1's insufficient-dosage-evidence caveat) as first-class
        # conflict items, instead of leaving them as free text a
        # reviewer might not notice. Not a new detection -- a new
        # presentation of an existing, traceable one.
        items = []
        for entry in acmg_conflicting_evidence or []:
            raw_text = entry.get("text") if isinstance(entry, dict) else entry
            # `entry.get("text")` can genuinely be missing/None (a
            # malformed caveat entry from an ACMG rule) -- caught by
            # mypy (G1, report review round 5) as `Any | None` flowing
            # into `evidence_a`'s `Dict[str, str]`. Coerced to a real,
            # honest string rather than letting `None` silently render
            # as the literal text "None" wherever this dict is later
            # interpolated into a report.
            text = str(raw_text) if raw_text is not None else "(no caveat text recorded)"
            sources = entry.get("sources", []) if isinstance(entry, dict) else []
            items.append(
                ConflictItem(
                    conflict_type="ACMG criterion internal caveat",
                    category="ACMG Criterion",
                    evidence_a={"source": ", ".join(sources) or "ACMG rule engine", "statement": text},
                    evidence_b={
                        "source": "n/a",
                        "statement": "No opposing evidence item; this is a caveat on the criterion's own applicability, not a two-sided disagreement.",
                    },
                    severity="Minor",
                    resolution="Criterion evaluation stands as recorded; caveat retained for reviewer awareness.",
                    resolution_rationale="This is a limitation the ACMG rule engine itself flagged while evaluating the criterion (e.g. an estimated coordinate or partial evidence), not a disagreement between two independent sources.",
                    confidence_impact="Already counted in the confidence engine's conflict penalty (each ACMG-criterion conflicting-evidence item is counted as one conflict unit there).",
                    priority_impact="Already counted in the prioritization engine's conflict penalty on the same basis.",
                )
            )
        return items

    @staticmethod
    def _expert_panel_disagreement_conflict(
        acmg_classification: Optional[str], clinvar_result: Optional[Dict[str, Any]]
    ) -> Optional[ConflictItem]:
        """
        Critical-tier conflict, and deliberately the only detector in
        this engine at that tier: GEPER's own bottom-line ACMG
        classification disagrees with a ClinVar record reviewed by an
        expert panel or endorsed as a practice guideline (ClinVar's own
        two highest-confidence review tiers) for this *exact* variant --
        gated on `clinvar_result.match_status == "matched"`, i.e. only
        once `database/clinvar_client.py::_variant_match` has confirmed
        this is the same allele, not merely a co-located ClinVar record.

        Every other detector in this engine documents a disagreement
        GEPER's rule engine already accounts for in some way (a
        cross-reference note, a lower confidence score, a priority
        factor) without the classification itself being in question.
        This one means the number a clinician will actually act on
        contradicts the most authoritative external classification
        available for this variant -- not a caveat to note alongside
        the result, but a reason to hold it for manual review.
        """
        if not (
            clinvar_result and clinvar_result.get("match_status") == "matched" and clinvar_result.get("primary_record")
        ):
            return None
        if not acmg_classification:
            return None

        top = clinvar_result["primary_record"]
        review_status = (top.get("review_status") or "").strip().lower()
        if review_status not in _AUTHORITATIVE_CLINVAR_REVIEW_STATUS:
            return None

        if not ConflictResolutionEngine._clinvar_disagrees(acmg_classification, top):
            return None

        return ConflictItem(
            conflict_type="GEPER classification disagrees with an expert-panel/practice-guideline ClinVar classification",
            category="Clinical",
            evidence_a={
                "source": "ClinVar",
                "statement": f"'{top.get('clinical_significance')}' (review status: {top.get('review_status')}).",
            },
            evidence_b={"source": "GEPER ACMG engine", "statement": f"classification: '{acmg_classification}'."},
            severity="Critical",
            resolution=(
                "This variant's GEPER classification must be manually reviewed before clinical use. GEPER's "
                "ACMG rule engine treats ClinVar as a cross-reference, not a direct classification input, so "
                "this disagreement did not automatically change the classification above -- but a mismatch "
                "against ClinVar's most authoritative review tier is the strongest signal this engine can "
                "raise that GEPER's primary-evidence-based classification may be missing something ClinVar's "
                "curators saw (or vice versa)."
            ),
            resolution_rationale=(
                "ClinVar records reviewed by an expert panel or endorsed as a practice guideline are ClinVar's "
                "own two highest-confidence review tiers (3- and 4-star), reflecting multi-submitter or "
                "guideline-level curation rather than a single lab's assertion -- disagreement with one of "
                "these is materially more serious than disagreement with an unreviewed or single-submitter "
                "record, which is why this is GEPER's highest-severity reviewer flag."
            ),
            confidence_impact="Not currently counted in the confidence engine's numeric conflict penalty.",
            priority_impact="Not currently counted in the prioritization engine's numeric conflict penalty.",
        )

    @staticmethod
    def _classification_tier(classification: str) -> Optional[str]:
        """
        Maps a classification string (ClinVar's `clinical_significance`
        or GEPER's own ACMG `classification`) to one of three coarse
        tiers -- "pathogenic", "benign", "uncertain" -- or `None` when it
        matches none of them (e.g. "risk factor", "drug response",
        "not provided": genuinely not comparable, so `_clinvar_disagrees`
        stays silent rather than guessing a direction for text it
        doesn't recognize).
        """
        s = classification.strip().lower()
        # Checked first: "conflicting classifications/interpretations OF
        # PATHOGENICITY" contains the substring "pathogenic" too, so it
        # must be matched before the pathogenic-leaning check below or
        # it would be misread as a pathogenic-direction assertion.
        if any(k in s for k in _UNCERTAIN_LEANING_CLASSIFICATIONS):
            return "uncertain"
        if any(k in s for k in _PATHOGENIC_LEANING_CLASSIFICATIONS):
            return "pathogenic"
        if any(k in s for k in _BENIGN_LEANING_CLASSIFICATIONS):
            return "benign"
        return None

    @staticmethod
    def _clinvar_disagrees(acmg_classification: str, primary_record: Dict[str, Any]) -> bool:
        """
        Shared classification-tier disagreement check used by every
        ClinVar-vs-GEPER detector at every review-status tier.

        Report review round 4, I4: the previous version only checked
        one direction -- "ClinVar asserts pathogenic/benign and GEPER
        doesn't assert the same direction" -- which silently missed the
        mirror case where GEPER stakes out a specific pathogenic/benign
        call and ClinVar's own expert panel explicitly could NOT
        ("Uncertain significance"). That asymmetry meant a GEPER "Likely
        Pathogenic" vs. an expert-panel "Uncertain significance" never
        flagged, even though an expert panel being unable to reach a
        confident call on the exact variant GEPER called confidently is
        at least as reportable as the reverse. Comparing both sides'
        *tier* (`_classification_tier`) instead of ClinVar's tier alone
        is symmetric by construction: it disagrees whenever the two
        tiers differ, in either direction, and -- deliberately --
        Pathogenic vs. Likely Pathogenic (and Benign vs. Likely Benign)
        still agree, since both land in the same tier; adjacent-tier
        agreement was never the bug, only the missing uncertain-tier
        comparison was.
        """
        clinvar_tier = ConflictResolutionEngine._classification_tier(primary_record.get("clinical_significance") or "")
        geper_tier = ConflictResolutionEngine._classification_tier(acmg_classification)
        if clinvar_tier is None or geper_tier is None:
            return False
        return clinvar_tier != geper_tier

    @staticmethod
    def _curated_clinvar_disagreement_conflict(
        acmg_classification: Optional[str], clinvar_result: Optional[Dict[str, Any]]
    ) -> Optional[ConflictItem]:
        """
        Moderate-tier counterpart to `_expert_panel_disagreement_conflict`
        (report review round 4, E3): a matched ClinVar record still
        reflects a genuine, curated classification (1- or 2-star --
        "criteria provided, single submitter" or "criteria provided,
        multiple submitters, no conflicts"), just not at expert-panel/
        practice-guideline confidence, so A3's tier design correctly
        keeps it below Critical -- but before this detector existed,
        this exact disagreement had NO dedicated signal at all. Its
        prior "Moderate" severity (pre-D4) was an accident of how many
        unrelated ACMG-criterion caveats happened to also be present on
        the finding, which D4 correctly stopped counting -- leaving a
        real classification disagreement under-represented rather than
        removing a false positive. This detector gives it an explicit,
        caveat-count-independent floor instead.
        """
        if not (
            clinvar_result and clinvar_result.get("match_status") == "matched" and clinvar_result.get("primary_record")
        ):
            return None
        if not acmg_classification:
            return None

        top = clinvar_result["primary_record"]
        review_status = (top.get("review_status") or "").strip().lower()
        if review_status not in _CURATED_CLINVAR_REVIEW_STATUS:
            return None

        if not ConflictResolutionEngine._clinvar_disagrees(acmg_classification, top):
            return None

        return ConflictItem(
            conflict_type="GEPER classification disagrees with a curated (non-expert-panel) ClinVar classification",
            category="Clinical",
            evidence_a={
                "source": "ClinVar",
                "statement": f"'{top.get('clinical_significance')}' (review status: {top.get('review_status')}).",
            },
            evidence_b={"source": "GEPER ACMG engine", "statement": f"classification: '{acmg_classification}'."},
            severity="Moderate",
            resolution=(
                "This variant's GEPER classification should be manually reviewed before clinical use. GEPER's "
                "ACMG rule engine treats ClinVar as a cross-reference, not a direct classification input, so "
                "this disagreement did not automatically change the classification above -- but ClinVar's "
                "record here still reflects independently curated submitter assertions, not an unreviewed "
                "single claim, so the disagreement is worth a reviewer's attention even though it does not "
                "meet the expert-panel/practice-guideline bar for GEPER's highest-severity flag."
            ),
            resolution_rationale=(
                "1- and 2-star ClinVar records ('criteria provided, single submitter' / 'criteria provided, "
                "multiple submitters, no conflicts') apply documented ACMG-style criteria, unlike the 0-star "
                "tiers ('no assertion provided'/'no assertion criteria provided'), and -- for the 2-star case "
                "in particular -- multiple submitters agree, unlike 'criteria provided, conflicting "
                "classifications' where ClinVar's own submitters are already split. A disagreement against "
                "this tier is real evidence worth a floor of its own, independent of how many (if any) "
                "ACMG-criterion caveats this specific finding happens to also carry."
            ),
            confidence_impact="Not currently counted in the confidence engine's numeric conflict penalty.",
            priority_impact="Not currently counted in the prioritization engine's numeric conflict penalty.",
        )

    @staticmethod
    def _clinical_conflict(
        clinvar_result: Optional[Dict[str, Any]], clingen_result: Optional[Dict[str, Any]]
    ) -> Optional[ConflictItem]:
        if not (
            clinvar_result and clinvar_result.get("match_status") == "matched" and clinvar_result.get("primary_record")
        ):
            return None
        if not (clingen_result and clingen_result.get("found")):
            return None
        top = clinvar_result["primary_record"]
        sig = (top.get("clinical_significance") or "").lower()
        validity = (clingen_result.get("clinical_validity_summary") or "").lower()
        if "pathogenic" not in sig:
            return None
        if validity not in ("disputed", "refuted", "limited"):
            return None
        return ConflictItem(
            conflict_type="ClinVar pathogenicity vs ClinGen gene-disease validity",
            category="Clinical",
            evidence_a={
                "source": "ClinVar",
                "statement": f"Variant classified as '{top.get('clinical_significance')}'.",
            },
            evidence_b={
                "source": "ClinGen",
                "statement": f"Gene-disease validity for {clingen_result.get('gene_symbol', 'this gene')} curated as '{clingen_result.get('clinical_validity_summary')}'.",
            },
            severity="Major",
            resolution="ACMG classification is unchanged by this conflict. GEPER's ACMG rule engine treats ClinVar as a cross-reference, not a direct classification input (see acmg_evaluation.clinvar_crossreference), so a pathogenic ClinVar assertion in a gene with weak ClinGen validity does not by itself alter the ACMG result -- but this combination warrants manual review before clinical use.",
            resolution_rationale="A pathogenic variant call is only as trustworthy as the underlying gene-disease relationship. ClinGen's curated validity rating is the more conservative, systematically-curated signal here.",
            confidence_impact="Not currently counted in the confidence engine's numeric conflict penalty (that penalty only counts ACMG-criterion-level conflicts and AI-model disagreement); the Clinical Evidence category score in confidence_breakdown does, however, already reflect ClinGen's validity rating directly.",
            priority_impact="Not currently counted in the prioritization engine's numeric conflict penalty; the Clinical Evidence priority factor is computed as an average across ClinVar and ClinGen, so a weak ClinGen validity already partially offsets a pathogenic ClinVar signal there.",
        )

    @staticmethod
    def _population_conflict(
        acmg_classification: Optional[str],
        clinvar_result: Optional[Dict[str, Any]],
        gnomad_result: Optional[Dict[str, Any]],
    ) -> Optional[ConflictItem]:
        gcfg = CONFIG.gnomad
        if not (gnomad_result and gnomad_result.get("found")):
            return None
        af = gnomad_result.get("global_af")
        if af is None or af < gcfg.BS1_AF_THRESHOLD:
            return None
        pathogenic_leaning = acmg_classification in ("Pathogenic", "Likely Pathogenic")
        clinvar_primary = (
            clinvar_result["primary_record"]
            if clinvar_result
            and clinvar_result.get("match_status") == "matched"
            and clinvar_result.get("primary_record")
            else None
        )
        clinvar_pathogenic = bool(
            clinvar_primary and "pathogenic" in (clinvar_primary.get("clinical_significance") or "").lower()
        )
        if not (pathogenic_leaning or clinvar_pathogenic):
            return None
        severity = "Major" if af >= gcfg.BA1_AF_THRESHOLD else "Moderate"
        clinical_side = []
        clinical_sources = []
        if pathogenic_leaning:
            clinical_side.append(f"ACMG classification: {acmg_classification}")
            clinical_sources.append("GEPER ACMG engine")
        if clinvar_pathogenic and clinvar_primary:
            # `clinvar_pathogenic` being True already implies
            # `clinvar_primary` is truthy (line above), but mypy can't
            # narrow `clinvar_primary`'s type across that separate bool
            # variable -- the redundant `and clinvar_primary` here is
            # purely a type-narrowing aid, not a behavior change.
            clinical_side.append(f"classification: {clinvar_primary.get('clinical_significance')}")
            clinical_sources.append("ClinVar")
        return ConflictItem(
            conflict_type="Population rarity inconsistent with pathogenic-leaning clinical interpretation",
            category="Population",
            evidence_a={
                "source": "gnomAD",
                "statement": f"Global allele frequency = {af:.2e}, {'above the BA1 common-variant threshold' if severity == 'Major' else 'above the BS1 expected-disease-frequency threshold'} ({gcfg.BA1_AF_THRESHOLD:.2e} / {gcfg.BS1_AF_THRESHOLD:.2e}).",
            },
            evidence_b={"source": " & ".join(clinical_sources), "statement": "; ".join(clinical_side)},
            severity=severity,
            resolution=(
                "ACMG classification is unchanged by this engine. If BA1/BS1 were correctly triggered by "
                "Phase 1's rule engine, they already pull the classification toward Benign/Likely Benign on "
                "their own (BA1 is stand-alone benign); if the classification is still pathogenic-leaning "
                "despite this allele frequency, that combination should be manually reviewed."
            ),
            resolution_rationale="Population frequency this common is difficult to reconcile with a highly penetrant pathogenic variant for most Mendelian disorders; gnomAD's frequency estimate is direct, large-scale population data.",
            confidence_impact="Not currently counted in the confidence engine's numeric conflict penalty; Population Evidence quality in confidence_breakdown reflects the frequency value itself, not this cross-category comparison.",
            priority_impact="Partially reflected: the Population Rarity priority factor already scores this variant low on rarity grounds, which pulls priority_score down independent of this conflict check.",
        )

    @staticmethod
    def _ai_conflict(
        ai_consensus: Optional[List[Dict[str, Any]]],
        confidence_penalty: float,
        confidence_explanation: str,
        priority_penalty: float,
        priority_explanation: str,
    ) -> Optional[ConflictItem]:
        directions = _ai_directions(ai_consensus)
        distinct = set(directions.values())
        if len(distinct) <= 1:
            return None
        # Same caveat wording `acmg_rules.py::_pp3_bp4` established,
        # shortened to fit this statement's shape: `directions` carries
        # only a direction ("likely_pathogenic"/"likely_benign"), never
        # `am_pathogenicity`, so there is no score for the "raw model
        # score" sentence to attach to -- the clinical-use-status clause
        # alone is what's factually available to disclose here.
        am = next(
            (
                f"AlphaMissense: {d} (not clinically validated; not approved for clinical use)"
                for s, d in directions.items()
                if s == "AlphaMissense"
            ),
            None,
        )
        mm = next((f"MMSplice: {d}" for s, d in directions.items() if s == "MMSplice"), None)
        return ConflictItem(
            conflict_type="AlphaMissense vs MMSplice direction disagreement (AI consensus discordant)",
            category="AI",
            evidence_a={"source": "AlphaMissense", "statement": am or "n/a"},
            evidence_b={"source": "MMSplice", "statement": mm or "n/a"},
            severity="Moderate",
            resolution="ACMG classification is unchanged. Both predictions are retained in ai_consensus for review; PP3/BP4 in the ACMG evaluation already require the two predictors to agree before contributing evidence in either direction, so a discordant pair like this does not spuriously trigger either criterion.",
            resolution_rationale="AlphaMissense scores missense pathogenicity; MMSplice scores splicing disruption -- they can legitimately disagree because they model different molecular mechanisms, but a pathogenic-vs-benign disagreement at the variant level still warrants a closer look.",
            confidence_impact=f'Already counted: this discordance contributes to the confidence engine\'s conflict penalty ({_pct(confidence_penalty)}; "{confidence_explanation}").',
            priority_impact=f'Already counted: this discordance contributes to the prioritization engine\'s conflict penalty ({_pct(priority_penalty)}; "{priority_explanation}").',
        )

    @staticmethod
    def _protein_conflict(
        uniprot_result: Optional[Dict[str, Any]], interpro_result: Optional[Dict[str, Any]]
    ) -> Optional[ConflictItem]:
        if not (uniprot_result and uniprot_result.get("found") and uniprot_result.get("reviewed")):
            return None
        if interpro_result and interpro_result.get("found"):
            return None  # InterPro data is present; no coverage gap to flag
        return ConflictItem(
            conflict_type="UniProt reviewed entry present but no InterPro/Pfam domain annotation resolved",
            category="Protein",
            evidence_a={
                "source": "UniProt",
                "statement": f"Reviewed (Swiss-Prot) entry found ({uniprot_result.get('accession', 'n/a')}): {uniprot_result.get('protein_name', 'n/a')}.",
            },
            evidence_b={"source": "InterPro", "statement": "No InterPro/Pfam annotation resolved for this protein."},
            severity="Minor",
            resolution="ACMG classification is unchanged. This is an annotation-coverage gap, not a biological disagreement between UniProt and InterPro -- PM1 (conserved-domain criterion) simply could not be evaluated for this variant as a result (see not_evaluated_rules / limitations).",
            resolution_rationale="A well-characterized (reviewed) UniProt entry existing without any InterPro/Pfam domain hit is unusual and worth noting, but is most often explained by the protein lacking well-defined Pfam domains, or an InterPro lookup gap, rather than a factual disagreement.",
            confidence_impact="Not separately counted; already implicitly reflected in the Protein Knowledge category's lower completeness score in confidence_breakdown (InterPro presence contributes to that category's score).",
            priority_impact="Not separately counted; the Conserved Domain priority factor already scores 0 in this scenario since PM1 cannot trigger without InterPro data.",
        )

    @staticmethod
    def _structural_conflict(
        alphafold_result: Optional[Dict[str, Any]], ai_consensus: Optional[List[Dict[str, Any]]]
    ) -> Optional[ConflictItem]:
        """
        Only fires against a genuine residue-specific confidence value
        (`affected_residue_band`) -- NOT a fallback to `mean_plddt_band`
        (report review round 4, E2). `mean_plddt_band` is the WHOLE
        protein's average confidence, computed independently of any
        variant; it is only present in `alphafold_result` at all because
        `pipeline/alphafold/lookup.py::AlphaFoldLookup.query_variant`
        falls back to the accession-level (position-independent) lookup
        whenever `canonical_protein_position` returned None -- which it
        always does for a splice-site/intronic variant, since there is
        no coding position to map (see that function's own docstring).
        For a large, mostly-disordered protein like BRCA1 (folded only
        in a few domains such as RING/BRCT), the whole-protein average
        is "low" almost by construction, regardless of whether the
        actually-affected position is itself disordered -- so treating
        it as "confidence at the affected residue" fabricated a claim
        about a position AlphaFold was never actually asked about, and
        spuriously conflicted with a splicing predictor's "damaging"
        call that has nothing to do with protein structure in the first
        place. Missing residue-specific confidence is an honest "not
        applicable" here, not evidence of low confidence.
        """
        cfg = CONFIG.conflict
        if not (alphafold_result and alphafold_result.get("found")):
            return None
        band = (alphafold_result.get("affected_residue_band") or "").lower()
        if band not in cfg.LOW_STRUCTURAL_CONFIDENCE_BANDS:
            return None
        directions = _ai_directions(ai_consensus)
        if "damaging" not in directions.values():
            return None
        damaging_sources = [s for s, d in directions.items() if d == "damaging"]
        return ConflictItem(
            conflict_type="Low AlphaFold structural confidence vs predicted damaging functional effect",
            category="Structure",
            evidence_a={
                "source": "AlphaFold DB",
                "statement": f"Structural confidence at the affected residue is '{band}' (AlphaFold DB's own low-confidence bands, typically indicating an intrinsically disordered or poorly-modeled region).",
            },
            evidence_b={
                "source": " & ".join(damaging_sources),
                "statement": f"{', '.join(damaging_sources)} predict{'s' if len(damaging_sources) == 1 else ''} a damaging effect for this variant.",
            },
            severity="Moderate",
            resolution="ACMG classification is unchanged. PM1/PP3 in the ACMG evaluation are driven by InterPro domain overlap and AlphaMissense/MMSplice respectively, not by AlphaFold confidence directly, so this conflict does not itself alter the classification -- but a damaging call in a low-confidence structural region deserves closer review, since disordered regions are less amenable to structure-based interpretation.",
            resolution_rationale="A low pLDDT region is often intrinsically disordered rather than misfolded, which can mean a 'damaging' sequence-based prediction reflects a real functional (e.g. linear-motif) effect that structure prediction simply can't resolve -- not necessarily that the prediction is wrong, but that structural evidence can't corroborate it either way.",
            confidence_impact="Not currently counted in the confidence engine's numeric conflict penalty; Structural Biology quality in confidence_breakdown already scores lower for a low-confidence band, which is the correct standalone treatment, but the cross-category tension with AI evidence isn't separately penalized.",
            priority_impact="Not currently counted in the prioritization engine's numeric conflict penalty; the Structural Evidence priority factor scores this case low on its own, but does not reduce the AI Agreement factor's contribution.",
        )

    @staticmethod
    def _sequence_conflict_note(blast_result: Optional[Dict[str, Any]]) -> ConflictItem:
        # Per the Phase 6 "do not fabricate evidence" requirement: BLAST
        # returns a homology hit count with no pathogenic/benign
        # direction, and Ensembl is not exposed as a separate per-variant
        # evidence dict in this pipeline (see prioritization_engine.py /
        # interpretation_result.py docstrings for the same point) -- so
        # there is no comparable directional signal here to check for
        # agreement or disagreement against. Reported honestly as
        # not-evaluable rather than skipped silently or invented.
        # Ruling #18 (2026-09-11): a count only when one was recorded. Every
        # not-run BLAST path reports `hit_count: 0` WITH `skipped: True`, so
        # the 0 is not a result there -- it used to be printed as one.
        blast = blast_result or {}
        hits = blast.get("hit_count")
        if blast.get("skipped"):
            blast_statement = (
                f"BLAST not run for this variant ({blast.get('reason') or 'no reason recorded'}) "
                "-- not evidence of no homology."
            )
        elif hits is None:
            blast_statement = "BLAST hit count not recorded -- not evidence of no homology."
        else:
            blast_statement = (
                f"{hits} homology hit(s) returned (no pathogenic/benign direction associated with this result)."
            )
        return ConflictItem(
            conflict_type="BLAST / Ensembl consistency check",
            category="Sequence",
            evidence_a={
                "source": "BLAST",
                "statement": blast_statement,
            },
            evidence_b={
                "source": "Ensembl",
                "statement": "Not exposed as a separate per-variant evidence source in this pipeline; used internally for sequence-context retrieval only.",
            },
            severity="Not evaluable",
            resolution="No conflict check performed.",
            resolution_rationale="GEPER has no directional (pathogenic/benign) signal from either BLAST or Ensembl to compare against other evidence, so a genuine conflict cannot be detected here without fabricating one.",
            confidence_impact="Not applicable.",
            priority_impact="Not applicable.",
        )

    # ------------------------------------------------------------------
    # Scoring / summary
    # ------------------------------------------------------------------

    @staticmethod
    def _score(real_conflicts: List[ConflictItem], cfg: ConflictConfig) -> float:
        if not real_conflicts:
            return 0.0
        total_weight = sum(getattr(cfg, _SEVERITY_WEIGHT_KEY[c.severity]) for c in real_conflicts)
        max_weight = cfg.SATURATION_WEIGHT_UNITS * cfg.MAJOR_WEIGHT
        return min(100.0, 100.0 * total_weight / max_weight) if max_weight > 0 else 0.0

    @staticmethod
    def _overall_severity(score: float, real_conflicts: List[ConflictItem], cfg: ConflictConfig) -> str:
        if not real_conflicts:
            return "None"
        # A single Critical-tier item (currently only
        # `_expert_panel_disagreement_conflict`) forces the overall
        # severity outright -- it is never diluted by averaging against
        # lower-severity items the way the score-threshold tiers below
        # are, since it means the classification itself is in doubt.
        if any(c.severity == "Critical" for c in real_conflicts):
            return "Critical"
        if score >= cfg.MAJOR_THRESHOLD:
            score_tier = "Major"
        elif score >= cfg.MODERATE_THRESHOLD:
            score_tier = "Moderate"
        else:
            score_tier = "Minor"
        # A genuine single-item disagreement (e.g. a matched-but-not-
        # expert-panel ClinVar record, `_curated_clinvar_disagreement_
        # conflict`) is never diluted below its own labeled severity by
        # the weighted-score threshold math above (report review round
        # 4, E3): that math is tuned for "how many/how severe conflicts
        # accumulated", which correctly needs several items to cross
        # into "Moderate"/"Major" -- but a lone item that is ITSELF
        # tagged Moderate or Major is a real, specific disagreement a
        # reviewer should see at that severity regardless of how many
        # (or how few) other, unrelated conflicts this finding also
        # carries. The floor is the highest individual severity present;
        # the score-threshold tier can only raise it further, never
        # lower it.
        severity_rank = {"Minor": 0, "Moderate": 1, "Major": 2}
        highest_item_tier = max(
            (c.severity for c in real_conflicts if c.severity in severity_rank),
            key=lambda s: severity_rank[s],
            default="Minor",
        )
        return max(score_tier, highest_item_tier, key=lambda s: severity_rank[s])

    @staticmethod
    def _summary(real_conflicts: List[ConflictItem], severity: str) -> str:
        if not real_conflicts:
            return "No significant conflicting evidence detected."
        by_type = ", ".join(f"{c.conflict_type} ({c.severity})" for c in real_conflicts)
        return f"{len(real_conflicts)} conflict(s) detected, overall severity {severity}: {by_type}."

    @staticmethod
    def _overall_resolution(real_conflicts: List[ConflictItem], acmg_classification: Optional[str]) -> str:
        if not real_conflicts:
            return "No conflicts to resolve."
        return (
            f"ACMG classification ('{acmg_classification or 'not classified'}') is unchanged by this engine "
            f"-- see each conflict's individual 'resolution' for how GEPER's existing rule engine already "
            f"handles (or explicitly does not yet numerically account for) that specific disagreement. All "
            f"{len(real_conflicts)} conflict(s) above are flagged for manual reviewer attention before "
            f"clinical use."
        )
