"""
Phase 7 -- Explainability Engine.

Makes every conclusion GEPER already reached (ACMG classification --
Phase 1, confidence -- Phase 3, priority -- Phase 4, conflict
resolution -- Phase 6) fully transparent and traceable. This engine
computes nothing new and changes nothing: it is a pure, read-only
reorganization of fields those engines already produced, into the 14
explainability views the Phase 7 spec asks for. Every string below is
either copied verbatim from an existing field or built by simple,
inspectable formatting of existing fields -- no new scoring, no new
evidence lookups, no new classification logic.

Because of that, this engine's only input is the already-fully-
populated set of `InterpretationResult` fields (passed as explicit
kwargs, matching the calling convention already used by
`ConfidenceEngine`/`PrioritizationEngine`/`ConflictResolutionEngine`)
-- it does not take raw provider dicts as separate arguments the way
those engines do, because by Phase 7 there is nothing left to compute
from them; `raw_evidence` (already carried on `InterpretationResult`)
is enough to explain which sources did or didn't contribute.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Canonical evidence-source names this pipeline can draw on, mapped to
# their key in `raw_evidence` (see interpretation_result.py). Used only
# to enumerate "which sources did NOT contribute" against whatever
# `evidence_sources` (Phase 2, already computed) says DID -- the
# per-source reason is still read from the actual raw dict, never
# guessed.
_SOURCE_RAW_KEYS = {
    "ClinVar": "clinvar",
    "dbSNP": "dbsnp",
    "gnomAD": "gnomad",
    "ClinGen": "clingen",
    "UniProt": "uniprot",
    "InterPro": "interpro",
    "AlphaFold DB": "alphafold",
    "AlphaMissense": "alphamissense",
    "MMSplice": "mmsplice",
    "BLAST": "blast",
    "protein_translator": "protein",
}

_CONTEXT_MODEL_NAMES = ("HyenaDNA", "Evo2", "RNA-FM", "ESM2")


@dataclass
class ExplainabilityResult:
    decision_summary: str
    reasoning_chain: List[str]
    acmg_rules_fired: List[Dict[str, Any]]
    evidence_contributed: List[str]
    evidence_not_contributed: List[Dict[str, str]]
    ai_models_influential: List[Dict[str, Any]]
    ai_models_contextual_only: List[str]
    highest_weight_evidence: Dict[str, Any]
    conflicts_detected: List[Dict[str, Any]]
    conflicts_resolved: str
    remaining_uncertainties: List[str]
    limitations: List[str]
    confidence_rationale: str
    priority_rationale: str
    evidence_trace: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_summary": self.decision_summary,
            "reasoning_chain": self.reasoning_chain,
            "acmg_rules_fired": self.acmg_rules_fired,
            "evidence_contributed": self.evidence_contributed,
            "evidence_not_contributed": self.evidence_not_contributed,
            "ai_models_influential": self.ai_models_influential,
            "ai_models_contextual_only": self.ai_models_contextual_only,
            "highest_weight_evidence": self.highest_weight_evidence,
            "conflicts_detected": self.conflicts_detected,
            "conflicts_resolved": self.conflicts_resolved,
            "remaining_uncertainties": self.remaining_uncertainties,
            "limitations": self.limitations,
            "confidence_rationale": self.confidence_rationale,
            "priority_rationale": self.priority_rationale,
            "evidence_trace": self.evidence_trace,
        }


class ExplainabilityEngine:
    def explain(
        self,
        *,
        variant_dict: Dict[str, Any],
        gene_symbol: Optional[str],
        acmg_classification: Optional[str],
        triggered_rules: List[Dict[str, Any]],
        not_triggered_rules: List[Dict[str, Any]],
        not_evaluated_rules: List[Dict[str, Any]],
        combining_rule_trace: List[str],
        conflicting_evidence: List[Dict[str, Any]],
        ai_consensus: List[Dict[str, Any]],
        ai_context_models: List[str],
        confidence_score: Optional[float],
        confidence_label: Optional[str],
        confidence_pending: bool,
        confidence_breakdown: Optional[Dict[str, Any]],
        priority_score: Optional[float],
        priority_category: Optional[str],
        priority_pending: bool,
        priority_explanation: List[str],
        priority_breakdown: Optional[Dict[str, Any]],
        conflict_summary: Optional[str],
        conflict_list: List[Dict[str, Any]],
        conflict_severity: Optional[str],
        conflict_resolution: Optional[str],
        evidence_sources: List[str],
        raw_evidence: Dict[str, Any],
    ) -> ExplainabilityResult:
        locus = (
            f"{variant_dict.get('chrom')}:{variant_dict.get('pos')} {variant_dict.get('ref')}>{variant_dict.get('alt')}"
        )

        decision_summary = self._decision_summary(
            locus,
            gene_symbol,
            acmg_classification,
            confidence_label,
            confidence_pending,
            priority_category,
            priority_pending,
            conflict_severity,
        )
        reasoning_chain = self._reasoning_chain(
            locus,
            triggered_rules,
            not_evaluated_rules,
            combining_rule_trace,
            acmg_classification,
            confidence_score,
            confidence_label,
            confidence_pending,
            priority_score,
            priority_category,
            priority_pending,
            conflict_list,
            conflict_severity,
        )
        evidence_contributed = list(evidence_sources)
        evidence_not_contributed = self._evidence_not_contributed(evidence_sources, raw_evidence)
        ai_influential = list(ai_consensus)
        ai_contextual = list(ai_context_models)
        highest_weight = self._highest_weight_evidence(confidence_breakdown, priority_breakdown)
        real_conflicts = [c for c in (conflict_list or []) if c.get("severity") in ("Minor", "Moderate", "Major")]
        remaining_uncertainties = self._remaining_uncertainties(
            not_evaluated_rules,
            confidence_pending,
            priority_pending,
            conflict_list,
        )
        limitations = self._limitations(not_evaluated_rules, confidence_pending, priority_pending)
        confidence_rationale = self._confidence_rationale(
            confidence_score, confidence_label, confidence_pending, confidence_breakdown
        )
        priority_rationale = self._priority_rationale(
            priority_score, priority_category, priority_pending, priority_explanation
        )
        evidence_trace = self._evidence_trace(triggered_rules, not_triggered_rules, not_evaluated_rules)

        return ExplainabilityResult(
            decision_summary=decision_summary,
            reasoning_chain=reasoning_chain,
            acmg_rules_fired=list(triggered_rules),
            evidence_contributed=evidence_contributed,
            evidence_not_contributed=evidence_not_contributed,
            ai_models_influential=ai_influential,
            ai_models_contextual_only=ai_contextual,
            highest_weight_evidence=highest_weight,
            conflicts_detected=real_conflicts,
            conflicts_resolved=conflict_resolution or "No conflicts to resolve.",
            remaining_uncertainties=remaining_uncertainties,
            limitations=limitations,
            confidence_rationale=confidence_rationale,
            priority_rationale=priority_rationale,
            evidence_trace=evidence_trace,
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _decision_summary(
        locus, gene_symbol, classification, conf_label, conf_pending, pri_cat, pri_pending, conflict_severity
    ) -> str:
        gene_clause = f" in {gene_symbol}" if gene_symbol else ""
        conf_clause = f"confidence {conf_label}" if not conf_pending and conf_label else "confidence not yet scored"
        pri_clause = f"priority {pri_cat}" if not pri_pending and pri_cat else "priority not yet scored"
        conflict_clause = (
            f", conflict severity {conflict_severity}" if conflict_severity and conflict_severity != "None" else ""
        )
        return (
            f"Bij AI classified {locus}{gene_clause} as '{classification or 'not classified'}' "
            f"({conf_clause}; {pri_clause}{conflict_clause})."
        )

    @staticmethod
    def _reasoning_chain(
        locus,
        triggered_rules,
        not_evaluated_rules,
        combining_rule_trace,
        classification,
        confidence_score,
        confidence_label,
        confidence_pending,
        priority_score,
        priority_category,
        priority_pending,
        conflict_list,
        conflict_severity,
    ) -> List[str]:
        steps = [f"1. Variant identified: {locus}."]
        if triggered_rules:
            codes = ", ".join(r["code"] for r in triggered_rules)
            steps.append(f"2. ACMG rule engine evaluated all 28 criteria; {len(triggered_rules)} triggered ({codes}).")
        else:
            steps.append("2. ACMG rule engine evaluated all 28 criteria; none triggered.")
        if not_evaluated_rules:
            steps.append(
                f"   {len(not_evaluated_rules)} criteria could not be evaluated due to missing evidence sources (see remaining_uncertainties)."
            )
        if combining_rule_trace:
            steps.append(f"3. Combining rules applied: {' '.join(combining_rule_trace)}")
        steps.append(f"4. Resulting ACMG classification: '{classification or 'not classified'}'.")
        if confidence_pending:
            steps.append("5. Confidence scoring did not complete for this variant.")
        else:
            steps.append(
                f"5. Confidence scored independently of classification: {confidence_score:.1f}% ('{confidence_label}'), based on evidence completeness/quality across 7 categories (see confidence_rationale)."
            )
        if priority_pending:
            steps.append("6. Priority scoring did not complete for this variant.")
        else:
            steps.append(
                f"6. Priority scored using the classification and confidence above as two of its inputs: {priority_score:.1f} ('{priority_category}') (see priority_rationale)."
            )
        real_conflicts = [c for c in (conflict_list or []) if c.get("severity") in ("Minor", "Moderate", "Major")]
        if real_conflicts:
            steps.append(
                f"7. Conflict resolution engine detected {len(real_conflicts)} conflict(s), overall severity '{conflict_severity}'; classification was not altered (see conflicts_resolved)."
            )
        else:
            steps.append("7. Conflict resolution engine detected no significant conflicts.")
        return steps

    @staticmethod
    def _evidence_not_contributed(evidence_sources, raw_evidence) -> List[Dict[str, str]]:
        out = []
        for name, key in _SOURCE_RAW_KEYS.items():
            if name in (evidence_sources or []):
                continue
            r = (raw_evidence or {}).get(key)
            if not r:
                reason = "Not queried for this variant."
            elif r.get("skipped"):
                reason = "Lookup skipped (see pipeline graceful-fallback log)."
            elif r.get("error") is not None:
                reason = f"Lookup errored: {r.get('error')}."
            elif r.get("found") is False:
                reason = "Queried successfully; no record/result found for this variant."
            else:
                reason = "Queried; result did not contribute qualifying evidence to any ACMG criterion."
            out.append({"source": name, "reason": reason})
        return out

    @staticmethod
    def _highest_weight_evidence(confidence_breakdown, priority_breakdown) -> Dict[str, Any]:
        result: Dict[str, Any] = {"confidence": None, "priority": None}
        cats = (confidence_breakdown or {}).get("category_breakdown") or []
        if cats:
            top = max(cats, key=lambda c: c.get("contribution", 0))
            result["confidence"] = {
                "category": top["category"],
                "contribution": top["contribution"],
                "rationale": top["rationale"],
            }
        factors = (priority_breakdown or {}).get("factor_breakdown") or []
        if factors:
            top = max(factors, key=lambda f: f.get("contribution", 0))
            result["priority"] = {
                "factor": top["factor"],
                "contribution": top["contribution"],
                "rationale": top["rationale"],
            }
        return result

    @staticmethod
    def _remaining_uncertainties(not_evaluated_rules, confidence_pending, priority_pending, conflict_list) -> List[str]:
        items = []
        if not_evaluated_rules:
            codes = ", ".join(r.get("code", "?") for r in not_evaluated_rules)
            items.append(
                f"{len(not_evaluated_rules)} ACMG criteria not evaluated due to missing evidence sources: {codes}."
            )
        if confidence_pending:
            items.append("Confidence score not yet available for this variant.")
        if priority_pending:
            items.append("Priority score not yet available for this variant.")
        not_evaluable_conflicts = [c for c in (conflict_list or []) if c.get("severity") == "Not evaluable"]
        for c in not_evaluable_conflicts:
            items.append(f"{c['conflict_type']}: {c['resolution_rationale']}")
        return items

    @staticmethod
    def _limitations(not_evaluated_rules, confidence_pending, priority_pending) -> List[str]:
        # Mirrors report/clinical_report_builder.py's `_limitations`
        # (same underlying facts -- ACMG evaluation gaps and
        # pending-engine flags) rather than importing the report layer
        # into the pipeline layer, which would invert this codebase's
        # existing pipeline -> report dependency direction.
        items = [
            "Bij AI is a variant prioritisation system that assists qualified clinicians and pathologists; "
            "it produces a draft classification requiring qualified human review and final sign-off "
            "before any clinical use, and does not independently provide final clinical interpretation.",
        ]
        if not_evaluated_rules:
            items.append(
                f"{len(not_evaluated_rules)} ACMG criteria could not be evaluated for this variant due to "
                f"missing evidence sources; the classification reflects only the criteria that could be checked."
            )
        if confidence_pending:
            items.append("Confidence scoring did not complete for this variant.")
        if priority_pending:
            items.append("Priority scoring did not complete for this variant.")
        return items

    @staticmethod
    def _confidence_rationale(score, label, pending, breakdown) -> str:
        if pending:
            return "Confidence scoring did not complete for this variant."
        cats = (breakdown or {}).get("category_breakdown") or []
        parts = [f"{c['category']}: {c['rationale']}" for c in cats]
        conflict_note = (breakdown or {}).get("conflict_explanation", "")
        return (
            f"Scored {score:.1f}% ('{label}') from "
            + " | ".join(parts)
            + (f" Conflict adjustment: {conflict_note}" if conflict_note else "")
        )

    @staticmethod
    def _priority_rationale(score, category, pending, explanation) -> str:
        if pending:
            return "Priority scoring did not complete for this variant."
        base = f"Scored {score:.1f} ('{category}')."
        if explanation:
            return base + " " + " ".join(explanation)
        return base

    @staticmethod
    def _evidence_trace(triggered_rules, not_triggered_rules, not_evaluated_rules) -> List[Dict[str, Any]]:
        trace = []
        for r in triggered_rules:
            trace.append(
                {
                    "code": r["code"],
                    "status": "triggered",
                    "rationale": r.get("rationale"),
                    "evidence_sources": r.get("evidence_sources", []),
                }
            )
        for r in not_triggered_rules:
            trace.append(
                {
                    "code": r["code"],
                    "status": "not_triggered",
                    "rationale": r.get("rationale"),
                    "evidence_sources": r.get("evidence_sources", []),
                }
            )
        for r in not_evaluated_rules:
            trace.append(
                {
                    "code": r["code"],
                    "status": "not_evaluated",
                    "rationale": r.get("rationale"),
                    "evidence_sources": r.get("evidence_sources", []),
                }
            )
        trace.sort(key=lambda x: x["code"])
        return trace
