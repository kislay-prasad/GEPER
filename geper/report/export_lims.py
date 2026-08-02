"""
Generic, standards-adjacent LIMS/LIMS-adjacent export.

WHY THIS EXISTS, AND WHY IT ISN'T FHIR (yet) -- read before "fixing"
this by bolting on FHIR resources
---------------------------------------------------------------------
This is a genuinely separate export step (not baked into
`report/report_generator.py`/`report/summary.py`) because GEPER has no
real target hospital/LIMS spec to build against yet -- per the task
this module was built for, a pilot customer's actual format is still
unknown. Two options were considered:

  1. HL7 FHIR Genomics Reporting Implementation Guide (Observation/
     DiagnosticReport/MolecularSequence profiles).
  2. A plain, clearly-documented structured export (JSON + flattened
     CSV).

Researched (live, this session -- not assumed from training data) before
choosing:
  - The IG is at STU3 (v3.0.0, published 2024-12-12) -- "Standard for
    Trial Use", NOT Normative. Version 4.0.0 is already an active
    ci-build, meaning the spec itself is still moving.
  - The IG's own home page states plainly: "The Clinical Genomics
    Working Group understands that this guide is not complete, and
    implementers might identify additional concepts and data
    elements." This is not a stable target to build a one-shot mapping
    against.
  - Real-world adoption found: eMERGE and CSER (large US federally-
    funded research consortia), the "Sync for Genes" pilot program,
    and Molit (one small German non-profit, ~2 years in production).
    No evidence found of adoption by commercial/community diagnostic
    labs generally, and NONE found for India specifically -- this
    product's actual target market (see `report/summary.py`'s own
    DPDP Act framing).
  - Implementing it properly means LOINC codes for every genomic
    Observation type, HGVS-validated Coding elements, SNOMED CT terms,
    ClinVar/ClinGen CodeableConcepts, and nested Observation.component
    hierarchies across Variant/Genotype/DiagnosticImplication/
    MolecularConsequence profiles -- real engineering investment spent
    validating against a spec with no real target implementer to
    confirm it against.

Decision: option 2. A plain, exhaustively-documented export (this
module + `LIMS_EXPORT_MAPPING.md`) that is universally ingestable
(every LIMS can parse JSON/CSV; not every LIMS has FHIR tooling) and
uses standards-adjacent values wherever GEPER already produces them
for free (ISO 8601 timestamps, real HGVS nomenclature, verbatim
ACMG/AMP 2015 classification terms, real external accessions) so a
future FHIR crosswalk -- once a real target spec exists -- is a
mapping exercise against `LIMS_EXPORT_MAPPING.md`'s table, not a
rewrite of this module.

WHAT THIS DOES NOT DO
---------------------------------------------------------------------
- Never invents a value for a field GEPER didn't actually determine:
  every field below traces to a real key in `geper_results.json`
  (see `LIMS_EXPORT_MAPPING.md`) or is `None` -- the same honest-gap
  discipline as `pipeline/stage_schemas.py`'s NOT_RUN/ERROR/NOT_FOUND/
  FOUND distinction.
- Never re-runs the pipeline or re-derives evidence: this reads an
  already-written `geper_results.json` (or an equivalent in-memory
  document) and reshapes it. `build_lims_export()` takes the parsed
  JSON document directly, so it can be run standalone against an
  existing output file (see this module's `__main__` CLI) without any
  GEPER pipeline dependency at import time beyond `pydantic` (already
  a GEPER requirement -- see `pipeline/stage_schemas.py`, the same
  validation-at-a-boundary pattern this module follows).
"""

from __future__ import annotations

import csv
import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from report.summary import _derive_run_id, _derive_sample_id
from utils.logger import get_logger

logger = get_logger(__name__)

EXPORT_FORMAT_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Schema (Pydantic, matching pipeline/stage_schemas.py's validation-at-a-
# boundary convention). Every model is frozen -- this is a one-shot
# export snapshot, never mutated after construction.
# ---------------------------------------------------------------------------


class LIMSVariant(BaseModel):
    """Genomic coordinates and nomenclature -- see LIMS_EXPORT_MAPPING.md's "Variant identity" section for exact source keys."""

    model_config = ConfigDict(frozen=True)

    chromosome: Optional[str] = None
    position: Optional[int] = None
    reference_allele: Optional[str] = None
    alternate_allele: Optional[str] = None
    variant_type: Optional[str] = None
    vcf_id: Optional[str] = None
    vcf_filter_status: Optional[str] = None
    genomic_hgvs: Optional[str] = None  # HGVS.g -- always computed when normalization succeeded
    coding_hgvs: Optional[str] = None  # HGVS.c -- only when a transcript resolved for this variant
    protein_hgvs: Optional[str] = None  # HGVS.p -- see docstring on _protein_hgvs() below for the honest gap here
    protein_hgvs_source: Optional[str] = None  # "alphamissense_catalogue" | None -- see _protein_hgvs()


class LIMSGene(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: Optional[str] = None


class LIMSAcmgCriterion(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: Optional[str] = None
    strength: Optional[str] = None
    direction: Optional[str] = None


class LIMSClassification(BaseModel):
    """ACMG/AMP 2015 classification -- verbatim terms GEPER's own ACMGRuleEngine produces, never remapped to a different vocabulary here."""

    model_config = ConfigDict(frozen=True)

    acmg_classification: Optional[str] = (
        None  # "Pathogenic" | "Likely Pathogenic" | "Uncertain Significance" | "Likely Benign" | "Benign" | None
    )
    triggered_criteria: List[LIMSAcmgCriterion] = []
    not_evaluated_criteria_count: Optional[int] = None


class LIMSConfidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    pending: bool = True
    score: Optional[float] = None  # 0-100
    label: Optional[str] = None


class LIMSPriority(BaseModel):
    model_config = ConfigDict(frozen=True)

    pending: bool = True
    score: Optional[float] = None  # 0-100
    category: Optional[str] = None  # "Critical" | "High" | "Moderate" | "Low"
    rank: Optional[int] = None  # 1 = highest priority in this run's batch


class LIMSCasePrioritization(BaseModel):
    """
    Case-level, HPO-phenotype-driven ranking (pipeline/case_prioritization.py)
    -- deliberately a SEPARATE object from LIMSClassification above,
    never merged into it: this is a reviewer-triage signal, not an
    ACMG criterion, and must not be misread as one by a downstream
    LIMS (see that module's own docstring for the full separation
    rationale, which applies identically here).
    """

    model_config = ConfigDict(frozen=True)

    case_rank: Optional[int] = None
    case_rank_score: Optional[float] = None
    phenotype_match_score: Optional[float] = None  # 0-100; None if the gene has no HPO-curated data at all
    tier: Optional[str] = None  # "phenotype_matched" | "no_gene_hpo_data" | "not_scored"
    reason: Optional[str] = None


class LIMSPopulationFrequency(BaseModel):
    model_config = ConfigDict(frozen=True)

    gnomad_queried: bool = False
    gnomad_found: bool = False
    gnomad_global_af: Optional[float] = None
    dbsnp_rsid: Optional[str] = None


class LIMSClinicalDatabase(BaseModel):
    model_config = ConfigDict(frozen=True)

    clinvar_significance: Optional[str] = None
    clinvar_review_status: Optional[str] = None
    clingen_gene_validity: Optional[str] = None


class LIMSFinding(BaseModel):
    """One variant's full LIMS-facing record. `finding_number` is the
    1-based position in `geper_results.json`'s own `variants` list --
    stable across export runs against the same file, and matches the
    numbering GEPER's own PDF/Markdown reports already use for the
    same variant (see report/summary.py's "Finding N" sections), so a
    reviewer can cross-reference this export against those reports."""

    model_config = ConfigDict(frozen=True)

    finding_number: int
    variant: LIMSVariant
    gene: LIMSGene
    classification: LIMSClassification
    confidence: LIMSConfidence
    priority: LIMSPriority
    case_prioritization: Optional[LIMSCasePrioritization] = None  # None when the patient supplied no HPO terms this run
    population_frequency: LIMSPopulationFrequency
    clinical_database: LIMSClinicalDatabase
    supporting_evidence: List[str] = []
    conflicting_evidence: List[str] = []
    recommendations: List[str] = []
    evidence_sources: List[str] = []
    stage_errors: List[str] = []  # non-fatal per-stage failures recorded for this variant (variant_result["errors"])
    interpretation_available: bool = True  # False if interpretation_result was missing/errored for this variant -- every field above is then honestly empty/None, not fabricated


class LIMSRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    sample_id: str
    run_id: str
    input_vcf: Optional[str] = None
    reference_assembly: Optional[str] = None
    variant_count: Optional[int] = None
    geper_code_version: Optional[str] = None
    generated_at: Optional[str] = None  # ISO 8601, as GEPER's own document already stores it


class LIMSExport(BaseModel):
    """Top-level export document. `export_format`/`export_format_version` let a downstream LIMS (or a future migration script) detect which mapping revision produced a given file -- see LIMS_EXPORT_MAPPING.md's changelog note."""

    model_config = ConfigDict(frozen=True)

    export_format: str = "geper_lims_export"
    export_format_version: str = EXPORT_FORMAT_VERSION
    run: LIMSRun
    case_phenotype_ranking_active: (
        bool  # True iff at least one finding carries case_prioritization -- mirrors report_generator.py's own gate
    )
    findings: List[LIMSFinding]


# ---------------------------------------------------------------------------
# Mapping: geper_results.json -> LIMSExport. Every extractor below reads
# only keys build_variant_result()/InterpretationResult.to_dict()/
# build_clinical_report() already produce -- nothing here re-derives
# evidence. See LIMS_EXPORT_MAPPING.md for the field-by-field table this
# code implements.
# ---------------------------------------------------------------------------


def _protein_hgvs(variant_result: Dict[str, Any]) -> tuple:
    """
    HGVS.p (protein-level notation): GEPER's own `pipeline/hgvs_utils.py`
    supports computing this (`to_hgvs_p`/`to_hgvs_p_for_substitution`),
    but no orchestrator stage currently calls it for every variant --
    confirmed by grepping `pipeline/orchestrator.py` for both function
    names (zero call sites). The one place a real, traceable protein
    change DOES already flow into `geper_results.json` is AlphaMissense's
    own precomputed catalogue match (`alphamissense["protein_variant"]`,
    e.g. "p.Val600Glu") -- available only for missense variants
    AlphaMissense actually scored, sourced from AlphaMissense's own data,
    not independently computed by GEPER. Returns `(None, None)` for
    every other variant rather than fabricating a protein change GEPER
    never computed.
    """
    am = variant_result.get("alphamissense") or {}
    if am.get("found") and am.get("protein_variant"):
        return am["protein_variant"], "alphamissense_catalogue"
    return None, None


def _variant(variant_result: Dict[str, Any]) -> LIMSVariant:
    v = variant_result.get("variant") or {}
    norm = variant_result.get("normalization") or {}
    protein_hgvs, protein_hgvs_source = _protein_hgvs(variant_result)
    return LIMSVariant(
        chromosome=v.get("chrom"),
        position=v.get("pos"),
        reference_allele=v.get("ref"),
        alternate_allele=v.get("alt"),
        variant_type=v.get("variant_type"),
        vcf_id=v.get("id"),
        vcf_filter_status=v.get("filter"),
        genomic_hgvs=norm.get("hgvs_g"),
        coding_hgvs=norm.get("hgvs_c"),
        protein_hgvs=protein_hgvs,
        protein_hgvs_source=protein_hgvs_source,
    )


def _classification(clinical: Dict[str, Any]) -> LIMSClassification:
    acmg = clinical.get("acmg_classification") or {}
    return LIMSClassification(
        acmg_classification=acmg.get("classification"),
        triggered_criteria=[
            LIMSAcmgCriterion(code=c.get("code"), strength=c.get("strength"), direction=c.get("direction"))
            for c in (acmg.get("triggered_criteria") or [])
        ],
        not_evaluated_criteria_count=acmg.get("not_evaluated_count"),
    )


def _confidence(clinical: Dict[str, Any]) -> LIMSConfidence:
    c = clinical.get("confidence") or {}
    return LIMSConfidence(pending=c.get("pending", True), score=c.get("score"), label=c.get("label"))


def _priority(clinical: Dict[str, Any]) -> LIMSPriority:
    p = clinical.get("priority") or {}
    return LIMSPriority(
        pending=p.get("pending", True), score=p.get("score"), category=p.get("category"), rank=p.get("rank")
    )


def _case_prioritization(variant_result: Dict[str, Any]) -> Optional[LIMSCasePrioritization]:
    cp = variant_result.get("case_prioritization")
    if not isinstance(cp, dict):
        return None
    pm = cp.get("phenotype_match") or {}
    return LIMSCasePrioritization(
        case_rank=cp.get("case_rank"),
        case_rank_score=cp.get("case_rank_score"),
        phenotype_match_score=pm.get("score"),
        tier=cp.get("tier"),
        reason=cp.get("reason"),
    )


def _population_frequency(variant_result: Dict[str, Any]) -> LIMSPopulationFrequency:
    gnomad = variant_result.get("gnomad") or {}
    dbsnp = variant_result.get("dbsnp") or {}
    return LIMSPopulationFrequency(
        gnomad_queried=not (gnomad.get("skipped") or gnomad.get("error")),
        gnomad_found=bool(gnomad.get("found")),
        gnomad_global_af=gnomad.get("global_af"),
        dbsnp_rsid=dbsnp.get("rsid") if dbsnp.get("found") else None,
    )


def _clinical_database(variant_result: Dict[str, Any]) -> LIMSClinicalDatabase:
    clinvar = variant_result.get("clinvar") or {}
    clingen = variant_result.get("clingen") or {}
    primary = clinvar.get("primary_record") or {}
    clinvar_matched = clinvar.get("match_status") == "matched" and bool(primary)
    return LIMSClinicalDatabase(
        clinvar_significance=primary.get("clinical_significance") if clinvar_matched else None,
        clinvar_review_status=primary.get("review_status") if clinvar_matched else None,
        clingen_gene_validity=clingen.get("clinical_validity_summary") if clingen.get("found") else None,
    )


def _build_finding(finding_number: int, variant_result: Dict[str, Any]) -> LIMSFinding:
    ir = variant_result.get("interpretation_result")
    clinical = variant_result.get("clinical_report")
    interpretation_available = isinstance(ir, dict) and "error" not in ir and clinical is not None

    if not interpretation_available:
        # Honest empty record, not a fabricated one: this variant's
        # interpretation genuinely failed/is missing, so every
        # evidence-derived field below stays at its None/empty
        # default rather than guessing. `variant`/`gene` identity
        # (from raw stage dicts, independent of interpretation
        # success) are still populated -- a reviewer needs to know
        # WHICH variant failed, not just that one did.
        return LIMSFinding(
            finding_number=finding_number,
            variant=_variant(variant_result),
            gene=LIMSGene(symbol=(ir or {}).get("gene_symbol") if isinstance(ir, dict) else None),
            classification=LIMSClassification(),
            confidence=LIMSConfidence(),
            priority=LIMSPriority(),
            # NOT unconditionally None here: case_prioritization is its
            # own top-level key on variant_result (see
            # pipeline/orchestrator.py::_apply_case_phenotype_ranking),
            # independent of interpretation_result -- a variant whose
            # ACMG interpretation failed can still have a genuine
            # case_prioritization record (typically tier="not_scored",
            # since that engine also reads priority_score off the same
            # failed interpretation_result and honestly declines to
            # rank it -- see rank_case()'s own tier_c). Dropping it here
            # unconditionally would silently hide that honest signal.
            case_prioritization=_case_prioritization(variant_result),
            population_frequency=_population_frequency(variant_result),
            clinical_database=_clinical_database(variant_result),
            stage_errors=list(variant_result.get("errors") or []),
            interpretation_available=False,
        )

    return LIMSFinding(
        finding_number=finding_number,
        variant=_variant(variant_result),
        gene=LIMSGene(symbol=ir.get("gene_symbol")),
        classification=_classification(clinical),
        confidence=_confidence(clinical),
        priority=_priority(clinical),
        case_prioritization=_case_prioritization(variant_result),
        population_frequency=_population_frequency(variant_result),
        clinical_database=_clinical_database(variant_result),
        supporting_evidence=list(clinical.get("supporting_evidence") or []),
        conflicting_evidence=list(clinical.get("conflicting_evidence") or []),
        recommendations=list(clinical.get("recommendations") or []),
        evidence_sources=list(clinical.get("evidence_sources") or []),
        stage_errors=list(variant_result.get("errors") or []),
        interpretation_available=True,
    )


def build_lims_export(document: Dict[str, Any], run_id: Optional[str] = None) -> LIMSExport:
    """
    Maps a `geper_results.json` document (already-parsed dict -- the
    same shape `report/json_builder.py::JSONResultBuilder.build()`
    produces) into a validated `LIMSExport`. Pure function, no I/O --
    see `export_lims_json`/`export_lims_csv` below for the file-writing
    entry points, and this module's own `__main__` CLI for running
    this against an existing output file without re-running the
    pipeline.

    `run_id`: same optional override `report/summary.py::generate_pdf`
    accepts -- a real LIMS/hospital deployment should supply its own
    authoritative run/accession ID; falls back to the same derivation
    `_derive_run_id` already uses for the PDF report, so the two stay
    consistent when both are generated from the same document.
    """
    variants = document.get("variants") or []
    findings = [_build_finding(i, vr) for i, vr in enumerate(variants, start=1)]

    return LIMSExport(
        run=LIMSRun(
            sample_id=_derive_sample_id(document),
            run_id=_derive_run_id(document, run_id),
            input_vcf=document.get("input_vcf"),
            reference_assembly=document.get("assembly"),
            variant_count=document.get("variant_count"),
            geper_code_version=document.get("code_version"),
            generated_at=document.get("generated_at"),
        ),
        case_phenotype_ranking_active=any(f.case_prioritization is not None for f in findings),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------


def export_lims_json(document: Dict[str, Any], output_path: str, run_id: Optional[str] = None) -> str:
    export = build_lims_export(document, run_id=run_id)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(export.model_dump_json(indent=2))
    logger.info(f"Wrote LIMS JSON export to '{output_path}' ({len(export.findings)} finding(s)).")
    return output_path


# CSV columns, in export order -- one row per finding. Kept as an
# explicit module-level list (not derived from the Pydantic model
# reflectively) so the column order/names are a deliberate, documented
# contract a LIMS's CSV importer can hard-code against, matching
# LIMS_EXPORT_MAPPING.md's own table exactly.
_CSV_COLUMNS = [
    "sample_id",
    "run_id",
    "finding_number",
    "chromosome",
    "position",
    "reference_allele",
    "alternate_allele",
    "variant_type",
    "genomic_hgvs",
    "coding_hgvs",
    "protein_hgvs",
    "protein_hgvs_source",
    "gene_symbol",
    "acmg_classification",
    "triggered_acmg_criteria",
    "not_evaluated_criteria_count",
    "confidence_score",
    "confidence_label",
    "confidence_pending",
    "priority_score",
    "priority_category",
    "priority_rank",
    "priority_pending",
    "case_rank",
    "case_rank_score",
    "phenotype_match_score",
    "case_prioritization_tier",
    "gnomad_global_af",
    "gnomad_found",
    "dbsnp_rsid",
    "clinvar_significance",
    "clinvar_review_status",
    "clingen_gene_validity",
    "evidence_sources",
    "stage_errors",
    "interpretation_available",
]


def _csv_row(run: LIMSRun, finding: LIMSFinding) -> Dict[str, Any]:
    v, c, conf, p = finding.variant, finding.classification, finding.confidence, finding.priority
    cp = finding.case_prioritization
    pop, cdb = finding.population_frequency, finding.clinical_database
    return {
        "sample_id": run.sample_id,
        "run_id": run.run_id,
        "finding_number": finding.finding_number,
        "chromosome": v.chromosome,
        "position": v.position,
        "reference_allele": v.reference_allele,
        "alternate_allele": v.alternate_allele,
        "variant_type": v.variant_type,
        "genomic_hgvs": v.genomic_hgvs,
        "coding_hgvs": v.coding_hgvs,
        "protein_hgvs": v.protein_hgvs,
        "protein_hgvs_source": v.protein_hgvs_source,
        "gene_symbol": finding.gene.symbol,
        "acmg_classification": c.acmg_classification,
        "triggered_acmg_criteria": ";".join(crit.code for crit in c.triggered_criteria if crit.code),
        "not_evaluated_criteria_count": c.not_evaluated_criteria_count,
        "confidence_score": conf.score,
        "confidence_label": conf.label,
        "confidence_pending": conf.pending,
        "priority_score": p.score,
        "priority_category": p.category,
        "priority_rank": p.rank,
        "priority_pending": p.pending,
        "case_rank": cp.case_rank if cp else None,
        "case_rank_score": cp.case_rank_score if cp else None,
        "phenotype_match_score": cp.phenotype_match_score if cp else None,
        "case_prioritization_tier": cp.tier if cp else None,
        "gnomad_global_af": pop.gnomad_global_af,
        "gnomad_found": pop.gnomad_found,
        "dbsnp_rsid": pop.dbsnp_rsid,
        "clinvar_significance": cdb.clinvar_significance,
        "clinvar_review_status": cdb.clinvar_review_status,
        "clingen_gene_validity": cdb.clingen_gene_validity,
        "evidence_sources": ";".join(finding.evidence_sources),
        "stage_errors": ";".join(finding.stage_errors),
        "interpretation_available": finding.interpretation_available,
    }


def export_lims_csv(document: Dict[str, Any], output_path: str, run_id: Optional[str] = None) -> str:
    """
    Flattened, one-row-per-variant CSV -- for LIMS/lab-management
    systems that ingest tabular/spreadsheet data rather than nested
    JSON (common for smaller diagnostic labs without an HL7/FHIR
    interface). List-valued fields (evidence sources, ACMG criteria
    codes, stage errors) are semicolon-joined single cells rather than
    exploded into repeated rows, so this stays exactly one row per
    finding -- matching `LIMS_EXPORT_MAPPING.md`'s documented contract.
    """
    export = build_lims_export(document, run_id=run_id)
    with open(output_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for finding in export.findings:
            writer.writerow(_csv_row(export.run, finding))
    logger.info(f"Wrote LIMS CSV export to '{output_path}' ({len(export.findings)} finding(s)).")
    return output_path


# ---------------------------------------------------------------------------
# Standalone CLI -- runs against an existing geper_results.json, no
# pipeline dependency (see module docstring, point 3: "genuinely
# separate export step").
# ---------------------------------------------------------------------------


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Export an existing GEPER geper_results.json to a generic LIMS-facing format (JSON and/or CSV). "
        "See LIMS_EXPORT_MAPPING.md for the full field-by-field mapping this implements.",
    )
    parser.add_argument("results_json", help="Path to an existing geper_results.json (from a prior GEPER run).")
    parser.add_argument("--json", dest="json_out", default=None, help="Output path for the LIMS JSON export.")
    parser.add_argument("--csv", dest="csv_out", default=None, help="Output path for the flattened LIMS CSV export.")
    parser.add_argument(
        "--run-id", dest="run_id", default=None, help="Override the derived run/accession ID (see LIMSRun.run_id)."
    )
    args = parser.parse_args()

    if not args.json_out and not args.csv_out:
        parser.error("at least one of --json or --csv must be given")

    with open(args.results_json, "r", encoding="utf-8") as fh:
        document = json.load(fh)

    if args.json_out:
        export_lims_json(document, args.json_out, run_id=args.run_id)
    if args.csv_out:
        export_lims_csv(document, args.csv_out, run_id=args.run_id)


if __name__ == "__main__":
    _main()
