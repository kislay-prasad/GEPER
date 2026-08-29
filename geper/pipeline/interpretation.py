"""
Unified interpretation engine.

Merges evidence from every stage (DNA model routing choice, RNA-FM,
ESM-2, BLAST, ClinVar, dbSNP) into a single human-readable summary
with a coarse confidence rating. This is a rules-based aggregator
(not a model) so its logic is fully transparent and auditable --
appropriate for a clinical-adjacent research tool where every
statement in the final report must be traceable to a concrete input.
"""

from typing import Any, Dict, List, Optional

from config import CONFIG
from utils.logger import get_logger
from pipeline.acmg_rules import ACMGRuleEngine
from pipeline.interpretation_result import build_interpretation_result
from pipeline.confidence_engine import ConfidenceEngine
from pipeline.prioritization_engine import PrioritizationEngine, floor_category_for_conflict_severity
from pipeline.conflict_resolution_engine import ConflictResolutionEngine
from pipeline.explainability_engine import ExplainabilityEngine
from pipeline.pvs1.models import LOF_ESTABLISHED, LOF_ESTABLISHED_RECESSIVE, LOF_NOT_ESTABLISHED, LOF_REFUTED
from pipeline.pvs1.utils import (
    alphamissense_evidence_sentence,
    dosage_sensitivity_verdict,
    null_variant_term,
    protein_effect_flags,
    protein_effect_undetermined_reason,
    transcript_from_result,
)

logger = get_logger(__name__)

_SIGNIFICANCE_WEIGHT = {
    "pathogenic": 3,
    "likely pathogenic": 2,
    "uncertain significance": 1,
    "likely benign": -2,
    "benign": -3,
}


class InterpretationEngine:
    """Combines multi-stage evidence into one unified interpretation block."""

    def __init__(self):
        self._acmg_engine = ACMGRuleEngine()
        self._confidence_engine = ConfidenceEngine()
        self._prioritization_engine = PrioritizationEngine()
        self._conflict_engine = ConflictResolutionEngine()
        self._explainability_engine = ExplainabilityEngine()

    def interpret(
        self,
        variant_dict: Dict[str, Any],
        dna_models_used: List[str],
        clinvar_result: Dict[str, Any],
        dbsnp_result: Dict[str, Any],
        protein_result: Dict[str, Any],
        blast_result: Dict[str, Any],
        alphamissense_result: Optional[Dict[str, Any]] = None,
        mmsplice_result: Optional[Dict[str, Any]] = None,
        gnomad_result: Optional[Dict[str, Any]] = None,
        conservation_result: Optional[Dict[str, Any]] = None,
        clingen_result: Optional[Dict[str, Any]] = None,
        uniprot_result: Optional[Dict[str, Any]] = None,
        interpro_result: Optional[Dict[str, Any]] = None,
        alphafold_result: Optional[Dict[str, Any]] = None,
        rna_result: Optional[Dict[str, Any]] = None,
        ensemble_result: Optional[Dict[str, Any]] = None,
        transcript_result: Optional[Dict[str, Any]] = None,
        clinvar_codon_result: Optional[Dict[str, Any]] = None,
        spliceformer_result: Optional[Dict[str, Any]] = None,
        splicebert_result: Optional[Dict[str, Any]] = None,
        hpo_result: Optional[Dict[str, Any]] = None,
        phenotype_result: Optional[Dict[str, Any]] = None,
        functional_evidence_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        evidence: List[str] = []
        # float, not int: weights accumulated below range from
        # _SIGNIFICANCE_WEIGHT's int literals to CONFIG.mmsplice's/
        # _gnomad_acmg_evidence's/_clingen_acmg_evidence's float ones,
        # in the same running total -- mypy caught this being narrowed
        # to "int" by inference from the first (int) weight added.
        significance_score: float = 0

        chrom, pos, ref, alt = (
            variant_dict.get("chrom"),
            variant_dict.get("pos"),
            variant_dict.get("ref"),
            variant_dict.get("alt"),
        )

        # ClinVar evidence carries the most clinical weight. Reads
        # `primary_record` (the allele-matched record), never
        # `records[0]` -- ClinVar's positional search can return
        # several distinct co-located variants at one genomic
        # position, and a bare first-record read used to attribute
        # whichever one happened to sort first to this variant, even
        # when it was a different allele entirely (confirmed live:
        # BRCA1 17:43094298 returns a genuinely Benign SNV alongside an
        # unrelated Pathogenic frameshift deletion at the same locus --
        # see `database/clinvar_client.py`'s module docstring).
        clinvar_records = (clinvar_result or {}).get("records") or []
        if clinvar_result and clinvar_result.get("match_status") == "matched" and clinvar_result.get("primary_record"):
            top = clinvar_result["primary_record"]
            sig = (top.get("clinical_significance") or "").strip().lower()
            # float, not int: this name is reused below for MMSplice's/
            # gnomAD's/ClinGen's float weights (same running total,
            # same variable name) -- an int-only annotation here made
            # mypy flag those later, legitimate float assignments.
            weight: float = _SIGNIFICANCE_WEIGHT.get(sig, 0)
            significance_score += weight
            evidence.append(
                f"ClinVar reports '{top.get('clinical_significance', 'unknown significance')}' "
                f"(review status: {top.get('review_status', 'n/a')})."
            )
        elif clinvar_result and clinvar_result.get("match_status") == "position_only":
            evidence.append(
                f"No ClinVar record found for this exact variant ({len(clinvar_records)} other, "
                "non-matching ClinVar-catalogued variant(s) exist at this genomic position; their "
                "classifications are not evidence about this variant)."
            )
        else:
            evidence.append("No ClinVar record found; clinical significance is undetermined from this source.")

        # dbSNP presence indicates the variant is catalogued (common or
        # previously observed). `found`/`rsid` only reflect an
        # allele-matched rsID (see `database/dbsnp_client.py`'s module
        # docstring) -- never the first rsID a position search happened
        # to return, which used to misattribute a co-located variant's
        # rsID to this one (confirmed live: 17:43094298 returned
        # rs397508848, the rsID of an unrelated 2bp deletion, instead
        # of rs80357024, this variant's own).
        if dbsnp_result and dbsnp_result.get("match_status") == "matched":
            evidence.append(f"Catalogued in dbSNP as {dbsnp_result.get('rsid')}.")
        elif dbsnp_result and dbsnp_result.get("match_status") == "position_only":
            evidence.append(
                f"Not catalogued in dbSNP under this exact allele ({dbsnp_result.get('record_count')} "
                "other rsID(s) exist at this genomic position, but do not match this allele)."
            )
        else:
            evidence.append("Not found in dbSNP; may be novel or a private variant.")

        # Protein-level consequence, called from the transcript's real
        # CDS reading frame -- never from
        # `pipeline/protein_translator.py`'s frame-unaware local
        # translation window. That window translates from the first
        # AUG it happens to find in a short flanking sequence, which is
        # an essentially arbitrary reading frame for any variant more
        # than a few dozen bases from the transcript's true start
        # codon -- it previously produced this exact line, "Translated
        # protein sequence is unchanged (synonymous at the protein
        # level)", for real missense variants (e.g. PRNP p.Pro102Leu)
        # purely by coincidence of which wrong codon the substitution
        # landed in. See `pipeline/pvs1/utils.py::protein_effect_flags`
        # and `coding_consequence_detail` for the transcript-CDS-frame
        # replacement.
        # `is_predicted_lof` is tracked explicitly (rather than
        # re-derived from the `evidence` text later) so the ClinGen
        # dosage-sensitivity block below can condition PVS1-style
        # reasoning on the actual translation outcome, not on string
        # matching against human-readable evidence text.
        is_predicted_lof = False
        protein_flags = protein_effect_flags(variant_dict, transcript_from_result(transcript_result))
        if not protein_flags.determined:
            evidence.append(
                "Protein-level consequence could not be determined from transcript data: "
                + protein_effect_undetermined_reason(variant_dict, transcript_result)
            )
        elif protein_flags.is_synonymous:
            evidence.append("Transcript-verified protein consequence: synonymous (no amino acid change).")
        elif protein_flags.is_lof:
            # HIGH 3 / T3-F3 (2026-08-28, ruled): term sourced from the
            # shared `null_variant_term` -- see its docstring for why
            # both "frameshift"/"nonsense (stop-gained)" are kept.
            is_frameshift = len(ref or "") != len(alt or "")
            evidence.append(f"Transcript-verified protein consequence: {null_variant_term(is_frameshift)}.")
            significance_score += 2
            is_predicted_lof = True
        elif protein_flags.is_inframe_indel:
            evidence.append("Transcript-verified protein consequence: in-frame insertion/deletion.")
        elif protein_flags.is_missense:
            evidence.append("Transcript-verified protein consequence: missense (amino acid substitution).")
            significance_score += 1

        # AlphaMissense: a modest, independent contribution alongside the
        # protein-level missense evidence above -- deliberately small
        # relative to ClinVar's weights, since AlphaMissense is a
        # computational predictor, not a clinical classification.
        # HIGH 3 / AM-07 (2026-08-28, ruled): text sourced from the
        # shared `alphamissense_evidence_sentence` -- this legacy line
        # and `acmg_rules.py::_pp3_bp4`'s own AlphaMissense line used to
        # be two independently-worded sentences that could both land in
        # the same variant's `supporting_evidence` (worded differently
        # enough that exact-string dedup never merged them: a caveated
        # and an uncaveated claim about the same tool, side by side, in
        # every report format -- a visible contradiction, not merely a
        # missing disclosure). Now byte-identical when both fire, so the
        # existing exact-match `_dedupe` in interpretation_result.py
        # collapses the duplicate on its own -- construction instead of
        # a second, by-hand-synced copy.
        am_sentence = alphamissense_evidence_sentence(alphamissense_result)
        if am_sentence is not None:
            evidence.append(am_sentence)
            am_class = ((alphamissense_result or {}).get("am_class") or "").strip().lower()
            if am_class == "likely_pathogenic":
                significance_score += 1
            elif am_class == "likely_benign":
                significance_score -= 1

        # MMSplice: an independent splice-effect evidence source,
        # additive alongside (never replacing) every evidence item
        # already appended above -- this block only ever appends one
        # more `evidence` entry and adds to `significance_score`; it
        # never rewrites or removes what ClinVar/dbSNP/protein/
        # AlphaMissense already contributed. Weighted by
        # `CONFIG.mmsplice.ACMG_EVIDENCE_WEIGHT` (default 1.0; set to 0
        # to keep MMSplice visible in reports/API output while
        # excluding it from the scored aggregate).
        if mmsplice_result and mmsplice_result.get("predicted"):
            interpretation_text = mmsplice_result.get("interpretation") or "MMSplice prediction available."
            delta_logit_psi = mmsplice_result.get("delta_logit_psi")
            delta_str = f"{delta_logit_psi:.3f}" if isinstance(delta_logit_psi, (int, float)) else "n/a"
            evidence.append(
                f"MMSplice predicts: {interpretation_text} (delta_logit_psi={delta_str}, "
                f"confidence={mmsplice_result.get('confidence', 'n/a')})."
            )
            category = mmsplice_result.get("interpretation_category")
            weight = CONFIG.mmsplice.ACMG_EVIDENCE_WEIGHT
            if category in ("strong_donor_loss", "strong_acceptor_loss", "exon_skipping", "intron_retention", "strong"):
                significance_score += 2 * weight
            elif category == "moderate":
                significance_score += 1 * weight
        elif mmsplice_result and mmsplice_result.get("supported") and not mmsplice_result.get("predicted"):
            evidence.append(
                f"MMSplice did not produce a prediction for this variant "
                f"({mmsplice_result.get('skip_reason', 'unknown reason')})."
            )

        # gnomAD: population-frequency evidence contributing to this
        # engine's own `significance_score` (BA1/BS1/PM2-shaped
        # weights, using configurable thresholds, CONFIG.gnomad.*).
        #
        # Q4-B/T3-F1 (HIGH 3, 2026-08-28): this block used to also
        # `evidence.append(text)` -- deliberately removed. `_gnomad_
        # acmg_evidence` only ever reads `global_af`/popmax; it has no
        # population-priority-AF awareness at all (unlike `ACMGRuleEngine.
        # _ba1_bs1`/`_pm2`, which do via `_population_priority_context`
        # when `CONFIG.gnomad.POPULATION_PRIORITY` is configured), so
        # under that supported config its sentence can be flatly wrong
        # (e.g. "no BA1/BS1/PM2 threshold met" for a variant the real
        # ACMG engine correctly flags as PM2-triggered from the priority
        # population's own AF) while sitting right beside the real
        # engine's own correct, disclosure-carrying sentence for the
        # same variant in the same rendered `supporting_evidence` pool.
        # The real engine already emits the correct frequency with its
        # own disclosures, so this line was a redundant sentence that
        # could be wrong under a supported config, not a needed one.
        # Removing it is smaller than correcting it and eliminates the
        # divergence rather than maintaining it (ruling: HIGH 3 Q4-B
        # amendment). The `significance_score` contribution below is
        # intentionally UNCHANGED -- that number is provably unread by
        # every report renderer (verified by search, HIGH 3 Q4-B) and
        # the underlying global_af-only inconsistency is its own,
        # separately carded, deliberately-not-dispatched issue; only the
        # reader-visible sentence is in scope here.
        gnomad_criteria = self._gnomad_acmg_evidence(gnomad_result)
        for _text, weight in gnomad_criteria:
            significance_score += weight

        # ClinGen: gene-level clinical evidence (gene-disease clinical
        # validity, dosage sensitivity) contributing supporting-only
        # ACMG/AMP evidence -- PVS1 support via haploinsufficiency for
        # a predicted loss-of-function variant, and a PP5/BP6-style
        # gene-context signal from the strongest curated gene-disease
        # classification. Like the gnomAD block above, this only ever
        # appends new `evidence` entries and adds to
        # `significance_score`; it never rewrites or removes anything
        # already contributed (requirement #5: "Never overwrite
        # existing ACMG evidence. Only add evidence.").
        clingen_criteria = self._clingen_acmg_evidence(clingen_result, is_predicted_lof)
        for text, weight in clingen_criteria:
            evidence.append(text)
            significance_score += weight

        # Biological evidence layer (UniProt protein function/disease
        # relevance, InterPro/Pfam domain context, AlphaFold DB
        # structural confidence). Unlike the gnomAD/ClinGen blocks
        # above, these are surfaced as purely informational context
        # (weight 0.0) rather than scored ACMG-style evidence: this
        # engine (unlike `ACMGRuleEngine._pm1`, which does score
        # InterPro domain overlap as PM1) has no per-criterion slot for
        # them, so they only add descriptive context for a reviewer,
        # never a score change. The protein position they're keyed to
        # (when present) is a transcript-verified canonical residue
        # number -- see the orchestrator's
        # `_canonical_protein_position` -- and is simply absent
        # (`None`) rather than a fabricated guess when it could not be
        # determined. Matches requirement #5's "never overwrite
        # existing ACMG evidence, only add evidence" for every other
        # evidence source.
        for text in self._biological_context_evidence(uniprot_result, interpro_result, alphafold_result):
            evidence.append(text)

        # DNA model routing context.
        #
        # Report review round 5, J4: this line only ever listed
        # `dna_models_used` (the routed HyenaDNA/Evo2 model), while the
        # Limitations block (`ai_context_models`, built in
        # `build_interpretation_result`) and the Evidence Completeness
        # table (`ConfidenceEngine._sequence_context_quality`) both
        # already counted RNA-FM/ESM2 too whenever they actually ran --
        # so a finding could read "Sequence context analyzed with:
        # hyenadna" in Supporting Evidence right next to "Sequence-
        # context model(s) actually used for this variant: hyenadna,
        # RNA-FM, ESM2" in Limitations, contradicting itself within the
        # same report. Same eligibility check as both of those (RNA-FM
        # ran and didn't error/skip; ESM2 present on the protein
        # result), so all three now agree on what "ran" rather than
        # "routed to" vs. "ran for" silently meaning different things.
        sequence_context_models = list(dna_models_used or [])
        if rna_result and not rna_result.get("skipped") and not rna_result.get("error"):
            sequence_context_models.append("RNA-FM")
        if protein_result and protein_result.get("esm2"):
            sequence_context_models.append("ESM2")
        if sequence_context_models:
            evidence.append(f"Sequence context analyzed with: {', '.join(sequence_context_models)}.")

        # BLAST context.
        if blast_result and blast_result.get("hit_count", 0) > 0:
            evidence.append(f"BLAST search returned {blast_result['hit_count']} homologous region(s).")

        summary, confidence = self._build_summary(chrom, pos, ref, alt, significance_score, bool(clinvar_records))

        # Phase 1: independent, criterion-by-criterion ACMG/AMP evaluation.
        # Additive only -- computed from the same evidence dicts already
        # passed in above, and returned under a new key. Never touches or
        # replaces any of the four keys this method has always returned,
        # so existing callers/report builders keep working unchanged.
        try:
            acmg_evaluation = self._acmg_engine.evaluate(
                clinvar_result=clinvar_result,
                dbsnp_result=dbsnp_result,
                protein_result=protein_result,
                alphamissense_result=alphamissense_result,
                mmsplice_result=mmsplice_result,
                gnomad_result=gnomad_result,
                conservation_result=conservation_result,
                clingen_result=clingen_result,
                interpro_result=interpro_result,
                ensemble_result=ensemble_result,
                # PVS1 needs the variant's own coordinates and the
                # transcript structure they sit in; every other criterion
                # is answered from the provider dicts alone.
                variant_dict=variant_dict,
                transcript_result=transcript_result,
                clinvar_codon_result=clinvar_codon_result,
                uniprot_result=uniprot_result,
                spliceformer_result=spliceformer_result,
                splicebert_result=splicebert_result,
                hpo_result=hpo_result,
                phenotype_result=phenotype_result,
                functional_evidence_result=functional_evidence_result,
            )
        except Exception:
            logger.exception("ACMG rule engine failed; falling back to legacy summary only.")
            acmg_evaluation = {"error": "ACMG rule engine failed; see logs.", "classification": None}

        legacy_result = {
            "summary": summary,
            "confidence": confidence,
            # Renamed from "significance_score" (report review round 10):
            # this is `InterpretationEngine`'s own pre-`ACMGRuleEngine`
            # scoring system (ClinVar-concordance + gnomAD/ClinGen/
            # MMSplice weights, `_SIGNIFICANCE_WEIGHT` above) -- not
            # derived from the 28 Tavtigian ACMG/AMP criteria at all.
            # The bare name "significance_score", serialized verbatim
            # into geper_results.json right beside
            # interpretation.acmg_evaluation.classification, reads as
            # the ACMG score to a JSON consumer with no repo access --
            # confirmed as a real, made-that-mistake-myself failure
            # mode, not a hypothetical one. `acmg_evaluation.net_points`
            # is the real Tavtigian number (see `ACMGRuleEngine.
            # _combine`/`CombineResult` in pipeline/acmg_rules.py).
            "legacy_pre_acmg_significance_score": significance_score,
            "supporting_evidence": evidence,
            "acmg_evaluation": acmg_evaluation,
        }

        # Phase 2: build the canonical InterpretationResult on top of the
        # legacy dict above (which now includes acmg_evaluation) plus the
        # same raw provider dicts this call already received. Additive
        # only -- every key `legacy_result` had a moment ago is still
        # present unchanged; this adds exactly one new key.
        try:
            result_obj = build_interpretation_result(
                variant_dict=variant_dict,
                interpretation=legacy_result,
                dna_models_used=dna_models_used,
                clinvar_result=clinvar_result,
                dbsnp_result=dbsnp_result,
                protein_result=protein_result,
                blast_result=blast_result,
                alphamissense_result=alphamissense_result,
                mmsplice_result=mmsplice_result,
                gnomad_result=gnomad_result,
                clingen_result=clingen_result,
                uniprot_result=uniprot_result,
                interpro_result=interpro_result,
                alphafold_result=alphafold_result,
                rna_result=rna_result,
            )

            # Phase 3: independent confidence scoring, applied on top of
            # the InterpretationResult Phase 2 just built. Reuses its
            # `conflicting_evidence` / `ai_consensus` (already derived,
            # not recomputed) alongside the same raw provider dicts.
            # Only these three fields are set here -- everything else on
            # `result_obj` (acmg_classification, triggered_rules, etc.)
            # is untouched.
            confidence_result = None  # pre-initialized so Phase 6 below can safely check "did this run"
            try:
                confidence_result = self._confidence_engine.score(
                    clinvar_result=clinvar_result,
                    clingen_result=clingen_result,
                    gnomad_result=gnomad_result,
                    dbsnp_result=dbsnp_result,
                    alphamissense_result=alphamissense_result,
                    mmsplice_result=mmsplice_result,
                    dna_models_used=dna_models_used,
                    rna_result=rna_result,
                    protein_result=protein_result,
                    uniprot_result=uniprot_result,
                    interpro_result=interpro_result,
                    alphafold_result=alphafold_result,
                    blast_result=blast_result,
                    conflicting_evidence=result_obj.conflicting_evidence,
                    ai_consensus=result_obj.ai_consensus,
                )
                result_obj.confidence_score = confidence_result.score
                result_obj.confidence_label = confidence_result.label
                result_obj.confidence_pending = False
                result_obj.confidence_breakdown = confidence_result.to_dict()
            except Exception:
                logger.exception("Confidence engine failed; confidence_pending remains True.")

            # Phase 4: variant prioritization. Independent scoring logic
            # from Phase 1/3, but explicitly takes the ACMG
            # classification and confidence score Phase 1/3 already
            # produced as two of its weighted inputs (per the Phase 4
            # spec), rather than ignoring or recomputing them. Batch-
            # relative `priority_rank` is intentionally left unset here
            # -- see `prioritization_engine.rank_batch`, applied once by
            # the orchestrator after every variant in a run is scored.
            priority_result = None  # pre-initialized so Phase 6 below can safely check "did this run"
            try:
                triggered_codes = [c.get("code") for c in result_obj.triggered_rules]
                # Report review round 4, I2: cheaply re-runs the same
                # Critical-conflict check Phase 6 will do properly below
                # (via `ConflictResolutionEngine.has_critical_conflict`,
                # a thin wrapper around `_expert_panel_disagreement_
                # conflict`) so Phase 4 can floor review priority at
                # Critical when it fires -- Phase 6 itself can't run
                # first here, since it needs Phase 4's own conflict-
                # penalty output as one of its inputs.
                critical_conflict = self._conflict_engine.has_critical_conflict(
                    result_obj.acmg_classification, clinvar_result
                )
                priority_result = self._prioritization_engine.score(
                    acmg_classification=result_obj.acmg_classification,
                    confidence_score=result_obj.confidence_score,
                    triggered_rule_codes=triggered_codes,
                    clinvar_result=clinvar_result,
                    clingen_result=clingen_result,
                    gnomad_result=gnomad_result,
                    dbsnp_result=dbsnp_result,
                    alphamissense_result=alphamissense_result,
                    mmsplice_result=mmsplice_result,
                    dna_models_used=dna_models_used,
                    rna_result=rna_result,
                    protein_result=protein_result,
                    interpro_result=interpro_result,
                    alphafold_result=alphafold_result,
                    blast_result=blast_result,
                    ai_consensus=result_obj.ai_consensus,
                    conflicting_evidence=result_obj.conflicting_evidence,
                    critical_conflict=critical_conflict,
                )
                result_obj.priority_score = priority_result.score
                result_obj.priority_category = priority_result.category
                result_obj.priority_pending = False
                result_obj.priority_explanation = priority_result.explanation
                result_obj.priority_breakdown = priority_result.to_dict()
            except Exception:
                logger.exception("Prioritization engine failed; priority_pending remains True.")

            # Phase 6: conflict resolution. Purely additive and purely
            # documentary -- never touches acmg_classification or the
            # confidence/priority scores above. Reuses the ACMG
            # criteria's own conflicting_evidence (already on
            # result_obj), ai_consensus (already on result_obj), and
            # the confidence/priority engines' own conflict-penalty
            # output computed just above, rather than re-deriving any
            # of that.
            try:
                conflict_result = self._conflict_engine.detect(
                    acmg_classification=result_obj.acmg_classification,
                    acmg_conflicting_evidence=result_obj.conflicting_evidence,
                    ai_consensus=result_obj.ai_consensus,
                    confidence_conflict_penalty=(
                        confidence_result.conflict_penalty_applied if confidence_result is not None else 0.0
                    ),
                    confidence_conflict_explanation=(
                        confidence_result.conflict_explanation
                        if confidence_result is not None
                        else "Confidence engine did not run for this variant."
                    ),
                    priority_conflict_penalty=(
                        priority_result.conflict_penalty_applied if priority_result is not None else 0.0
                    ),
                    priority_conflict_explanation=(
                        priority_result.conflict_explanation
                        if priority_result is not None
                        else "Prioritization engine did not run for this variant."
                    ),
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
                result_obj.conflict_summary = conflict_result.conflict_summary
                result_obj.conflict_list = [c.to_dict() for c in conflict_result.conflict_list]
                result_obj.conflict_score = conflict_result.conflict_score
                result_obj.conflict_severity = conflict_result.conflict_severity
                result_obj.conflict_resolution = conflict_result.conflict_resolution

                # Report review round 5, J3: I2's Critical-only floor
                # (applied inside Phase 4 above, from the cheap
                # pre-check) left Moderate/Major conflicts free to sort
                # below no-conflict findings. Now that Phase 6 has
                # computed the real, full-tier conflict_severity, apply
                # the same floor shape monotonically across every tier
                # -- see `floor_category_for_conflict_severity`'s own
                # docstring for why this has to happen here rather than
                # inside Phase 4 itself.
                if priority_result is not None and not result_obj.priority_pending:
                    floored_category, floor_reason = floor_category_for_conflict_severity(
                        result_obj.priority_category, result_obj.conflict_severity
                    )
                    if floor_reason is not None:
                        result_obj.priority_category = floored_category
                        result_obj.priority_explanation = list(result_obj.priority_explanation or []) + [floor_reason]
                        if isinstance(result_obj.priority_breakdown, dict):
                            result_obj.priority_breakdown["category"] = floored_category
                            result_obj.priority_breakdown["explanation"] = result_obj.priority_explanation
            except Exception:
                logger.exception("Conflict resolution engine failed; conflict_severity remains unset.")

            # Phase 7: explainability. Runs last and reads only fields
            # `result_obj` already has by this point (ACMG/confidence/
            # priority/conflict all populated above) -- computes nothing
            # new, changes nothing on result_obj except the single
            # additive `explainability` field itself.
            try:
                explain_result = self._explainability_engine.explain(
                    variant_dict=result_obj.variant,
                    gene_symbol=result_obj.gene_symbol,
                    acmg_classification=result_obj.acmg_classification,
                    triggered_rules=result_obj.triggered_rules,
                    not_triggered_rules=result_obj.not_triggered_rules,
                    not_evaluated_rules=result_obj.not_evaluated_rules,
                    combining_rule_trace=result_obj.combining_rule_trace,
                    conflicting_evidence=result_obj.conflicting_evidence,
                    ai_consensus=result_obj.ai_consensus,
                    ai_context_models=result_obj.ai_context_models,
                    confidence_score=result_obj.confidence_score,
                    confidence_label=result_obj.confidence_label,
                    confidence_pending=result_obj.confidence_pending,
                    confidence_breakdown=result_obj.confidence_breakdown,
                    priority_score=result_obj.priority_score,
                    priority_category=result_obj.priority_category,
                    priority_pending=result_obj.priority_pending,
                    priority_explanation=result_obj.priority_explanation,
                    priority_breakdown=result_obj.priority_breakdown,
                    conflict_summary=result_obj.conflict_summary,
                    conflict_list=result_obj.conflict_list,
                    conflict_severity=result_obj.conflict_severity,
                    conflict_resolution=result_obj.conflict_resolution,
                    evidence_sources=result_obj.evidence_sources,
                    raw_evidence=result_obj.raw_evidence,
                )
                result_obj.explainability = explain_result.to_dict()
            except Exception:
                logger.exception("Explainability engine failed; explainability remains unset.")

            interpretation_result = result_obj.to_dict()
        except Exception:
            logger.exception("InterpretationResult aggregation failed; legacy fields remain unaffected.")
            interpretation_result = {"error": "InterpretationResult aggregation failed; see logs."}

        legacy_result["interpretation_result"] = interpretation_result
        return legacy_result

    @staticmethod
    def _gnomad_acmg_evidence(gnomad_result: Optional[Dict[str, Any]]) -> List["tuple[str, float]"]:
        """
        Translate a gnomAD evidence dict (see `pipeline/gnomad/models.py::
        GnomadAnnotation.to_dict()`) into zero or more (evidence_text,
        score_weight) pairs for ACMG/AMP BA1/BS1/PM2, using
        `CONFIG.gnomad`'s configurable thresholds. Returns an empty
        list (no evidence appended, no score change) when gnomAD was
        skipped, errored, or the variant was not found there but PM2
        cannot be safely inferred (see below) -- this function is a
        pure, side-effect-free helper so it's independently unit-
        testable without constructing a full InterpretationEngine call
        (see tests/test_gnomad_acmg.py).
        """
        # `error` by presence -- see `acmg_rules.py::_pm2`: a failed
        # lookup must contribute no evidence and no score weight, and
        # an empty error message is still a failure.
        if not gnomad_result or gnomad_result.get("skipped") or gnomad_result.get("error") is not None:
            return []

        cfg = CONFIG.gnomad
        results: List["tuple[str, float]"] = []

        if not gnomad_result.get("found"):
            # Absent from gnomAD is exactly PM2's own criterion
            # ("Absent from controls ... in a population database").
            results.append(
                (
                    "gnomAD: variant not found in the population database (PM2 evidence -- absent from gnomAD).",
                    1.0,
                )
            )
            return results

        global_af = gnomad_result.get("global_af")
        popmax_af = None
        for pop_freq in (gnomad_result.get("population_breakdown") or {}).values():
            af = pop_freq.get("af")
            if af is not None and (popmax_af is None or af > popmax_af):
                popmax_af = af

        ba1_bs1_af = popmax_af if (cfg.USE_POPMAX_FOR_BA1_BS1 and popmax_af is not None) else global_af

        if ba1_bs1_af is not None and ba1_bs1_af >= cfg.BA1_AF_THRESHOLD:
            results.append(
                (
                    f"gnomAD: allele frequency {ba1_bs1_af:.4%} "
                    f"({'population-max' if ba1_bs1_af is popmax_af else 'global'}) exceeds the "
                    f"BA1 stand-alone-benign threshold ({cfg.BA1_AF_THRESHOLD:.2%}).",
                    -3.0,
                )
            )
        elif ba1_bs1_af is not None and ba1_bs1_af >= cfg.BS1_AF_THRESHOLD:
            results.append(
                (
                    f"gnomAD: allele frequency {ba1_bs1_af:.4%} "
                    f"({'population-max' if ba1_bs1_af is popmax_af else 'global'}) exceeds the "
                    f"BS1 strong-benign threshold ({cfg.BS1_AF_THRESHOLD:.2%}) for a rare "
                    f"disease variant.",
                    -2.0,
                )
            )
        elif global_af is not None and global_af <= cfg.PM2_AF_THRESHOLD:
            results.append(
                (
                    f"gnomAD: global allele frequency {global_af:.6%} is at or below the "
                    f"PM2 rarity threshold ({cfg.PM2_AF_THRESHOLD:.4%}).",
                    1.0,
                )
            )
        else:
            af_str = f"{global_af:.6%}" if global_af is not None else "n/a"
            results.append((f"gnomAD: global allele frequency {af_str}; no BA1/BS1/PM2 threshold met.", 0.0))

        return results

    @staticmethod
    def _clingen_acmg_evidence(
        clingen_result: Optional[Dict[str, Any]], is_predicted_lof: bool
    ) -> List["tuple[str, float]"]:
        """
        Translate a ClinGen evidence dict (see
        `pipeline/clingen/models.py::ClinGenGeneEvidence.to_dict()`)
        into zero or more (evidence_text, score_weight) pairs.

        Two independent contributions, using `CONFIG.clingen`'s
        configurable thresholds:

          - PVS1 support: when the translation predicts a
            loss-of-function change (`is_predicted_lof`) *and* the
            gene's haploinsufficiency score is one of
            `DOSAGE_SUFFICIENT_EVIDENCE_SCORES`, that combination is
            exactly what ACMG/AMP's PVS1 rule requires as its
            gene-level prerequisite (a LOF variant in a gene where
            losing one allele is established as sufficient to cause
            disease) -- so it's surfaced as supporting evidence with a
            positive weight. Membership, not a threshold: ClinGen's
            score scale is not ordinal above 3 (see that constant).
            Conversely, a predicted LOF variant in a gene ClinGen has
            curated as "dosage sensitivity unlikely" is a caution
            against a naive PVS1 application, surfaced with a small
            negative weight (never below what the protein-truncating
            evidence already added -- this only tempers it, never
            reverses it to benign on its own). A gene curated as
            "associated with autosomal recessive phenotype" gets a
            third, explicitly neutral (0.0) disclosure: it is not
            haploinsufficiency support, but it is also not the same
            state as "no curation exists", and collapsing those two
            into one silence is what let the previous defect here go
            unnoticed.
          - Gene-disease clinical validity: the strongest curated
            classification for this gene contributes a modest,
            PP5/BP6-style gene-level signal (Definitive/Strong lean
            supporting; Disputed/Refuted lean against a causal role
            for this gene), independent of the specific variant.

        Returns an empty list when ClinGen was skipped, errored, the
        gene wasn't resolved, or no curation exists for the gene --
        this function is a pure, side-effect-free helper so it's
        independently unit-testable without constructing a full
        `InterpretationEngine` call (see tests/test_clingen_acmg.py).
        """
        if not clingen_result or clingen_result.get("skipped") or clingen_result.get("error") is not None:
            return []
        if not clingen_result.get("found"):
            return []

        cfg = CONFIG.clingen
        results: List["tuple[str, float]"] = []
        gene_symbol = clingen_result.get("gene_symbol") or "this gene"

        dosage = clingen_result.get("dosage_sensitivity")
        if is_predicted_lof and dosage:
            # HIGH 3 / T3-F2a (2026-08-28, ruled): verdict sourced from
            # the shared `dosage_sensitivity_verdict` (pvs1/utils.py),
            # passing this engine's own CONFIG.clingen thresholds through
            # so its existing "all thresholds/configuration must remain
            # configurable" guarantee is unaffected -- membership, not
            # `>=`, is still what the shared function checks (ClinGen's
            # Score column is not one ordinal range; 30 and 40 both argue
            # AGAINST haploinsufficiency).
            #
            # PVS1's unconditional behaviour wins on WHICH SCORES produce
            # evidence: this engine used to silently emit NOTHING for
            # scores 0/1/2 ("curated, but not established"), collapsing
            # that state into "no curation exists" -- a reviewer seeing
            # nothing couldn't tell which. The LOF_NOT_ESTABLISHED branch
            # below is new; every other branch's wording/weight is
            # unchanged, since this engine's own scoring is not the
            # shared function's decision to make.
            mechanism, hi_score, hi_label = dosage_sensitivity_verdict(
                clingen_result,
                sufficient_scores=cfg.DOSAGE_SUFFICIENT_EVIDENCE_SCORES,
                autosomal_recessive_score=cfg.DOSAGE_AUTOSOMAL_RECESSIVE_SCORE,
                unlikely_score=cfg.DOSAGE_UNLIKELY_SCORE,
            )
            if mechanism == LOF_ESTABLISHED:
                results.append(
                    (
                        f"ClinGen: {gene_symbol} has sufficient curated evidence for haploinsufficiency "
                        f"({hi_label}), supporting a PVS1-style "
                        f"interpretation of this predicted loss-of-function variant.",
                        1.5,
                    )
                )
            elif mechanism == LOF_REFUTED:
                results.append(
                    (
                        f"ClinGen: {gene_symbol} is curated as '{hi_label}' "
                        f"-- a naive PVS1 application to this predicted loss-of-function variant should be applied with caution.",
                        -0.5,
                    )
                )
            elif mechanism == LOF_ESTABLISHED_RECESSIVE:
                # Deliberately weight 0.0, and deliberately not silent.
                # ClinGen curating a gene as autosomal-recessive says
                # one damaged allele is NOT sufficient, so this is not
                # PVS1 gene-level support -- but it is a real curated
                # finding, and dropping it on the floor would make it
                # indistinguishable from "ClinGen has no dosage
                # curation for this gene". What the correct non-zero
                # weight would be is a clinical judgement, not an
                # engineering one; 0.0 surfaces the finding for a
                # reviewer without asserting a direction this module
                # cannot justify (the convention
                # `_biological_context_evidence` already uses).
                results.append(
                    (
                        f"ClinGen: {gene_symbol} is curated as '{hi_label}' "
                        f"(haploinsufficiency score {hi_score}), so ClinGen's dosage curation does not establish that a "
                        f"single loss-of-function allele is sufficient to cause disease -- this predicted "
                        f"loss-of-function variant gets no PVS1 gene-level support from dosage sensitivity. Loss of "
                        f"function may still be this gene's disease mechanism when both alleles are affected.",
                        0.0,
                    )
                )
            elif mechanism == LOF_NOT_ESTABLISHED:
                # New branch (HIGH 3 / T3-F2a): scores 0/1/2. Weight 0.0
                # for the same reason as the autosomal-recessive branch
                # above -- a real curated finding, not a directional
                # claim this module can justify inventing.
                results.append(
                    (
                        f"ClinGen: {gene_symbol} is curated for haploinsufficiency as '{hi_label}' "
                        f"(score {hi_score}) -- curated evidence exists, but does not establish that a single "
                        f"loss-of-function allele is sufficient to cause disease. This predicted loss-of-function "
                        f"variant gets no PVS1 gene-level support from dosage sensitivity yet, but this is a "
                        f"different state from 'ClinGen has no dosage curation for this gene at all'.",
                        0.0,
                    )
                )

        classification = clingen_result.get("clinical_validity_summary")
        if classification:
            if classification in ("Definitive", "Strong"):
                results.append(
                    (
                        f"ClinGen: {gene_symbol} has a '{classification}' gene-disease clinical validity "
                        f"classification (expert panel: {clingen_result.get('expert_panel', 'n/a')}), "
                        f"supporting a plausible disease mechanism for variants in this gene.",
                        0.5,
                    )
                )
            elif classification in ("Disputed", "Refuted"):
                results.append(
                    (
                        f"ClinGen: {gene_symbol}'s gene-disease relationship is classified as "
                        f"'{classification}', which argues against a causal role for variants in this gene.",
                        -0.5,
                    )
                )
            else:
                results.append(
                    (
                        f"ClinGen: {gene_symbol} has a '{classification}' gene-disease clinical validity classification.",
                        0.0,
                    )
                )

        return results

    @staticmethod
    def _biological_context_evidence(
        uniprot_result: Optional[Dict[str, Any]] = None,
        interpro_result: Optional[Dict[str, Any]] = None,
        alphafold_result: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """
        Purely descriptive (non-scored) context from the biological
        evidence layer: what the gene's protein does, whether it has
        known disease relevance, whether the variant's estimated
        residue falls in a conserved domain, and how structurally
        confident the AlphaFold model is at that residue. Returns an
        empty list (never raises) when a source was skipped, errored,
        or found nothing -- side-effect-free and independently
        unit-testable, matching `_gnomad_acmg_evidence` /
        `_clingen_acmg_evidence`'s shape.
        """
        lines: List[str] = []

        if (
            uniprot_result
            and not uniprot_result.get("skipped")
            and not uniprot_result.get("error")
            and uniprot_result.get("found")
        ):
            protein_name = uniprot_result.get("protein_name")
            if protein_name:
                reviewed_note = "reviewed (Swiss-Prot)" if uniprot_result.get("reviewed") else "unreviewed"
                lines.append(
                    f"UniProt: encodes '{protein_name}' ({reviewed_note} entry {uniprot_result.get('accession', 'n/a')})."
                )
            if uniprot_result.get("disease_comments"):
                lines.append(
                    f"UniProt: this gene's protein has {len(uniprot_result['disease_comments'])} documented "
                    f"disease-association comment(s) in UniProtKB."
                )

        if (
            interpro_result
            and not interpro_result.get("skipped")
            and not interpro_result.get("error")
            and interpro_result.get("found")
        ):
            affected = interpro_result.get("affected_domains")
            # Report review round 4, I8: this residue-specific
            # "overlaps N domain(s)" sentence duplicated
            # `ACMGRuleEngine._pm1`'s own `supporting_evidence` line for
            # the identical fact (same `affected_domains`/
            # `protein_position`), worded slightly differently, so the
            # merged Supporting Evidence list showed it twice --
            # `interpretation_result.py`'s exact-string `_dedupe` can't
            # catch two different sentences about the same fact. PM1's
            # criterion-level text is the more complete, more current
            # source (it also carries the I7 count/truncation fix this
            # one never had), so this purely-descriptive duplicate is
            # dropped rather than kept in sync by hand. The
            # protein-overall-domain-count branch just below states a
            # genuinely different fact (not tied to this variant's
            # residue) and is unaffected.
            if not affected and interpro_result.get("domains"):
                lines.append(
                    f"InterPro/Pfam: {len(interpro_result['domains'])} conserved domain/family region(s) "
                    f"are annotated on this protein overall."
                )

        if (
            alphafold_result
            and not alphafold_result.get("skipped")
            and not alphafold_result.get("error")
            and alphafold_result.get("found")
        ):
            band = alphafold_result.get("affected_residue_band")
            if band:
                position = alphafold_result.get("protein_position")
                lines.append(
                    f"AlphaFold DB: predicted structural confidence (pLDDT) at residue {position} "
                    f"(transcript-verified) is '{band}'."
                )
            elif alphafold_result.get("mean_plddt_band"):
                lines.append(
                    f"AlphaFold DB: overall predicted structure confidence for this protein is "
                    f"'{alphafold_result['mean_plddt_band']}' (mean pLDDT)."
                )

        return lines

    @staticmethod
    def _build_summary(chrom, pos, ref, alt, score: float, has_clinvar: bool) -> "tuple[str, str]":
        variant_label = f"{chrom}:{pos} {ref}>{alt}"

        if not has_clinvar:
            return (
                f"{variant_label} has no existing clinical classification in ClinVar. "
                f"Interpretation is based on computational sequence, protein, and homology "
                f"evidence only and should be treated as preliminary.",
                "low",
            )
        if score >= 3:
            return (
                f"{variant_label} shows evidence consistent with pathogenicity based on "
                f"ClinVar classification and supporting computational evidence.",
                "high",
            )
        if score >= 1:
            return (
                f"{variant_label} shows some evidence suggestive of clinical relevance, "
                f"but classification is not strongly supported across all evidence sources.",
                "moderate",
            )
        if score <= -2:
            return (
                f"{variant_label} is most consistent with a benign or likely benign "
                f"classification based on available evidence.",
                "high",
            )
        return (
            f"{variant_label} has uncertain clinical significance based on currently available evidence.",
            "low",
        )
