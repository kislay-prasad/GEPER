"""
Phase 3 -- Confidence Scoring Engine.

Estimates how confident GEPER is in a variant's *interpretation as a
whole*, independent of whether that interpretation is Pathogenic or
Benign. This is deliberately a separate axis from ACMG classification
(Phase 1): a variant can be classified "Uncertain Significance" with
HIGH confidence (many evidence sources were checked and they cleanly
agree there just isn't enough to classify it), or classified "Likely
Pathogenic" with LOWER confidence (the classification crossed the
point threshold, but several evidence categories were unavailable or
disagreed). Report builders (Phase 5) show both numbers side by side,
never one in place of the other.

Design, per Phase 3 requirements:
  - Confidence is computed only from evidence GEPER already collected
    for this variant (the same raw provider dicts already threaded
    through `InterpretationEngine`/`InterpretationResult`) -- nothing
    is queried or fabricated here.
  - No magic numbers: every weight, penalty, and label threshold is a
    named, documented, environment-overridable field on
    `CONFIG.confidence` (see config.py's `ConfidenceConfig`).
  - Every category's contribution is individually explained in the
    output (`category_breakdown`), so Phase 7 (explainability) can
    surface "why this score" without recomputing anything.

Scoring model
-------------
Each of the 7 evidence categories below produces two numbers in [0, 1]:
  - `presence`  -- how much of that category's evidence GEPER actually
                   has for this variant (e.g. did ClinVar/ClinGen
                   return a record at all?). This is what "evidence
                   completeness" means at the category level.
  - `quality`   -- given what is present, how strong/decisive is it
                   (e.g. a ClinVar record with multiple submitters and
                   no conflicts scores higher quality than a
                   single-submitter, no-assertion-criteria record).
    `quality` is only meaningful when `presence > 0`; categories with
    no evidence contribute 0 to both by construction (never guessed).

A category's contribution to the raw score is
    weight * presence * quality
and the final percentage is that sum divided by the maximum possible
(sum of weights), scaled to 0-100, then reduced by a conflict penalty
(see `_conflict_penalty`) before being bucketed into a label using
`CONFIG.confidence`'s thresholds.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config import CONFIG


@dataclass
class CategoryScore:
    category: str
    weight: float
    presence: float  # 0-1: how much of this category's evidence exists
    quality: float  # 0-1: how strong/decisive the present evidence is
    contribution: float  # weight * presence * quality, before normalization
    rationale: str
    sources_checked: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "weight": self.weight,
            "presence": round(self.presence, 3),
            "quality": round(self.quality, 3),
            "contribution": round(self.contribution, 3),
            "rationale": self.rationale,
            "sources_checked": self.sources_checked,
        }


@dataclass
class ConfidenceResult:
    score: float  # 0-100
    label: str  # "Very High" | "High" | "Moderate" | "Low"
    category_breakdown: List[CategoryScore]
    conflict_penalty_applied: float
    conflict_explanation: str
    raw_score_before_penalty: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "label": self.label,
            "raw_score_before_conflict_penalty": round(self.raw_score_before_penalty, 1),
            "conflict_penalty_applied": round(self.conflict_penalty_applied, 3),
            "conflict_explanation": self.conflict_explanation,
            "category_breakdown": [c.to_dict() for c in self.category_breakdown],
        }


class ConfidenceEngine:
    """
    Computes an independent 0-100 confidence score + qualitative label
    for an already-built `InterpretationResult`, using only the raw
    evidence dicts GEPER collected for that variant. Does not touch
    `acmg_classification`, `triggered_rules`, or any other Phase 1/2
    field -- callers apply this engine's output separately (see
    `InterpretationEngine.interpret`, which sets
    `interpretation_result.confidence_score` /
    `.confidence_label` / `.confidence_pending = False` from this
    engine's `ConfidenceResult`, and nothing else).
    """

    def score(
        self,
        *,
        clinvar_result: Dict[str, Any] = None,
        clingen_result: Dict[str, Any] = None,
        gnomad_result: Dict[str, Any] = None,
        dbsnp_result: Dict[str, Any] = None,
        alphamissense_result: Dict[str, Any] = None,
        mmsplice_result: Dict[str, Any] = None,
        dna_models_used: List[str] = None,
        rna_result: Dict[str, Any] = None,
        protein_result: Dict[str, Any] = None,
        uniprot_result: Dict[str, Any] = None,
        interpro_result: Dict[str, Any] = None,
        alphafold_result: Dict[str, Any] = None,
        blast_result: Dict[str, Any] = None,
        conflicting_evidence: List[str] = None,
        ai_consensus: List[Dict[str, Any]] = None,
    ) -> ConfidenceResult:
        cfg = CONFIG.confidence

        categories = [
            self._clinical_quality(clinvar_result, clingen_result, cfg.CLINICAL_WEIGHT),
            self._population_quality(gnomad_result, dbsnp_result, cfg.POPULATION_WEIGHT),
            self._ai_quality(alphamissense_result, mmsplice_result, ai_consensus, cfg.AI_WEIGHT),
            self._protein_quality(uniprot_result, interpro_result, cfg.PROTEIN_WEIGHT),
            self._structural_quality(alphafold_result, cfg.STRUCTURAL_WEIGHT),
            self._sequence_context_quality(dna_models_used, rna_result, protein_result, cfg.SEQUENCE_CONTEXT_WEIGHT),
            self._additional_evidence_quality(blast_result, cfg.ADDITIONAL_WEIGHT),
        ]

        max_possible = sum(c.weight for c in categories)
        raw_sum = sum(c.contribution for c in categories)
        raw_score = 100.0 * (raw_sum / max_possible) if max_possible > 0 else 0.0

        penalty, penalty_explanation = self._conflict_penalty(conflicting_evidence, ai_consensus, cfg)
        final_score = max(0.0, raw_score * (1.0 - penalty))

        label = self._label(final_score, cfg)

        return ConfidenceResult(
            score=final_score,
            label=label,
            category_breakdown=categories,
            conflict_penalty_applied=penalty,
            conflict_explanation=penalty_explanation,
            raw_score_before_penalty=raw_score,
        )

    # ------------------------------------------------------------------
    # Category scorers. Each returns a CategoryScore with presence/
    # quality independently justified in `rationale` -- never a bare
    # number with no explanation.
    # ------------------------------------------------------------------

    @staticmethod
    def _clinical_quality(clinvar_result, clingen_result, weight: float) -> CategoryScore:
        sources = []
        presence_parts: List[float] = []
        quality_parts: List[float] = []
        notes: List[str] = []

        if clinvar_result and clinvar_result.get("records"):
            sources.append("ClinVar")
            presence_parts.append(1.0)
            top = clinvar_result["records"][0]
            review_status = (top.get("review_status") or "").lower()
            # ClinVar's own star-rating concept, reused descriptively
            # (not re-derived as a new metric): more submitters / an
            # expert panel / practice guideline = higher quality;
            # a single submitter with no assertion criteria = lower.
            if "practice guideline" in review_status or "expert panel" in review_status:
                quality_parts.append(1.0)
                notes.append(f"ClinVar record has '{top.get('review_status')}' review status (high confidence tier).")
            elif "multiple submitters" in review_status and "no conflicts" in review_status:
                quality_parts.append(0.85)
                notes.append("ClinVar record has multiple submitters with no conflicting interpretations.")
            elif "criteria provided" in review_status:
                quality_parts.append(0.6)
                notes.append("ClinVar record has assertion criteria provided (single or unreviewed submitter set).")
            elif review_status:
                quality_parts.append(0.3)
                notes.append(f"ClinVar record present but review status ('{top.get('review_status')}') indicates limited curation.")
            else:
                quality_parts.append(0.2)
                notes.append("ClinVar record present with no reported review status.")
        else:
            notes.append("No ClinVar record found for this variant.")

        if clingen_result and not clingen_result.get("skipped") and not clingen_result.get("error") and clingen_result.get("found"):
            sources.append("ClinGen")
            presence_parts.append(1.0)
            validity = clingen_result.get("clinical_validity_summary")
            if validity in ("Definitive", "Strong"):
                quality_parts.append(1.0)
                notes.append(f"ClinGen gene-disease validity curated as '{validity}'.")
            elif validity in ("Moderate", "Limited"):
                quality_parts.append(0.5)
                notes.append(f"ClinGen gene-disease validity curated as '{validity}'.")
            elif validity in ("Disputed", "Refuted"):
                quality_parts.append(0.2)
                notes.append(f"ClinGen gene-disease validity curated as '{validity}' (weak/contradicted evidence).")
            else:
                quality_parts.append(0.3)
                notes.append("ClinGen curation found but without a clear-cut validity classification.")
        else:
            notes.append("No ClinGen gene curation found for this variant's gene.")

        presence = sum(presence_parts) / 2.0
        quality = (sum(quality_parts) / len(quality_parts)) if quality_parts else 0.0
        return CategoryScore(
            "Clinical Evidence", weight, presence, quality, weight * presence * quality,
            " ".join(notes), sources,
        )

    @staticmethod
    def _population_quality(gnomad_result, dbsnp_result, weight: float) -> CategoryScore:
        sources = []
        presence_parts: List[float] = []
        quality_parts: List[float] = []
        notes: List[str] = []

        if gnomad_result and not gnomad_result.get("skipped") and not gnomad_result.get("error"):
            sources.append("gnomAD")
            presence_parts.append(1.0)
            if gnomad_result.get("found"):
                af = gnomad_result.get("global_af")
                if af is not None:
                    quality_parts.append(1.0)
                    notes.append(f"gnomAD returned an allele frequency (AF={af:.2e}), a decisive population-rarity signal.")
                else:
                    quality_parts.append(0.5)
                    notes.append("gnomAD record found but no allele frequency reported.")
            else:
                # Absence-from-gnomAD is itself informative (it is what
                # drives PM2 in Phase 1) -- not treated as "no evidence".
                quality_parts.append(0.8)
                notes.append("gnomAD queried successfully; variant is absent, itself an informative rarity signal.")
        else:
            notes.append("gnomAD lookup was skipped or unavailable for this variant.")

        if dbsnp_result and dbsnp_result.get("found"):
            sources.append("dbSNP")
            presence_parts.append(1.0)
            quality_parts.append(0.6)
            notes.append("Variant is catalogued in dbSNP.")
        else:
            notes.append("Variant not found in dbSNP (may be novel/private, or lookup was unavailable).")

        presence = sum(presence_parts) / 2.0
        quality = (sum(quality_parts) / len(quality_parts)) if quality_parts else 0.0
        return CategoryScore(
            "Population Evidence", weight, presence, quality, weight * presence * quality,
            " ".join(notes), sources,
        )

    @staticmethod
    def _ai_quality(alphamissense_result, mmsplice_result, ai_consensus, weight: float) -> CategoryScore:
        sources = []
        presence_parts: List[float] = []
        quality_parts: List[float] = []
        notes: List[str] = []

        if alphamissense_result and not alphamissense_result.get("skipped") and alphamissense_result.get("found"):
            sources.append("AlphaMissense")
            presence_parts.append(1.0)
            am_class = (alphamissense_result.get("am_class") or "").lower()
            quality_parts.append(1.0 if am_class in ("likely_pathogenic", "likely_benign") else 0.4)
            notes.append(f"AlphaMissense produced a prediction ('{am_class or 'unclassified'}').")
        else:
            notes.append("AlphaMissense produced no prediction (not applicable, skipped, or unavailable for this variant).")

        if mmsplice_result and mmsplice_result.get("predicted"):
            sources.append("MMSplice")
            presence_parts.append(1.0)
            quality_parts.append(0.8)
            notes.append("MMSplice produced a splice-effect prediction.")
        else:
            notes.append("MMSplice produced no prediction (not applicable, skipped, or unavailable for this variant).")

        # Agreement bonus/penalty: reuses `ai_consensus` (already built
        # in Phase 2's `InterpretationResult.ai_consensus`) rather than
        # re-deriving directions here.
        if ai_consensus and len(ai_consensus) >= 2:
            directions = set()
            for v in ai_consensus:
                pred = (v.get("prediction") or "").lower()
                if "pathogenic" in pred or pred in ("strong_donor_loss", "strong_acceptor_loss", "exon_skipping", "intron_retention", "strong", "moderate"):
                    directions.add("damaging")
                elif "benign" in pred:
                    directions.add("benign")
            if len(directions) > 1:
                quality_parts.append(0.1)
                notes.append("AlphaMissense and MMSplice disagree in direction; AI evidence quality reduced.")
            elif len(directions) == 1:
                quality_parts.append(1.0)
                notes.append("AlphaMissense and MMSplice agree in direction.")

        presence = (sum(presence_parts) / 2.0) if presence_parts else 0.0
        quality = (sum(quality_parts) / len(quality_parts)) if quality_parts else 0.0
        return CategoryScore(
            "AI Evidence", weight, presence, quality, weight * presence * quality,
            " ".join(notes), sources,
        )

    @staticmethod
    def _protein_quality(uniprot_result, interpro_result, weight: float) -> CategoryScore:
        sources = []
        presence_parts: List[float] = []
        quality_parts: List[float] = []
        notes: List[str] = []

        if uniprot_result and not uniprot_result.get("skipped") and not uniprot_result.get("error") and uniprot_result.get("found"):
            sources.append("UniProt")
            presence_parts.append(1.0)
            quality_parts.append(1.0 if uniprot_result.get("reviewed") else 0.6)
            notes.append(f"UniProt entry found ({'reviewed' if uniprot_result.get('reviewed') else 'unreviewed'}).")
        else:
            notes.append("No UniProt entry resolved for this gene/protein.")

        if interpro_result and not interpro_result.get("skipped") and not interpro_result.get("error") and interpro_result.get("found"):
            sources.append("InterPro")
            presence_parts.append(1.0)
            # `affected_domains` is three-valued (see
            # `pipeline/interpro/lookup.py::query_variant`'s
            # docstring): None means domain overlap was never checked
            # (no transcript-verified residue position was available),
            # [] means it was checked and genuinely didn't overlap, and
            # a non-empty list means it did. Collapsing None and [] via
            # `or []` here would silently score "we don't know" the
            # same as "we checked and it's negative" -- a real
            # confirmed negative is more informative evidence than an
            # unchecked gap, so they get different quality weights.
            affected = interpro_result.get("affected_domains")
            position = interpro_result.get("protein_position")
            if affected:
                quality_parts.append(0.9)
                notes.append(f"InterPro/Pfam annotates {len(affected)} domain(s) overlapping residue {position} (transcript-verified).")
            elif affected is None:
                quality_parts.append(0.3)
                notes.append("InterPro/Pfam annotation available for this protein, but this variant's residue position could not be "
                              "determined from the transcript structure, so domain overlap could not be checked.")
            else:
                quality_parts.append(0.6)
                notes.append(f"InterPro/Pfam annotation available; no domain overlaps residue {position} (transcript-verified).")
        else:
            notes.append("No InterPro/Pfam annotation available for this protein.")

        presence = (sum(presence_parts) / 2.0) if presence_parts else 0.0
        quality = (sum(quality_parts) / len(quality_parts)) if quality_parts else 0.0
        return CategoryScore(
            "Protein Knowledge", weight, presence, quality, weight * presence * quality,
            " ".join(notes), sources,
        )

    @staticmethod
    def _structural_quality(alphafold_result, weight: float) -> CategoryScore:
        sources = []
        notes: List[str] = []
        if alphafold_result and not alphafold_result.get("skipped") and not alphafold_result.get("error") and alphafold_result.get("found"):
            sources.append("AlphaFold DB")
            band = (alphafold_result.get("affected_residue_band") or alphafold_result.get("mean_plddt_band") or "").lower()
            # Bug fix (found during Phase 6 prep): AlphaFold DB's real
            # confidence_band() values are "very_high"/"confident"/"low"/
            # "very_low" (underscored -- see pipeline/alphafold/models.py),
            # not space-separated. The space-keyed map below silently
            # missed "very high"/"very low" bands, always falling through
            # to the 0.5 default for the two highest/lowest-confidence
            # cases. Fixed to match the actual provider output.
            quality_map = {"very_high": 1.0, "confident": 0.75, "low": 0.4, "very_low": 0.2}
            quality = quality_map.get(band, 0.5)
            notes.append(f"AlphaFold DB structure available (confidence band: '{band or 'n/a'}').")
            return CategoryScore("Structural Biology", weight, 1.0, quality, weight * 1.0 * quality, " ".join(notes), sources)
        notes.append("No AlphaFold DB structure resolved for this protein.")
        return CategoryScore("Structural Biology", weight, 0.0, 0.0, 0.0, " ".join(notes), sources)

    @staticmethod
    def _sequence_context_quality(dna_models_used, rna_result, protein_result, weight: float) -> CategoryScore:
        # These models inform routing/sequence context in this pipeline
        # rather than emitting a per-variant pathogenicity verdict (see
        # `interpretation_result._ai_consensus`'s docstring for the same
        # distinction). Their contribution here is therefore
        # completeness-only: how much of the sequence-context layer
        # actually ran for this variant, not a judgment about the
        # quality of any prediction, since there is no per-variant
        # verdict to judge.
        sources = list(dna_models_used or [])
        ran = len(sources)
        total_expected = 5  # HyenaDNA/Evo2 (routed, one runs) + RNA-FM + ESM2
        if rna_result and not rna_result.get("skipped") and not rna_result.get("error"):
            sources.append("RNA-FM")
            ran += 1
        if protein_result and protein_result.get("esm2"):
            sources.append("ESM2")
            ran += 1
        presence = min(1.0, ran / total_expected) if total_expected else 0.0
        notes = (
            f"{ran} sequence-context model(s) ran for this variant ({', '.join(sources) if sources else 'none'}); "
            f"these models contribute routing/embedding context, not a direct pathogenicity verdict in this "
            f"pipeline, so their contribution here reflects completeness, not a quality judgment."
        )
        # quality is fixed at 1.0 when present since there is nothing to
        # judge as "more or less decisive" -- honest treatment of a
        # category with only a completeness signal, not a strength one.
        quality = 1.0 if ran > 0 else 0.0
        return CategoryScore("Sequence Context", weight, presence, quality, weight * presence * quality, notes, sources)

    @staticmethod
    def _additional_evidence_quality(blast_result, weight: float) -> CategoryScore:
        sources = []
        notes: List[str] = []
        presence = quality = 0.0
        if blast_result and blast_result.get("hit_count", 0) > 0:
            sources.append("BLAST")
            presence = 1.0
            quality = 0.7
            notes.append(f"BLAST returned {blast_result.get('hit_count')} homology hit(s).")
        else:
            notes.append("BLAST returned no hits (or BLAST was skipped/unavailable).")
        # Ensembl is used internally for sequence-context retrieval
        # (transcript/region lookups) rather than surfaced to
        # `InterpretationEngine` as its own per-variant result dict, so
        # it cannot be scored as a separate, independently-verifiable
        # input without fabricating a number for it. Its actual
        # contribution is already reflected upstream (sequence context
        # + protein translation succeeding at all).
        notes.append("Ensembl contributes indirectly via sequence-context retrieval and is not independently scorable here (no per-variant Ensembl evidence dict is passed to this engine).")
        return CategoryScore("Additional Evidence", weight, presence, quality, weight * presence * quality, " ".join(notes), sources)

    # ------------------------------------------------------------------
    # Conflict penalty + labeling
    # ------------------------------------------------------------------

    @staticmethod
    def _conflict_penalty(conflicting_evidence, ai_consensus, cfg) -> Tuple[float, str]:
        conflict_units = 0
        reasons: List[str] = []

        n_conflicts = len(conflicting_evidence or [])
        if n_conflicts:
            conflict_units += n_conflicts
            reasons.append(f"{n_conflicts} conflicting-evidence item(s) recorded across ACMG criteria.")

        if ai_consensus and len(ai_consensus) >= 2:
            directions = set()
            for v in ai_consensus:
                pred = (v.get("prediction") or "").lower()
                if "pathogenic" in pred or pred in ("strong_donor_loss", "strong_acceptor_loss", "exon_skipping", "intron_retention", "strong", "moderate"):
                    directions.add("damaging")
                elif "benign" in pred:
                    directions.add("benign")
            if len(directions) > 1:
                conflict_units += 1
                reasons.append("AI classifiers (AlphaMissense/MMSplice) disagree in predicted direction.")

        penalty = min(cfg.MAX_CONFLICT_PENALTY, conflict_units * cfg.CONFLICT_PENALTY_PER_CONFLICT)
        explanation = " ".join(reasons) if reasons else "No conflicting evidence detected."
        return penalty, explanation

    @staticmethod
    def _label(score: float, cfg) -> str:
        if score >= cfg.VERY_HIGH_THRESHOLD:
            return "Very High"
        if score >= cfg.HIGH_THRESHOLD:
            return "High"
        if score >= cfg.MODERATE_THRESHOLD:
            return "Moderate"
        return "Low"
