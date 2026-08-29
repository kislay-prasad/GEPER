"""
Case-level, HPO-phenotype-driven variant ranking.

Answers a question no existing engine in this pipeline does: given N
variants from one patient's VCF and that patient's observed HPO
symptom terms (`--hpo-terms`/`--phenotype-file`), which variants best
explain those symptoms -- so a reviewer with dozens of candidate
variants can triage by phenotype relevance instead of reading them in
VCF order.

NOT ACMG, NOT PP4 -- read before touching this file
--------------------------------------------------------------------
`ACMGRuleEngine._pp4` (pipeline/acmg_rules.py) already does a
phenotype check, but it answers a completely different question with a
completely different shape: PP4 is one binary ACMG/AMP criterion
("phenotype specific for a disease with a single genetic etiology"),
gated by a strict overlap-fraction threshold AND a distinct-disease-
count cap (`CONFIG.hpo.PP4_*`), evaluated independently per variant,
and its triggered/not_triggered/not_evaluated verdict feeds directly
into ACMG classification via the standard Richards et al. point-based
combining rules.

The score this module computes is none of that:
  - Continuous (0-100), not binary triggered/not_triggered.
  - Uses ontology ancestor-based partial credit (see
    `pipeline/hpo/ontology.py`) when available, not just exact-ID
    overlap -- PP4 only ever checks exact overlap.
  - Case-wide: only meaningful relative to the other variants in the
    same run, computed once after every variant is fully interpreted
    (mirrors `prioritization_engine.py::rank_batch`'s own "batch-
    relative, computed at the end of the run" shape).
  - NEVER read by `ACMGRuleEngine`, NEVER folded into
    `acmg_classification`, `confidence_score`, or `priority_score`.
    It is a separate, additive, post-hoc triage signal -- surfaced
    under its own `case_prioritization` key (see
    `pipeline/orchestrator.py::_apply_case_phenotype_ranking`), never
    merged into `interpretation_result`.

If you are tempted to make PP4 "smarter" using this module's
similarity logic, or to let `case_rank` influence classification: stop
-- that is explicitly out of scope by design (see class docstrings
below and `config.py::CasePrioritizationConfig`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import CONFIG
from pipeline.hpo.ontology import HPOOntology
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TermMatch:
    """One patient-observed HPO term's best match against a gene's HPO-curated term set."""

    patient_term_id: str
    patient_term_name: Optional[str]
    matched_gene_term_id: Optional[str]
    matched_gene_term_name: Optional[str]
    similarity: float  # 0.0-1.0
    match_type: str  # "exact" | "ancestor" | "none"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "patient_term_id": self.patient_term_id,
            "patient_term_name": self.patient_term_name,
            "matched_gene_term_id": self.matched_gene_term_id,
            "matched_gene_term_name": self.matched_gene_term_name,
            "similarity": round(self.similarity, 3),
            "match_type": self.match_type,
        }


@dataclass
class PhenotypeMatchScore:
    """
    One variant's gene-level phenotype-match score against the
    patient's observed HPO terms -- NOT an ACMG criterion (see this
    module's docstring). `None` fields throughout (not fabricated
    zeros) whenever the underlying data genuinely doesn't exist, so a
    "no HPO data for this gene" case is never confused with a
    "checked, poor match" case (0.0).
    """

    score: Optional[float]  # 0-100; None if the gene has no HPO data at all
    term_matches: List[TermMatch] = field(default_factory=list)
    ontology_available: bool = False
    gene_hpo_available: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(self.score, 1) if self.score is not None else None,
            "term_matches": [m.to_dict() for m in self.term_matches],
            "ontology_available": self.ontology_available,
            "gene_hpo_available": self.gene_hpo_available,
            "reason": self.reason,
        }


@dataclass
class CaseRankResult:
    """
    One variant's case-level rank + the combined score that produced
    it. `case_rank`/`case_rank_score` are deliberately named apart from
    `priority_rank`/`priority_score` (Phase 4) -- distinct concepts,
    distinct fields, never conflated (see this module's docstring).
    """

    case_rank: Optional[int]  # 1 = best phenotype/evidence match in this batch; None if this variant was never scored
    case_rank_score: Optional[float]  # 0-100 combined score; None if not scored
    phenotype_match: Optional[PhenotypeMatchScore]
    tier: str  # "phenotype_matched" | "no_gene_hpo_data" | "not_scored"
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_rank": self.case_rank,
            "case_rank_score": round(self.case_rank_score, 1) if self.case_rank_score is not None else None,
            "phenotype_match": self.phenotype_match.to_dict() if self.phenotype_match is not None else None,
            "tier": self.tier,
            "reason": self.reason,
        }


def _term_similarity(patient_term: str, gene_term: str, ontology: HPOOntology, min_similarity: float) -> float:
    """
    Similarity between two HPO term IDs: 1.0 for an exact match;
    otherwise, when `ontology` is available, the Jaccard index of each
    term's ancestor closure (each term's own ancestor set including
    itself, so two sibling terms with several shared parents score
    higher than two terms sharing only one distant common ancestor) --
    a simple, transparent graph-based partial-credit measure that
    needs no corpus-wide term-frequency statistics (unlike Resnik/Lin
    information-content similarity, which GEPER has no disease-corpus
    to compute from). Below `min_similarity`, treated as no match
    (0.0) -- avoids nearly every term pair scoring some tiny nonzero
    value purely from sharing HPO's own root-level "Phenotypic
    abnormality" ancestor.

    Returns 0.0 (never raises, never fabricates a match) when the
    ontology is unavailable and the terms aren't identical -- the
    honest exact-match-only degrade path this module's docstring
    promises.
    """
    if patient_term == gene_term:
        return 1.0
    if not ontology.is_available:
        return 0.0

    patient_closure = ontology.ancestors(patient_term) | {patient_term}
    gene_closure = ontology.ancestors(gene_term) | {gene_term}
    union = patient_closure | gene_closure
    if not union:
        return 0.0
    similarity = len(patient_closure & gene_closure) / len(union)
    return similarity if similarity >= min_similarity else 0.0


def score_phenotype_match(
    patient_term_ids: List[str],
    gene_hpo_result: Optional[Dict[str, Any]],
    ontology: Optional[HPOOntology] = None,
    cfg: Optional[Any] = None,
) -> PhenotypeMatchScore:
    """
    Scores one variant's gene against the patient's observed HPO
    terms. `gene_hpo_result` is a variant's own `hpo` provider dict
    (`pipeline/hpo/lookup.py::HPOLookup.query_variant`'s return shape
    -- same dict GEPER already computes per variant and stores at
    `variant_result["hpo"]`; nothing here re-queries HPO).

    Best-match, patient-term-anchored (information-retrieval-style
    "how well is each observed symptom explained by this gene"): for
    every patient term, finds the single best-matching gene term (not
    an average over all pairs, which would dilute a strong match by
    every unrelated gene term in a large curated set) and averages
    those per-term best scores across all patient terms.
    """
    cfg = cfg or CONFIG.case_prioritization
    ontology = ontology if ontology is not None else HPOOntology()

    if (
        not gene_hpo_result
        or gene_hpo_result.get("skipped")
        or gene_hpo_result.get("error") is not None
        or not gene_hpo_result.get("found")
    ):
        return PhenotypeMatchScore(
            score=None,
            gene_hpo_available=False,
            ontology_available=ontology.is_available,
            reason="No HPO-curated phenotype data available for this variant's gene.",
        )

    gene_terms = gene_hpo_result.get("distinct_phenotype_terms") or []
    if not gene_terms:
        return PhenotypeMatchScore(
            score=None,
            gene_hpo_available=False,
            ontology_available=ontology.is_available,
            reason="Gene HPO lookup succeeded but returned no phenotype terms.",
        )

    matches: List[TermMatch] = []
    total = 0.0
    for patient_term in patient_term_ids:
        best_similarity = 0.0
        best_gene_term: Optional[Dict[str, str]] = None
        for gene_term in gene_terms:
            gene_term_id = gene_term.get("hpo_id")
            if not gene_term_id:
                continue
            similarity = _term_similarity(patient_term, gene_term_id, ontology, cfg.MIN_ANCESTOR_SIMILARITY)
            if similarity > best_similarity:
                best_similarity = similarity
                best_gene_term = gene_term

        total += best_similarity
        matches.append(
            TermMatch(
                patient_term_id=patient_term,
                patient_term_name=None,  # patient-supplied IDs carry no name in phenotype_result -- see build_phenotype_result
                matched_gene_term_id=best_gene_term.get("hpo_id") if best_gene_term else None,
                matched_gene_term_name=best_gene_term.get("hpo_name") if best_gene_term else None,
                similarity=best_similarity,
                match_type="exact" if best_similarity == 1.0 else ("ancestor" if best_similarity > 0 else "none"),
            )
        )

    score = 100.0 * (total / len(patient_term_ids)) if patient_term_ids else 0.0
    reason = (
        f"Matched against {len(gene_terms)} HPO-curated term(s) for this gene "
        f"({'ontology ancestor-based partial credit available' if ontology.is_available else 'exact-ID match only -- HPO ontology structure not loaded this run'})."
    )
    return PhenotypeMatchScore(
        score=score,
        term_matches=matches,
        gene_hpo_available=True,
        ontology_available=ontology.is_available,
        reason=reason,
    )


def rank_case(
    variant_hpo_results: List[Optional[Dict[str, Any]]],
    priority_scores: List[Optional[float]],
    patient_term_ids: List[str],
    ontology: Optional[HPOOntology] = None,
    cfg: Optional[Any] = None,
) -> List[CaseRankResult]:
    """
    Ranks a whole batch of variants by combined phenotype-match +
    priority evidence. Pure function over parallel lists (mirrors
    `prioritization_engine.py::rank_batch`'s own shape) -- the caller
    (`pipeline/orchestrator.py::_apply_case_phenotype_ranking`) is
    responsible for extracting `variant_hpo_results`/`priority_scores`
    from each variant's already-built result dict and writing the
    returned ranks back.

    Two-tier ranking, not a single blended formula across every
    variant: variants whose gene has real HPO data (tier
    "phenotype_matched") are ranked first, by
    `PHENOTYPE_MATCH_WEIGHT * phenotype_match_score_normalized +
    PRIORITY_WEIGHT * priority_score_normalized`; variants whose gene
    has NO HPO data at all (tier "no_gene_hpo_data") are ranked after
    every phenotype-matched variant, by `priority_score` alone --
    exactly the honest "rank last with a clear reason, not silently at
    position 1" behavior a naive average would violate (a variant with
    no phenotype data averaged against a 0 would look identical to one
    that scored a genuine 0/100 phenotype match, when those are very
    different findings). A variant with no `priority_score` either
    (prioritization itself failed/was skipped) gets tier
    "not_scored", `case_rank=None` -- never a fabricated position.
    """
    cfg = cfg or CONFIG.case_prioritization
    ontology = ontology if ontology is not None else HPOOntology()

    n = len(variant_hpo_results)
    phenotype_matches: List[Optional[PhenotypeMatchScore]] = [
        score_phenotype_match(patient_term_ids, variant_hpo_results[i], ontology, cfg) for i in range(n)
    ]

    max_weight = cfg.PHENOTYPE_MATCH_WEIGHT + cfg.PRIORITY_WEIGHT

    tier_a: List[tuple] = []  # (index, combined_score)
    tier_b: List[tuple] = []  # (index, priority_score) -- no gene HPO data
    tier_c: List[int] = []  # not scorable at all

    for i in range(n):
        pm = phenotype_matches[i]
        priority = priority_scores[i]
        # `priority is None` is checked FIRST, ahead of phenotype-data
        # availability: the combined score below is defined as a
        # blend of both inputs, so a missing priority_score can never
        # be silently treated as 0 (that would fabricate "this scores
        # badly on priority" out of "priority is simply unknown" --
        # exactly the class of error this codebase's HPOGeneEvidence/
        # provenance "not_consulted" vs "unknown" distinctions exist
        # to avoid elsewhere). A variant with real phenotype data but
        # no priority score is honestly "not_scored", not silently
        # folded into tier_a with a fake priority=0.
        if priority is None:
            tier_c.append(i)
        elif pm is not None and pm.score is not None:
            priority_component = max(0.0, min(1.0, priority / 100.0))
            phenotype_component = max(0.0, min(1.0, pm.score / 100.0))
            combined = 100.0 * (
                (cfg.PHENOTYPE_MATCH_WEIGHT * phenotype_component + cfg.PRIORITY_WEIGHT * priority_component)
                / max_weight
                if max_weight > 0
                else 0.0
            )
            tier_a.append((i, combined))
        else:
            tier_b.append((i, priority))

    tier_a.sort(key=lambda pair: (-pair[1], pair[0]))
    tier_b.sort(key=lambda pair: (-pair[1], pair[0]))

    results: List[Optional[CaseRankResult]] = [None] * n
    rank = 1
    for i, combined in tier_a:
        results[i] = CaseRankResult(
            case_rank=rank,
            case_rank_score=combined,
            phenotype_match=phenotype_matches[i],
            tier="phenotype_matched",
            reason="Ranked by combined phenotype-match and priority evidence.",
        )
        rank += 1
    for i, _priority in tier_b:
        results[i] = CaseRankResult(
            case_rank=rank,
            case_rank_score=None,
            phenotype_match=phenotype_matches[i],
            tier="no_gene_hpo_data",
            reason=(
                phenotype_matches[i].reason
                if phenotype_matches[i] is not None
                else "No HPO-curated phenotype data available for this variant's gene."
            )
            + " Ranked after every phenotype-matched variant, by priority score alone.",
        )
        rank += 1
    for i in tier_c:
        results[i] = CaseRankResult(
            case_rank=None,
            case_rank_score=None,
            phenotype_match=phenotype_matches[i],
            tier="not_scored",
            reason="Priority score unavailable for this variant; not included in case-level ranking.",
        )

    return [r for r in results if r is not None]
