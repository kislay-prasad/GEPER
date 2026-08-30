"""
Phase 4 -- Variant Prioritization Engine.

Answers a different question than Phase 1 (ACMG classification) or
Phase 3 (confidence): not "is this variant pathogenic" or "how much/
how consistent is the evidence", but "how urgently does this variant
deserve human review, given everything GEPER found." It is independent
of both in the sense that it computes its own score with its own
weights -- but, per the Phase 4 spec, it *uses* the ACMG classification
and confidence score as two of its weighted inputs rather than ignoring
or recomputing them.

Same no-magic-numbers discipline as Phase 3: every weight and threshold
is a documented, environment-overridable field on
`CONFIG.prioritization` (see config.py's `PrioritizationConfig`), and
every factor's contribution is individually explained in the output.

Reuse rather than reimplementation: rather than re-deriving "does this
variant hit a conserved domain" or "is this a loss-of-function variant"
from the raw protein/InterPro dicts a second time, this engine reads
that straight off the ACMG triggered-rule codes Phase 1 already
computed (PM1 = domain overlap, PVS1/PM4 = protein-impact), and reuses
`ConfidenceEngine._structural_quality` for the structural-evidence
factor instead of duplicating its AlphaFold-band mapping.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from config import CONFIG
from pipeline.confidence_engine import ConfidenceEngine

_ACMG_DIRECTIONAL_SCORE = {
    "Pathogenic": 1.0,
    "Likely Pathogenic": 0.75,
    "Uncertain Significance": 0.35,
    "Likely Benign": 0.05,
    "Benign": 0.0,
}

_CLINVAR_DIRECTIONAL_SCORE = {
    "pathogenic": 1.0,
    "likely pathogenic": 0.75,
    "uncertain significance": 0.3,
    "likely benign": 0.05,
    "benign": 0.0,
}

_CLINGEN_VALIDITY_SCORE = {
    "definitive": 1.0,
    "strong": 0.85,
    "moderate": 0.5,
    "limited": 0.25,
    "disputed": 0.05,
    "refuted": 0.0,
}

_SPLICE_SEVERITY_SCORE = {
    "strong_donor_loss": 1.0,
    "strong_acceptor_loss": 1.0,
    "exon_skipping": 1.0,
    "intron_retention": 0.9,
    "strong": 1.0,
    "moderate": 0.6,
    "weak": 0.2,
}


@dataclass
class PriorityFactor:
    factor: str
    weight: float
    value: float  # 0-1, direction-aware (1.0 = strongly raises priority)
    contribution: float
    rationale: str
    reason_tag: Optional[str] = None  # short checkmark-style reason, or None if not notable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "factor": self.factor,
            "weight": self.weight,
            "value": round(self.value, 3),
            "contribution": round(self.contribution, 3),
            "rationale": self.rationale,
        }


@dataclass
class PriorityResult:
    score: float  # 0-100
    category: str  # "Critical" | "High" | "Moderate" | "Low"
    rank: Optional[int]  # position within a batch; None for a single variant scored alone
    factor_breakdown: List[PriorityFactor]
    conflict_penalty_applied: float
    conflict_explanation: str
    raw_score_before_penalty: float
    explanation: List[str]  # ["✓ Pathogenic ClinVar record", ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "category": self.category,
            "rank": self.rank,
            "raw_score_before_conflict_penalty": round(self.raw_score_before_penalty, 1),
            "conflict_penalty_applied": round(self.conflict_penalty_applied, 3),
            "conflict_explanation": self.conflict_explanation,
            "factor_breakdown": [f.to_dict() for f in self.factor_breakdown],
            "explanation": self.explanation,
        }


class PrioritizationEngine:
    """
    Scores one variant's review urgency from evidence GEPER already
    collected for it (including Phase 1's ACMG result and Phase 3's
    confidence result, passed in as inputs -- never recomputed here).
    """

    def __init__(self):
        # Reused only for its static AlphaFold-band mapping (see
        # `_structural_factor`) -- no confidence state is read or
        # written here.
        self._confidence_engine = ConfidenceEngine()

    def score(
        self,
        *,
        acmg_classification: Optional[str],
        confidence_score: Optional[float],
        triggered_rule_codes: List[str],
        clinvar_result: Dict[str, Any] = None,
        clingen_result: Dict[str, Any] = None,
        gnomad_result: Dict[str, Any] = None,
        dbsnp_result: Dict[str, Any] = None,
        alphamissense_result: Dict[str, Any] = None,
        mmsplice_result: Dict[str, Any] = None,
        dna_models_used: List[str] = None,
        rna_result: Dict[str, Any] = None,
        protein_result: Dict[str, Any] = None,
        interpro_result: Dict[str, Any] = None,
        alphafold_result: Dict[str, Any] = None,
        blast_result: Dict[str, Any] = None,
        ai_consensus: List[Dict[str, Any]] = None,
        real_conflicts: List[Any] = None,
        critical_conflict: bool = False,
    ) -> PriorityResult:
        cfg = CONFIG.prioritization
        triggered = set(triggered_rule_codes or [])

        factors: List[PriorityFactor] = []
        reasons: List[str] = []

        factors.append(self._acmg_factor(acmg_classification, cfg.ACMG_WEIGHT, reasons))
        factors.append(self._confidence_factor(confidence_score, cfg.CONFIDENCE_WEIGHT, reasons))
        factors.append(self._clinical_factor(clinvar_result, clingen_result, cfg.CLINICAL_WEIGHT, reasons))
        factors.append(self._population_rarity_factor(gnomad_result, cfg.POPULATION_RARITY_WEIGHT, reasons))
        factors.append(self._ai_agreement_factor(ai_consensus, cfg.AI_AGREEMENT_WEIGHT, reasons))
        factors.append(self._protein_impact_factor(triggered, cfg.PROTEIN_IMPACT_WEIGHT, reasons))
        factors.append(self._conserved_domain_factor(triggered, interpro_result, cfg.CONSERVED_DOMAIN_WEIGHT, reasons))
        factors.append(self._structural_factor(alphafold_result, cfg.STRUCTURAL_WEIGHT, reasons))
        factors.append(self._splicing_factor(mmsplice_result, cfg.SPLICING_WEIGHT, reasons))
        factors.append(
            self._sequence_context_factor(dna_models_used, rna_result, protein_result, cfg.SEQUENCE_CONTEXT_WEIGHT)
        )
        factors.append(self._additional_factor(blast_result, cfg.ADDITIONAL_WEIGHT))

        max_possible = sum(f.weight for f in factors)
        raw_sum = sum(f.contribution for f in factors)
        raw_score = 100.0 * (raw_sum / max_possible) if max_possible > 0 else 0.0

        penalty, penalty_explanation = self._conflict_penalty(real_conflicts, cfg)
        if penalty > 0:
            reasons.append(f"✗ {penalty_explanation}")
        final_score = max(0.0, raw_score * (1.0 - penalty))

        category = self._category(final_score, cfg)
        if critical_conflict and category != "Critical":
            # Report review round 4, I2: review priority was previously
            # driven purely by this engine's own evidence-completeness
            # factors, which are near-orthogonal to whether GEPER's
            # classification disagrees with an expert-panel/practice-
            # guideline ClinVar record (a synonymous or otherwise
            # evidence-sparse variant scores low on Protein Impact/
            # Conserved Domain/Splicing regardless of how urgent the
            # classification disagreement itself is) -- so a Critical
            # clinical conflict could rank *below* findings with no
            # conflict at all. Floored here the same shape
            # `ConflictResolutionEngine._overall_severity` already uses
            # for conflict severity itself: a single Critical-tier
            # signal overrides the score-threshold tier outright, never
            # diluted by averaging against this engine's other factors.
            category = "Critical"
            reasons.append(
                "✗ Review priority floored at Critical: Bij AI's classification disagrees with an "
                "expert-panel/practice-guideline ClinVar record."
            )

        return PriorityResult(
            score=final_score,
            category=category,
            rank=None,  # filled in by `rank_batch` once every variant in a run is scored
            factor_breakdown=factors,
            conflict_penalty_applied=penalty,
            conflict_explanation=penalty_explanation,
            raw_score_before_penalty=raw_score,
            explanation=reasons,
        )

    # ------------------------------------------------------------------
    # Factor scorers. Each is direction-aware (unlike Phase 3's
    # completeness/quality split): `value` close to 1.0 means "this
    # evidence argues for reviewing this variant urgently", not just
    # "this evidence exists".
    # ------------------------------------------------------------------

    @staticmethod
    def _acmg_factor(classification, weight, reasons) -> PriorityFactor:
        value = _ACMG_DIRECTIONAL_SCORE.get(classification or "", 0.0)
        if classification in ("Pathogenic", "Likely Pathogenic"):
            reasons.append(f"✓ ACMG classification: {classification}")
        rationale = (
            f"ACMG classification is '{classification}'." if classification else "No ACMG classification available."
        )
        return PriorityFactor("ACMG Classification", weight, value, weight * value, rationale)

    @staticmethod
    def _confidence_factor(confidence_score, weight, reasons) -> PriorityFactor:
        if confidence_score is None:
            return PriorityFactor(
                "Confidence Score", weight, 0.0, 0.0, "Confidence score not yet available for this variant."
            )
        value = max(0.0, min(1.0, confidence_score / 100.0))
        if confidence_score >= CONFIG.confidence.HIGH_THRESHOLD:
            reasons.append(f"✓ High confidence score ({confidence_score:.1f}%)")
        return PriorityFactor(
            "Confidence Score",
            weight,
            value,
            weight * value,
            f"Confidence score is {confidence_score:.1f}%.",
        )

    @staticmethod
    def _clinical_factor(clinvar_result, clingen_result, weight, reasons) -> PriorityFactor:
        parts, notes = [], []
        if clinvar_result and clinvar_result.get("match_status") == "matched" and clinvar_result.get("primary_record"):
            top = clinvar_result["primary_record"]
            sig = (top.get("clinical_significance") or "").strip().lower()
            v = _CLINVAR_DIRECTIONAL_SCORE.get(sig, 0.2 if sig else 0.0)
            parts.append(v)
            notes.append(f"ClinVar classifies this variant as '{top.get('clinical_significance')}'.")
            if v >= 0.75:
                reasons.append(f"✓ {'Pathogenic' if v == 1.0 else 'Likely pathogenic'} ClinVar record")
        elif clinvar_result and clinvar_result.get("match_status") == "position_only":
            notes.append(
                "No ClinVar record found for this exact variant (other, non-matching variants exist at this position)."
            )
        else:
            notes.append("No ClinVar record found.")
        if clingen_result and clingen_result.get("found"):
            validity = (clingen_result.get("clinical_validity_summary") or "").strip().lower()
            v = _CLINGEN_VALIDITY_SCORE.get(validity, 0.3)
            parts.append(v)
            notes.append(f"ClinGen gene-disease validity: '{clingen_result.get('clinical_validity_summary')}'.")
            if v >= 0.85:
                reasons.append("✓ Strong ClinGen gene-disease validity")
        value = (sum(parts) / len(parts)) if parts else 0.0
        return PriorityFactor("Clinical Evidence", weight, value, weight * value, " ".join(notes))

    @staticmethod
    def _population_rarity_factor(gnomad_result, weight, reasons) -> PriorityFactor:
        gcfg = CONFIG.gnomad
        if not gnomad_result or gnomad_result.get("skipped") or gnomad_result.get("error") is not None:
            return PriorityFactor("Population Rarity", weight, 0.0, 0.0, "gnomAD lookup unavailable for this variant.")
        if not gnomad_result.get("found"):
            reasons.append("✓ Absent from gnomAD (very rare)")
            return PriorityFactor("Population Rarity", weight, 1.0, weight, "Variant absent from gnomAD.")
        af = gnomad_result.get("global_af")
        if af is None:
            return PriorityFactor(
                "Population Rarity", weight, 0.3, weight * 0.3, "gnomAD record found but no allele frequency reported."
            )
        if af >= gcfg.BA1_AF_THRESHOLD:
            return PriorityFactor(
                "Population Rarity",
                weight,
                0.0,
                0.0,
                f"Common variant (gnomAD AF={af:.2e} >= BA1 threshold); low priority on rarity grounds.",
            )
        if af >= gcfg.BS1_AF_THRESHOLD:
            return PriorityFactor(
                "Population Rarity",
                weight,
                0.15,
                weight * 0.15,
                f"gnomAD AF={af:.2e} exceeds expected disease frequency (BS1 range).",
            )
        if af <= gcfg.PM2_AF_THRESHOLD:
            reasons.append(f"✓ Very rare in gnomAD (AF={af:.2e})")
            return PriorityFactor(
                "Population Rarity", weight, 1.0, weight, f"gnomAD AF={af:.2e} is at/below the PM2 rarity threshold."
            )
        return PriorityFactor(
            "Population Rarity",
            weight,
            0.5,
            weight * 0.5,
            f"gnomAD AF={af:.2e} is intermediate (below BS1, above PM2 threshold).",
        )

    @staticmethod
    def _ai_agreement_factor(ai_consensus, weight, reasons) -> PriorityFactor:
        if not ai_consensus:
            return PriorityFactor(
                "AI Agreement", weight, 0.0, 0.0, "No classifying AI model (AlphaMissense/MMSplice) produced a result."
            )
        directions = set()
        names = []
        for v in ai_consensus:
            pred = (v.get("prediction") or "").lower()
            names.append(v.get("source") or v.get("model") or "model")
            if "pathogenic" in pred or pred in (
                "strong_donor_loss",
                "strong_acceptor_loss",
                "exon_skipping",
                "intron_retention",
                "strong",
                "moderate",
            ):
                directions.add("damaging")
            elif "benign" in pred:
                directions.add("benign")
        if len(directions) > 1:
            return PriorityFactor(
                "AI Agreement", weight, 0.4, weight * 0.4, f"AI models disagree in direction ({', '.join(names)})."
            )
        if directions == {"damaging"}:
            reasons.append(f"✓ {' and '.join(names)} predicts damaging effect")
            return PriorityFactor(
                "AI Agreement", weight, 1.0, weight, f"{', '.join(names)} concordantly predict a damaging effect."
            )
        if directions == {"benign"}:
            return PriorityFactor(
                "AI Agreement", weight, 0.0, 0.0, f"{', '.join(names)} concordantly predict a benign effect."
            )
        return PriorityFactor(
            "AI Agreement",
            weight,
            0.3,
            weight * 0.3,
            f"{', '.join(names)} produced results with no clear damaging/benign direction.",
        )

    @staticmethod
    def _protein_impact_factor(triggered_codes, weight, reasons) -> PriorityFactor:
        if "PVS1" in triggered_codes:
            reasons.append("✓ Predicted loss-of-function (PVS1)")
            return PriorityFactor(
                "Protein Impact",
                weight,
                1.0,
                weight,
                "PVS1 triggered: predicted null variant with established LOF disease mechanism.",
            )
        if "PM4" in triggered_codes:
            reasons.append("✓ In-frame protein-length change (PM4)")
            return PriorityFactor(
                "Protein Impact", weight, 0.6, weight * 0.6, "PM4 triggered: in-frame insertion/deletion."
            )
        return PriorityFactor(
            "Protein Impact", weight, 0.0, 0.0, "No ACMG protein-impact criterion (PVS1/PM4) triggered."
        )

    @staticmethod
    def _conserved_domain_factor(triggered_codes, interpro_result, weight, reasons) -> PriorityFactor:
        if "PM1" in triggered_codes:
            reasons.append("✓ Overlaps a conserved/functional domain (PM1)")
            return PriorityFactor(
                "Conserved Domain",
                weight,
                1.0,
                weight,
                "PM1 triggered: variant residue overlaps an annotated InterPro/Pfam domain.",
            )
        if interpro_result and interpro_result.get("found"):
            return PriorityFactor(
                "Conserved Domain",
                weight,
                0.0,
                0.0,
                "InterPro annotation available but variant does not overlap an annotated domain.",
            )
        return PriorityFactor("Conserved Domain", weight, 0.0, 0.0, "No InterPro/Pfam domain annotation available.")

    def _structural_factor(self, alphafold_result, weight, reasons) -> PriorityFactor:
        # Reuse Phase 3's AlphaFold-band mapping rather than
        # re-implementing it; only the resulting presence*quality
        # signal is used here, not any confidence-engine state.
        cat_score = self._confidence_engine._structural_quality(alphafold_result, weight=1.0)
        value = cat_score.presence * cat_score.quality
        if value >= 0.75:
            reasons.append("✓ AlphaFold structural support (high-confidence region)")
        return PriorityFactor("Structural Evidence", weight, value, weight * value, cat_score.rationale)

    @staticmethod
    def _splicing_factor(mmsplice_result, weight, reasons) -> PriorityFactor:
        if not mmsplice_result or not mmsplice_result.get("predicted"):
            return PriorityFactor(
                "Splicing Impact", weight, 0.0, 0.0, "No MMSplice prediction available for this variant."
            )
        category = mmsplice_result.get("interpretation_category")
        value = _SPLICE_SEVERITY_SCORE.get(category, 0.0)
        if value >= 0.9:
            reasons.append(f"✓ Strong predicted splicing disruption ({mmsplice_result.get('interpretation')})")
        return PriorityFactor(
            "Splicing Impact",
            weight,
            value,
            weight * value,
            f"MMSplice predicts: {mmsplice_result.get('interpretation', category)}.",
        )

    @staticmethod
    def _sequence_context_factor(dna_models_used, rna_result, protein_result, weight) -> PriorityFactor:
        # Completeness-only, exactly as in Phase 3 -- these models
        # inform routing/context, not a per-variant priority verdict.
        ran = len(dna_models_used or [])
        if rna_result and not rna_result.get("skipped") and not rna_result.get("error"):
            ran += 1
        if protein_result and protein_result.get("esm2"):
            ran += 1
        presence = min(1.0, ran / 5.0)
        return PriorityFactor(
            "Sequence Context",
            weight,
            presence,
            weight * presence,
            f"{ran} sequence-context model(s) ran; contributes completeness only, not a priority verdict.",
        )

    @staticmethod
    def _additional_factor(blast_result, weight) -> PriorityFactor:
        if blast_result and blast_result.get("hit_count", 0) > 0:
            value = 0.3  # homology hits are context, not a priority driver on their own
            return PriorityFactor(
                "Additional Evidence",
                weight,
                value,
                weight * value,
                f"BLAST returned {blast_result.get('hit_count')} homology hit(s).",
            )
        return PriorityFactor(
            "Additional Evidence",
            weight,
            0.0,
            0.0,
            "BLAST returned no hits (or was skipped/unavailable). Ensembl contributes indirectly via sequence-context "
            "retrieval and is not independently scorable here (same as in the Phase 3 confidence engine).",
        )

    # ------------------------------------------------------------------
    # Conflict penalty + category bucketing
    # ------------------------------------------------------------------

    @staticmethod
    def _conflict_penalty(real_conflicts, cfg) -> Tuple[float, str]:
        """
        HIGH-1: same change, same reasoning, as
        `ConfidenceEngine._conflict_penalty` -- see its docstring. Kept
        as a separate function rather than shared because the two
        engines' rates are configured independently on purpose
        (`GEPER_PRIORITY_CONFLICT_PENALTY` vs
        `GEPER_CONFIDENCE_CONFLICT_PENALTY`); only the *definition of a
        conflict* is now shared, via
        `ConflictResolutionEngine._real_conflicts`.

        This site matters on its own: it is what demoted TP53 R248W
        (Pathogenic, the highest net points of the five-variant run)
        from High review priority to Moderate on the strength of three
        self-flagged ACMG caveats. Fixing only the confidence engine
        would have left that demotion in place.
        """
        units = len(real_conflicts or [])
        penalty = min(cfg.MAX_CONFLICT_PENALTY, units * cfg.CONFLICT_PENALTY_PER_CONFLICT)
        if not units:
            return penalty, "No conflicting evidence detected."
        detail = "; ".join(f"{c.conflict_type} ({c.severity})" for c in real_conflicts)
        return penalty, f"{units} conflict(s) detected between independent evidence sources: {detail}."

    @staticmethod
    def _category(score: float, cfg) -> str:
        if score >= cfg.CRITICAL_THRESHOLD:
            return "Critical"
        if score >= cfg.HIGH_THRESHOLD:
            return "High"
        if score >= cfg.MODERATE_THRESHOLD:
            return "Moderate"
        return "Low"


_CATEGORY_RANK = {"Low": 0, "Moderate": 1, "High": 2, "Critical": 3}

_CONFLICT_SEVERITY_PRIORITY_FLOOR = {
    "Critical": "Critical",
    "Major": "High",
    "Moderate": "Moderate",
}


def floor_category_for_conflict_severity(
    category: Optional[str], conflict_severity: Optional[str]
) -> Tuple[str, Optional[str]]:
    """
    Report review round 5, J3: I2's floor above only special-cased
    Critical-severity conflicts, so every other conflict tier stayed
    free to sort below score-driven categories with no conflict at
    all. Confirmed live: nuclear_test PRNP P102L (GEPER VUS vs.
    ClinVar Pathogenic at 2-star) carries the run's only conflict flag
    ("Conflicting evidence (Moderate)"), yet its Protein
    Impact/Conserved Domain/Splicing factors are all quiet, so it
    scored "Low" -- the one finding a reviewer most needs to see
    sorted to the bottom of a five-finding report.

    Extends I2's same floor shape monotonically across every
    conflict-severity tier `ConflictResolutionEngine._overall_severity`
    itself produces, rather than special-casing Critical alone: Major
    conflicts floor at High, Moderate conflicts floor at Moderate.
    Minor/None apply no floor -- a single caveat-level conflict item
    is not by itself a reason to escalate review urgency.

    Only ever raises the category, never lowers it (a variant that
    already scored High on its own merits keeps High even if its only
    conflict is Moderate-severity) -- the same "never diluted"
    principle `_overall_severity` already applies to conflict severity
    itself.

    Called from `pipeline/interpretation.py` after Phase 6
    (`ConflictResolutionEngine.detect`) runs, since that is the first
    point the real, full-tier `conflict_severity` is known -- Phase 4's
    own `score()` above still applies its own cheap Critical-only
    pre-check (`ConflictResolutionEngine.has_critical_conflict`)
    immediately, rather than waiting for this, since Phase 6 needs
    Phase 4's conflict-penalty output as one of its own inputs and
    can't run first (see `has_critical_conflict`'s docstring). The two
    checks agree on the Critical tier by construction (both trace back
    to the same `_expert_panel_disagreement_conflict` detector), so
    this is a genuine no-op for Critical, not a second, divergent rule.
    """
    floor = _CONFLICT_SEVERITY_PRIORITY_FLOOR.get(conflict_severity or "")
    current = category or "Low"
    if floor is None:
        return current, None
    current_rank = _CATEGORY_RANK.get(current, 0)
    floor_rank = _CATEGORY_RANK[floor]
    if floor_rank <= current_rank:
        return current, None
    reason = (
        f"✗ Review priority floored at {floor}: conflict resolution found a '{conflict_severity}'-severity "
        "disagreement for this finding."
    )
    return floor, reason


def rank_batch(priority_scores: List[Optional[float]]) -> List[Optional[int]]:
    """
    Given the `priority_score` of every variant in a run (in processing
    order, `None` for any variant priority scoring failed/was skipped
    for), returns the 1-indexed rank of each (highest score = rank 1;
    ties keep the order they appeared in; `None` scores get `None`
    rank rather than being fabricated a position).

    This is the "Priority Rank" the Phase 4 spec asks for -- a
    variant's position *relative to the rest of the batch* -- which is
    only meaningful once every variant in a run has been scored, so it
    is computed once at the end of `orchestrator.run()` rather than
    per-variant inside `_process_variant`.
    """
    indexed = [(i, s) for i, s in enumerate(priority_scores) if s is not None]
    indexed.sort(key=lambda pair: (-pair[1], pair[0]))
    ranks: List[Optional[int]] = [None] * len(priority_scores)
    for rank, (i, _score) in enumerate(indexed, start=1):
        ranks[i] = rank
    return ranks
