"""
ACMG/AMP criterion-by-criterion rule engine (Phase 1 of the interpretation
roadmap).

This module is deliberately separate from `pipeline/interpretation.py`'s
existing scored-evidence aggregator. That module already produces a good
single `significance_score` + free-text evidence list; this module adds a
second, independently-traceable layer on top: for each of the 28 ACMG/AMP
2015 criteria (PVS1, PS1-4, PM1-6, PP1-5, BA1, BS1-4, BP1-7) it reports
whether the criterion was:

  - "triggered"      -- the integrated evidence sources support applying it
  - "not_triggered"  -- evidence sources were checked and do NOT support it
  - "not_evaluated"  -- GEPER has no integrated evidence source for this
                        criterion (e.g. segregation data, case-control
                        counts, functional assay results). We never guess
                        at these; they are reported as gaps, not silently
                        dropped.

Every triggered/not_triggered criterion carries: rationale, supporting
evidence, conflicting evidence, evidence sources, and a qualitative
confidence. Nothing here is invented -- every string is built from a field
already present in an existing provider result dict (ClinVar, gnomAD,
ClinGen, AlphaMissense, MMSplice, protein translation, InterPro, dbSNP).
If the input evidence a rule needs is missing/skipped/errored, that rule
reports "not_evaluated" rather than assuming a direction.

This engine does NOT replace `InterpretationEngine.interpret()`'s existing
`summary` / `confidence` / `significance_score` / `supporting_evidence`
output -- both are returned together (see `InterpretationEngine.interpret`,
which adds this engine's output under the new `acmg_evaluation` key without
touching any pre-existing key). This preserves backward compatibility for
any caller that already consumes the older shape.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import CONFIG
from pipeline.gnomad.models import POPULATION_LABELS
from pipeline.ps1_pm5.decision import PS1PM5Evaluator, PS1PM5Thresholds
from pipeline.ps1_pm5.utils import matches_from_clinvar_codon_result
from pipeline.pvs1.decision_tree import PVS1DecisionTree
from pipeline.pvs1.models import (
    LOF_ESTABLISHED,
    LOF_ESTABLISHED_RECESSIVE,
    LOF_UNKNOWN,
    NULL_WHOLE_GENE_DELETION,
    QUALIFYING_NULL_TYPES,
)
from pipeline.pvs1.utils import (
    PM4_IN_FRAME_INDEL,
    build_pvs1_input,
    classify_pm4_variant,
    coding_consequence_detail,
    lof_mechanism_from_clingen,
    population_af_from_gnomad,
    protein_effect_flags,
    transcript_from_result,
)

# ACMG/AMP 2015 criterion categories and the direction of pathogenicity
# each one argues for. Used only for labelling / combining rules; the
# combination rules below (PVS1+2PM etc.) mirror the standard 2015
# Richards et al. point-based approximation.
_STRENGTH = {
    "PVS1": ("pathogenic", "very_strong"),
    "PS1": ("pathogenic", "strong"),
    "PS2": ("pathogenic", "strong"),
    "PS3": ("pathogenic", "strong"),
    "PS4": ("pathogenic", "strong"),
    "PM1": ("pathogenic", "moderate"),
    "PM2": ("pathogenic", "moderate"),
    "PM3": ("pathogenic", "moderate"),
    "PM4": ("pathogenic", "moderate"),
    "PM5": ("pathogenic", "moderate"),
    "PM6": ("pathogenic", "moderate"),
    "PP1": ("pathogenic", "supporting"),
    "PP2": ("pathogenic", "supporting"),
    "PP3": ("pathogenic", "supporting"),
    "PP4": ("pathogenic", "supporting"),
    "PP5": ("pathogenic", "supporting"),
    "BA1": ("benign", "stand_alone"),
    "BS1": ("benign", "strong"),
    "BS2": ("benign", "strong"),
    "BS3": ("benign", "strong"),
    "BS4": ("benign", "strong"),
    "BP1": ("benign", "supporting"),
    "BP2": ("benign", "supporting"),
    "BP3": ("benign", "supporting"),
    "BP4": ("benign", "supporting"),
    "BP5": ("benign", "supporting"),
    "BP6": ("benign", "supporting"),
    "BP7": ("benign", "supporting"),
}

_POINTS = {"stand_alone": 8, "very_strong": 8, "strong": 4, "moderate": 2, "supporting": 1}


@dataclass
class CriterionResult:
    code: str
    direction: str
    strength: str
    status: str  # "triggered" | "not_triggered" | "not_evaluated"
    rationale: str
    supporting_evidence: List[str] = field(default_factory=list)
    conflicting_evidence: List[str] = field(default_factory=list)
    evidence_sources: List[str] = field(default_factory=list)
    confidence: Optional[str] = None  # "High" | "Moderate" | "Low"
    # Optional per-criterion detail block for rules whose reasoning is a
    # multi-step decision tree rather than a single threshold test (so
    # far: PVS1, which carries its full ClinGen SVI decision path and
    # the list of caveats it could and could not check). Omitted from
    # `to_dict()` when absent, so every existing consumer of this shape
    # sees exactly the keys it saw before.
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        # Explicit Dict[str, Any], not inferred from the literal below:
        # a bare-literal inference would narrow the value type to the
        # union of this dict's own field types (str | list[str] | ...),
        # which then rejects `self.details` (a genuinely different
        # shape, Dict[str, Any]) being added below.
        payload: Dict[str, Any] = {
            "code": self.code,
            "direction": self.direction,
            "strength": self.strength,
            "status": self.status,
            "rationale": self.rationale,
            "supporting_evidence": self.supporting_evidence,
            "conflicting_evidence": self.conflicting_evidence,
            "evidence_sources": self.evidence_sources,
            "confidence": self.confidence,
        }
        if self.details is not None:
            payload["details"] = self.details
        return payload


def _functional_evidence_not_evaluated_reason(functional_evidence_result: Optional[Dict[str, Any]]) -> str:
    """
    Honest PS3/BS3 "not evaluated" rationale -- distinguishes a source
    that was actually queried and had nothing for this exact variant
    from one that was skipped this run because `utils/service_health
    .py::HEALTH` had already confirmed it offline (see
    `pipeline/functional_evidence/lookup.py`'s `unavailable_sources`).
    Reporting "both sources were checked" when one was never queried
    would misrepresent a data-collection gap as a completed,
    negative-for-nothing search -- a clinician reading this text needs
    to know which one actually happened.
    """
    unavailable = list((functional_evidence_result or {}).get("unavailable_sources") or [])
    all_sources = ["ClinGen Evidence Repository", "MaveDB"]
    unavailable_full = {"ClinGen ERepo": "ClinGen Evidence Repository", "MaveDB": "MaveDB"}
    unavailable_names = [unavailable_full.get(name, name) for name in unavailable]
    checked_names = [name for name in all_sources if name not in unavailable_names]

    if not unavailable_names:
        return (
            "requires published functional/experimental assay results for this exact variant; the "
            "ClinGen Evidence Repository and MaveDB were both checked (see pipeline/functional_evidence/) "
            "but neither had a curated/calibrated result for it."
        )
    if not checked_names:
        return (
            "requires published functional/experimental assay results for this exact variant; neither "
            "source could be queried this run -- " + " and ".join(unavailable_names) + " were confirmed "
            "unreachable during this analysis run (see the report's data-availability caveat). This is a "
            "data-collection gap for this run, not a confirmed absence of functional evidence."
        )
    return (
        "requires published functional/experimental assay results for this exact variant; "
        + " and ".join(unavailable_names)
        + " could not be queried this run (confirmed unreachable during this analysis run -- see the "
        "report's data-availability caveat), so only "
        + " and ".join(checked_names)
        + " was checked, and it had no curated/calibrated result for this variant. This is not the same "
        "as both sources having been checked."
    )


def _at_codon(codon_number: Optional[int]) -> str:
    """
    " at codon N", or "" when `codon_number` is None -- e.g. an indel
    whose breakpoints sit on an exon boundary, so no single genomic
    base of the variant maps cleanly into the CDS (see
    `TranscriptContext.first_affected_codon`'s docstring). Rationale
    templates should splice this in rather than interpolating
    `codon_number` directly, so an unavailable codon degrades to
    omitting the clause instead of leaking a literal "None" into
    clinician-facing text.
    """
    return f" at codon {codon_number}" if codon_number is not None else ""


def _not_evaluated(code: str, reason: str) -> CriterionResult:
    direction, strength = _STRENGTH[code]
    return CriterionResult(
        code=code,
        direction=direction,
        strength=strength,
        status="not_evaluated",
        rationale=f"Not evaluated: {reason}",
    )


class ACMGRuleEngine:
    """
    Evaluates every ACMG/AMP 2015 criterion independently from the same
    provider-result dicts the orchestrator already collects, and combines
    the triggered ones into a classification via the standard Richards et
    al. point-based combining rules.
    """

    def evaluate(
        self,
        *,
        clinvar_result: Optional[Dict[str, Any]] = None,
        dbsnp_result: Optional[Dict[str, Any]] = None,
        protein_result: Optional[Dict[str, Any]] = None,
        alphamissense_result: Optional[Dict[str, Any]] = None,
        mmsplice_result: Optional[Dict[str, Any]] = None,
        gnomad_result: Optional[Dict[str, Any]] = None,
        conservation_result: Optional[Dict[str, Any]] = None,
        clingen_result: Optional[Dict[str, Any]] = None,
        interpro_result: Optional[Dict[str, Any]] = None,
        ensemble_result: Optional[Dict[str, Any]] = None,
        variant_dict: Optional[Dict[str, Any]] = None,
        transcript_result: Optional[Dict[str, Any]] = None,
        clinvar_codon_result: Optional[Dict[str, Any]] = None,
        uniprot_result: Optional[Dict[str, Any]] = None,
        spliceformer_result: Optional[Dict[str, Any]] = None,
        splicebert_result: Optional[Dict[str, Any]] = None,
        hpo_result: Optional[Dict[str, Any]] = None,
        phenotype_result: Optional[Dict[str, Any]] = None,
        functional_evidence_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        criteria: Dict[str, CriterionResult] = {}

        is_synonymous, is_missense = self._protein_effect_flags(variant_dict, transcript_result)

        criteria["PVS1"] = self._pvs1(
            variant_dict=variant_dict,
            protein_result=protein_result,
            clingen_result=clingen_result,
            gnomad_result=gnomad_result,
            interpro_result=interpro_result,
            transcript_result=transcript_result,
            mmsplice_result=mmsplice_result,
        )
        criteria["PS1"] = self._ps1(
            variant_dict=variant_dict,
            transcript_result=transcript_result,
            clinvar_codon_result=clinvar_codon_result,
        )
        criteria["PM5"] = self._pm5(
            variant_dict=variant_dict,
            transcript_result=transcript_result,
            clinvar_codon_result=clinvar_codon_result,
        )
        criteria["PM1"] = self._pm1(interpro_result)
        criteria["PM2"] = self._pm2(gnomad_result)
        criteria["PM4"] = self._pm4(
            variant_dict=variant_dict,
            transcript_result=transcript_result,
            uniprot_result=uniprot_result,
        )
        criteria["PS4"] = self._ps4(gnomad_result)
        criteria["PP3"], criteria["BP4"] = self._pp3_bp4(
            alphamissense_result, mmsplice_result, ensemble_result, conservation_result
        )
        criteria["BA1"], criteria["BS1"] = self._ba1_bs1(gnomad_result)
        criteria["BP7"] = self._bp7(is_synonymous, mmsplice_result, spliceformer_result, splicebert_result)
        criteria["PP1"] = self._pp1(clingen_result)
        criteria["BS4"] = self._bs4(clingen_result)
        criteria["BP1"] = self._bp1(is_missense, clingen_result)
        criteria["BP3"] = self._bp3(
            variant_dict=variant_dict,
            transcript_result=transcript_result,
            uniprot_result=uniprot_result,
        )
        criteria["BP6"] = self._bp6(clinvar_result)
        criteria["PP4"] = self._pp4(phenotype_result, hpo_result)
        criteria["PS3"] = self._ps3(functional_evidence_result)
        criteria["BS3"] = self._bs3(functional_evidence_result)

        # Criteria GEPER has no integrated evidence source for. Listed
        # explicitly (rather than silently omitted) so every one of the 28
        # ACMG/AMP criteria is accounted for in the output, per the
        # "never fabricate evidence" requirement -- each of these would
        # require a data source this pipeline does not yet integrate.
        criteria["PS2"] = _not_evaluated(
            "PS2", "requires confirmed de novo trio (parental) sequencing data; not integrated."
        )
        criteria["PM3"] = _not_evaluated("PM3", "requires trans-phase data for a recessive disorder; not integrated.")
        criteria["PM6"] = _not_evaluated(
            "PM6", "requires confirmed (non-parentally-tested) de novo status; not integrated."
        )
        criteria["PP2"] = _not_evaluated(
            "PP2", "requires a gene-level missense-constraint metric (e.g. gnomAD missense Z-score); not integrated."
        )
        criteria["PP5"] = _not_evaluated("PP5", "deprecated in the 2015 ACMG/AMP guideline update; not applied.")
        criteria["BS2"] = _not_evaluated(
            "BS2", "requires observation in unaffected individuals at the expected penetrance age; not integrated."
        )
        criteria["BP2"] = _not_evaluated("BP2", "requires trans/cis phase data; not integrated.")
        criteria["BP5"] = _not_evaluated(
            "BP5", "requires case-level data on an alternate molecular cause; not integrated."
        )

        # Attach ClinVar as descriptive cross-reference (never used to
        # directly trigger a rule here -- ClinVar's own classification is
        # already surfaced via `InterpretationEngine`'s existing summary,
        # and is intentionally kept out of the ACMG combining rules below
        # to avoid circularity between "what ClinVar says" and "what our
        # own ACMG engine concludes from primary evidence").
        clinvar_note = self._clinvar_crossref(clinvar_result)

        classification, combining_trace = self._combine(criteria)

        triggered = [c.to_dict() for c in criteria.values() if c.status == "triggered"]
        not_triggered = [c.to_dict() for c in criteria.values() if c.status == "not_triggered"]
        not_evaluated = [c.to_dict() for c in criteria.values() if c.status == "not_evaluated"]

        return {
            "classification": classification,
            "combining_rule_trace": combining_trace,
            "clinvar_crossreference": clinvar_note,
            "triggered_criteria": triggered,
            "not_triggered_criteria": not_triggered,
            "not_evaluated_criteria": not_evaluated,
            "all_criteria": {code: c.to_dict() for code, c in criteria.items()},
        }

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _protein_effect_flags(
        variant_dict: Optional[Dict[str, Any]], transcript_result: Optional[Dict[str, Any]]
    ) -> "tuple[Optional[bool], Optional[bool]]":
        """
        (is_synonymous, is_missense) for BP7/BP1, called from the
        transcript's real CDS reading frame -- never from
        `pipeline/protein_translator.py`'s frame-unaware local
        translation window. That window translates from the first AUG
        it happens to find in a short flanking sequence, which for a
        coding variant more than a few dozen bases from the transcript's
        true start codon is an essentially arbitrary frame: it produced
        matching ref/alt "protein" text for real missense variants
        (BP7 wrongly triggering, contributing benign points) purely by
        coincidence of which wrong codon the substitution happened to
        land in. See `pipeline/pvs1/utils.py::protein_effect_flags` and
        `coding_consequence_detail` for the transcript-CDS-frame
        replacement and its own docstrings for the full history.

        Returns `(None, None)` -- never a guessed boolean -- when the
        consequence genuinely could not be determined from transcript
        data (no transcript structure, no CDS sequence fetched, a
        build/transcript coordinate mismatch, or a multi-nucleotide
        same-length substitution). Callers must report "not_evaluated"
        for that case, not a triggered/not_triggered guess.
        """
        transcript = transcript_from_result(transcript_result)
        flags = protein_effect_flags(variant_dict, transcript)
        if not flags.determined:
            return None, None
        return flags.is_synonymous, flags.is_missense

    # ------------------------------------------------------------------
    # Individually-evaluated criteria
    # ------------------------------------------------------------------

    @staticmethod
    def _pvs1(
        variant_dict: Optional[Dict[str, Any]] = None,
        protein_result: Optional[Dict[str, Any]] = None,
        clingen_result: Optional[Dict[str, Any]] = None,
        gnomad_result: Optional[Dict[str, Any]] = None,
        interpro_result: Optional[Dict[str, Any]] = None,
        transcript_result: Optional[Dict[str, Any]] = None,
        mmsplice_result: Optional[Dict[str, Any]] = None,
    ) -> CriterionResult:
        """
        PVS1, evaluated through the ClinGen SVI decision tree rather than
        as a single boolean.

        Unlike every other rule in this module, PVS1 is not a threshold
        test: ACMG/AMP 2015 attaches four caveats to it (LOF must be a
        known mechanism for the gene; 3'-most truncations are suspect;
        exon-skipping splice variants may leave the protein intact;
        multiple transcripts complicate all of the above), and the
        ClinGen SVI Working Group turned those caveats into a decision
        tree with graded outcomes. The tree lives in
        `pipeline/pvs1/decision_tree.py`; this method is the adapter
        between GEPER's provider-result dicts and that tree, and between
        the tree's output and this module's `CriterionResult`.

        Two consequences for callers:
          - A triggered PVS1 can carry `strength` of very_strong,
            strong, moderate or supporting. `_combine` reads
            `CriterionResult.strength`, so a downgraded PVS1
            automatically contributes the right number of points.
          - "not_evaluated" is used (rather than "not_triggered") when
            the gene's LOF mechanism is uncurated or the transcript
            structure was unavailable -- those are gaps in the input,
            not a curated negative, and the report distinguishes them.
        """
        direction, nominal_strength = _STRENGTH["PVS1"]

        pvs1_input = build_pvs1_input(
            variant_dict=variant_dict,
            protein_result=protein_result,
            clingen_result=clingen_result,
            gnomad_result=gnomad_result,
            interpro_result=interpro_result,
            transcript_result=transcript_result,
            mmsplice_result=mmsplice_result,
            config=CONFIG.pvs1,
        )
        evaluation = PVS1DecisionTree().evaluate(pvs1_input)

        if evaluation.applies:
            status, strength = "triggered", evaluation.strength
        else:
            strength = nominal_strength
            not_a_null = evaluation.null_variant_type not in QUALIFYING_NULL_TYPES
            missing_inputs = evaluation.lof_mechanism == LOF_UNKNOWN or (
                evaluation.transcript_id is None
                and not not_a_null
                and evaluation.null_variant_type != NULL_WHOLE_GENE_DELETION
            )
            status = "not_evaluated" if (missing_inputs and not not_a_null) else "not_triggered"

        return CriterionResult(
            "PVS1",
            direction,
            strength,
            status,
            evaluation.rationale,
            supporting_evidence=list(evaluation.supporting_evidence),
            conflicting_evidence=list(evaluation.conflicting_evidence),
            evidence_sources=list(dict.fromkeys(evaluation.evidence_sources)),
            confidence=evaluation.confidence,
            details=evaluation.to_dict(),
        )

    @staticmethod
    def _ps1_pm5_query_context(
        variant_dict: Optional[Dict[str, Any]], transcript_result: Optional[Dict[str, Any]]
    ) -> "tuple":
        """
        Shared setup for `_ps1`/`_pm5`: the transcript, the query
        variant's own genomic coordinates, and its amino-acid
        consequence computed in the transcript's real reading frame
        (never GEPER's frame-unaware protein-translation window -- see
        `pipeline/pvs1/utils.py::coding_consequence_detail`'s
        docstring for why that window is unusable for this, the same
        reason it was unusable for PVS1).
        """
        transcript = transcript_from_result(transcript_result)
        pos = variant_dict.get("pos") if variant_dict else None
        ref = (variant_dict.get("ref") or "") if variant_dict else ""
        alt = (variant_dict.get("alt") or "") if variant_dict else ""
        query = (
            coding_consequence_detail(transcript, int(pos), ref, alt)
            if (transcript is not None and pos is not None)
            else None
        )
        return transcript, (int(pos) if pos is not None else None), ref, alt, query

    @staticmethod
    def _ps1_pm5_result(evaluation, direction: str, nominal_strength: str) -> CriterionResult:
        """
        Adapt a `pipeline/ps1_pm5/models.py::PS1PM5Evaluation` into this
        module's `CriterionResult`, matching `_pvs1`'s status-mapping
        convention: "not_evaluated" for a genuine input gap (the
        variant's consequence couldn't be computed, or ClinVar had no
        record at all at this codon to check against); "not_triggered"
        for anything ACMG-AMP itself concludes doesn't apply (wrong
        variant class, no anchor met the confidence bar, or the
        splice-proximity caveat withheld it).
        """
        if evaluation.applies:
            status = "triggered"
        else:
            no_consequence = (
                "could not be determined" in evaluation.rationale or "was not evaluated" in evaluation.rationale
            )
            status = "not_evaluated" if no_consequence else "not_triggered"
        return CriterionResult(
            evaluation.code,
            direction,
            nominal_strength,
            status,
            evaluation.rationale,
            supporting_evidence=list(evaluation.supporting_evidence),
            conflicting_evidence=list(evaluation.conflicting_evidence),
            evidence_sources=list(dict.fromkeys(evaluation.evidence_sources)),
            confidence=evaluation.confidence,
            details=evaluation.to_dict(),
        )

    @staticmethod
    def _ps1(
        variant_dict: Optional[Dict[str, Any]] = None,
        transcript_result: Optional[Dict[str, Any]] = None,
        clinvar_codon_result: Optional[Dict[str, Any]] = None,
    ) -> CriterionResult:
        """
        PS1 (Strong): this variant produces the identical amino acid
        change as an already-established pathogenic ClinVar variant at
        the same codon, via a different nucleotide change.

        See `pipeline/ps1_pm5/decision.py` for the full comparison
        (confidence-tier filtering, self-match exclusion, the
        splice-proximity caveat) -- this method is only the adapter
        between GEPER's provider-result dicts and that logic, matching
        `_pvs1`'s role for the PVS1 decision tree.
        """
        direction, nominal_strength = _STRENGTH["PS1"]
        transcript, pos, ref, alt, query = ACMGRuleEngine._ps1_pm5_query_context(variant_dict or {}, transcript_result)
        matches = matches_from_clinvar_codon_result(clinvar_codon_result)
        evaluation = PS1PM5Evaluator(
            thresholds=PS1PM5Thresholds(
                min_star_rating=CONFIG.ps1_pm5.MIN_STAR_RATING,
                splice_proximity_exon_bp=CONFIG.ps1_pm5.SPLICE_PROXIMITY_EXON_BP,
            )
        ).evaluate_ps1(query, matches, transcript, pos, ref, alt)
        return ACMGRuleEngine._ps1_pm5_result(evaluation, direction, nominal_strength)

    @staticmethod
    def _pm5(
        variant_dict: Optional[Dict[str, Any]] = None,
        transcript_result: Optional[Dict[str, Any]] = None,
        clinvar_codon_result: Optional[Dict[str, Any]] = None,
    ) -> CriterionResult:
        """
        PM5 (Moderate): this variant produces a novel amino acid change
        at a codon where a *different* amino acid change is
        already-established pathogenic in ClinVar.

        Explicitly excludes same-amino-acid matches (that is PS1's
        evidence, not PM5's) and carries an always-on caveat when it
        triggers: GEPER does not model whether this variant's specific
        substitution is physicochemically comparable to the anchor's
        (conservative vs. non-conservative) -- see
        `pipeline/ps1_pm5/decision.py::evaluate_pm5`.
        """
        direction, nominal_strength = _STRENGTH["PM5"]
        transcript, pos, ref, alt, query = ACMGRuleEngine._ps1_pm5_query_context(variant_dict or {}, transcript_result)
        matches = matches_from_clinvar_codon_result(clinvar_codon_result)
        evaluation = PS1PM5Evaluator(
            thresholds=PS1PM5Thresholds(
                min_star_rating=CONFIG.ps1_pm5.MIN_STAR_RATING,
                splice_proximity_exon_bp=CONFIG.ps1_pm5.SPLICE_PROXIMITY_EXON_BP,
            )
        ).evaluate_pm5(query, matches, transcript, pos, ref, alt)
        return ACMGRuleEngine._ps1_pm5_result(evaluation, direction, nominal_strength)

    @staticmethod
    def _pm1(interpro_result: Optional[Dict[str, Any]]) -> CriterionResult:
        """
        PM1 (ACMG/AMP 2015): variant is located in a mutational hot
        spot and/or critical, well-established functional domain
        without benign variation.

        The domain-overlap check below needs an actual protein residue
        number to compare against InterPro/Pfam's own (UniProt
        canonical-isoform) domain boundaries. That number comes from
        `orchestrator._canonical_protein_position` -- a transcript-
        verified mapping (via `TranscriptContext.codon_at`, the same
        exon-aware, strand-aware coordinate arithmetic PM4/PS1/PM5
        already rely on), computed only against this gene's MANE
        Select / Ensembl-canonical transcript so the numbering is
        guaranteed to line up with InterPro's own coordinates.

        `protein_position is None` (checked explicitly, separately
        from `affected_domains` being empty) covers every case that
        number could not be determined: no transcript structure was
        fetched, the resolved transcript isn't the canonical one, or
        the variant falls outside the CDS (intronic/UTR/no residue
        here). That is reported `not_evaluated` -- a gap, not a
        negative. Reporting `not_triggered` instead would be a
        confident wrong claim: "checked, no domain" is not the same
        statement as "couldn't check", and this pipeline used to
        conflate the two (see `InterProLookup.query_variant`'s
        docstring for the history -- the old position source was a
        local, unspliced, non-strand-aware translation-window guess
        that was wrong for almost every real variant).
        """
        direction, strength = _STRENGTH["PM1"]
        if (
            not interpro_result
            or interpro_result.get("skipped")
            or interpro_result.get("error")
            or not interpro_result.get("found")
        ):
            return _not_evaluated("PM1", "InterPro domain annotation was unavailable for this gene/residue.")

        protein_position = interpro_result.get("protein_position")
        if protein_position is None:
            return _not_evaluated(
                "PM1",
                "InterPro domain annotation was available for this gene, but this variant's protein "
                "residue could not be determined from the transcript structure (no transcript structure "
                "was fetched, the resolved transcript is not this gene's MANE Select/canonical one, or "
                "the position falls outside the coding sequence) -- domain overlap cannot be checked "
                "without a residue number, so this is a gap, not a negative finding.",
            )

        affected = interpro_result.get("affected_domains") or []
        if affected:
            names = ", ".join(d.get("name") or d.get("member_accession") or "unnamed domain" for d in affected[:3])
            return CriterionResult(
                "PM1",
                direction,
                strength,
                "triggered",
                f"Residue {protein_position} (transcript-verified against this gene's MANE Select/"
                f"canonical transcript) falls within an annotated functional domain/family region "
                f"({names}), a location InterPro/Pfam curation flags as structurally/functionally "
                f"significant.",
                supporting_evidence=[
                    f"InterPro/Pfam: residue {protein_position} overlaps {len(affected)} domain/family region(s): {names}."
                ],
                conflicting_evidence=[
                    "Checked only against this gene's MANE Select/Ensembl-canonical transcript; a different disease-relevant transcript could number this residue differently."
                ],
                evidence_sources=["InterPro"],
                confidence="Moderate",
            )
        return CriterionResult(
            "PM1",
            direction,
            strength,
            "not_triggered",
            f"Residue {protein_position} (transcript-verified against this gene's MANE Select/canonical "
            f"transcript) does not overlap any annotated InterPro/Pfam domain region.",
            evidence_sources=["InterPro"],
            confidence="Moderate",
        )

    @staticmethod
    def _population_priority_context(gnomad_result: Dict[str, Any], cfg: Any) -> Optional[Dict[str, Any]]:
        """
        Resolves `CONFIG.gnomad.POPULATION_PRIORITY` (e.g. "sas") against
        this variant's already-computed `gnomad_result["population_breakdown"]`
        -- returns `None` when no priority population is configured at all
        (the ordinary case; every rule falls through to its pre-existing
        global/popmax behavior unchanged), or `{"code", "label", "af"}`
        when one is: `af` is the priority population's own gnomAD AF, or
        `None` when that population has no data for this variant (a real,
        honest gap -- gnomAD's own population-level breakdown genuinely
        doesn't cover every population for every variant/release). Callers
        (`_pm2`, `_ba1_bs1`) are responsible for disclosing that gap in
        their rationale rather than silently substituting global/popmax AF
        for it -- this helper only resolves the data, it doesn't decide
        the rule outcome.
        """
        priority = (getattr(cfg, "POPULATION_PRIORITY", "") or "").strip().lower()
        if not priority:
            return None
        breakdown = gnomad_result.get("population_breakdown") or {}
        entry = breakdown.get(priority) or {}
        return {
            "code": priority,
            "label": POPULATION_LABELS.get(priority, priority.upper()),
            "af": entry.get("af"),
        }

    @staticmethod
    def _pm2(gnomad_result: Optional[Dict[str, Any]]) -> CriterionResult:
        direction, strength = _STRENGTH["PM2"]
        if not gnomad_result or gnomad_result.get("skipped") or gnomad_result.get("error"):
            return _not_evaluated("PM2", "gnomAD lookup was skipped or errored for this variant.")
        cfg = CONFIG.gnomad
        if not gnomad_result.get("found"):
            return CriterionResult(
                "PM2",
                direction,
                strength,
                "triggered",
                "Variant is absent from gnomAD, consistent with PM2's 'absent from controls in a population database'.",
                supporting_evidence=["gnomAD: variant not found."],
                evidence_sources=["gnomAD"],
                confidence="Moderate",
            )

        global_af = gnomad_result.get("global_af")
        priority = ACMGRuleEngine._population_priority_context(gnomad_result, cfg)
        fallback_note = ""

        if priority is not None:
            if priority["af"] is not None:
                af = priority["af"]
                af_desc = f"gnomAD {priority['label']} population allele frequency (AF_{priority['code']}={af:.2e})"
                if af <= cfg.PM2_AF_THRESHOLD:
                    return CriterionResult(
                        "PM2",
                        direction,
                        strength,
                        "triggered",
                        f"{af_desc} is at or below the PM2 rarity threshold ({cfg.PM2_AF_THRESHOLD:.2e}).",
                        supporting_evidence=[f"{af_desc}."],
                        evidence_sources=["gnomAD"],
                        confidence="Moderate",
                    )
                global_note = (
                    f" (global AF = {global_af:.2e})" if global_af is not None else " (global AF not available)"
                )
                return CriterionResult(
                    "PM2",
                    direction,
                    strength,
                    "not_triggered",
                    f"{af_desc} exceeds the PM2 rarity threshold; variant is not rare enough for PM2 in the "
                    f"prioritized population{global_note}.",
                    evidence_sources=["gnomAD"],
                    confidence="Moderate",
                )
            # Priority population configured but genuinely unavailable for
            # this variant -- never silently substitute global AF for it;
            # fall through to the global-only logic below with an explicit
            # disclosure appended to whichever rationale it produces.
            fallback_note = (
                f" (population-priority '{priority['code']}' allele frequency was not available for this "
                f"variant; falling back to gnomAD global allele frequency)"
            )

        if global_af is not None and global_af <= cfg.PM2_AF_THRESHOLD:
            return CriterionResult(
                "PM2",
                direction,
                strength,
                "triggered",
                f"gnomAD global allele frequency ({global_af:.2e}) is at or below the PM2 rarity "
                f"threshold ({cfg.PM2_AF_THRESHOLD:.2e}).{fallback_note}",
                supporting_evidence=[f"gnomAD global AF = {global_af:.2e}."],
                evidence_sources=["gnomAD"],
                confidence="Moderate",
            )
        return CriterionResult(
            "PM2",
            direction,
            strength,
            "not_triggered",
            f"gnomAD global allele frequency ({global_af if global_af is not None else 'n/a'}) exceeds "
            f"the PM2 rarity threshold; variant is not rare enough for PM2.{fallback_note}",
            evidence_sources=["gnomAD"],
            confidence="Moderate",
        )

    @staticmethod
    def _ps4(gnomad_result: Optional[Dict[str, Any]]) -> CriterionResult:
        """
        PS4 (Strong): "the prevalence of the variant in affected
        individuals is significantly increased compared to its
        prevalence in unaffected/control populations" -- an odds-ratio
        style comparison between a *case* frequency (this variant's
        rate among individuals with the phenotype) and a *control*
        frequency (its rate in the general/unaffected population).

        GEPER integrates only the control side: gnomAD. There is no
        integrated case-frequency source -- no disease-specific
        cohort/registry, no published case-series counts, no GWAS-style
        case dataset -- so the actual comparison PS4 requires can never
        be computed here, and this method can never return
        "triggered". That is a genuine, structural gap in this
        pipeline's data integration, not a per-variant judgement call,
        so it is stated plainly rather than approximated: rarity in
        gnomAD is a *necessary* precondition for PS4 (a variant common
        in the general population cannot show case-control enrichment)
        but is not evidence of enrichment by itself, and is never used
        here to substitute for the missing case-frequency comparison.
        The gnomAD context is still surfaced (per the task's explicit
        request to "implement what's possible... as a supporting
        signal") purely as informational context for a human reviewer,
        clearly labeled as such -- it is not scored, and it can never
        move `status` to "triggered".

        Always returns "not_evaluated": PS4's own defining requirement
        (case-frequency data) is unintegrated regardless of what
        gnomAD shows, so there is no basis on which this could ever be
        a curated negative ("not_triggered") either -- "not enough
        evidence to check" and "checked and it doesn't apply" are
        different claims, and only the former is true here.
        """
        direction, strength = _STRENGTH["PS4"]
        base_reason = (
            "PS4 requires comparing this variant's prevalence in affected (case) individuals against its "
            "prevalence in unaffected/control populations. GEPER integrates only the control side "
            "(gnomAD); no case-frequency source (e.g. a disease-specific cohort/registry or published "
            "case-control study) is integrated, so the case-control comparison PS4 requires cannot be "
            "computed, and this criterion can never be triggered by this pipeline as currently built."
        )
        if not gnomad_result or gnomad_result.get("skipped") or gnomad_result.get("error"):
            return _not_evaluated(
                "PS4", base_reason + " gnomAD control-population data was also unavailable for this variant."
            )

        af, af_label = population_af_from_gnomad(gnomad_result)
        if af is None:
            return _not_evaluated(
                "PS4", base_reason + " No usable gnomAD allele frequency was returned for this variant either."
            )

        cfg = CONFIG.gnomad
        if af >= cfg.BA1_AF_THRESHOLD:
            context = (
                f"For context only (not part of the PS4 comparison): gnomAD allele frequency is {af_label}, "
                f"at or above the BA1 stand-alone-benign threshold -- common enough in the general "
                "population that a case-control enrichment argument is very unlikely to hold even if case "
                "data existed."
            )
        elif af <= cfg.PM2_AF_THRESHOLD:
            context = (
                f"For context only (not part of the PS4 comparison): gnomAD allele frequency is {af_label}, "
                "at or below the PM2 rarity threshold -- rare enough in the general population that PS4's "
                "case-control precondition would not be excluded on frequency grounds alone, but rarity is "
                "not itself evidence of case enrichment."
            )
        else:
            context = f"For context only (not part of the PS4 comparison): gnomAD allele frequency is {af_label}."

        return CriterionResult(
            "PS4",
            direction,
            strength,
            "not_evaluated",
            base_reason + " " + context,
            evidence_sources=["gnomAD"],
            confidence="Low",
            details={
                "gnomad_allele_frequency": af,
                "gnomad_allele_frequency_label": af_label,
                "case_frequency_source": None,
            },
        )

    # UniProt's own controlled-vocabulary feature-type strings (verified
    # live against the REST API, e.g. Titin/Q8WZ42's "Z-repeat"/"PEVK"
    # entries, both typed "Repeat") that mark a region as tandemly
    # repetitive or low-complexity, where an in-frame length change is
    # plausibly tolerated population variation rather than a functional
    # disruption -- exactly the class of region ACMG/AMP's PM4 caveat
    # names. Matched case-insensitively. Deliberately excludes
    # "Domain"/"Region": those mark a structurally/functionally
    # significant span, the opposite of PM4's caveat, even for a
    # multi-copy domain (e.g. an Ig-like repeat) -- see
    # `pipeline/pvs1/utils.py::_LOCALISING_INTERPRO_TYPES`'s docstring
    # for the same "don't let 'occurs more than once' stand in for
    # 'no known function'" distinction on the InterPro side.
    _REPEAT_FEATURE_TYPES = ("repeat", "compositional bias")

    @staticmethod
    def _pm4_repeat_region_hit(
        uniprot_result: Optional[Dict[str, Any]], protein_position: Optional[int]
    ) -> "tuple[Optional[bool], Optional[Dict[str, Any]]]":
        """
        Whether `protein_position` falls inside a UniProt-annotated
        repeat/compositional-bias region for this gene's canonical
        protein.

        Returns `(checked, feature)`: `checked=None` when the check
        could not be performed at all (no UniProt data, or no protein
        position to check) -- a gap, not a negative finding;
        `checked=False, feature=None` when UniProt data was available
        and no repeat region overlapped; `checked=True, feature={...}`
        when one did, with the matching feature's own fields to cite.

        `pipeline/interpro/lookup.py` pre-filters its domains by
        protein position at the lookup stage (`affected_domains`); the
        UniProt stage does not do the equivalent for its `features`
        list, so this filters the raw list directly rather than
        assuming a pre-filtered key exists.
        """
        if (
            not uniprot_result
            or uniprot_result.get("skipped")
            or uniprot_result.get("error")
            or not uniprot_result.get("found")
        ):
            return None, None
        if protein_position is None:
            return None, None
        for feature in uniprot_result.get("features") or []:
            feature_type = (feature.get("feature_type") or "").strip().lower()
            if feature_type not in ACMGRuleEngine._REPEAT_FEATURE_TYPES:
                continue
            begin, end = feature.get("begin"), feature.get("end")
            if begin is not None and end is not None and begin <= protein_position <= end:
                return True, feature
        return False, None

    @staticmethod
    def _pm4(
        variant_dict: Optional[Dict[str, Any]] = None,
        transcript_result: Optional[Dict[str, Any]] = None,
        uniprot_result: Optional[Dict[str, Any]] = None,
    ) -> CriterionResult:
        """
        PM4: an in-frame insertion/deletion (length change a multiple
        of 3, no frameshift) or a stop-loss substitution, in a region
        not already known to tolerate such changes as benign population
        variation.

        Variant classification and the affected codon both come from
        `pipeline/pvs1/utils.py::classify_pm4_variant`, which uses the
        same CDS-frame arithmetic PVS1/PS1/PM5 already rely on rather
        than GEPER's frame-unaware local protein-translation window
        (see that function's docstring for the known gap it leaves:
        an indel that deletes the reference stop codon outright, as
        opposed to a substitution converting it, is not currently
        recognized as stop-loss).

        The repeat-region caveat is a hard block, not a downgrade:
        ACMG/AMP's own wording is "in a non-repeat region", not "in a
        non-repeat region, with reduced strength otherwise" -- a length
        change inside an annotated tandem-repeat/compositional-bias
        region is reported "not_triggered", never "triggered" at a
        lesser confidence.
        """
        direction, strength = _STRENGTH["PM4"]
        transcript = transcript_from_result(transcript_result)
        detail, notes = classify_pm4_variant(variant_dict or {}, transcript)

        if detail is None:
            if transcript is None:
                return _not_evaluated(
                    "PM4", "transcript structure was unavailable to classify this variant's coding consequence."
                )
            return CriterionResult(
                "PM4",
                direction,
                strength,
                "not_triggered",
                "Predicted consequence is neither an in-frame insertion/deletion nor a stop-loss "
                "substitution." + (f" ({notes[-1]})" if notes else ""),
                evidence_sources=["transcript_structure"],
            )

        unchecked = [
            "Indel-deletes-the-stop-codon-outright and stop-loss-extension-length are not modeled here "
            "(would require 3' UTR sequence this pipeline does not fetch) -- see classify_pm4_variant's "
            "docstring.",
        ]
        checked, repeat_feature = ACMGRuleEngine._pm4_repeat_region_hit(uniprot_result, detail.codon_number)

        if checked is True:
            # _pm4_repeat_region_hit only ever returns (True, feature)
            # together (see its own docstring/return statements) --
            # this makes that pairing an explicit, checked invariant
            # rather than one mypy can't see across the two return
            # values.
            assert repeat_feature is not None
            label = repeat_feature.get("description") or repeat_feature.get("feature_type")
            return CriterionResult(
                "PM4",
                direction,
                strength,
                "not_triggered",
                f"Predicted consequence is a qualifying {detail.category.replace('_', ' ')}"
                f"{_at_codon(detail.codon_number)}, but that position falls within a UniProt-annotated "
                f"{repeat_feature.get('feature_type')} region ({label}, residues "
                f"{repeat_feature.get('begin')}-{repeat_feature.get('end')}) -- PM4 does not apply in a "
                "known repeat/low-complexity region, where length changes are plausible benign population "
                "variation rather than a functional disruption.",
                conflicting_evidence=[
                    f"UniProt: {repeat_feature.get('feature_type')} ({label}), residues {repeat_feature.get('begin')}-{repeat_feature.get('end')}."
                ],
                evidence_sources=["transcript_structure", "UniProt"],
                confidence="Moderate",
                details={
                    "variant_detail": {
                        "category": detail.category,
                        "codon_number": detail.codon_number,
                        "residues_changed": detail.residues_changed,
                    },
                    "notes": notes,
                },
            )

        supporting = list(notes)
        confidence = "Moderate"
        if checked is None:
            unchecked.append(
                "Repeat-region caveat: no UniProt annotation was available for this gene/position, so it "
                "could not be confirmed that this position sits outside a known repeat region."
            )
            confidence = "Low"
        else:
            supporting.append("UniProt: no repeat/compositional-bias region annotated at this codon.")

        return CriterionResult(
            "PM4",
            direction,
            strength,
            "triggered",
            f"Predicted consequence is a qualifying {detail.category.replace('_', ' ')}"
            + (f" ({detail.residues_changed} residue(s))" if detail.residues_changed else "")
            + _at_codon(detail.codon_number)
            + (
                ", and UniProt confirms this position is not in an annotated repeat/low-complexity region."
                if checked is False
                else ", but no UniProt annotation was available to confirm this is outside a known repeat region."
            ),
            supporting_evidence=supporting,
            evidence_sources=["transcript_structure"] + (["UniProt"] if checked is not None else []),
            confidence=confidence,
            details={
                "variant_detail": {
                    "category": detail.category,
                    "codon_number": detail.codon_number,
                    "residues_changed": detail.residues_changed,
                },
                "notes": notes,
                "unchecked_caveats": unchecked,
            },
        )

    # Each integrated conservation score type: which
    # `ConservationAnnotation`/result-dict field it lives in, its
    # human-readable label for `evidence_sources`/rationale text, and
    # which `CONFIG.conservation.*_THRESHOLD` pair classifies it.
    # PhyloP and PhastCons are on genuinely different scales (see
    # `CONFIG.conservation`'s own docstring), so each gets its own
    # threshold pair rather than sharing one -- GERP++ will add a
    # third entry here, not a parallel code path.
    _CONSERVATION_SCORE_TYPES = (
        ("phylop_score", "PhyloP", "PHYLOP_CONSERVED_THRESHOLD", "PHYLOP_NOT_CONSERVED_THRESHOLD"),
        ("phastcons_score", "PhastCons", "PHASTCONS_CONSERVED_THRESHOLD", "PHASTCONS_NOT_CONSERVED_THRESHOLD"),
        ("gerp_score", "GERP++", "GERP_CONSERVED_THRESHOLD", "GERP_NOT_CONSERVED_THRESHOLD"),
    )

    @staticmethod
    def _conservation_signal(
        conservation_result: Optional[Dict[str, Any]],
        score_key: str,
        conserved_threshold: float,
        not_conserved_threshold: float,
    ):
        """
        Classifies one conservation score type from a
        `pipeline.conservation.lookup.ConservationLookup` result into
        "conserved" (supports PP3) / "not_conserved" (supports BP4) /
        "ambiguous" (supports neither -- the score is real but falls
        between the two configurable thresholds) / `None` (no usable
        score at all: skipped, errored, not found, or this specific
        score type wasn't returned). Generic over `score_key`
        (`"phylop_score"` / `"phastcons_score"`) and its own threshold
        pair so `_pp3`/`_bp4` call this once per integrated score type
        (see `_CONSERVATION_SCORE_TYPES`) rather than duplicating this
        logic per type.
        """
        if (
            not conservation_result
            or conservation_result.get("skipped")
            or conservation_result.get("error")
            or not conservation_result.get("found")
        ):
            return None, None
        score = conservation_result.get(score_key)
        if score is None:
            return None, None
        if score >= conserved_threshold:
            return "conserved", score
        if score <= not_conserved_threshold:
            return "not_conserved", score
        return "ambiguous", score

    @staticmethod
    def _pp3(
        alphamissense_result: Dict[str, Any],
        mmsplice_result: Dict[str, Any],
        ensemble_result: Optional[Dict[str, Any]] = None,
        conservation_result: Optional[Dict[str, Any]] = None,
    ) -> CriterionResult:
        pp3, _ = ACMGRuleEngine._pp3_bp4(alphamissense_result, mmsplice_result, ensemble_result, conservation_result)
        return pp3

    @staticmethod
    def _bp4(
        alphamissense_result: Dict[str, Any],
        mmsplice_result: Dict[str, Any],
        ensemble_result: Optional[Dict[str, Any]] = None,
        conservation_result: Optional[Dict[str, Any]] = None,
    ) -> CriterionResult:
        _, bp4 = ACMGRuleEngine._pp3_bp4(alphamissense_result, mmsplice_result, ensemble_result, conservation_result)
        return bp4

    @staticmethod
    def _pp3_bp4(
        alphamissense_result: Optional[Dict[str, Any]],
        mmsplice_result: Optional[Dict[str, Any]],
        ensemble_result: Optional[Dict[str, Any]] = None,
        conservation_result: Optional[Dict[str, Any]] = None,
    ) -> "tuple[CriterionResult, CriterionResult]":
        """
        PP3 ("computational evidence supports a deleterious effect") and
        BP4 ("computational evidence suggests no deleterious effect")
        evaluated together, from one shared scan of the same four
        computational evidence sources (AlphaMissense, MMSplice, the
        Enformer/Borzoi AI ensemble, and PhyloP/PhastCons/GERP++
        conservation).

        BUG THIS REPLACES: `_pp3` and `_bp4` used to each independently
        scan this same evidence for their own favored direction only,
        with no check against the opposite direction. Every individual
        *source* is internally consistent (AlphaMissense reports exactly
        one am_class; MMSplice/the ensemble report exactly one
        classification -- none of those can argue both directions at
        once), but nothing stopped PP3 finding a damaging signal from
        one source while BP4 simultaneously found a benign signal from
        a *different* source on the same variant -- e.g. AlphaMissense
        'likely_pathogenic' (triggers PP3) alongside PhastCons
        'not_conserved' (triggers BP4). Both were reported as
        "triggered" side by side, which is a contradiction: PP3 and BP4
        are logical opposites and the report should never assert both.
        (There was also a half-finished attempt at this exact fix
        already in `_bp4` -- `checked_mm`/`mm_neutral_only`/
        `no_damaging_signal` were computed but never actually used to
        gate anything; this replaces that dead code with a real,
        symmetric fix, and along the way lets MMSplice's own "no
        significant splice effect" reading count as genuine BP4
        evidence, matching how MMSplice's damaging reading already
        counted for PP3.)

        RESOLUTION -- when should conflicting predictors win?
        When the same evaluation finds *both* a damaging and a benign
        computational signal, computational evidence is
        self-contradictory for this variant, and this reports
        "not_triggered" for *both* PP3 and BP4 (never one arbitrarily
        overriding the other), with the opposing signal(s) surfaced in
        `conflicting_evidence` so the disagreement stays visible rather
        than being silently dropped. Reasoning:
          - ACMG/AMP 2015 and the ClinGen SVI's later in silico
            calibration work (Pejaver et al. 2022) both expect
            computational evidence to come from a source (or sources)
            that agree, not from picking whichever predictor argues the
            direction you want; GEPER has no validated ranking of
            AlphaMissense vs. conservation vs. the splice ensemble that
            would justify letting one silently outvote another.
          - This mirrors a precedent already in this exact engine: BP7
            (see `_bp7`) already treats "a damaging call from any
            splice source blocks the synonymous/benign claim" as
            correct -- extending the same "any dissent blocks the
            claim" principle symmetrically to both PP3 and BP4 here is
            the internally consistent choice for this codebase.
          - A majority-vote or fixed-priority scheme (e.g.
            "AlphaMissense always wins") was considered and rejected:
            it would still be picking a winner GEPER has no evidence
            base to justify, dressed up as a rule instead of an
            assumption.
        This is deliberately the most conservative resolution (favors
        neither direction) rather than an attempt to be "decisive".
        """
        pp3_dir, pp3_strength = _STRENGTH["PP3"]
        bp4_dir, bp4_strength = _STRENGTH["BP4"]
        sources: List[str] = []
        damaging: List[tuple] = []  # (source_label, evidence_text)
        benign: List[tuple] = []

        if alphamissense_result and not alphamissense_result.get("skipped") and alphamissense_result.get("found"):
            am_class = (alphamissense_result.get("am_class") or "").strip().lower()
            sources.append("AlphaMissense")
            if am_class == "likely_pathogenic":
                damaging.append(
                    (
                        "AlphaMissense",
                        f"AlphaMissense predicts 'likely_pathogenic' (am_pathogenicity={alphamissense_result.get('am_pathogenicity')}).",
                    )
                )
            elif am_class == "likely_benign":
                benign.append(
                    (
                        "AlphaMissense",
                        f"AlphaMissense predicts 'likely_benign' (am_pathogenicity={alphamissense_result.get('am_pathogenicity')}).",
                    )
                )

        if mmsplice_result and mmsplice_result.get("predicted"):
            category = mmsplice_result.get("interpretation_category")
            sources.append("MMSplice")
            if category in (
                "strong_donor_loss",
                "strong_acceptor_loss",
                "exon_skipping",
                "intron_retention",
                "strong",
                "moderate",
            ):
                damaging.append(
                    (
                        "MMSplice",
                        f"MMSplice predicts a damaging splice effect ({mmsplice_result.get('interpretation')}).",
                    )
                )
            else:
                benign.append(("MMSplice", "MMSplice predicts no significant splice disruption."))

        # Ensemble (Enformer/Borzoi) evidence -- additive, third source.
        # Follows the routing rules documented in
        # pipeline/models/ensemble.py::EnsembleManager.evaluate:
        #   0 models used (or no ensemble_result passed at all) -> no
        #     contribution here, same as before this integration existed.
        #   1 model used  -> that single model's own prediction, labeled
        #     as such in the rationale (not a "consensus").
        #   2 models used -> the genuine two-model consensus.
        if ensemble_result and ensemble_result.get("models_used"):
            models_used = ensemble_result["models_used"]
            classification = ensemble_result.get("classification")
            consensus_score = ensemble_result.get("consensus_score")
            basis = ensemble_result.get("basis")
            basis_label = (
                "single-model prediction"
                if basis == "single_model"
                else (f"{len(models_used)}-model consensus (agreement={ensemble_result.get('agreement_percentage')}%)")
            )
            label = f"AI-ensemble({'+'.join(models_used)})"
            sources.append(label)
            score_str = f"{consensus_score:.3f}" if consensus_score is not None else "n/a"
            if classification in ("large_effect", "moderate_effect"):
                damaging.append(
                    (
                        label,
                        (
                            f"Splicing/regulatory AI ensemble ({basis_label}) predicts a "
                            f"'{classification}' effect (score={score_str}). {ensemble_result.get('reasoning', '')}"
                        ),
                    )
                )
            elif classification == "no_significant_effect":
                benign.append(
                    (
                        label,
                        (
                            f"Splicing/regulatory AI ensemble ({basis_label}) predicts "
                            f"'no_significant_effect' (score={score_str}). {ensemble_result.get('reasoning', '')}"
                        ),
                    )
                )

        # Conservation (PhyloP, PhastCons, GERP++) evidence -- additive,
        # fourth+ source(s). See `_conservation_signal`'s own docstring
        # for the conserved/not_conserved/ambiguous/None classification.
        # Each integrated score type (see `_CONSERVATION_SCORE_TYPES`)
        # is checked independently -- PhyloP and PhastCons agreeing is
        # two lines of evidence, not double-counted as one, and the two
        # disagreeing (one "conserved", the other "not_conserved") is
        # exactly the kind of internal conflict this method now surfaces
        # rather than letting each side of it feed a different criterion.
        for score_key, label, conserved_field, not_conserved_field in ACMGRuleEngine._CONSERVATION_SCORE_TYPES:
            cfg = CONFIG.conservation
            cons_direction, cons_score = ACMGRuleEngine._conservation_signal(
                conservation_result, score_key, getattr(cfg, conserved_field), getattr(cfg, not_conserved_field)
            )
            if cons_direction is None:
                continue
            sources.append(label)
            if cons_direction == "conserved":
                damaging.append(
                    (
                        label,
                        (
                            f"{label} conservation score ({cons_score:.2f}) is at or above the conserved "
                            f"threshold ({getattr(cfg, conserved_field)}), indicating evolutionary constraint "
                            "at this position."
                        ),
                    )
                )
            elif cons_direction == "not_conserved":
                benign.append(
                    (
                        label,
                        (
                            f"{label} conservation score ({cons_score:.2f}) is at or below the not-conserved "
                            f"threshold ({getattr(cfg, not_conserved_field)}), indicating no evolutionary "
                            "constraint at this position."
                        ),
                    )
                )

        if not sources:
            na = _not_evaluated(
                "PP3",
                "no computational predictor (AlphaMissense/MMSplice/AI-ensemble/PhyloP/PhastCons/GERP++) produced a result for this variant.",
            )
            nb = _not_evaluated(
                "BP4",
                "no computational predictor (AlphaMissense/MMSplice/AI-ensemble/PhyloP/PhastCons/GERP++) produced a result for this variant.",
            )
            return na, nb

        damaging_texts = [text for _, text in damaging]
        benign_texts = [text for _, text in benign]

        if damaging and benign:
            damaging_labels = ", ".join(dict.fromkeys(label for label, _ in damaging))
            benign_labels = ", ".join(dict.fromkeys(label for label, _ in benign))
            conflict_note = (
                f"Computational predictors disagree for this variant: {damaging_labels} indicate a "
                f"deleterious effect, while {benign_labels} indicate no deleterious effect. Neither PP3 "
                "nor BP4 is applied when the integrated computational evidence is self-contradictory."
            )
            pp3 = CriterionResult(
                "PP3",
                pp3_dir,
                pp3_strength,
                "not_triggered",
                conflict_note,
                supporting_evidence=damaging_texts,
                conflicting_evidence=benign_texts,
                evidence_sources=sources,
                confidence="Low",
            )
            bp4 = CriterionResult(
                "BP4",
                bp4_dir,
                bp4_strength,
                "not_triggered",
                conflict_note,
                supporting_evidence=benign_texts,
                conflicting_evidence=damaging_texts,
                evidence_sources=sources,
                confidence="Low",
            )
            return pp3, bp4

        if damaging:
            pp3 = CriterionResult(
                "PP3",
                pp3_dir,
                pp3_strength,
                "triggered",
                "Computational evidence (from "
                + " and ".join(dict.fromkeys(label for label, _ in damaging))
                + ") supports a deleterious effect on the gene/gene product.",
                supporting_evidence=damaging_texts,
                evidence_sources=sources,
                confidence="Moderate" if len(damaging_texts) > 1 else "Low",
            )
            bp4 = CriterionResult(
                "BP4",
                bp4_dir,
                bp4_strength,
                "not_triggered",
                "Computational predictors that returned a result (" + ", ".join(sources) + ") did not "
                "converge on a benign prediction.",
                conflicting_evidence=damaging_texts,
                evidence_sources=sources,
                confidence="Low",
            )
            return pp3, bp4

        if benign:
            bp4 = CriterionResult(
                "BP4",
                bp4_dir,
                bp4_strength,
                "triggered",
                "Computational evidence ("
                + " and ".join(dict.fromkeys(label for label, _ in benign))
                + ") suggests no deleterious effect.",
                supporting_evidence=benign_texts,
                evidence_sources=sources,
                confidence="Low",
            )
            pp3 = CriterionResult(
                "PP3",
                pp3_dir,
                pp3_strength,
                "not_triggered",
                "Computational predictors that returned a result (" + ", ".join(sources) + ") did not "
                "indicate a damaging effect.",
                conflicting_evidence=benign_texts,
                evidence_sources=sources,
                confidence="Low",
            )
            return pp3, bp4

        # Sources returned results, but none crossed either directional
        # threshold (e.g. only "ambiguous" conservation scores).
        pp3 = CriterionResult(
            "PP3",
            pp3_dir,
            pp3_strength,
            "not_triggered",
            "Computational predictors that returned a result (" + ", ".join(sources) + ") did not "
            "indicate a damaging effect.",
            evidence_sources=sources,
            confidence="Low",
        )
        bp4 = CriterionResult(
            "BP4",
            bp4_dir,
            bp4_strength,
            "not_triggered",
            "Computational predictors that returned a result did not converge on a benign prediction.",
            evidence_sources=sources,
            confidence="Low",
        )
        return pp3, bp4

    @staticmethod
    def _ba1_bs1(gnomad_result: Optional[Dict[str, Any]]):
        ba1_dir, ba1_strength = _STRENGTH["BA1"]
        bs1_dir, bs1_strength = _STRENGTH["BS1"]
        if (
            not gnomad_result
            or gnomad_result.get("skipped")
            or gnomad_result.get("error")
            or not gnomad_result.get("found")
        ):
            reason = (
                "gnomAD lookup was skipped, errored, or the variant was not found."
                if not (gnomad_result and gnomad_result.get("found"))
                else ""
            )
            na = _not_evaluated("BA1", reason or "gnomAD data unavailable.")
            nb = _not_evaluated("BS1", reason or "gnomAD data unavailable.")
            return na, nb

        cfg = CONFIG.gnomad
        global_af = gnomad_result.get("global_af")
        popmax_af = None
        for pop_freq in (gnomad_result.get("population_breakdown") or {}).values():
            pop_af = pop_freq.get("af")
            if pop_af is not None and (popmax_af is None or pop_af > popmax_af):
                popmax_af = pop_af

        priority = ACMGRuleEngine._population_priority_context(gnomad_result, cfg)
        fallback_note = ""
        contrast_note = ""
        af_label: Optional[str] = None

        if priority is not None and priority["af"] is not None:
            af = priority["af"]
            af_label = f"{af:.2e} (AF_{priority['code']}, {priority['label']})"
            # Only worth calling out when looking at global AF alone
            # would have understated how common this allele actually is
            # in the deployment's target population -- "rare" here means
            # "below the more permissive of the two common-variant
            # thresholds (BS1)", not merely "lower than the priority AF",
            # so a global AF that's already well above BA1/BS1 on its own
            # (just slightly lower than the priority population's) isn't
            # misreported as "rare in the global population". See
            # GnomadConfig.POPULATION_PRIORITY's docstring for why this
            # gap exists (gnomAD's global AF pools every population,
            # diluting one that's genuinely common in just one of them).
            if global_af is not None and global_af < af and global_af < cfg.BS1_AF_THRESHOLD:
                contrast_note = (
                    f" Common in the {priority['label']} population (gnomAD AF_{priority['code']}={af:.2e}), "
                    f"though rare in the global population (gnomAD global AF={global_af:.2e})."
                )
        elif priority is not None and priority["af"] is None:
            # Priority population configured but genuinely unavailable for
            # this variant -- never silently substitute; fall back to the
            # pre-existing popmax/global logic with an explicit disclosure.
            af = popmax_af if (cfg.USE_POPMAX_FOR_BA1_BS1 and popmax_af is not None) else global_af
            source_label = "popmax" if (cfg.USE_POPMAX_FOR_BA1_BS1 and popmax_af is not None) else "global"
            af_label = f"{af:.2e} ({source_label})" if af is not None else None
            fallback_note = (
                f" (population-priority '{priority['code']}' allele frequency was not available for this "
                f"variant; used gnomAD {source_label} AF instead)"
            )
        else:
            af = popmax_af if (cfg.USE_POPMAX_FOR_BA1_BS1 and popmax_af is not None) else global_af
            af_label = (
                f"{af:.2e}" + (" (popmax)" if (cfg.USE_POPMAX_FOR_BA1_BS1 and popmax_af is not None) else " (global)")
                if af is not None
                else None
            )

        if af is None:
            na = _not_evaluated("BA1", "gnomAD record found but no allele frequency reported.")
            nb = _not_evaluated("BS1", "gnomAD record found but no allele frequency reported.")
            return na, nb

        if af >= cfg.BA1_AF_THRESHOLD:
            ba1 = CriterionResult(
                "BA1",
                ba1_dir,
                ba1_strength,
                "triggered",
                f"Allele frequency {af_label} is at or above the BA1 stand-alone-benign threshold "
                f"({cfg.BA1_AF_THRESHOLD:.2e}), too common to be a rare-disease-causing variant."
                f"{contrast_note}{fallback_note}",
                supporting_evidence=[f"gnomAD allele frequency = {af_label}."],
                evidence_sources=["gnomAD"],
                confidence="High",
            )
            bs1 = CriterionResult(
                "BS1",
                bs1_dir,
                bs1_strength,
                "not_triggered",
                "Superseded by BA1 (stand-alone).",
                evidence_sources=["gnomAD"],
            )
            return ba1, bs1

        ba1 = CriterionResult(
            "BA1",
            ba1_dir,
            ba1_strength,
            "not_triggered",
            f"Allele frequency {af_label} is below the BA1 threshold.{fallback_note}",
            evidence_sources=["gnomAD"],
        )

        if af >= cfg.BS1_AF_THRESHOLD:
            bs1 = CriterionResult(
                "BS1",
                bs1_dir,
                bs1_strength,
                "triggered",
                f"Allele frequency {af_label} exceeds the expected frequency for the disorder "
                f"(BS1 threshold {cfg.BS1_AF_THRESHOLD:.2e}).{contrast_note}{fallback_note}",
                supporting_evidence=[f"gnomAD allele frequency = {af_label}."],
                evidence_sources=["gnomAD"],
                confidence="Moderate",
            )
        else:
            bs1 = CriterionResult(
                "BS1",
                bs1_dir,
                bs1_strength,
                "not_triggered",
                f"Allele frequency {af_label} is below the BS1 threshold.{fallback_note}",
                evidence_sources=["gnomAD"],
            )
        return ba1, bs1

    @staticmethod
    def _bp7(
        is_synonymous: Optional[bool],
        mmsplice_result: Optional[Dict[str, Any]],
        spliceformer_result: Optional[Dict[str, Any]] = None,
        splicebert_result: Optional[Dict[str, Any]] = None,
    ) -> CriterionResult:
        """
        BP7 (ACMG/AMP 2015): "A synonymous (silent) variant for which
        splicing prediction algorithms predict no impact to the splice
        consensus sequence nor the creation of a new splice site" --
        Benign, Supporting.

        Previously checked MMSplice alone; now additionally reads
        `spliceformer_result`/`splicebert_result` directly --
        `pipeline/models/spliceformer_plugin.py::SpliceFormerPlugin`/
        `pipeline/models/splicebert_plugin.py::SpliceBERTPlugin`'s own
        `predict()` output (`{"score", "classification", "confidence",
        "details", ...}`, `classification` in the same
        no_significant_effect/moderate_effect/large_effect buckets
        those two plugins and Enformer/Borzoi all share). A damaging
        call from *any* source blocks triggering -- BP7's "no impact"
        claim has to hold against every available splice predictor, not
        just the first one checked.

        Deliberately NOT `ensemble_result`: that field is
        `pipeline/models/ensemble.py::EnsembleManager`'s Enformer+Borzoi
        consensus specifically (see that module's own docstring,
        `_ENSEMBLE_MODEL_KEYS = ("enformer", "borzoi")`) -- a different,
        general regulatory-effect model pair already dedicated to
        PP3/BP4, not the dedicated splice predictors requested here.

        INTEGRATION GAP, stated honestly rather than implied away:
        GEPER's orchestrator does not currently run a per-variant
        SpliceFormer/SpliceBERT inference stage feeding into this
        engine -- both plugins are registered in the model registry and
        directly callable (`ModelManager.predict("spliceformer"/
        "splicebert", ref_seq, alt_seq)`, exercised in
        `tests/test_spliceformer_integration.py` and
        `benchmark_spliceformer.py`) and SpliceFormer specifically is
        gated behind `CONFIG.splicing.ENABLE_SPLICEFORMER` (default
        off), but no orchestrator stage yet reshapes either plugin's
        output into a value it passes through
        `InterpretationEngine.interpret()` to `evaluate()`. These two
        parameters exist now so that gap is a plumbing task, not a rule
        rewrite, once such a stage exists -- exactly how `ensemble_result`
        itself was added as an accept-if-given, None-otherwise parameter
        before any caller supplied it. Until then, BP7 in production
        keeps operating on MMSplice alone, same as before this change.
        """
        direction, strength = _STRENGTH["BP7"]
        if is_synonymous is None:
            return _not_evaluated(
                "BP7",
                "This variant's protein-level consequence could not be determined from transcript "
                "data (no transcript/CDS structure was available, or this is a multi-nucleotide "
                "substitution), so synonymous status cannot be confirmed -- BP7 requires a confirmed "
                "synonymous call, never an assumption.",
            )
        if not is_synonymous:
            return CriterionResult(
                "BP7",
                direction,
                strength,
                "not_triggered",
                "Variant is not synonymous at the protein level (transcript-CDS-frame consequence call).",
                evidence_sources=["transcript_cds"],
            )

        sources, supporting, conflicting = [], [], []
        mm_damaging = False
        if mmsplice_result and mmsplice_result.get("predicted"):
            sources.append("MMSplice")
            category = mmsplice_result.get("interpretation_category")
            if category in (
                "strong_donor_loss",
                "strong_acceptor_loss",
                "exon_skipping",
                "intron_retention",
                "strong",
                "moderate",
            ):
                mm_damaging = True
                conflicting.append(f"MMSplice: {mmsplice_result.get('interpretation')}.")
            else:
                supporting.append("MMSplice: no significant splice disruption predicted.")

        plugin_damaging = False
        for label, plugin_result in (("SpliceFormer", spliceformer_result), ("SpliceBERT", splicebert_result)):
            if not plugin_result or not plugin_result.get("classification"):
                continue
            sources.append(label)
            classification = plugin_result.get("classification")
            score = plugin_result.get("score")
            score_str = f"{score:.3f}" if isinstance(score, (int, float)) else "n/a"
            if classification in ("large_effect", "moderate_effect"):
                plugin_damaging = True
                conflicting.append(f"{label} predicts a '{classification}' effect (score={score_str}).")
            else:
                supporting.append(f"{label}: no significant splice disruption predicted (score={score_str}).")

        if not sources:
            return CriterionResult(
                "BP7",
                direction,
                strength,
                "triggered",
                "Variant is synonymous at the protein level; no splice-prediction evidence (MMSplice, "
                "SpliceFormer, or SpliceBERT) was available to check for a conflicting splice effect.",
                supporting_evidence=["Synonymous at the protein level."],
                conflicting_evidence=[
                    "No splice predictor produced a result for this variant, so a splice effect cannot be ruled out."
                ],
                evidence_sources=["transcript_cds"],
                confidence="Low",
            )

        if mm_damaging or plugin_damaging:
            return CriterionResult(
                "BP7",
                direction,
                strength,
                "not_triggered",
                "Variant is synonymous at the protein level, but "
                + " and ".join(sources)
                + " predicts a damaging splice effect, so BP7 (synonymous with no splice impact) does "
                "not apply.",
                conflicting_evidence=conflicting,
                evidence_sources=["transcript_cds"] + sources,
                confidence="Moderate",
            )

        return CriterionResult(
            "BP7",
            direction,
            strength,
            "triggered",
            "Variant is synonymous at the protein level and splice-effect evidence from "
            + ", ".join(sources)
            + " does not predict a significant splice-disrupting effect.",
            supporting_evidence=["Synonymous at the protein level."] + supporting,
            evidence_sources=["transcript_cds"] + sources,
            confidence="Moderate",
        )

    # PP1/BS4 both rest on the same gene-disease clinical validity
    # prerequisite -- "cosegregation" only means something for a
    # gene-disease relationship that actually exists -- so both check
    # `clinical_validity_summary` the same way, in opposite directions.
    # ClinGen SVI classification tiers (see
    # `pipeline/clingen/models.py::CLINICAL_VALIDITY_CLASSIFICATIONS`);
    # only these three definitively rule the prerequisite out.
    _VALIDITY_PREREQUISITE_FAILURES = ("Refuted", "Disputed", "No Known Disease Relationship")

    @staticmethod
    def _pp1(clingen_result: Optional[Dict[str, Any]]) -> CriterionResult:
        """
        PP1 (cosegregation with disease in multiple affected family
        members) requires per-family pedigree/linkage data -- GEPER
        annotates single samples with no pedigree input at all, so this
        criterion can never be *triggered* by this pipeline; fabricating
        a segregation conclusion from gene-level curation would be
        exactly the kind of invented evidence this engine's docstring
        commits to never producing.

        What ClinGen's gene-disease clinical validity curation *can*
        answer is PP1's own precondition: "cosegregates with disease" is
        only a meaningful statement for a gene with an established
        disease relationship. When ClinGen curates the gene as Refuted,
        Disputed, or having No Known Disease Relationship, PP1 is
        definitively inapplicable regardless of what any hypothetical
        segregation data would show -- there is no established disease
        for the variant to segregate with -- so this returns
        "not_triggered" rather than "not_evaluated" in that case. Every
        other case (Definitive/Strong/Moderate/Limited validity, or no
        ClinGen curation at all) leaves PP1 "not_evaluated": the
        prerequisite is satisfied or unknown, but the actual missing
        ingredient -- real segregation data -- still is.
        """
        direction, strength = _STRENGTH["PP1"]
        if (
            not clingen_result
            or clingen_result.get("skipped")
            or clingen_result.get("error")
            or not clingen_result.get("found")
        ):
            return _not_evaluated(
                "PP1",
                "requires family segregation data (not integrated); ClinGen gene-disease validity curation was also unavailable to check PP1's prerequisite.",
            )

        classification = clingen_result.get("clinical_validity_summary")
        gene = clingen_result.get("gene_symbol") or "this gene"
        if classification in ACMGRuleEngine._VALIDITY_PREREQUISITE_FAILURES:
            return CriterionResult(
                "PP1",
                direction,
                strength,
                "not_triggered",
                f"PP1 requires cosegregation with disease in multiple affected family members, which "
                f"presupposes an established gene-disease relationship. ClinGen curates {gene}'s "
                f"gene-disease relationship as '{classification}', so PP1 cannot apply regardless of any "
                f"segregation data (which this pipeline does not integrate in any case).",
                conflicting_evidence=[f"ClinGen gene-disease clinical validity: {classification}."],
                evidence_sources=["ClinGen"],
                confidence="Moderate",
            )
        return CriterionResult(
            "PP1",
            direction,
            strength,
            "not_evaluated",
            f"Not evaluated: requires family segregation data, which this pipeline does not integrate "
            f"(no pedigree/linkage input). ClinGen's gene-disease clinical validity curation for {gene} "
            f"('{classification or 'no curation found'}') does not rule PP1 out, but cannot substitute "
            f"for actual segregation observations.",
            supporting_evidence=(
                [f"ClinGen gene-disease clinical validity: {classification} (prerequisite for PP1 is satisfied)."]
                if classification
                else []
            ),
            evidence_sources=["ClinGen"],
            confidence="Low",
        )

    @staticmethod
    def _bs4(clingen_result: Optional[Dict[str, Any]]) -> CriterionResult:
        """
        BS4 (lack of segregation with disease) is PP1's benign-direction
        mirror and needs the same pedigree/linkage data GEPER never has
        -- it can never be *triggered* here either. See `_pp1` for why a
        Refuted/Disputed/No-Known-Disease-Relationship ClinGen
        classification is nonetheless a definitive "not_triggered": if
        there is no established disease for this gene, there is nothing
        for the variant to have failed to segregate with, so the
        question BS4 asks is moot rather than merely unanswered.
        """
        direction, strength = _STRENGTH["BS4"]
        if (
            not clingen_result
            or clingen_result.get("skipped")
            or clingen_result.get("error")
            or not clingen_result.get("found")
        ):
            return _not_evaluated(
                "BS4",
                "requires family segregation data (not integrated); ClinGen gene-disease validity curation was also unavailable to check BS4's prerequisite.",
            )

        classification = clingen_result.get("clinical_validity_summary")
        gene = clingen_result.get("gene_symbol") or "this gene"
        if classification in ACMGRuleEngine._VALIDITY_PREREQUISITE_FAILURES:
            return CriterionResult(
                "BS4",
                direction,
                strength,
                "not_triggered",
                f"BS4 requires observed lack of segregation with disease in family members, which "
                f"presupposes an established gene-disease relationship to segregate (or fail to "
                f"segregate) with. ClinGen curates {gene}'s gene-disease relationship as "
                f"'{classification}', so BS4 does not meaningfully apply either.",
                conflicting_evidence=[f"ClinGen gene-disease clinical validity: {classification}."],
                evidence_sources=["ClinGen"],
                confidence="Low",
            )
        return CriterionResult(
            "BS4",
            direction,
            strength,
            "not_evaluated",
            f"Not evaluated: requires family segregation data, which this pipeline does not integrate "
            f"(no pedigree/linkage input). ClinGen's gene-disease clinical validity curation for {gene} "
            f"('{classification or 'no curation found'}') does not rule BS4 out, but cannot substitute "
            f"for actual segregation observations.",
            evidence_sources=["ClinGen"],
            confidence="Low",
        )

    @staticmethod
    def _bp1(is_missense: Optional[bool], clingen_result: Optional[Dict[str, Any]]) -> CriterionResult:
        """
        BP1 (ACMG/AMP 2015): "Missense variant in a gene for which
        primarily truncating variants are known to cause disease" --
        Benign, Supporting. The mirror image of PVS1's own gene-
        mechanism precondition, so it reuses the identical mechanism
        state `pipeline/pvs1/utils.py::lof_mechanism_from_clingen`
        already derives from ClinGen's dosage-sensitivity curation,
        rather than a second gene-mechanism source: a gene ClinGen
        curates as "sufficient evidence for dosage pathogenicity"
        (score 3, `LOF_ESTABLISHED`) or the autosomal-recessive
        equivalent (score 30, `LOF_ESTABLISHED_RECESSIVE`) is, by
        definition, a gene where loss-of-function/truncating variants
        are an established disease mechanism -- BP1's precondition,
        read from the same axis PVS1 already reads.

        Caveat (post-2015 ClinGen SVI guidance): SVI's later
        gene-specific calibration work (Tavtigian et al. 2020's
        Bayesian point framework and subsequent per-gene specifications)
        found BP1's "primarily truncating" precondition doesn't hold
        uniformly across every dosage-sensitive gene -- some
        haploinsufficient genes still have a real missense mechanism
        (e.g. dominant-negative effects), so a per-gene-calibrated point
        value is recommended where a specification exists. GEPER has no
        such per-gene calibration integrated, so this applies the
        unmodified 2015 default ("supporting") strength and says so in
        the rationale, rather than silently assuming calibration that
        was never done. Unlike PP5/BP6 (see `_bp6`), BP1 itself was not
        deprecated by SVI -- default use is still expected wherever the
        gene mechanism is genuinely established.
        """
        direction, strength = _STRENGTH["BP1"]
        if is_missense is None:
            return _not_evaluated(
                "BP1",
                "This variant's protein-level consequence could not be determined from transcript "
                "data (no transcript/CDS structure was available, or this is a multi-nucleotide "
                "substitution), so missense status cannot be confirmed -- BP1 requires a confirmed "
                "missense call, never an assumption.",
            )
        if not is_missense:
            return CriterionResult(
                "BP1",
                direction,
                strength,
                "not_triggered",
                "Variant is not a missense substitution at the protein level (transcript-CDS-frame consequence call).",
                evidence_sources=["transcript_cds"],
            )

        mechanism, evidence = lof_mechanism_from_clingen(clingen_result)
        gene = (clingen_result or {}).get("gene_symbol") or "this gene"

        if mechanism == LOF_UNKNOWN:
            return _not_evaluated(
                "BP1",
                "requires a gene-level mechanism curation ('primarily truncating variants known to "
                "cause disease'); ClinGen dosage-sensitivity curation was unavailable for this gene.",
            )

        if mechanism in (LOF_ESTABLISHED, LOF_ESTABLISHED_RECESSIVE):
            return CriterionResult(
                "BP1",
                direction,
                strength,
                "triggered",
                f"Variant is missense, and ClinGen curates {gene} as a gene where loss-of-function/"
                "truncating variants are an established disease mechanism (read from the same ClinGen "
                "dosage-sensitivity axis PVS1 uses). No gene-specific BP1 point calibration is "
                "integrated here, so the unmodified 2015 default (supporting) strength is applied.",
                supporting_evidence=evidence,
                evidence_sources=["ClinGen", "transcript_cds"],
                confidence="Low",
                details={"lof_mechanism": mechanism},
            )

        # LOF_NOT_ESTABLISHED or LOF_REFUTED: curated, but doesn't
        # establish a primarily-truncating mechanism -- BP1's own
        # precondition fails, so this is a negative finding, not a gap.
        return CriterionResult(
            "BP1",
            direction,
            strength,
            "not_triggered",
            f"Variant is missense, but ClinGen's dosage-sensitivity curation for {gene} does not "
            "establish loss-of-function/truncating variants as this gene's disease mechanism, so BP1's "
            "precondition is not met.",
            conflicting_evidence=evidence,
            evidence_sources=["ClinGen", "transcript_cds"],
            confidence="Low",
            details={"lof_mechanism": mechanism},
        )

    _PM4_REPEAT_ONLY_CATEGORIES = (PM4_IN_FRAME_INDEL,)

    @staticmethod
    def _bp3(
        variant_dict: Optional[Dict[str, Any]] = None,
        transcript_result: Optional[Dict[str, Any]] = None,
        uniprot_result: Optional[Dict[str, Any]] = None,
    ) -> CriterionResult:
        """
        BP3 (ACMG/AMP 2015): "In-frame deletions/insertions in a
        repetitive region without a known function" -- Benign,
        Supporting. PM4's repeat-region caveat read as its own positive
        criterion: reuses `classify_pm4_variant`
        (`pipeline/pvs1/utils.py`) for the same CDS-frame-aware variant
        classification PM4/PS1/PM5 already share, and
        `_pm4_repeat_region_hit` for the identical UniProt
        repeat/compositional-bias check PM4 treats as a hard block --
        rather than a second implementation of either. Deliberately
        excludes PM4's other qualifying category (stop-loss
        substitution): ACMG/AMP's BP3 wording is specific to in-frame
        insertions/deletions, not substitutions of any kind.
        """
        direction, strength = _STRENGTH["BP3"]
        transcript = transcript_from_result(transcript_result)
        detail, notes = classify_pm4_variant(variant_dict or {}, transcript)

        if detail is None or detail.category not in ACMGRuleEngine._PM4_REPEAT_ONLY_CATEGORIES:
            if transcript is None:
                return _not_evaluated(
                    "BP3", "transcript structure was unavailable to classify this variant's coding consequence."
                )
            return CriterionResult(
                "BP3",
                direction,
                strength,
                "not_triggered",
                "Predicted consequence is not an in-frame insertion/deletion." + (f" ({notes[-1]})" if notes else ""),
                evidence_sources=["transcript_structure"],
            )

        checked, repeat_feature = ACMGRuleEngine._pm4_repeat_region_hit(uniprot_result, detail.codon_number)

        if checked is None:
            return _not_evaluated(
                "BP3",
                "in-frame indel confirmed, but no UniProt annotation was available to check whether "
                "this position falls in a repetitive region without known function.",
            )

        if checked is False:
            return CriterionResult(
                "BP3",
                direction,
                strength,
                "not_triggered",
                f"In-frame indel{_at_codon(detail.codon_number)} does not fall within a UniProt-"
                "annotated repeat/compositional-bias region.",
                supporting_evidence=list(notes),
                evidence_sources=["transcript_structure", "UniProt"],
                confidence="Moderate",
            )

        # checked is True here by elimination (None and False both
        # returned above) -- _pm4_repeat_region_hit only ever pairs
        # checked=True with a real feature dict, an invariant mypy
        # can't see across the two return values on its own.
        assert repeat_feature is not None
        label = repeat_feature.get("description") or repeat_feature.get("feature_type")
        return CriterionResult(
            "BP3",
            direction,
            strength,
            "triggered",
            f"In-frame {detail.category.replace('_', ' ')}{_at_codon(detail.codon_number)} falls within "
            f"a UniProt-annotated {repeat_feature.get('feature_type')} region ({label}, residues "
            f"{repeat_feature.get('begin')}-{repeat_feature.get('end')}), a repetitive region with no "
            "annotated function.",
            supporting_evidence=[
                f"UniProt: {repeat_feature.get('feature_type')} ({label}), residues "
                f"{repeat_feature.get('begin')}-{repeat_feature.get('end')}."
            ]
            + list(notes),
            evidence_sources=["transcript_structure", "UniProt"],
            confidence="Moderate",
            details={
                "variant_detail": {
                    "category": detail.category,
                    "codon_number": detail.codon_number,
                    "residues_changed": detail.residues_changed,
                },
                "notes": notes,
            },
        )

    _BP6_ACCEPTABLE_SIGNIFICANCE = ("benign", "likely benign")
    _BP6_UNRELIABLE_REVIEW_STATUS = (
        "no assertion provided",
        "no assertion criteria provided",
        "no classification provided",
    )
    _BP6_DEPRECATION_CAVEAT = (
        "Caveat: ClinGen's SVI Working Group (Biesecker & Harrison 2018) recommends against applying "
        "BP6 (and its pathogenic mirror, PP5) at all going forward, because deferring to another lab's "
        "classification without independent evidence is circular -- exactly what an ACMG/AMP evaluation "
        "is meant to avoid. This module's PP5 entry already reflects that by reporting 'not_evaluated' "
        "unconditionally. BP6 is evaluated here only for completeness/parity and to reuse the "
        "already-integrated ClinVar layer; it is reported at capped Low confidence and is excluded from "
        "this engine's own point-based combining rules (see `_combine`), so it cannot silently move the "
        "final classification -- read it as informational, not as independent evidence."
    )

    @staticmethod
    def _clinvar_not_found_reason(clinvar_result: Optional[Dict[str, Any]]) -> str:
        """
        Honest "why is there no ClinVar evidence for this variant" text,
        distinguishing `database/clinvar_client.py::ClinVarMatchStatus`'s
        two negative states: `POSITION_ONLY` (other ClinVar-catalogued
        variants exist at this genomic position, but none of them are
        this allele -- surfaced as context, never as this variant's own
        classification) vs `NOT_FOUND` (ClinVar has nothing at this
        position at all). Collapsing these into one "not found" message
        is exactly the bug this whole fix exists to remove -- see
        `ClinVarClient`'s module docstring for the real BRCA1 case where
        a co-located, unrelated variant's classification used to leak
        through as if it were this variant's own.
        """
        if not clinvar_result:
            return "no ClinVar record was found for this exact variant."
        status = clinvar_result.get("match_status")
        if status == "position_only":
            others = clinvar_result.get("record_count") or 0
            return (
                f"no ClinVar record was found for this exact variant -- {others} other ClinVar-catalogued "
                "variant(s) exist at this genomic position, but none match this allele, so their "
                "classifications are not evidence about this variant."
            )
        return "no ClinVar record was found for this exact variant."

    @staticmethod
    def _bp6(clinvar_result: Optional[Dict[str, Any]]) -> CriterionResult:
        """
        BP6 (ACMG/AMP 2015): "Reputable source recently reports variant
        as benign, but the evidence is not available to the laboratory
        to perform an independent evaluation" -- Benign, Supporting.
        Reuses the same `ClinVarClient.query_variant` result
        (`clinvar_result`) already computed for this pipeline's
        descriptive cross-reference (`_clinvar_crossref`), rather than a
        second ClinVar lookup.

        See `_BP6_DEPRECATION_CAVEAT` -- read it before trusting this
        criterion's output. Contrast with PS1/PM5, which also read
        ClinVar but for *other* variants at the *same position/codon*,
        not this variant's own prior classification, so they don't
        share BP6's circularity problem.
        """
        direction, strength = _STRENGTH["BP6"]
        if not clinvar_result or not clinvar_result.get("found") or not clinvar_result.get("primary_record"):
            return _not_evaluated(
                "BP6",
                ACMGRuleEngine._clinvar_not_found_reason(clinvar_result) + " " + ACMGRuleEngine._BP6_DEPRECATION_CAVEAT,
            )

        top = clinvar_result["primary_record"]
        raw_significance = top.get("clinical_significance")
        raw_review_status = top.get("review_status")
        significance = (raw_significance or "").strip().lower()
        review_status = (raw_review_status or "").strip().lower()
        accession = top.get("accession") or "this record"
        caveat = ACMGRuleEngine._BP6_DEPRECATION_CAVEAT

        if review_status in ACMGRuleEngine._BP6_UNRELIABLE_REVIEW_STATUS:
            return CriterionResult(
                "BP6",
                direction,
                strength,
                "not_triggered",
                f"ClinVar record {accession} reports '{raw_significance}', but its review status "
                f"('{raw_review_status}') carries no assertion criteria, too weak a source to treat as "
                f"'reputable' even under BP6's own wording. {caveat}",
                conflicting_evidence=[f"ClinVar {accession}: {raw_significance} ({raw_review_status})."],
                evidence_sources=["ClinVar"],
                confidence="Low",
                details={"clinvar_significance": raw_significance, "clinvar_review_status": raw_review_status},
            )

        if significance in ACMGRuleEngine._BP6_ACCEPTABLE_SIGNIFICANCE:
            return CriterionResult(
                "BP6",
                direction,
                strength,
                "triggered",
                f"ClinVar record {accession} reports '{raw_significance}' (review status: "
                f"{raw_review_status}). {caveat}",
                supporting_evidence=[f"ClinVar {accession}: {raw_significance} ({raw_review_status})."],
                evidence_sources=["ClinVar"],
                confidence="Low",
                details={"clinvar_significance": raw_significance, "clinvar_review_status": raw_review_status},
            )

        return CriterionResult(
            "BP6",
            direction,
            strength,
            "not_triggered",
            f"ClinVar record {accession} reports '{raw_significance}', not Benign/Likely benign. {caveat}",
            evidence_sources=["ClinVar"],
            confidence="Low",
            details={"clinvar_significance": raw_significance, "clinvar_review_status": raw_review_status},
        )

    @staticmethod
    def _pp4(phenotype_result: Optional[Dict[str, Any]], hpo_result: Optional[Dict[str, Any]]) -> CriterionResult:
        """
        PP4 (ACMG/AMP 2015): "Patient's phenotype or family history is
        highly specific for a disease with a single genetic etiology."
        Pathogenic, Supporting.

        INPUT MECHANISM: `main.py`'s CLI accepts `--hpo-terms` (a
        comma-separated list of the patient's own observed HPO IDs,
        e.g. `HP:0001250,HP:0002011`) and/or `--phenotype-file` (a
        JSON/text file of HPO IDs); `build_phenotype_result()` in
        `pipeline/hpo/utils.py` turns either into `phenotype_result`
        (`{"hpo_term_ids": [...]}`), which `main.py` passes through
        to `InterpretationEngine.interpret()`. When neither flag is
        passed for a run, `phenotype_result` is `None`/empty and PP4
        is reported `not_evaluated` below -- that is a per-run *data*
        gap (no phenotype terms supplied for this sample), not a
        pipeline *capability* gap.

        When phenotype terms are supplied, the logic below checks their overlap
        against this variant's gene's own HPO-curated phenotype set
        (`hpo_result`, see `pipeline/hpo/`,
        reusing the same gene-level annotation the orchestrator's HPO
        stage already resolves -- no second lookup here) as a proxy for
        "highly specific for this gene's disease", and the number of
        distinct HPO-curated disease entries for that gene
        (`hpo_result['distinct_disease_ids']`) as a proxy for "single
        genetic etiology".

        CAVEAT on that second proxy, disclosed rather than overclaimed:
        ACMG/AMP's "single genetic etiology" is itself a qualitative
        clinical judgment, not a count. Multiple HPO-curated disease
        IDs for one gene can mean either genuinely distinct phenotypic
        spectra (e.g. CFTR causing both classic cystic fibrosis and
        isolated congenital bilateral absence of the vas deferens -- a
        real difference in etiology/severity) or duplicate cross-
        database accessions for the *same* disease (an OMIM entry and
        an Orphanet entry both describing cystic fibrosis). This
        threshold (`CONFIG.hpo.PP4_MAX_DISTINCT_DISEASES`) cannot tell
        those two cases apart and is reported as an explicit heuristic
        approximation, never as the clinical judgment call PP4
        ultimately requires a human to make.
        """
        direction, strength = _STRENGTH["PP4"]
        patient_terms = set((phenotype_result or {}).get("hpo_term_ids") or [])
        if not patient_terms:
            return _not_evaluated(
                "PP4",
                "requires patient phenotype data (observed HPO terms); none were supplied for this run "
                "-- pass --hpo-terms and/or --phenotype-file to activate PP4. The input mechanism exists "
                "and this logic runs automatically once phenotype terms are provided.",
            )

        if not hpo_result or hpo_result.get("skipped") or hpo_result.get("error") or not hpo_result.get("found"):
            return _not_evaluated(
                "PP4",
                "patient phenotype data was provided, but this variant's gene has no HPO gene-phenotype "
                "annotation available to compare it against.",
            )

        gene = hpo_result.get("gene_symbol") or "this gene"
        gene_terms = {t["hpo_id"] for t in (hpo_result.get("distinct_phenotype_terms") or [])}
        overlap = patient_terms & gene_terms
        overlap_ratio = len(overlap) / len(patient_terms)
        distinct_diseases = hpo_result.get("distinct_disease_ids") or []

        cfg = CONFIG.hpo
        specific_enough = overlap_ratio >= cfg.PP4_OVERLAP_THRESHOLD
        single_etiology = len(distinct_diseases) <= cfg.PP4_MAX_DISTINCT_DISEASES

        details = {
            "patient_term_count": len(patient_terms),
            "gene_term_count": len(gene_terms),
            "overlap_count": len(overlap),
            "overlap_ratio": round(overlap_ratio, 3),
            "distinct_disease_count": len(distinct_diseases),
            "overlap_threshold": cfg.PP4_OVERLAP_THRESHOLD,
            "max_distinct_diseases_threshold": cfg.PP4_MAX_DISTINCT_DISEASES,
        }

        if specific_enough and single_etiology:
            return CriterionResult(
                "PP4",
                direction,
                strength,
                "triggered",
                f"{len(overlap)}/{len(patient_terms)} ({overlap_ratio:.0%}) of the patient's reported "
                f"phenotype terms match {gene}'s HPO-curated phenotype set, and HPO curates {gene} "
                f"against {len(distinct_diseases)} distinct disease entr{'y' if len(distinct_diseases) == 1 else 'ies'} "
                "(heuristic proxy for a single genetic etiology -- see rationale caveat in source).",
                supporting_evidence=[
                    f"HPO: {len(overlap)} shared phenotype term(s) with {gene} ({', '.join(sorted(overlap)[:5])}{'...' if len(overlap) > 5 else ''})."
                ],
                evidence_sources=["HPO"],
                confidence="Low",
                details=details,
            )

        reasons = []
        if not specific_enough:
            reasons.append(
                f"phenotype overlap ({overlap_ratio:.0%}) is below the {cfg.PP4_OVERLAP_THRESHOLD:.0%} threshold"
            )
        if not single_etiology:
            reasons.append(
                f"{gene} has {len(distinct_diseases)} distinct HPO-curated disease entries (above the {cfg.PP4_MAX_DISTINCT_DISEASES} threshold for 'single etiology')"
            )
        return CriterionResult(
            "PP4",
            direction,
            strength,
            "not_triggered",
            f"Patient phenotype was compared against {gene}'s HPO-curated phenotype set: " + "; ".join(reasons) + ".",
            evidence_sources=["HPO"],
            confidence="Low",
            details=details,
        )

    @staticmethod
    def _ps3(functional_evidence_result: Optional[Dict[str, Any]]) -> CriterionResult:
        """
        PS3 (ACMG/AMP 2015): "Well-established in vitro or in vivo
        functional studies supportive of a damaging effect on the gene
        or gene product." Pathogenic, Strong by default -- overridden
        by a source's own explicit strength assignment when it gives
        one (see below).

        Evidence source (see `pipeline/functional_evidence/`), per the
        ClinGen SVI's PS3/BS3 recommendation (Brnich et al. 2019,
        Genome Medicine):
          - **ClinGen Evidence Repository** (primary): an
            already-adjudicated VCEP "PS3: Met" call for this exact
            variant, at whatever strength that VCEP's own published
            specification assigned (e.g. a literal "PS3_Moderate"
            evidence code) -- the panel has already completed the
            SVI framework's four steps (disease mechanism, assay-class
            validity, specific-instance validity, application to this
            variant), so this is used as-is, at High confidence.
          - **MaveDB** (secondary, only consulted when ERepo had
            nothing for this variant): a raw multiplexed-assay score,
            bucketed into "abnormal" using that score set's own
            investigator-provided calibration thresholds -- never a
            threshold GEPER invents. This is one step below an
            already-adjudicated VCEP call in the SVI framework's
            validation hierarchy, so it defaults to Moderate strength
            (Supporting if the calibration itself is flagged
            research-use-only), at Moderate/Low confidence
            respectively -- configurable via
            `CONFIG.functional_evidence.MAVEDB_CLINICAL_GRADE_STRENGTH`
            / `MAVEDB_RESEARCH_USE_ONLY_STRENGTH`.

        Reported "not_evaluated" (not a fabricated "not_triggered")
        when neither source has any result at all for this variant --
        e.g. CFTR, confirmed live during development to have zero
        curated/assayed results in either source as of this
        integration; see both providers' module docstrings.
        """
        direction, strength = _STRENGTH["PS3"]
        if (
            not functional_evidence_result
            or functional_evidence_result.get("skipped")
            or functional_evidence_result.get("error")
            or not functional_evidence_result.get("found")
        ):
            return _not_evaluated("PS3", _functional_evidence_not_evaluated_reason(functional_evidence_result))

        records = [r for r in (functional_evidence_result.get("records") or []) if r.get("call") == "PS3"]
        if not records:
            # `found` (checked above) means at least one source
            # actually returned a curated/calibrated result for this
            # exact variant -- the evidence was checked and it simply
            # doesn't support a damaging call, which is a negative
            # finding, not a gap. Reporting `not_evaluated` here (as
            # this used to) would misleadingly claim the evidence
            # couldn't be checked at all, when it demonstrably was
            # (see BS3's own "call" filter on the same records: a
            # damaging-vs-no-damaging assay result answers both
            # questions from the same source, so PS3 not matching is
            # itself informative, not unknown).
            return CriterionResult(
                "PS3",
                direction,
                strength,
                "not_triggered",
                "Functional-evidence sources (ClinGen Evidence Repository and/or MaveDB) had a result for "
                "this variant, but none of it supported a damaging (PS3) call -- see BS3 for whether it "
                "instead supports a benign call.",
                evidence_sources=["ClinGen Evidence Repository", "MaveDB"],
                confidence="Low",
            )

        return ACMGRuleEngine._functional_evidence_criterion("PS3", direction, strength, records[0])

    @staticmethod
    def _bs3(functional_evidence_result: Optional[Dict[str, Any]]) -> CriterionResult:
        """
        BS3 (ACMG/AMP 2015): "Well-established in vitro or in vivo
        functional studies show no damaging effect on protein function
        or splicing." Benign, Strong by default -- PS3's exact
        benign-direction mirror; see `_ps3`'s docstring for the full
        rationale on source priority, strength defaults, and the
        honest "not_evaluated" fallback (both draw from the same
        `functional_evidence_result` dict, just filtering for BS3
        "Met"/"normal" calls instead of PS3 "Met"/"abnormal" ones).
        """
        direction, strength = _STRENGTH["BS3"]
        if (
            not functional_evidence_result
            or functional_evidence_result.get("skipped")
            or functional_evidence_result.get("error")
            or not functional_evidence_result.get("found")
        ):
            return _not_evaluated("BS3", _functional_evidence_not_evaluated_reason(functional_evidence_result))

        records = [r for r in (functional_evidence_result.get("records") or []) if r.get("call") == "BS3"]
        if not records:
            # Mirrors `_ps3`'s identical fix: `found` means the
            # evidence was genuinely checked, so no BS3-supporting
            # record is a negative finding, not a gap -- see `_ps3`'s
            # comment for the full reasoning.
            return CriterionResult(
                "BS3",
                direction,
                strength,
                "not_triggered",
                "Functional-evidence sources (ClinGen Evidence Repository and/or MaveDB) had a result for "
                "this variant, but none of it supported a benign (BS3) call -- see PS3 for whether it "
                "instead supports a damaging call.",
                evidence_sources=["ClinGen Evidence Repository", "MaveDB"],
                confidence="Low",
            )

        return ACMGRuleEngine._functional_evidence_criterion("BS3", direction, strength, records[0])

    @staticmethod
    def _functional_evidence_criterion(
        code: str,
        direction: str,
        default_strength: str,
        record: Dict[str, Any],
    ) -> CriterionResult:
        """Shared `CriterionResult` construction for `_ps3`/`_bs3` -- both
        just filter `functional_evidence_result["records"]` for their own
        call direction and hand the winning record here."""
        strength = record.get("strength") or default_strength
        source = record.get("source")

        if source == "clingen_erepo":
            gene_clause = f" ({record['expert_panel']})" if record.get("expert_panel") else ""
            rationale = (
                f"ClinGen Evidence Repository: an expert panel{gene_clause} curated this variant "
                f"({record.get('matched_hgvs')}) with '{code}: Met'"
                + (
                    f", classifying it as {record['classification_outcome']}"
                    if record.get("classification_outcome")
                    else ""
                )
                + (f" for {record['condition']}" if record.get("condition") else "")
                + "."
            )
            supporting = [
                f"ClinGen ERepo {code}: Met"
                + (f" ({record['expert_panel']})" if record.get("expert_panel") else "")
                + "."
            ]
            evidence_sources = ["ClinGen Evidence Repository"]
            confidence = "High"
            details = {
                "matched_hgvs": record.get("matched_hgvs"),
                "expert_panel": record.get("expert_panel"),
                "specification_url": record.get("specification_url"),
                "classification_outcome": record.get("classification_outcome"),
                "condition": record.get("condition"),
            }
        else:  # "mavedb"
            ruo_clause = " (research-use-only calibration)" if record.get("research_use_only") else ""
            rationale = (
                f"MaveDB: a calibrated functional-assay score of {record.get('raw_score'):.3g} for this variant "
                f"({record.get('matched_hgvs')}) falls in the '{record.get('functional_classification')}' range "
                f"of score set {record.get('score_set_urn')}'s own investigator-provided calibration{ruo_clause}, "
                "consistent with " + ("a damaging" if code == "PS3" else "no damaging") + " effect."
            )
            supporting = [
                f"MaveDB {record.get('score_set_urn')}: score {record.get('raw_score'):.3g} "
                f"-> '{record.get('functional_classification')}'{ruo_clause}."
            ]
            evidence_sources = ["MaveDB"]
            confidence = "Low" if record.get("research_use_only") else "Moderate"
            details = {
                "matched_hgvs": record.get("matched_hgvs"),
                "raw_score": record.get("raw_score"),
                "score_set_urn": record.get("score_set_urn"),
                "functional_classification": record.get("functional_classification"),
                "research_use_only": record.get("research_use_only"),
                "publication": record.get("publication"),
            }

        return CriterionResult(
            code,
            direction,
            strength,
            "triggered",
            rationale,
            supporting_evidence=supporting,
            evidence_sources=evidence_sources,
            confidence=confidence,
            details=details,
        )

    @staticmethod
    def _clinvar_crossref(clinvar_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Uses `primary_record` (the allele-matched ClinVar record), never
        `records[0]` -- a bare first-record read used to attribute a
        co-located, unrelated variant's classification to the one under
        analysis whenever ClinVar's positional search returned more
        than one variant at the same locus (see
        `database/clinvar_client.py`'s module docstring for the real
        BRCA1 case this fixes).

        When ClinVar has *other*, non-matching variants at this exact
        position (`match_status == "position_only"`), they are surfaced
        here as explicit context -- clearly labelled as not this
        variant's own classification -- rather than silently discarded,
        so a reviewer can see why a nearby, differently-classified
        variant might appear in other tools' output for this locus.
        """
        if not clinvar_result:
            return {"available": False}

        if clinvar_result.get("match_status") == "matched" and clinvar_result.get("primary_record"):
            top = clinvar_result["primary_record"]
            return {
                "available": True,
                "clinical_significance": top.get("clinical_significance"),
                "review_status": top.get("review_status"),
                "accession": top.get("accession"),
                "note": "Provided for cross-reference only; not used as an input to this engine's own combining rules.",
            }

        if clinvar_result.get("match_status") == "position_only":
            others = [
                {
                    "accession": r.get("accession"),
                    "title": r.get("title"),
                    "clinical_significance": r.get("clinical_significance"),
                    "review_status": r.get("review_status"),
                }
                for r in (clinvar_result.get("records") or [])
            ]
            return {
                "available": False,
                "co_located_other_variants": others,
                "note": (
                    f"No ClinVar record found for this exact variant. {len(others)} other, non-matching "
                    "ClinVar-catalogued variant(s) exist at this genomic position (listed for context "
                    "only -- their classifications are not evidence about this variant)."
                ),
            }

        return {"available": False}

    # ------------------------------------------------------------------
    # Combining rules (Richards et al. 2015 point-based approximation)
    # ------------------------------------------------------------------

    @staticmethod
    def _combine(criteria: Dict[str, CriterionResult]) -> "tuple[str, List[str]]":
        trace: List[str] = []
        triggered = {code: c for code, c in criteria.items() if c.status == "triggered"}

        if "BA1" in triggered:
            trace.append("BA1 triggered (stand-alone benign) -> classification = Benign.")
            return "Benign", trace

        path_points = 0.0
        benign_points = 0.0
        for code, c in triggered.items():
            # BP6 is deliberately excluded from the point tally, same
            # rationale as `_clinvar_crossref` being kept out of these
            # combining rules entirely: it would let ClinVar's own
            # classification move GEPER's classification, the exact
            # circularity ClinGen SVI's 2018 PP5/BP6 deprecation warns
            # against (see `_bp6`'s docstring / `_BP6_DEPRECATION_CAVEAT`).
            # It is still reported in `all_criteria`/`triggered_criteria`
            # for transparency -- only excluded from scoring.
            if code == "BP6":
                trace.append(
                    "BP6 triggered, but excluded from point totals (ClinGen SVI 2018 deprecation -- see _bp6)."
                )
                continue
            pts = _POINTS.get(c.strength, 0)
            if c.direction == "pathogenic":
                path_points += pts
            elif c.direction == "benign" and code != "BA1":
                benign_points += pts
            trace.append(f"{code} triggered ({c.strength}, {c.direction}) contributes {pts} point(s).")

        net = path_points - benign_points
        trace.append(f"Pathogenic points = {path_points}, benign points = {benign_points}, net = {net}.")

        if benign_points >= 8:
            return "Benign", trace
        if benign_points >= 4 and path_points < 4:
            return "Likely Benign", trace
        if path_points >= 10:
            return "Pathogenic", trace
        if path_points >= 6:
            return "Likely Pathogenic", trace
        return "Uncertain Significance", trace
