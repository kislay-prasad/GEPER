"""
Phase 5 -- Clinical Report Upgrade.

Builds one structured, clinician-oriented "clinical report" per variant
from the canonical `InterpretationResult` dict (Phase 2), including the
confidence (Phase 3) and prioritization (Phase 4) data already attached
to it. Every section below reads a field `InterpretationResult` already
computed -- nothing here re-derives evidence from raw provider dicts a
second time (the one exception, `raw_evidence`, is itself a field
`InterpretationResult` already carries -- see interpretation_result.py),
and nothing is invented that GEPER doesn't actually have.

Single source of truth: both `report/json_builder.py` (adds this dict
as an additive `clinical_report` key) and `report/report_generator.py`
(renders this same dict as Markdown) call `build_clinical_report()` --
so the two output formats can never drift out of sync, per the Phase 5
requirement to "reuse InterpretationResult instead of rebuilding logic
repeatedly."
"""

from typing import Any, Dict, List, Optional

from config import CONFIG

# Which evidence-combining system GEPER actually applies to turn
# triggered ACMG/AMP criteria into a final classification -- stated
# explicitly and identically everywhere the classification itself
# appears (report header, per-finding line, methodology/limitations),
# so a reviewing geneticist never has to guess. `pipeline/acmg_rules.py
# ::ACMGRuleEngine._combine` uses Tavtigian et al. 2018's Bayesian-
# calibrated point system (point weights 8/4/2/1 for very_strong/
# strong/moderate/supporting, thresholded on net pathogenic-minus-
# benign points) -- NOT Richards et al. 2015's original categorical
# combining table, which the unqualified phrase "ACMG/AMP
# Classification" would otherwise imply. The two systems do not always
# agree: PVS1 (very_strong) plus one Moderate criterion alone reaches
# Pathogenic under Tavtigian's net-points thresholds (8+2=10) but only
# Likely Pathogenic under Richards' categorical table (which requires
# a second Moderate, or a Moderate plus a Supporting, alongside a
# Very Strong to reach Pathogenic). See `_combine`'s own docstring
# comment for the full citation and reasoning.
ACMG_METHODOLOGY_STATEMENT = (
    "ACMG/AMP criteria are combined into a final classification using the Bayesian-calibrated point "
    'system of Tavtigian SV et al., "Modeling the ACMG/AMP variant classification guidelines as a '
    'Bayesian classification framework", Genet Med 2018 (PMID 29300386) -- not the original Richards '
    "et al. 2015 categorical combining table (PMID 25741868). The two can disagree on some evidence "
    "combinations (e.g. one top-tier-strength criterion such as PVS1 plus one Moderate criterion "
    "alone reaches Pathogenic under this point system's thresholds, but only Likely Pathogenic under "
    "the 2015 categorical table)."
)

# Display-only label for the net-points band a classification landed
# in -- report review round 10, so a reviewer can see why a variant got
# its classification without recomputing `ACMGRuleEngine._combine`'s
# thresholds by hand. Keyed off the classification string itself
# (already the authoritative output of `_combine`'s own threshold
# comparisons), never a second, independent re-implementation of those
# thresholds -- that would risk drifting from `_combine`'s real
# `>= 10` / `>= 6` / `<= -1` / `<= -7` comparisons (`pipeline/
# acmg_rules.py`) the way the discarded net ever could have. "Benign"
# has no band label since BA1's stand-alone short-circuit never runs
# the point tally at all (see `CombineResult`'s docstring) -- there is
# no band to name for that classification.
_ACMG_NET_POINTS_BAND_LABEL = {
    "Pathogenic": "≥10",  # >=10
    "Likely Pathogenic": "6–9",  # 6-9 (en dash)
    "Uncertain Significance": "0–5",  # 0-5
    "Likely Benign": "≤−1",  # <=-1
}


def acmg_net_points_band_label(classification: Optional[str]) -> Optional[str]:
    """The Tavtigian net-points band (e.g. "≥10") for `classification`,
    or `None` when there is no band to show (no classification yet, or
    Benign via BA1's stand-alone short-circuit -- see this module's
    `_ACMG_NET_POINTS_BAND_LABEL` comment)."""
    return _ACMG_NET_POINTS_BAND_LABEL.get(classification or "")


# Single source of truth for the clarifying caption shown next to every
# place `ConfidenceEngine.score()`'s output is displayed (full PDF,
# short PDF, Markdown) -- C2, report review round 2: this score
# measures evidence *completeness*, not classification *certainty*
# ("Pathogenic / Low (25%)" previously read as doubt about the call).
# Renamed from "Confidence" to "Evidence Completeness" everywhere the
# label itself appears; this exact sentence is the shared substring
# every renderer includes verbatim, so the caption can never drift out
# of sync the way independently-worded footnotes would.
EVIDENCE_COMPLETENESS_CAPTION = (
    "Evidence Completeness reflects how much evidence GEPER could gather for this variant, not how "
    "certain the classification is."
)

# Stable, well-known public-resource references for whichever sources
# actually contributed evidence to this variant (via `evidence_sources`,
# already computed by Phase 2 -- not re-derived here). These are fixed
# citations to the resource itself, not fabricated per-variant claims.
#
# "HPO" is included here (not as a separate unconditional citation the
# way Orphanet is below) because PP4 genuinely tags `evidence_sources
# =["HPO"]` whenever it evaluates at all (`pipeline/acmg_rules.py
# ::ACMGRuleEngine._pp4`, both the "triggered" and "not_triggered"
# branches) -- so this dict's existing conditional-on-evidence_sources
# mechanism already does the right thing for HPO with no other change
# needed. Citation text follows HPO's own citation guidance
# (obophenotype.github.io/human-phenotype-ontology/community/cite/,
# confirmed live 2026-08-08: "we recommend citing the most recent
# article" -- currently Gargano et al. 2024, NAR).
_REFERENCES = {
    "ClinVar": "ClinVar -- https://www.ncbi.nlm.nih.gov/clinvar/",
    "dbSNP": "dbSNP -- https://www.ncbi.nlm.nih.gov/snp/",
    "gnomAD": "gnomAD -- https://gnomad.broadinstitute.org/",
    "ClinGen": "ClinGen -- https://clinicalgenome.org/",
    "UniProt": "UniProt -- https://www.uniprot.org/",
    "InterPro": "InterPro/Pfam -- https://www.ebi.ac.uk/interpro/",
    "AlphaFold DB": "AlphaFold Protein Structure Database -- https://alphafold.ebi.ac.uk/",
    "AlphaMissense": "AlphaMissense -- Cheng et al. 2023, Science (DeepMind/AlphaMissense)",
    "MMSplice": "MMSplice -- Cheng et al. 2019, Genome Biology",
    "BLAST": "NCBI BLAST -- https://blast.ncbi.nlm.nih.gov/",
    "HPO": "Human Phenotype Ontology (HPO) -- Gargano et al. 2024, Nucleic Acids Research",
    "protein_translator": "GEPER internal reference/alternate protein translation (standard genetic code).",
}

# Orphanet/Orphadata (CC-BY-4.0) is a genuine exception to the
# conditional-on-evidence_sources mechanism above: GEPER's pipeline
# fetches and caches Orphanet's gene-disorder association file every
# run (`pipeline/orphanet/bootstrap.py`, live-verified working in
# `DATA_PROVENANCE.md`), but as of 2026-08-08 that data is not wired
# into any ACMG criterion or otherwise surfaced in the report body --
# it never becomes an `evidence_sources` entry, so it could never earn
# a place in `_REFERENCES` above no matter how many reports were
# generated. Since CC-BY-4.0 requires attribution wherever GEPER's
# overall product incorporates the dataset -- not only in the specific
# runs where a criterion happened to consult it -- this is cited
# unconditionally instead, in every report, rather than silently never
# citing a source the pipeline genuinely integrates. (See
# `DATA_SOURCE_LICENSE_AUDIT.md`'s HPO/Orphanet entries for the
# original finding and this decision.) Citation text follows Orphanet's
# own recommended format for its Orphadata products
# (orphadata.com/legal-notice/, confirmed live 2026-08-08): "Orphadata
# Products: Data and services from Orphanet. (c) INSERM 1999."
_ORPHANET_REFERENCE = (
    "Orphanet/Orphadata -- https://www.orphadata.com/ "
    "(Orphadata Products: Data and services from Orphanet, (c) INSERM 1999)"
)

_TOOL_LIMITATIONS = (
    "This report is generated by an automated research pipeline combining public database "
    "lookups, curated clinical resources, and machine-learning predictors. It is not a "
    "substitute for professional clinical genetic interpretation, diagnosis, or advice, and "
    "should be reviewed by a qualified clinical geneticist or genetic counselor before any "
    "medical decision is made.",
    ACMG_METHODOLOGY_STATEMENT,
    "Protein-residue positions used for domain (InterPro/Pfam) and structural (AlphaFold DB) "
    "overlap checks are transcript-verified (computed from the variant's real, strand- and "
    "splice-aware coding-sequence mapping, not a flat unspliced translation window) but only "
    "when the resolved transcript is that gene's MANE Select or Ensembl-canonical isoform -- "
    "InterPro/UniProt/AlphaFold DB coordinates are only meaningful against the canonical "
    "isoform. When the resolved transcript is a different one, or no transcript could be "
    "resolved at all, no residue position is reported and the corresponding domain/structure "
    "criteria are marked not evaluated rather than shown against a mismatched coordinate.",
    "PS3/BS3 functional-evidence lookups query the ClinGen Evidence Repository (ERepo) first; "
    "MaveDB is consulted only as a fallback, and only for a variant ERepo has no record for. "
    "The two sources are never queried together and never cross-checked against each other for "
    "the same variant -- a PS3/BS3 call always comes from a single source, not a combination.",
)


def build_clinical_report(
    interpretation_result: Optional[Dict[str, Any]],
    variant_dict: Dict[str, Any] = None,
    raw_evidence: Optional[Dict[str, Any]] = None,
    indigenomes_result: Optional[Dict[str, Any]] = None,
    thousand_genomes_sas_result: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Returns the 17-section clinical report dict, or `None` if
    `interpretation_result` is missing/errored (e.g. the aggregation
    engine failed for this variant) -- callers must not fabricate a
    report when the underlying data isn't there.

    `raw_evidence`: the raw per-stage provider dicts (uniprot, interpro,
    alphafold, gnomad, dbsnp, clinvar, clingen, blast, ...) this
    variant's evidence-dependent sections (protein/structural/
    population/clinical knowledge, sequence context) are built from.

    Pass this explicitly whenever the caller has the live provider
    dicts in hand (as `report/json_builder.py::build_variant_result`
    now does -- see that module) rather than relying on
    `interpretation_result["raw_evidence"]`: `InterpretationResult
    .to_dict()` (`pipeline/interpretation_result.py`) deliberately
    OMITS `raw_evidence` from its serialized output (to avoid
    duplicating data already emitted under the top-level `uniprot`/
    `interpro`/`gnomad`/etc. keys), so `interpretation_result` here --
    which is that *serialized* dict, not the live dataclass instance --
    never actually carries it. Before this parameter existed, every
    caller silently fell through to `ir.get("raw_evidence") or {}`,
    which was *always* an empty dict, so every one of this function's
    raw-evidence-derived sections (protein/structural/population/
    clinical knowledge, sequence context) unconditionally reported
    "not found"/"no annotation available" regardless of what the
    corresponding stage had actually returned -- a real bug (found via
    a live Colab run where the report claimed "UniProt: no entry
    resolved" in this section while the same report's own Annotation
    Detail trail, built from the actual stage output, showed a
    resolved accession two sections later). Kept optional and
    falling back to the old (broken-if-empty) behavior only so this
    isn't a breaking API change for any other future caller.

    `indigenomes_result`: VESTIGIAL as of 2026-08-08 -- IndiGenomes was
    retired from GEPER's active query path entirely (its own terms
    restrict commercial use, which GEPER has not licensed; see
    `config.py::IndiGenomesConfig`'s docstring and
    `DATA_SOURCE_LICENSE_AUDIT.md`). Still accepted here, purely so
    every existing caller (`report/json_builder.py`, this module's own
    tests) keeps working without a signature-breaking change, but it is
    no longer read or passed to `_indian_population_frequency` below --
    there is no longer an "IndiGenomes vs. fallback" branch to feed. If
    IndiGenomes is ever reinstated, restore the argument's use here
    alongside `pipeline/orchestrator.py::_run_indigenomes_stage`.

    `thousand_genomes_sas_result`: the raw 1000 Genomes SAS provider
    dict (see `annotation/thousand_genomes_sas.py
    ::ThousandGenomesSASLookup.query_variant`) -- the SOLE source for
    the `indian_population_frequency` section below as of 2026-08-08
    (previously an IndiGenomes fallback, shown only when IndiGenomes
    was confirmed offline; now shown unconditionally). Passed as its
    own explicit parameter, NOT folded into `raw_evidence` -- that
    dict's validated shape (`pipeline/stage_schemas.py::RawEvidenceBundle`)
    has a fixed 11 fields and this is not one of them (an India-
    deployment-only source, unlike gnomAD/ClinVar/etc.); adding an
    unlisted key there would raise a `TypeError` at the schema boundary
    rather than degrade gracefully.
    """
    if not interpretation_result or "error" in interpretation_result:
        return None
    ir = interpretation_result
    raw = raw_evidence if raw_evidence is not None else (ir.get("raw_evidence") or {})

    return {
        "executive_summary": _executive_summary(ir, variant_dict),
        "acmg_classification": _acmg_section(ir),
        "confidence": _confidence_section(ir),
        "priority": _priority_section(ir),
        "supporting_evidence": ir.get("supporting_evidence", []),
        "conflicting_evidence": _conflicting_evidence(ir),
        "conflict_resolution": _conflict_resolution_section(ir),
        "explainability": ir.get("explainability"),
        "ai_consensus": _ai_consensus_section(ir),
        "protein_knowledge": _protein_knowledge(raw),
        "structural_knowledge": _structural_knowledge(raw),
        "population_evidence": _population_evidence(raw),
        "indian_population_frequency": _indian_population_frequency(raw, thousand_genomes_sas_result),
        "clinical_evidence": _clinical_evidence(raw),
        "sequence_context": _sequence_context(ir, raw),
        "recommendations": ir.get("recommendations", []),
        "limitations": _limitations(ir),
        "references": _references(ir),
        "evidence_sources": ir.get("evidence_sources", []),
    }


# ----------------------------------------------------------------------
# Section builders. Each reads only fields InterpretationResult already
# computed (Phase 1-4) -- no new scoring, classification, or evidence
# derivation happens here.
# ----------------------------------------------------------------------


def _executive_summary(ir: Dict[str, Any], variant_dict: Dict[str, Any] = None) -> str:
    variant_dict = variant_dict or ir.get("variant") or {}
    locus = f"{variant_dict.get('chrom')}:{variant_dict.get('pos')} {variant_dict.get('ref')}>{variant_dict.get('alt')}"
    gene = ir.get("gene_symbol")
    gene_clause = f" in {gene}" if gene else ""

    classification = ir.get("acmg_classification") or "not classified"
    conf_label = ir.get("confidence_label")
    conf_clause = (
        f" (evidence completeness: {conf_label})"
        if conf_label and not ir.get("confidence_pending", True)
        else " (evidence completeness not yet scored)"
    )

    priority_cat = ir.get("priority_category")
    priority_clause = (
        f" Assigned {priority_cat} review priority." if priority_cat and not ir.get("priority_pending", True) else ""
    )

    # Full accounting, not just two of the three buckets (C3, report
    # review round 2): a reader who saw only "N triggered" (here) and
    # "M could not be evaluated" (in Limitations) naturally added the
    # two expecting a total -- but that omits `not_triggered_rules`
    # (checked, and did not trigger), so the two numbers alone never
    # summed to the real 28-criterion total. Every one of the 28
    # ACMG/AMP criteria `pipeline/acmg_rules.py::ACMGRuleEngine.evaluate`
    # evaluates lands in exactly one of these three buckets -- this is
    # a structural partition of a fixed 28-entry set (`_STRENGTH`), not
    # something that can silently lose or double-count a criterion --
    # so the three numbers below always sum to the fourth.
    n_triggered = len(ir.get("triggered_rules", []))
    n_not_triggered = len(ir.get("not_triggered_rules", []))
    n_not_evaluated = len(ir.get("not_evaluated_rules", []))
    n_total = n_triggered + n_not_triggered + n_not_evaluated
    evidence_clause = (
        f" Of {n_total} ACMG/AMP criteria evaluated: {n_triggered} triggered, {n_not_triggered} checked but "
        f"not triggered, {n_not_evaluated} could not be evaluated due to missing data sources."
    )

    return (
        f"Variant {locus}{gene_clause} was classified as **{classification}**{conf_clause}."
        f"{priority_clause}{evidence_clause}"
    )


def _acmg_section(ir: Dict[str, Any]) -> Dict[str, Any]:
    # `triggered_rules` and `not_triggered_rules` are rendered here in
    # the same shape so every one of the 28 ACMG/AMP criteria this
    # engine evaluates is accounted for somewhere in the report -- a
    # criterion that was checked and came back negative (e.g. BP1 not
    # triggered because an opposing missense predictor fired) used to
    # be silently absent from every report section (not in the
    # triggered table, not in `_limitations`'s not-evaluated-codes
    # footnote either, since that only lists `not_evaluated_rules`),
    # which read identically to that criterion never having been
    # checked at all.
    return {
        "classification": ir.get("acmg_classification"),
        "triggered_criteria": [
            {
                "code": c.get("code"),
                "strength": c.get("strength"),
                "direction": c.get("direction"),
                "rationale": c.get("rationale"),
                # PVS1's decision-tree audit trail (decision_path/
                # caveats_checked/unchecked_caveats) -- carried through
                # here so it can actually be rendered (C5, report
                # review round 2): the rationale text already refers a
                # reader to "the decision tree" by name, but until now
                # that tree's own path was computed and then dropped at
                # this exact mapping, never reaching any report format.
                "details": c.get("details"),
            }
            for c in ir.get("triggered_rules", [])
        ],
        "not_triggered_criteria": [
            {
                "code": c.get("code"),
                "strength": c.get("strength"),
                "direction": c.get("direction"),
                "rationale": c.get("rationale"),
                "details": c.get("details"),
            }
            for c in ir.get("not_triggered_rules", [])
        ],
        "combining_rule_trace": ir.get("combining_rule_trace", []),
        "not_evaluated_count": len(ir.get("not_evaluated_rules", [])),
        # Report review round 10: the real Tavtigian point totals next
        # to the classification they actually decided -- see
        # `InterpretationResult.acmg_net_points`'s docstring and
        # `acmg_net_points_band_label` above. `None` on all three only
        # when BA1's stand-alone-benign short-circuit fired (no point
        # tally was run), never a fabricated `0`.
        "net_points": ir.get("acmg_net_points"),
        "pathogenic_points": ir.get("acmg_pathogenic_points"),
        "benign_points": ir.get("acmg_benign_points"),
        "net_points_band": acmg_net_points_band_label(ir.get("acmg_classification")),
    }


def _confidence_section(ir: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "pending": ir.get("confidence_pending", True),
        "score": ir.get("confidence_score"),
        "label": ir.get("confidence_label"),
        "breakdown": ir.get("confidence_breakdown"),
    }


def _priority_section(ir: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "pending": ir.get("priority_pending", True),
        "score": ir.get("priority_score"),
        "category": ir.get("priority_category"),
        "rank": ir.get("priority_rank"),
        "explanation": ir.get("priority_explanation", []),
        "breakdown": ir.get("priority_breakdown"),
    }


def _conflicting_evidence(ir: Dict[str, Any]) -> List[str]:
    conflicts = ir.get("conflicting_evidence", [])
    return conflicts  # already a deduped, sourced list from Phase 2; empty list if none -- never fabricated


def _conflict_resolution_section(ir: Dict[str, Any]) -> Dict[str, Any]:
    # Phase 6, added as a new top-level section rather than changing the
    # shape of the existing `conflicting_evidence` key above (which
    # Phase 5 already shipped as a plain list) -- keeps that key
    # backward compatible while still surfacing the richer, resolved
    # conflict data from the Conflict Resolution Engine.
    return {
        "summary": ir.get("conflict_summary"),
        "score": ir.get("conflict_score"),
        "severity": ir.get("conflict_severity"),
        "resolution": ir.get("conflict_resolution"),
        "conflicts": ir.get("conflict_list", []),
    }


def _ai_consensus_section(ir: Dict[str, Any]) -> Dict[str, Any]:
    """
    `model_errors` distinguishes "this model genuinely crashed" from
    "this model was never applicable/available" -- an absent entry in
    `classifying_models` alone is ambiguous between the two (a
    synonymous variant never routed to AlphaMissense looks identical
    to an AlphaMissense that crashed, from `classifying_models` alone).
    Same distinction `_protein_knowledge`/`_structural_knowledge`/
    `_clinical_evidence`'s `*_error` fields already make for UniProt/
    InterPro/AlphaFold/ClinVar/ClinGen -- see
    `pipeline/interpretation_result.py::InterpretationResult
    .ai_model_errors`'s docstring for where this is computed.
    """
    return {
        "classifying_models": ir.get("ai_consensus", []),  # AlphaMissense / MMSplice verdicts only
        "context_models_used": ir.get("ai_context_models", []),  # HyenaDNA/Evo2/RNA-FM/ESM2, no verdict
        "model_errors": ir.get("ai_model_errors", []),
    }


def _protein_knowledge(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    `uniprot_error`/`interpro_error` are surfaced as their own field,
    distinct from `*_available` -- a failed external lookup (e.g. the
    EBI InterPro API returning an error after retries) is not the same
    statement as "this protein genuinely has no annotation", and
    conflating the two would misreport an external-service outage as a
    negative biological finding. Matches the distinction
    `report/report_generator.py::_render_uniprot`/`_render_interpro`
    (the Annotation Detail audit trail) already make for the same
    provider dicts -- this brings the Clinical Interpretation Report's
    summary section into agreement with it.
    """
    uniprot = raw.get("uniprot") or {}
    interpro = raw.get("interpro") or {}
    out: Dict[str, Any] = {
        "uniprot_available": False,
        "uniprot_error": uniprot.get("error"),
        "interpro_available": False,
        "interpro_error": interpro.get("error"),
    }
    if uniprot.get("found"):
        out["uniprot_available"] = True
        out["uniprot"] = {
            "accession": uniprot.get("accession"),
            "protein_name": uniprot.get("protein_name"),
            "reviewed": uniprot.get("reviewed"),
        }
    if interpro.get("found"):
        out["interpro_available"] = True
        out["interpro"] = {
            "protein_position": interpro.get("protein_position"),
            # `affected_domains` stays three-valued here too (None =
            # position unknown, domain overlap never checked; [] =
            # checked, no overlap; non-empty = overlap found) -- see
            # `pipeline/interpro/lookup.py::query_variant`'s
            # docstring. `.get(..., [])`'s default only applies when
            # the key is absent, so an explicit `None` value passes
            # through unchanged rather than being silently coerced.
            "affected_domains": interpro.get("affected_domains", []),
            "domain_overlap": bool(interpro.get("affected_domains")),
        }
    return out


def _structural_knowledge(raw: Dict[str, Any]) -> Dict[str, Any]:
    """`error` distinguishes a failed AlphaFold DB lookup from a
    genuine "no structure resolved" result -- see `_protein_knowledge`'s
    docstring for the same distinction and why it matters."""
    alphafold = raw.get("alphafold") or {}
    if not alphafold.get("found"):
        return {"available": False, "error": alphafold.get("error")}
    # `confidence_band`/`confidence_band_is_residue_specific` distinguish
    # "pLDDT at this variant's own residue" from a silent fallback to
    # the whole-protein mean when the residue position is unknown --
    # both are legitimate numbers, but reporting one under a label that
    # doesn't say which would let a reader assume residue-specificity
    # that isn't there.
    residue_band = alphafold.get("affected_residue_band")
    return {
        "available": True,
        "confidence_band": residue_band or alphafold.get("mean_plddt_band"),
        "confidence_band_is_residue_specific": residue_band is not None,
        "protein_position": alphafold.get("protein_position"),
        "mean_plddt": alphafold.get("mean_plddt"),
        "affected_residue_plddt": alphafold.get("affected_residue_plddt"),
        "model_version": alphafold.get("model_version"),
        "pdb_url": alphafold.get("pdb_url"),
        "cif_url": alphafold.get("cif_url"),
        "protein_position_basis": alphafold.get("protein_position_basis"),
    }


def _population_evidence(raw: Dict[str, Any]) -> Dict[str, Any]:
    gnomad = raw.get("gnomad") or {}
    dbsnp = raw.get("dbsnp") or {}
    return {
        "gnomad": {
            "queried": not (gnomad.get("skipped") or gnomad.get("error")),
            "found": bool(gnomad.get("found")),
            "global_af": gnomad.get("global_af"),
        },
        "dbsnp": {
            "found": bool(dbsnp.get("found")),
            "rsid": dbsnp.get("rsid") if dbsnp.get("found") else None,
        },
    }


def _indian_population_frequency(
    raw: Dict[str, Any],
    thousand_genomes_sas_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    India-deployment feature: gnomAD's South Asian (SAS) subpopulation
    allele frequency alongside the 1000 Genomes Project's SAS
    sub-population frequency (see `annotation/thousand_genomes_sas.py`'s
    module docstring), plus a single "common in Indian populations"
    flag when either exceeds `CONFIG.indigenomes.COMMON_AF_THRESHOLD`
    (default 1%).

    AS OF 2026-08-08, 1000 Genomes SAS is the SOLE Indian/South-Asian
    cohort source this section shows, run unconditionally for every
    variant -- see `annotation/thousand_genomes_sas.py`'s module
    docstring and `config.py::ThousandGenomesSASConfig`'s docstring for
    why: IndiGenomes (the source this originally sat alongside, and
    which this was built as an offline-fallback for) was retired from
    GEPER's active query path entirely, because its own terms restrict
    commercial use and GEPER has not obtained a license (see
    `DATA_SOURCE_LICENSE_AUDIT.md`). There is therefore no longer an
    "IndiGenomes vs. 1000 Genomes SAS fallback" branch here -- unlike
    the prior version of this function, `sas_*` below is simply THE
    result whenever this section has anything to show, the same way any
    other single-source report field is rendered, not something gated
    on another source's confirmed-offline state.

    The two mandatory disclosures (`SAMPLE_SIZE_DISCLOSURE`/
    `DIASPORA_DISCLOSURE`, see `annotation/thousand_genomes_sas.py`)
    matter more now than when this was an occasional fallback -- they
    must appear every time this section shows anything from this
    source, not conditionally. This function doesn't render them
    itself (that's `report/report_generator.py`'s and
    `report/summary.py`'s job, both importing the same shared text so
    Markdown and PDF never drift on wording), but `sas_shown` below is
    unconditionally true whenever there's a gnomAD-SAS or 1000-Genomes-
    SAS figure to report, so the renderer always has a reason to
    include them.

    `gnomad_af_sas` is read from `raw["gnomad"]["population_breakdown"]`
    (already computed by the gnomAD stage -- see
    `pipeline/gnomad/models.py::POPULATIONS`), not re-queried here.
    `sas_population_labels`/`sas_sample_sizes`/`sas_total_sample_size`
    are carried through explicitly (not left for the renderer to
    hardcode or, worse, omit) so every render of this source --
    Markdown and both PDF paths -- states both mandatory disclosures
    the same way, from the same source of truth. Deliberately NOT
    folded into `common_in_indian_population` below: `sas_pooled`'s AF
    is a small, diaspora-sourced figure (see
    `annotation/thousand_genomes_sas.py`'s disclosures) and mixing it
    into the same threshold check as gnomAD's much larger SAS reference
    population would let a genuinely low-confidence number quietly
    influence a clinical flag on equal footing with a better-powered
    one -- so the flag is driven by gnomAD SAS alone now (previously
    gnomAD SAS + IndiGenomes; IndiGenomes is gone, and 1000 Genomes SAS
    was never eligible for this flag for the same reason).
    """
    gnomad = raw.get("gnomad") or {}
    gnomad_sas = (gnomad.get("population_breakdown") or {}).get("sas") or {}
    gnomad_sas_af = gnomad_sas.get("af")

    threshold = CONFIG.indigenomes.COMMON_AF_THRESHOLD
    common_in_indian_population = gnomad_sas_af is not None and gnomad_sas_af >= threshold

    tgs = thousand_genomes_sas_result or {}
    sas_available = bool(tgs.get("found"))
    # True whenever the 1000 Genomes SAS stage actually ran this
    # variant (`pipeline/orchestrator.py::_run_thousand_genomes_sas_stage`,
    # unconditional as of 2026-08-08) -- i.e. whenever a caller passed a
    # real result dict at all, found/not-found/errored alike. Only
    # False in a test/caller that passes `None` outright (the stage
    # genuinely wasn't invoked), mirroring how `fallback_shown` used to
    # gate on "was IndiGenomes confirmed offline" -- now it gates on
    # "did this stage run", which in real pipeline operation is always.
    sas_shown = thousand_genomes_sas_result is not None

    return {
        "gnomad_af_sas": gnomad_sas_af,
        "gnomad_sas_queried": not (gnomad.get("skipped") or gnomad.get("error")),
        "common_af_threshold": threshold,
        "common_in_indian_population": common_in_indian_population,
        "sas_shown": sas_shown,
        "sas_available": sas_available,
        "sas_error": tgs.get("error") if sas_shown else None,
        "sas_rsid": tgs.get("rsid") if sas_shown else None,
        "sas_pooled": tgs.get("sas_pooled") if sas_available else None,
        "sas_sub_populations": tgs.get("sub_populations") if sas_available else None,
        "sas_population_labels": tgs.get("population_labels") if sas_available else None,
        "sas_sample_sizes": tgs.get("sample_sizes") if sas_available else None,
        "sas_total_sample_size": tgs.get("total_sample_size") if sas_available else None,
    }


def _clinical_evidence(raw: Dict[str, Any]) -> Dict[str, Any]:
    """`clinvar_error`/`clingen_error` distinguish a failed lookup from
    a genuine "no record"/"no curation" result -- see `_protein_knowledge`'s
    docstring for the same distinction and why it matters."""
    clinvar = raw.get("clinvar") or {}
    clingen = raw.get("clingen") or {}
    out: Dict[str, Any] = {
        "clinvar_available": False,
        "clinvar_error": clinvar.get("error"),
        "clingen_available": False,
        "clingen_error": clingen.get("error"),
    }
    # `primary_record`, never `records[0]` -- ClinVar's positional
    # search can return several distinct co-located variants at one
    # locus, and a bare first-record read used to attribute whichever
    # one sorted first to this variant regardless of whether it was
    # actually the same allele (see `database/clinvar_client.py`'s
    # module docstring for the real case this fixes). `match_status ==
    # "position_only"` means other, non-matching variants exist at
    # this position -- surfaced as count-only context, distinct from
    # "no record found" (`not_found`).
    if clinvar.get("match_status") == "matched" and clinvar.get("primary_record"):
        primary = clinvar["primary_record"]
        out["clinvar_available"] = True
        out["clinvar"] = {
            "clinical_significance": primary.get("clinical_significance"),
            "review_status": primary.get("review_status"),
        }
    elif clinvar.get("match_status") == "position_only":
        out["clinvar_co_located_count"] = len(clinvar.get("records") or [])
    if clingen.get("found"):
        out["clingen_available"] = True
        out["clingen"] = {
            "gene_symbol": clingen.get("gene_symbol"),
            "clinical_validity_summary": clingen.get("clinical_validity_summary"),
            "dosage_sensitivity": clingen.get("dosage_sensitivity"),
        }
    return out


def _sequence_context(ir: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    `blast["error"]` distinguishes a failed BLAST search from a
    genuine "no homology hits" result -- see `_protein_knowledge`'s
    docstring for the same distinction elsewhere. Unlike UniProt/
    InterPro/AlphaFold/ClinVar/ClinGen, BLAST previously had NO
    mechanism anywhere in this report to make that distinction (see
    `pipeline/orchestrator.py::_run_blast_stage`).
    """
    blast = raw.get("blast") or {}
    return {
        "context_models_used": ir.get("ai_context_models", []),
        "blast": {
            "hit_count": blast.get("hit_count", 0),
            "error": blast.get("error"),
        },
        # Ensembl is used internally for sequence-context retrieval
        # (transcript/region lookups feeding the DNA/RNA models above)
        # rather than surfaced as its own per-variant evidence dict, so
        # it is noted here descriptively rather than scored -- same
        # treatment as in the Phase 3/4 engines.
        "ensembl_note": (
            "Ensembl contributes indirectly via sequence-context retrieval (transcript/region "
            "lookups feeding the sequence-context models above); it is not surfaced as a separate "
            "per-variant evidence source in this pipeline."
        ),
    }


def _limitations(ir: Dict[str, Any]) -> List[str]:
    limitations = list(_TOOL_LIMITATIONS)
    # Built per-finding from `ai_context_models` (what this variant's
    # DNA-model router actually ran, per `pipeline/orchestrator.py`'s
    # `dna_models_used`, plus RNA-FM/ESM2 when applicable -- see
    # `interpretation_result.py`), never a fixed claim that all four of
    # HyenaDNA/Evo2/RNA-FM/ESM2 ran for every variant, which previously
    # contradicted findings whose own evidence line named only one.
    context_models = ir.get("ai_context_models") or []
    if context_models:
        limitations.append(
            f"Sequence-context model(s) actually used for this variant: {', '.join(context_models)}. "
            "These contribute routing and embedding context in this pipeline; they do not themselves "
            "output a per-variant pathogenic/benign verdict, and are reported separately from "
            "AlphaMissense/MMSplice for that reason."
        )
    else:
        limitations.append(
            "No sequence-context model (HyenaDNA, Evo2, RNA-FM, ESM2) produced a result for this variant."
        )
    not_evaluated = ir.get("not_evaluated_rules", [])
    if not_evaluated:
        codes = ", ".join(c.get("code", "?") for c in not_evaluated)
        n_total = len(ir.get("triggered_rules", [])) + len(ir.get("not_triggered_rules", [])) + len(not_evaluated)
        limitations.append(
            f"{len(not_evaluated)} of {n_total} ACMG criteria could not be evaluated for this variant due "
            f"to missing evidence sources ({codes}); see the ACMG classification section (both the "
            f"triggered and the checked-but-not-triggered tables) for the remaining criteria and the "
            f"specific reason each not-evaluated one was skipped."
        )
    if ir.get("confidence_pending", True):
        limitations.append("Confidence scoring did not complete for this variant.")
    if ir.get("priority_pending", True):
        limitations.append("Priority scoring did not complete for this variant.")
    return limitations


def _references(ir: Dict[str, Any]) -> List[str]:
    sources = ir.get("evidence_sources", [])
    references = [_REFERENCES[s] for s in sources if s in _REFERENCES]
    # Orphanet is cited unconditionally, not gated on `evidence_sources`
    # -- see `_ORPHANET_REFERENCE`'s own comment for why. Appended once,
    # after the conditional entries, rather than merged into
    # `_REFERENCES` itself, so that dict's docstring-documented
    # "only what actually contributed to this variant" contract stays
    # true for every other entry in it.
    references.append(_ORPHANET_REFERENCE)
    return references
