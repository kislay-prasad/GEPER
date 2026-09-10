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

import re
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
    # Report review round 10: the actual Tavtigian 2018 point totals
    # `ACMGRuleEngine._combine` computes its threshold comparisons
    # against (>= 10 Pathogenic, 6-9 Likely Pathogenic, 0-5 Uncertain
    # Significance, <= -1 Likely Benign, <= -7 Benign) -- see
    # `pipeline/acmg_rules.py::CombineResult`'s docstring for why this
    # used to be discarded after picking `acmg_classification` and
    # never recorded anywhere. `None` on all three (never a fabricated
    # `0.0`) only when BA1's stand-alone-benign short-circuit fired --
    # that path never ran the point tally at all.
    acmg_net_points: Optional[float] = None
    acmg_pathogenic_points: Optional[float] = None
    acmg_benign_points: Optional[float] = None
    # Interpretation-outcome state (human ruling, 2026-09-09/10) --
    # carried ALONGSIDE acmg_classification above, never replacing it,
    # never derived from it or from the per-source retrieval states.
    # One of "interpreted" / "insufficient_evidence" / "review_required"
    # -- "conflicting_evidence" was a fourth value through round 2 but
    # was REMOVED from the public InterpretationOutcome enum by ruling
    # (b), 2026-09-10 (round 3); the comparable-weight-conflict evidence
    # pattern it used to name now always escalates straight to
    # "review_required" instead. See `pipeline/interpretation_outcome.py`.
    interpretation_outcome: Optional[str] = None
    # Round 2 (RULED, 2026-09-09/10): which review_required trigger(s)
    # actually fired -- always a list (empty when interpretation_outcome
    # is not "review_required"), never omitted. The answer to "if both
    # [triggers] need to be visible, say so": interpretation_outcome
    # alone cannot distinguish a genuine evidence conflict from a
    # Pathogenic/Likely Pathogenic call from both at once, since only
    # one string is ever reported there. See
    # `pipeline/interpretation_outcome.py::determine_review_required_reasons`.
    review_required_reasons: List[str] = field(default_factory=list)

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

    # Which AI-consensus/context models (AlphaMissense, MMSplice,
    # ESM-2, RNA-FM) genuinely crashed for this variant, as opposed to
    # being legitimately not-run (disabled, ineligible variant type,
    # unavailable in this environment) -- distinct from `ai_consensus`
    # (which only ever lists a model that actually produced a real
    # verdict) precisely so a crash is never silently indistinguishable
    # from "not applicable" in `ai_consensus`'s own absence. See
    # `pipeline/stage_schemas.py::StageStatus`'s docstring and
    # `pipeline/orchestrator.py`'s `_run_rna_stage`/`_run_protein_stage`/
    # `_run_alphamissense_stage`/`_run_mmsplice_stage` for the `error`
    # key this is built from.
    ai_model_errors: List[Dict[str, Any]] = field(default_factory=list)

    # Legacy fields kept verbatim for any caller still reading the old
    # `interpretation` dict shape directly.
    legacy_summary: Optional[str] = None
    legacy_confidence: Optional[str] = None
    # Renamed from `legacy_significance_score` (report review round 10):
    # a JSON consumer with no repo access read this field, sitting right
    # next to `acmg_classification`, as if it WERE the ACMG/Tavtigian
    # score -- it is not. This is `InterpretationEngine.interpret()`'s
    # own pre-`ACMGRuleEngine` scoring system (ClinVar-concordance +
    # gnomAD/ClinGen/MMSplice weights, see `pipeline/interpretation.py`'s
    # `_SIGNIFICANCE_WEIGHT`), kept only as the fallback summary/
    # confidence source when the real ACMG rule engine itself raises
    # (`interpretation.py`: "falling back to legacy summary only").
    # `acmg_net_points` above is the real Tavtigian number.
    legacy_pre_acmg_significance_score: Optional[float] = None

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
            "acmg_net_points": self.acmg_net_points,
            "acmg_pathogenic_points": self.acmg_pathogenic_points,
            "acmg_benign_points": self.acmg_benign_points,
            "interpretation_outcome": self.interpretation_outcome,
            "review_required_reasons": self.review_required_reasons,
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
            "ai_model_errors": self.ai_model_errors,
            "legacy_summary": self.legacy_summary,
            "legacy_confidence": self.legacy_confidence,
            "legacy_pre_acmg_significance_score": self.legacy_pre_acmg_significance_score,
            # `raw_evidence` is intentionally NOT included in `to_dict()`
            # output by default -- it duplicates data already emitted
            # under the top-level `clinvar`/`gnomad`/`clingen`/etc. keys
            # in `build_variant_result()`'s output, and including it here
            # too would double the JSON report's size for no benefit to
            # a human or machine reader. It remains available in-process
            # (via the dataclass instance) for Phase 3/4/6/7 engines.
        }


# Verbs that open an instruction to the clinician. A WHITELIST, and it
# must stay one: a list of rejected phrasings would pass every test we
# could write today and admit the next conclusion-shaped entry
# unchallenged, which is exactly how the two entries this gate removed
# got in.
#
# Extending this list is expected and fine. Extending it to admit a
# particular sentence that will not otherwise pass is not -- that is the
# gate being edited to fit the entry rather than the entry being written
# to fit the gate.
_CLINICIAN_ACTION_OPENERS = frozenset(
    {
        "arrange",
        "avoid",
        "confirm",
        "consider",
        "correlate",
        "discuss",
        "do",
        "document",
        "monitor",
        "obtain",
        "order",
        "reanalyse",
        "reanalyze",
        "reclassify",
        "refer",
        "repeat",
        "request",
        "review",
        "schedule",
        "seek",
        "verify",
    }
)

# Clause boundaries: a semicolon, or a sentence break. The sentence rule
# requires two letters before the period so "(e.g. Sanger sequencing)"
# is not treated as the end of a clause.
# A clause boundary is a semicolon, or a full stop that ends a real word
# (two lowercase letters before it, so "e.g. Sanger" is not a boundary)
# followed by any further text.
#
# THE LOOKAHEAD IS `\S`, NOT `[A-Z]`, AND THE DIFFERENCE IS A FIXED BUG:
# requiring a capital meant "Discuss with the lab. the result is benign."
# was never split, so the second half was never inspected as a clause and
# its content was never checked at all. The gate returned True. That is
# the silent direction -- it reported "this is an action" about text
# carrying a conclusion.
#
# *** WHAT THIS SPLITTER STILL DOES NOT CATCH, STATED BECAUSE AN UNSTATED
# ASSUMPTION IS THE THING THAT ROTS: A COMMA IS NOT A BOUNDARY, AND
# NEITHER IS "and". "Refer to a specialist, the evidence favors
# pathogenicity." PASSES THIS GATE. That is not an oversight left to be
# tidied later -- adding "," rejects two entries shipping today
# ("(segregation, functional studies)" and "population, functional, or
# segregation data"), so closing it needs a splitter that understands
# parentheses and lists. The hole is pinned by two deliberately
# wrong-looking tests in test_recommendation_gate.py named
# test_KNOWN_LIMIT_*, so it is checked rather than merely written down. ***
_CLAUSE_BOUNDARY = re.compile(r";|(?<=[a-z]{2})\.\s+(?=\S)")


def is_clinician_action(text: str) -> bool:
    """
    True if `text` reads as a next step the clinician takes, rather than
    a statement about what the evidence means.

    EVERY clause must open with an imperative from
    `_CLINICIAN_ACTION_OPENERS`. All of them, not one: a single
    directive clause does not license a conclusion sitting beside it,
    and "Evidence favors X; reclassification should follow" is a
    conclusion whichever half you read.

    HONEST LIMIT, stated so nobody mistakes this for more than it is:
    this is a STRUCTURAL proxy for a semantic distinction. "Consider
    that this variant is benign" opens with an accepted imperative and
    would pass while smuggling a conclusion. So does "Refer to a
    specialist, the evidence favors pathogenicity." -- a comma is not a
    clause boundary here, and that one is the stronger bypass, because
    the smuggled half need not open with anything at all. See the
    `_CLAUSE_BOUNDARY` comment for why it is not simply closed, and
    `test_KNOWN_LIMIT_*` for where it is pinned. The gate makes the defect
    require deliberate circumvention instead of being what you get by
    default -- it does not make it impossible. A reviewer still reads
    the entry.
    """
    clauses = [c.strip() for c in _CLAUSE_BOUNDARY.split(text or "") if c.strip()]
    if not clauses:
        return False
    for clause in clauses:
        first = clause.split()[0].strip("\"'(),.:").lower() if clause.split() else ""
        if first not in _CLINICIAN_ACTION_OPENERS:
            return False
    return True


# Next-step language keyed off the ACMG classification, surfaced to the
# clinician as-is by every renderer.
#
# EVERY ENTRY IS A STEP THE CLINICIAN TAKES, NEVER A STATEMENT ABOUT
# WHAT THE EVIDENCE MEANS. That distinction is not merely asserted here
# -- `_assert_entries_are_clinician_actions()` below enforces it at
# import time, so an entry that fails cannot enter the map at all. The
# previous version of this comment asserted the same property while two
# of its five entries lacked it, which is why the property is now
# checked rather than claimed.
#
# "Benign" carries no entry deliberately. Its previous entry
# ("Population frequency ... is inconsistent with a rare-disease-causing
# role for this variant") named no step at all -- it was a conclusion
# end to end, so nothing survived the gate. Writing replacement clinical
# guidance is not an engineering decision, so none was invented. The
# Markdown renderer prints an explicit "No specific recommendations
# generated." line, so this is a visible empty state, not a silent one.
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
        # Was "Insufficient evidence for a definitive classification; do
        # not use ...". The leading clause stated a conclusion about the
        # evidence and did not survive the gate; the instruction it was
        # attached to is unchanged.
        "Do not use for clinical decision-making without additional evidence.",
        "Consider reanalysis as new population, functional, or segregation data becomes available.",
    ],
    "Likely Benign": [
        # Was "Evidence favors a benign interpretation; reclassification
        # should follow if new conflicting evidence emerges." The first
        # clause was a conclusion. The second is the same instruction,
        # re-expressed as the action it always described -- no new
        # clinical advice is added here.
        "Reclassify if new conflicting evidence emerges.",
    ],
    "Benign": [],
}


def _assert_entries_are_clinician_actions() -> None:
    """
    Import-time gate. Raises rather than warning: a conclusion stated in
    GEPER's own voice reaches a clinician through every renderer, and
    this codebase's recurring defect is a bad state that reads exactly
    like a good one. Failing at import is loud, immediate, impossible to
    misread, and happens long before anything renders.
    """
    for classification, entries in _RECOMMENDATIONS_BY_CLASSIFICATION.items():
        for entry in entries:
            if not is_clinician_action(entry):
                raise ValueError(
                    f"_RECOMMENDATIONS_BY_CLASSIFICATION[{classification!r}] contains an entry that is not a "
                    f"step the clinician takes: {entry!r}. Recommendations state next steps; they never state "
                    f"what the evidence means about the variant."
                )


_assert_entries_are_clinician_actions()


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
    dna_models_used: Optional[List[str]] = None,
    clinvar_result: Optional[Dict[str, Any]] = None,
    dbsnp_result: Optional[Dict[str, Any]] = None,
    protein_result: Optional[Dict[str, Any]] = None,
    blast_result: Optional[Dict[str, Any]] = None,
    alphamissense_result: Optional[Dict[str, Any]] = None,
    mmsplice_result: Optional[Dict[str, Any]] = None,
    gnomad_result: Optional[Dict[str, Any]] = None,
    clingen_result: Optional[Dict[str, Any]] = None,
    uniprot_result: Optional[Dict[str, Any]] = None,
    interpro_result: Optional[Dict[str, Any]] = None,
    alphafold_result: Optional[Dict[str, Any]] = None,
    rna_result: Optional[Dict[str, Any]] = None,
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
    _variant_ref = (
        f"{variant_dict.get('chrom')}:{variant_dict.get('pos')}{variant_dict.get('ref')}>{variant_dict.get('alt')}"
    )
    validate_acmg_evaluation(acmg, variant_ref=_variant_ref)

    gene_symbol = None
    if clingen_result and clingen_result.get("gene_symbol"):
        gene_symbol = clingen_result["gene_symbol"]
    elif uniprot_result and uniprot_result.get("gene_symbol"):
        gene_symbol = uniprot_result["gene_symbol"]

    triggered_rules = acmg.get("triggered_criteria", [])
    not_triggered_rules = acmg.get("not_triggered_criteria", [])
    not_evaluated_rules = acmg.get("not_evaluated_criteria", [])

    # Report review round 4, I8: `interpretation["supporting_evidence"]`
    # (the "legacy" pre-Phase-1 evidence list `InterpretationEngine.
    # interpret()` builds for its own `legacy_summary`/
    # `legacy_pre_acmg_significance_score` -- described here until
    # 2026-09-11 as "unused by any report view, confirmed by search",
    # which was right about the FIELDS and wrong about the VALUES:
    # neither number is printed, but the score decides
    # `summary`/`confidence`, which `report_generator.py::
    # _render_interpretation` renders. See the correction at
    # `interpretation.py`'s gnomAD call site and
    # `tests/test_gnomad_weight_reaches_the_reader.py`.)
    # once independently restated one fact PM2's own `supporting_evidence`
    # also states, worded differently ("gnomAD: variant not found in
    # the population database (PM2 evidence -- absent from gnomAD)."
    # vs. PM2's own "gnomAD: variant not found."), so the exact-string
    # `_dedupe` below never caught it and it showed twice. A
    # `_SUPERSEDED_LEGACY_EVIDENCE` tuple used to filter that one
    # sentence out here.
    #
    # THE FILTER IS GONE (2026-09-11) BECAUSE ITS SUBJECT IS GONE: the
    # HIGH 3 Q4-B/T3-F1 ruling removed the append at the source, so
    # `interpret()` reads `for _text, weight in gnomad_criteria:` and
    # that sentence is never placed in `supporting_evidence` by anything.
    # A filter whose input cannot occur is not protection, it is a
    # statement about the past that reads as one about the present --
    # and it was kept alive by a test that HAND-BUILT the impossible
    # input itself. Same treatment as InterPro's equivalent legacy/PM1
    # duplicate, which was likewise removed at its source rather than
    # filtered downstream.
    #
    # AND IT NEVER COVERED THE SURFACE IT LOOKED LIKE IT COVERED:
    # this filter only ever touched the merged `InterpretationResult`.
    # `report_generator.py::_render_interpretation` renders the LEGACY
    # `interpretation["supporting_evidence"]` list directly (:1391), so
    # a re-appended sentence would have reached the Markdown report
    # whether or not this line existed.
    #
    # THE GUARANTEE NOW LIVES WHERE IT CAN FAIL: if anyone re-appends
    # that sentence, `tests/test_supporting_evidence_dedup.py` goes RED
    # on the real `interpret()` path -- both because the fact would then
    # be stated twice and because the legacy list would carry it again.
    # A comment saying "do not re-add this" would not have.
    supporting_evidence = list(interpretation.get("supporting_evidence") or [])
    conflicting_evidence: List[str] = []
    for rule in triggered_rules:
        supporting_evidence.extend(rule.get("supporting_evidence") or [])
        conflicting_evidence.extend(rule.get("conflicting_evidence") or [])
    for rule in not_triggered_rules:
        conflicting_evidence.extend(rule.get("conflicting_evidence") or [])

    ai_consensus: List[Dict[str, Any]] = []
    if alphamissense_result and not alphamissense_result.get("skipped") and alphamissense_result.get("found"):
        ai_consensus.append(
            {
                "source": "AlphaMissense",
                "prediction": alphamissense_result.get("am_class"),
                "score": alphamissense_result.get("am_pathogenicity"),
                "target": alphamissense_result.get("protein_variant"),
            }
        )
    if mmsplice_result and mmsplice_result.get("predicted"):
        ai_consensus.append(
            {
                "source": "MMSplice",
                "prediction": mmsplice_result.get("interpretation_category"),
                "score": mmsplice_result.get("delta_logit_psi"),
                "target": "splicing",
            }
        )

    biological_evidence = []
    try:
        # Reuse the existing helper rather than re-deriving the same
        # UniProt/InterPro/AlphaFold conditionals here a second time.
        from pipeline.interpretation import InterpretationEngine as _IE

        biological_evidence = _IE._biological_context_evidence(uniprot_result, interpro_result, alphafold_result)
    except Exception:
        biological_evidence = []

    evidence_sources: set[str] = set()
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

    # `or ""` matters, not just type-cleanliness: an absent
    # "classification" key would otherwise pass None as the lookup key
    # below, which Dict[str, ...].get() never matches anyway (silently
    # falling through to the [] default) -- making that explicit avoids
    # relying on dict.get's untyped-key leniency.
    classification = acmg.get("classification") or ""
    recommendations = list(_RECOMMENDATIONS_BY_CLASSIFICATION.get(classification, []))

    # Bug fix (found during Phase 5 prep): this field's own docstring
    # ("HyenaDNA, Evo2, RNA-FM, ESM2 -- sequence/embedding
    # models that were invoked") always promised RNA-FM/ESM2 would be
    # included, but only `dna_models_used` was ever assigned here.
    # Mirrors the exact same check already used in
    # `ConfidenceEngine`/`PrioritizationEngine`'s sequence-context
    # factor, so all three engines now agree on what "ran".
    ai_context_models = list(dna_models_used or [])
    # INERT (2026-08-31): truthiness on `error`, not `is not None`, but
    # `_run_rna_stage` (orchestrator.py) keeps `skipped: True` on every
    # failure path -- `not rna_result.get("skipped")` already excludes
    # a failed run regardless of how `error` is read. Fixed to
    # `is not None` for consistency only; same check duplicated (and
    # fixed alongside this one) in confidence_engine.py,
    # interpretation.py, and prioritization_engine.py.
    if rna_result and not rna_result.get("skipped") and rna_result.get("error") is None:
        ai_context_models.append("RNA-FM")
    if protein_result and protein_result.get("esm2"):
        ai_context_models.append("ESM2")

    # A genuine crash (vs. a legitimate "not eligible"/"not available"
    # skip) for any AI-consensus or context model, so the report can
    # say "this model failed" instead of silently omitting it the same
    # way an ineligible variant would be -- see this field's own
    # docstring on `InterpretationResult`. Keyed off the `error` field
    # `pipeline/orchestrator.py`'s `_run_*_stage` methods now set ONLY
    # on their genuine-exception paths (never on a normal skip), so
    # this check can't misfire on an ordinary "AlphaMissense not
    # eligible for this variant" result.
    # POSITIVE-POLARITY TRUTHINESS (2026-08-31, sweep Batch 4): `if X.get
    # ("error"):` misses an empty-but-present error, silently dropping a
    # genuine model crash from this summary list entirely -- the mirror
    # image of the `not X.get("error")` sites Batch 3 fixed. Safe today
    # because every one of these four producers (orchestrator.py's
    # _run_rna_stage/_run_protein_stage/_run_alphamissense_stage/
    # _run_mmsplice_stage) was already fixed in Batches 1-2 to never
    # emit an empty string; fixed here anyway so the class cannot reopen
    # if any of those producers regresses or a fifth model is added
    # without the same care.
    ai_model_errors: List[Dict[str, Any]] = []
    if rna_result and rna_result.get("error") is not None:
        ai_model_errors.append({"source": "RNA-FM", "error": rna_result["error"]})
    if protein_result and protein_result.get("error") is not None:
        ai_model_errors.append({"source": "ESM2", "error": protein_result["error"]})
    if alphamissense_result and alphamissense_result.get("error") is not None:
        ai_model_errors.append({"source": "AlphaMissense", "error": alphamissense_result["error"]})
    if mmsplice_result and mmsplice_result.get("error") is not None:
        ai_model_errors.append({"source": "MMSplice", "error": mmsplice_result["error"]})

    return InterpretationResult(
        variant=variant_dict,
        gene_symbol=gene_symbol,
        acmg_classification=classification,
        triggered_rules=triggered_rules,
        not_triggered_rules=not_triggered_rules,
        not_evaluated_rules=not_evaluated_rules,
        combining_rule_trace=acmg.get("combining_rule_trace", []),
        # Report review round 10: the real Tavtigian point totals --
        # see `InterpretationResult.acmg_net_points`'s own docstring.
        # `None` (never `.get(..., 0)`) when absent, matching every
        # other not-yet-known field in this codebase.
        acmg_net_points=acmg.get("net_points"),
        acmg_pathogenic_points=acmg.get("pathogenic_points"),
        acmg_benign_points=acmg.get("benign_points"),
        interpretation_outcome=acmg.get("interpretation_outcome"),
        review_required_reasons=acmg.get("review_required_reasons") or [],
        supporting_evidence=_dedupe(supporting_evidence),
        conflicting_evidence=_dedupe(conflicting_evidence),
        ai_consensus=ai_consensus,
        ai_context_models=ai_context_models,
        biological_evidence=biological_evidence,
        recommendations=recommendations,
        evidence_sources=sorted(evidence_sources),
        ai_model_errors=ai_model_errors,
        legacy_summary=interpretation.get("summary"),
        legacy_confidence=interpretation.get("confidence"),
        legacy_pre_acmg_significance_score=interpretation.get("legacy_pre_acmg_significance_score"),
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
