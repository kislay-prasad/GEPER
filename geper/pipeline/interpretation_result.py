"""
Phase 2 -- Evidence Aggregation Engine.

`InterpretationResult` is the single canonical, serializable representation
of a fully-interpreted variant. It does not recompute anything: it is built
purely by reorganizing outputs that already exist --
`InterpretationEngine.interpret()`'s legacy summary dict, its new
`acmg_evaluation` (Phase 1), and the raw per-stage provider result dicts the
orchestrator already collects (ClinVar, dbSNP, gnomAD, ClinGen, AlphaMissense,
MMSplice, UniProt, InterPro, AlphaFold DB, protein translation, DNA/RNA model
results).

Why this exists: before Phase 2, every consumer (report_generator.py,
json_builder.py, any future GUI/API) that wanted "the supporting evidence"
or "the biological context" had to re-walk the same provider dicts and
re-implement the same conditionals `InterpretationEngine` already has.
`InterpretationResult` is meant to be the one object those consumers pull
from going forward (wired in Phase 5), so that logic lives in exactly one
place.

Backward compatibility: this is purely additive. `InterpretationEngine
.interpret()` still returns its original four keys unchanged, plus
`acmg_evaluation` (Phase 1) and now `interpretation_result` (this phase,
`InterpretationResult.to_dict()`). `build_variant_result()` gains one new
optional kwarg (default `None`, matching the existing pattern used for
`gnomad_result`/`clingen_result`/etc.) so old callers are unaffected.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pipeline.stage_schemas import validate_acmg_evaluation


@dataclass
class InterpretationResult:
    """Canonical, serializable representation of one interpreted variant."""

    # Identity
    variant: Dict[str, Any] = field(default_factory=dict)
    gene_symbol: Optional[str] = None

    # ACMG (Phase 1 output, reorganized -- not recomputed)
    acmg_classification: Optional[str] = None
    triggered_rules: List[Dict[str, Any]] = field(default_factory=list)
    not_triggered_rules: List[Dict[str, Any]] = field(default_factory=list)
    not_evaluated_rules: List[Dict[str, Any]] = field(default_factory=list)
    combining_rule_trace: List[str] = field(default_factory=list)

    # Aggregated evidence (deduplicated, sourced from the ACMG criteria
    # plus the legacy free-text evidence list -- nothing new is derived
    # here, only pooled).
    supporting_evidence: List[str] = field(default_factory=list)
    conflicting_evidence: List[str] = field(default_factory=list)

    # AI model outputs. Split deliberately into `ai_consensus` (only
    # models that produce an actual interpretable verdict -- currently
    # AlphaMissense and MMSplice) and `ai_context_models` (
    # HyenaDNA, Evo2, RNA-FM, ESM2 -- sequence/embedding models that were
    # invoked but do not themselves output a pathogenic/benign verdict in
    # this pipeline). Keeping them separate avoids fabricating a
    # "consensus" that includes models with no actual verdict to agree or
    # disagree with.
    ai_consensus: List[Dict[str, Any]] = field(default_factory=list)
    ai_context_models: List[str] = field(default_factory=list)

    # Protein / structural / population / clinical context (descriptive,
    # reused verbatim from InterpretationEngine's existing biological
    # context helper -- not re-derived here).
    biological_evidence: List[str] = field(default_factory=list)

    # Placeholders for Phases 3 and 4. Deliberately left unset (None)
    # rather than fabricated -- populated by ConfidenceEngine (Phase 3)
    # and PrioritizationEngine (Phase 4) respectively, which mutate a
    # copy of this object rather than duplicating its inputs.
    confidence_score: Optional[float] = None
    confidence_label: Optional[str] = None
    confidence_pending: bool = True
    # Full per-category ConfidenceEngine explanation (Phase 3). None
    # until the confidence engine actually runs -- mirrors
    # confidence_pending rather than being fabricated ahead of it.
    confidence_breakdown: Optional[Dict[str, Any]] = None
    priority_score: Optional[float] = None
    priority_category: Optional[str] = None  # "Critical" | "High" | "Moderate" | "Low"
    priority_rank: Optional[int] = None  # position within the batch this variant was run in; set by rank_batch()
    priority_pending: bool = True
    priority_explanation: List[str] = field(default_factory=list)  # ["✓ Pathogenic ClinVar record", ...]
    # Full per-factor PrioritizationEngine explanation (Phase 4),
    # mirrors confidence_breakdown. None until the engine actually runs.
    priority_breakdown: Optional[Dict[str, Any]] = None

    # Phase 6: Conflict Resolution. Purely additive -- never affects
    # acmg_classification or the confidence/priority score fields
    # above. conflict_severity defaults to None (not yet computed);
    # once the engine runs, it becomes "None"/"Minor"/"Moderate"/"Major"
    # (the string "None" meaning "no conflicts found", distinct from
    # the Python None meaning "not yet evaluated").
    conflict_summary: Optional[str] = None
    conflict_list: List[Dict[str, Any]] = field(default_factory=list)
    conflict_score: float = 0.0
    conflict_severity: Optional[str] = None
    conflict_resolution: Optional[str] = None

    # Phase 7: Explainability. Purely additive, purely derived from
    # everything above -- never recomputes or changes
    # acmg_classification, confidence_score, priority_score, or
    # conflict_resolution. A single grouped dict (14 named sub-keys,
    # see explainability_engine.ExplainabilityResult), matching the
    # confidence_breakdown/priority_breakdown pattern rather than
    # adding 14 separate top-level fields.
    explainability: Optional[Dict[str, Any]] = None

    recommendations: List[str] = field(default_factory=list)

    # Every provider/source name that contributed anything to this
    # result, for Phase 7 (explainability) to enumerate without
    # re-deriving it from the raw evidence.
    evidence_sources: List[str] = field(default_factory=list)

    # Legacy fields kept verbatim for any caller still reading the old
    # `interpretation` dict shape directly.
    legacy_summary: Optional[str] = None
    legacy_confidence: Optional[str] = None
    legacy_significance_score: Optional[float] = None

    # Raw per-stage evidence dicts, kept by reference (not copied/
    # re-parsed) so Phase 3/4/6/7 engines can consume the *same* source
    # data this object was built from, instead of the orchestrator
    # passing them around a second time and each engine re-implementing
    # its own parsing of e.g. `gnomad_result`.
    raw_evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "variant": self.variant,
            "gene_symbol": self.gene_symbol,
            "acmg_classification": self.acmg_classification,
            "triggered_rules": self.triggered_rules,
            "not_triggered_rules": self.not_triggered_rules,
            "not_evaluated_rules": self.not_evaluated_rules,
            "combining_rule_trace": self.combining_rule_trace,
            "supporting_evidence": self.supporting_evidence,
            "conflicting_evidence": self.conflicting_evidence,
            "ai_consensus": self.ai_consensus,
            "ai_context_models": self.ai_context_models,
            "biological_evidence": self.biological_evidence,
            "confidence_score": self.confidence_score,
            "confidence_label": self.confidence_label,
            "confidence_pending": self.confidence_pending,
            "confidence_breakdown": self.confidence_breakdown,
            "priority_score": self.priority_score,
            "priority_category": self.priority_category,
            "priority_rank": self.priority_rank,
            "priority_pending": self.priority_pending,
            "priority_explanation": self.priority_explanation,
            "priority_breakdown": self.priority_breakdown,
            "conflict_summary": self.conflict_summary,
            "conflict_list": self.conflict_list,
            "conflict_score": self.conflict_score,
            "conflict_severity": self.conflict_severity,
            "conflict_resolution": self.conflict_resolution,
            "explainability": self.explainability,
            "recommendations": self.recommendations,
            "evidence_sources": self.evidence_sources,
            "legacy_summary": self.legacy_summary,
            "legacy_confidence": self.legacy_confidence,
            "legacy_significance_score": self.legacy_significance_score,
            # `raw_evidence` is intentionally NOT included in `to_dict()`
            # output by default -- it duplicates data already emitted
            # under the top-level `clinvar`/`gnomad`/`clingen`/etc. keys
            # in `build_variant_result()`'s output, and including it here
            # too would double the JSON report's size for no benefit to
            # a human or machine reader. It remains available in-process
            # (via the dataclass instance) for Phase 3/4/6/7 engines.
        }


# Generic, non-diagnostic next-step language keyed off the ACMG
# classification. This is standard clinical-genetics workflow guidance
# (e.g. "confirm with an orthogonal method"), not a patient-specific
# recommendation, and is not a substitute for clinician judgment -- callers
# surface it as-is.
_RECOMMENDATIONS_BY_CLASSIFICATION = {
    "Pathogenic": [
        "Confirm the variant call with an orthogonal method (e.g. Sanger sequencing) before clinical reporting.",
        "Correlate with patient phenotype and family history; consider genetic counseling referral.",
    ],
    "Likely Pathogenic": [
        "Confirm the variant call with an orthogonal method before clinical reporting.",
        "Consider additional evidence (segregation, functional studies) to strengthen classification if clinically actionable.",
    ],
    "Uncertain Significance": [
        "Insufficient evidence for a definitive classification; do not use for clinical decision-making without additional evidence.",
        "Consider reanalysis as new population, functional, or segregation data becomes available.",
    ],
    "Likely Benign": [
        "Evidence favors a benign interpretation; reclassification should follow if new conflicting evidence emerges.",
    ],
    "Benign": [
        "Population frequency and/or other evidence is inconsistent with a rare-disease-causing role for this variant.",
    ],
}


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def build_interpretation_result(
    *,
    variant_dict: Dict[str, Any],
    interpretation: Dict[str, Any],
    dna_models_used: List[str] = None,
    clinvar_result: Dict[str, Any] = None,
    dbsnp_result: Dict[str, Any] = None,
    protein_result: Dict[str, Any] = None,
    blast_result: Dict[str, Any] = None,
    alphamissense_result: Dict[str, Any] = None,
    mmsplice_result: Dict[str, Any] = None,
    gnomad_result: Dict[str, Any] = None,
    clingen_result: Dict[str, Any] = None,
    uniprot_result: Dict[str, Any] = None,
    interpro_result: Dict[str, Any] = None,
    alphafold_result: Dict[str, Any] = None,
    rna_result: Dict[str, Any] = None,
) -> InterpretationResult:
    """
    Build the canonical `InterpretationResult` from the outputs
    `InterpretationEngine.interpret()` already produced this call, plus the
    same raw per-stage dicts the orchestrator already has in hand. Pure
    reorganization -- no new scoring or evidence derivation happens here.
    """
    acmg = interpretation.get("acmg_evaluation") or {}
    # Schema validation at this boundary (pipeline/stage_schemas.py) --
    # logged loudly on a malformed acmg_evaluation, never fatal here
    # either: `acmg` above (the original, unvalidated dict) is still
    # what the rest of this function reads, exactly as before this
    # check existed.
    _variant_ref = f"{variant_dict.get('chrom')}:{variant_dict.get('pos')}{variant_dict.get('ref')}>{variant_dict.get('alt')}"
    validate_acmg_evaluation(acmg, variant_ref=_variant_ref)

    gene_symbol = None
    if clingen_result and clingen_result.get("gene_symbol"):
        gene_symbol = clingen_result["gene_symbol"]
    elif uniprot_result and uniprot_result.get("gene_symbol"):
        gene_symbol = uniprot_result["gene_symbol"]

    triggered_rules = acmg.get("triggered_criteria", [])
    not_triggered_rules = acmg.get("not_triggered_criteria", [])
    not_evaluated_rules = acmg.get("not_evaluated_criteria", [])

    supporting_evidence = list(interpretation.get("supporting_evidence") or [])
    conflicting_evidence: List[str] = []
    for rule in triggered_rules:
        supporting_evidence.extend(rule.get("supporting_evidence") or [])
        conflicting_evidence.extend(rule.get("conflicting_evidence") or [])
    for rule in not_triggered_rules:
        conflicting_evidence.extend(rule.get("conflicting_evidence") or [])

    ai_consensus: List[Dict[str, Any]] = []
    if alphamissense_result and not alphamissense_result.get("skipped") and alphamissense_result.get("found"):
        ai_consensus.append({
            "source": "AlphaMissense",
            "prediction": alphamissense_result.get("am_class"),
            "score": alphamissense_result.get("am_pathogenicity"),
            "target": alphamissense_result.get("protein_variant"),
        })
    if mmsplice_result and mmsplice_result.get("predicted"):
        ai_consensus.append({
            "source": "MMSplice",
            "prediction": mmsplice_result.get("interpretation_category"),
            "score": mmsplice_result.get("delta_logit_psi"),
            "target": "splicing",
        })

    biological_evidence = []
    try:
        # Reuse the existing helper rather than re-deriving the same
        # UniProt/InterPro/AlphaFold conditionals here a second time.
        from pipeline.interpretation import InterpretationEngine as _IE
        biological_evidence = _IE._biological_context_evidence(uniprot_result, interpro_result, alphafold_result)
    except Exception:
        biological_evidence = []

    evidence_sources = set()
    for rule in triggered_rules + not_triggered_rules:
        evidence_sources.update(rule.get("evidence_sources") or [])
    if ai_consensus:
        evidence_sources.update(c["source"] for c in ai_consensus)
    if biological_evidence:
        evidence_sources.update(["UniProt", "InterPro", "AlphaFold DB"])
    if blast_result and blast_result.get("hit_count", 0) > 0:
        evidence_sources.add("BLAST")
    if dbsnp_result and dbsnp_result.get("found"):
        evidence_sources.add("dbSNP")
    if clinvar_result and clinvar_result.get("records"):
        evidence_sources.add("ClinVar")

    classification = acmg.get("classification")
    recommendations = list(_RECOMMENDATIONS_BY_CLASSIFICATION.get(classification, []))

    # Bug fix (found during Phase 5 prep): this field's own docstring
    # ("HyenaDNA, Evo2, RNA-FM, ESM2 -- sequence/embedding
    # models that were invoked") always promised RNA-FM/ESM2 would be
    # included, but only `dna_models_used` was ever assigned here.
    # Mirrors the exact same check already used in
    # `ConfidenceEngine`/`PrioritizationEngine`'s sequence-context
    # factor, so all three engines now agree on what "ran".
    ai_context_models = list(dna_models_used or [])
    if rna_result and not rna_result.get("skipped") and not rna_result.get("error"):
        ai_context_models.append("RNA-FM")
    if protein_result and protein_result.get("esm2"):
        ai_context_models.append("ESM2")

    return InterpretationResult(
        variant=variant_dict,
        gene_symbol=gene_symbol,
        acmg_classification=classification,
        triggered_rules=triggered_rules,
        not_triggered_rules=not_triggered_rules,
        not_evaluated_rules=not_evaluated_rules,
        combining_rule_trace=acmg.get("combining_rule_trace", []),
        supporting_evidence=_dedupe(supporting_evidence),
        conflicting_evidence=_dedupe(conflicting_evidence),
        ai_consensus=ai_consensus,
        ai_context_models=ai_context_models,
        biological_evidence=biological_evidence,
        recommendations=recommendations,
        evidence_sources=sorted(evidence_sources),
        legacy_summary=interpretation.get("summary"),
        legacy_confidence=interpretation.get("confidence"),
        legacy_significance_score=interpretation.get("significance_score"),
        raw_evidence={
            "clinvar": clinvar_result,
            "dbsnp": dbsnp_result,
            "protein": protein_result,
            "blast": blast_result,
            "alphamissense": alphamissense_result,
            "mmsplice": mmsplice_result,
            "gnomad": gnomad_result,
            "clingen": clingen_result,
            "uniprot": uniprot_result,
            "interpro": interpro_result,
            "alphafold": alphafold_result,
        },
    )
