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
        clinvar_result: Optional[Dict[str, Any]] = None,
        clingen_result: Optional[Dict[str, Any]] = None,
        gnomad_result: Optional[Dict[str, Any]] = None,
        dbsnp_result: Optional[Dict[str, Any]] = None,
        alphamissense_result: Optional[Dict[str, Any]] = None,
        mmsplice_result: Optional[Dict[str, Any]] = None,
        dna_models_used: Optional[List[str]] = None,
        rna_result: Optional[Dict[str, Any]] = None,
        protein_result: Optional[Dict[str, Any]] = None,
        uniprot_result: Optional[Dict[str, Any]] = None,
        interpro_result: Optional[Dict[str, Any]] = None,
        alphafold_result: Optional[Dict[str, Any]] = None,
        blast_result: Optional[Dict[str, Any]] = None,
        real_conflicts: Optional[List[Any]] = None,
        ai_consensus: Optional[List[Dict[str, Any]]] = None,
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

        penalty, penalty_explanation = self._conflict_penalty(real_conflicts, cfg)
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

        if clinvar_result and clinvar_result.get("match_status") == "matched" and clinvar_result.get("primary_record"):
            sources.append("ClinVar")
            presence_parts.append(1.0)
            top = clinvar_result["primary_record"]
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
                notes.append(
                    f"ClinVar record present but review status ('{top.get('review_status')}') indicates limited curation."
                )
            else:
                quality_parts.append(0.2)
                notes.append("ClinVar record present with no reported review status.")
        elif clinvar_result and clinvar_result.get("match_status") == "position_only":
            notes.append(
                "No ClinVar record found for this exact variant (other, non-matching variants are "
                "catalogued at this genomic position, but do not count as evidence about this variant)."
            )
        else:
            notes.append("No ClinVar record found for this variant.")

        if (
            clingen_result
            and not clingen_result.get("skipped")
            # INERT (2026-08-31): truthiness here, not `is not None`, but
            # `clingen_result.get("found")` two lines below already
            # requires True, and a failed lookup always returns
            # found=False alongside its error -- so this sub-condition
            # never changes which branch fires; a genuinely failed
            # lookup with error="" already fails the AND at `found`
            # regardless of how `error` itself is read. Fixed to
            # `is not None` anyway for consistency with the rest of this
            # module, not because today's score or rationale changes.
            and clingen_result.get("error") is None
            and clingen_result.get("found")
        ):
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
            "Clinical Evidence",
            weight,
            presence,
            quality,
            weight * presence * quality,
            " ".join(notes),
            sources,
        )

    @staticmethod
    def _population_quality(gnomad_result, dbsnp_result, weight: float) -> CategoryScore:
        sources = []
        presence_parts: List[float] = []
        quality_parts: List[float] = []
        notes: List[str] = []

        # `error` by presence: this branch's own rationale asserts the
        # lookup "queried successfully", so a falsy-but-present error
        # string put a false statement into reader-facing text.
        if gnomad_result and not gnomad_result.get("skipped") and gnomad_result.get("error") is None:
            sources.append("gnomAD")
            presence_parts.append(1.0)
            if gnomad_result.get("found"):
                af = gnomad_result.get("global_af")
                if af is not None:
                    quality_parts.append(1.0)
                    notes.append(
                        f"gnomAD returned an allele frequency (AF={af:.2e}), a decisive population-rarity signal."
                    )
                else:
                    quality_parts.append(0.5)
                    notes.append("gnomAD record found but no allele frequency reported.")
            else:
                # Absence-from-gnomAD is itself informative (it is what
                # drives PM2 in Phase 1) -- not treated as "no evidence".
                quality_parts.append(0.8)
                notes.append("gnomAD queried successfully; variant is absent, itself an informative rarity signal.")
        elif gnomad_result and gnomad_result.get("skipped") and gnomad_result.get("reason"):
            # Round 16: a deliberate skip (e.g. mtDNA compartment gate --
            # gnomAD's mitochondrial callset is a separate resource this
            # pipeline never queries) carries its own honest reason; that
            # is a different statement from "unavailable" (an outage/
            # timeout), which the fallback below still covers.
            notes.append(f"gnomAD was not queried: {gnomad_result['reason']}")
        else:
            notes.append("gnomAD lookup was skipped or unavailable for this variant.")

        if dbsnp_result and dbsnp_result.get("match_status") == "matched":
            sources.append("dbSNP")
            presence_parts.append(1.0)
            quality_parts.append(0.6)
            notes.append("Variant is catalogued in dbSNP.")
        elif dbsnp_result and dbsnp_result.get("match_status") == "position_only":
            notes.append(
                "Variant not catalogued in dbSNP under this exact allele (other rsIDs exist at this position)."
            )
        else:
            notes.append("Variant not found in dbSNP (may be novel/private, or lookup was unavailable).")

        presence = sum(presence_parts) / 2.0
        quality = (sum(quality_parts) / len(quality_parts)) if quality_parts else 0.0
        return CategoryScore(
            "Population Evidence",
            weight,
            presence,
            quality,
            weight * presence * quality,
            " ".join(notes),
            sources,
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
            notes.append(
                "AlphaMissense produced no prediction (not applicable, skipped, or unavailable for this variant)."
            )

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
                quality_parts.append(0.1)
                notes.append("AlphaMissense and MMSplice disagree in direction; AI evidence quality reduced.")
            elif len(directions) == 1:
                quality_parts.append(1.0)
                notes.append("AlphaMissense and MMSplice agree in direction.")

        presence = (sum(presence_parts) / 2.0) if presence_parts else 0.0
        quality = (sum(quality_parts) / len(quality_parts)) if quality_parts else 0.0
        return CategoryScore(
            "AI Evidence",
            weight,
            presence,
            quality,
            weight * presence * quality,
            " ".join(notes),
            sources,
        )

    @staticmethod
    def _protein_quality(uniprot_result, interpro_result, weight: float) -> CategoryScore:
        sources = []
        presence_parts: List[float] = []
        quality_parts: List[float] = []
        notes: List[str] = []

        if (
            uniprot_result
            and not uniprot_result.get("skipped")
            # INERT (2026-08-31): truthiness here, not `is not None`, but
            # `uniprot_result.get("found")` two lines below already
            # requires True, and a failed lookup always returns
            # found=False alongside its error -- so this sub-condition
            # never changes which branch fires. Confirmed EXECUTED: a
            # failure with error="" also lacks the "reason" key the elif
            # below checks, so it falls to the same final `else` either
            # way. Fixed to `is not None` for consistency, not because
            # today's score or rationale changes.
            and uniprot_result.get("error") is None
            and uniprot_result.get("found")
        ):
            sources.append("UniProt")
            presence_parts.append(1.0)
            quality_parts.append(1.0 if uniprot_result.get("reviewed") else 0.6)
            notes.append(f"UniProt entry found ({'reviewed' if uniprot_result.get('reviewed') else 'unreviewed'}).")
        elif uniprot_result and uniprot_result.get("reason"):
            # Round 16: an honest, stated cause (e.g. this gene's Ensembl
            # biotype confirms no protein-coding transcript exists --
            # `pipeline/acmg_rules.py::non_protein_coding_gene_reason`)
            # is not the same claim as "we looked and couldn't find one".
            notes.append(uniprot_result["reason"])
        else:
            notes.append("No UniProt entry resolved for this gene/protein.")

        if (
            interpro_result
            and not interpro_result.get("skipped")
            # INERT (2026-08-31): same reasoning as UniProt above --
            # `found` two lines below already gates this branch, and a
            # failure's shape carries no "reason" key either, so it
            # falls to the same shared final `else` regardless of how
            # `error` is read. Confirmed EXECUTED alongside UniProt's
            # case in the same probe. Fixed to `is not None` for
            # consistency only.
            and interpro_result.get("error") is None
            and interpro_result.get("found")
        ):
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
                notes.append(
                    f"InterPro/Pfam annotates {len(affected)} domain(s) overlapping residue {position} (transcript-verified)."
                )
            elif affected is None:
                quality_parts.append(0.3)
                notes.append(
                    "InterPro/Pfam annotation available for this protein, but this variant's residue position could not be "
                    "determined from the transcript structure, so domain overlap could not be checked."
                )
            else:
                quality_parts.append(0.6)
                notes.append(
                    f"InterPro/Pfam annotation available; no domain overlaps residue {position} (transcript-verified)."
                )
        elif interpro_result and interpro_result.get("reason"):
            notes.append(interpro_result["reason"])
        else:
            notes.append("No InterPro/Pfam annotation available for this protein.")

        presence = (sum(presence_parts) / 2.0) if presence_parts else 0.0
        quality = (sum(quality_parts) / len(quality_parts)) if quality_parts else 0.0
        # Round 23: UniProt's and InterPro's `reason` both come from the
        # same orchestrator-computed `skip_reason` (see
        # `pipeline/orchestrator.py`'s `non_protein_coding_gene_reason`
        # call, shared verbatim across uniprot_result/interpro_result/
        # alphafold_result) whenever a gene has no protein-coding
        # transcript at all -- so `notes` can carry the identical
        # sentence twice. `dict.fromkeys` dedupes while preserving
        # order, without touching the distinct-reason case (e.g. a real
        # UniProt hit alongside an InterPro lookup failure), where every
        # note differs and nothing is dropped.
        deduped_notes = list(dict.fromkeys(notes))
        return CategoryScore(
            "Protein Knowledge",
            weight,
            presence,
            quality,
            weight * presence * quality,
            " ".join(deduped_notes),
            sources,
        )

    @staticmethod
    def _structural_quality(alphafold_result, weight: float) -> CategoryScore:
        sources = []
        notes: List[str] = []
        if (
            alphafold_result
            and not alphafold_result.get("skipped")
            and alphafold_result.get("error") is None
            and alphafold_result.get("found")
        ):
            sources.append("AlphaFold DB")
            # No fallback to `mean_plddt_band` (the whole-protein average)
            # when `affected_residue_band` is absent -- same defect, same
            # fix, as `conflict_resolution_engine.py::_structural_conflict`'s
            # docstring already condemned in writing: treating the
            # whole-protein average as this variant's own residue
            # confidence fabricates a claim AlphaFold was never asked
            # about. `raw_band is not None` (not truthiness) decides
            # presence, so a genuinely-absent band stays `None` rather
            # than collapsing to `""` -- an empty string here would put
            # "present but empty" and "absent" back on one observable,
            # one layer inside the fix meant to remove that ambiguity.
            raw_band = alphafold_result.get("affected_residue_band")
            band = raw_band.lower() if raw_band is not None else None
            # Bug fix (found during Phase 6 prep): AlphaFold DB's real
            # confidence_band() values are "very_high"/"confident"/"low"/
            # "very_low" (underscored -- see pipeline/alphafold/models.py),
            # not space-separated. The space-keyed map below silently
            # missed "very high"/"very low" bands, always falling through
            # to the 0.5 default for the two highest/lowest-confidence
            # cases. Fixed to match the actual provider output.
            quality_map = {"very_high": 1.0, "confident": 0.75, "low": 0.4, "very_low": 0.2}
            quality = quality_map.get(band, 0.5)
            notes.append(
                f"AlphaFold DB structure available (confidence band: '{band if band is not None else 'n/a'}')."
            )
            return CategoryScore(
                "Structural Biology", weight, 1.0, quality, weight * 1.0 * quality, " ".join(notes), sources
            )
        if alphafold_result and alphafold_result.get("reason"):
            notes.append(alphafold_result["reason"])
        else:
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
        # INERT (2026-08-31): truthiness on `error`, not `is not None`,
        # but `_run_rna_stage` (orchestrator.py) keeps `skipped: True`
        # on every failure path -- `not rna_result.get("skipped")`
        # already excludes a failed run regardless of how `error` is
        # read. Fixed to `is not None` for consistency only; this
        # mirrors the identical check duplicated in interpretation.py,
        # interpretation_result.py, and prioritization_engine.py, all
        # fixed alongside this one for the same reason.
        if rna_result and not rna_result.get("skipped") and rna_result.get("error") is None:
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
        notes.append(
            "Ensembl contributes indirectly via sequence-context retrieval and is not independently scorable here (no per-variant Ensembl evidence dict is passed to this engine)."
        )
        return CategoryScore(
            "Additional Evidence", weight, presence, quality, weight * presence * quality, " ".join(notes), sources
        )

    # ------------------------------------------------------------------
    # Conflict penalty + labeling
    # ------------------------------------------------------------------

    @staticmethod
    def _conflict_penalty(real_conflicts, cfg) -> Tuple[float, str]:
        """
        HIGH-1: penalises the conflicts
        `ConflictResolutionEngine._real_conflicts` recognises, not the
        raw per-criterion `conflicting_evidence` list this used to
        count. Two things changed and both are load-bearing:

        1. ACMG-criterion caveats no longer penalise. They are the
           items D4 excluded from the Attention flag on the grounds
           that they are "not a disagreement between two independent
           sources"; penalising them here while refusing to flag them
           there meant one document asserted a conflict and denied it
           six fields apart.
        2. The separate `ai_consensus` direction-disagreement branch is
           GONE, deliberately. That disagreement now arrives as an
           `AI`/`Moderate` ConflictItem inside `real_conflicts`
           (`_ai_conflict`), so keeping the branch would have counted
           the same discordance twice -- once per path -- for a penalty
           of 0.30 where 0.15 is meant. Nothing in the five-variant run
           exercised it, which is exactly why it is pinned by a test.

        Flat count, not severity-weighted: one unit per real conflict,
        whatever its tier. `ConflictResolutionEngine._score` keeps its
        own severity weighting for the flag; this stays legible until
        there is evidence for a particular weighting here.
        """
        conflict_units = len(real_conflicts or [])
        penalty = min(cfg.MAX_CONFLICT_PENALTY, conflict_units * cfg.CONFLICT_PENALTY_PER_CONFLICT)
        if not conflict_units:
            return penalty, "No conflicting evidence detected."
        detail = "; ".join(f"{c.conflict_type} ({c.severity})" for c in real_conflicts)
        explanation = f"{conflict_units} conflict(s) detected between independent evidence sources: {detail}."
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
