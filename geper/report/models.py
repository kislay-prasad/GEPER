"""
Report models and content verification for clinical reports.

Content verification (ISO 15189 7.5 nonconformance detection):
- Hash computation: deterministic serialization of clinical content
- Hash storage: captured at approval time
- Hash verification: re-computed at export time
- Verification failure: HARD STOP (export refused, audit entry written)

Clinical content includes: findings, classifications, interpretations, clinical metadata
Clinical content excludes: timestamps, file paths, review status (workflow state only)

Hash versions (ClinVar audit C2 follow-up). The digest is always 64 hex characters; WHICH content it covers is
recorded next to it, as `content_hash_version` in the document (the sign-off manifest binds the document, version
included, through `results_json_sha256`; its own key set is unchanged):

* version 1 (LEGACY, and what a document with no `content_hash_version` means): the content exactly as it was
  before the ClinVar record-type policy. Unchanged byte for byte, so every report approved before the policy
  still verifies; nothing is rewritten.
* version 2 (written by every new approval): version 1's content PLUS a per-variant ClinVar attestation --
  evidence-policy version, match status, whether the query failed, the eligible individual-variant records, any
  conflict between them, the context-only records, the non-matching co-located records and the record-type
  warnings -- plus the context-only data sources. Context records still never affect ACMG scoring; they are
  attested so that reviewer-visible ClinVar information cannot change after approval without invalidating it.

An unknown version never verifies.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional


def _serialize_clinical_content(obj: Any) -> str:
    """
    Deterministically serialize an object to JSON for hashing.

    Ensures identical clinical content always produces identical hash:
    - Sorted dictionary keys (no variation across Python versions or dict ordering)
    - Fixed separators (no ambiguous delimiters)
    - UTF-8 encoding (explicit, no BOM)
    - Consistent float formatting (max 6 decimal places, strip trailing zeros)
    - Canonical representation of None/True/False

    Returns the JSON string ready for hashing.
    """

    def _normalize(val: Any) -> Any:
        """Normalize values for deterministic serialization."""
        if val is None or isinstance(val, (bool, int, str)):
            return val
        if isinstance(val, float):
            # Format with 6 decimal places, strip trailing zeros, then parse back
            # to ensure consistent representation (e.g., 1.5 == 1.500000 after strip)
            formatted = f"{val:.6f}".rstrip("0").rstrip(".")
            # Return as string to preserve exact representation in JSON
            try:
                # If it looks like a number, return as float for JSON
                return float(formatted) if "." in formatted or "e" in formatted.lower() else int(formatted)
            except (ValueError, TypeError):
                return val
        if isinstance(val, dict):
            return {k: _normalize(v) for k, v in sorted(val.items())}
        if isinstance(val, (list, tuple)):
            return [_normalize(item) for item in val]
        return val

    normalized = _normalize(obj)
    return json.dumps(normalized, separators=(",", ":"), ensure_ascii=True, sort_keys=True)


LEGACY_CONTENT_HASH_VERSION = 1
CONTENT_HASH_VERSION = 2  # written by every new approval
SUPPORTED_CONTENT_HASH_VERSIONS = (LEGACY_CONTENT_HASH_VERSION, CONTENT_HASH_VERSION)

# Fields of a ClinVar record that a report shows or that identify the evidence behind it.
_ATTESTED_CLINVAR_RECORD_FIELDS = (
    "uid",
    "accession",
    "title",
    "record_type",
    "component_count",
    "record_role",
    "eligible_for_variant_classification",
    "variant_match",
    "clinical_significance",
    "review_status",
    "last_evaluated",
    "condition",
)


def content_hash_version_of(document: Dict[str, Any]) -> Optional[int]:
    """
    The hash version a document declares: `content_hash_version`, or the legacy version 1 when the document
    has none (everything approved before versioning). None for a declared version this code does not know.
    """
    declared = document.get("content_hash_version") if isinstance(document, dict) else None
    if declared is None:
        return LEGACY_CONTENT_HASH_VERSION
    if isinstance(declared, bool) or not isinstance(declared, int) or declared not in SUPPORTED_CONTENT_HASH_VERSIONS:
        return None
    return declared


def _attested_clinvar_record(record: Any) -> Dict[str, Any]:
    if not isinstance(record, dict):
        return {"unreadable_record": True}
    return {field: record.get(field) for field in _ATTESTED_CLINVAR_RECORD_FIELDS}


def _attested_clinvar_records(records: Any) -> list:
    """A list of records as order-independent attestations (sorted by accession, then uid, then content)."""
    attested = [_attested_clinvar_record(r) for r in (records if isinstance(records, list) else [])]
    return sorted(
        attested,
        key=lambda a: (str(a.get("accession")), str(a.get("uid")), _serialize_clinical_content(a)),
    )


def _clinvar_attestation(variant_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Version-2 ClinVar content for one variant: everything a reviewer is shown from ClinVar. Built from the
    stored `clinvar` block only (never re-derived), so it is the same on every recomputation.
    """
    clinvar = variant_result.get("clinvar")
    clinvar = clinvar if isinstance(clinvar, dict) else {}
    primary = clinvar.get("primary_record")
    records = clinvar.get("records")
    warnings = clinvar.get("record_type_warnings")
    return {
        "evidence_policy": clinvar.get("evidence_policy"),
        "match_status": clinvar.get("match_status"),
        "query_failed": bool(clinvar.get("error")),
        "individual_records_conflict": bool(clinvar.get("individual_records_conflict")),
        "primary_accession": primary.get("accession") if isinstance(primary, dict) else None,
        "eligible_records": _attested_clinvar_records(clinvar.get("matched_records")),
        "context_records": _attested_clinvar_records(clinvar.get("context_records")),
        "non_matching_records": _attested_clinvar_records(
            [
                r
                for r in (records if isinstance(records, list) else [])
                if isinstance(r, dict) and r.get("variant_match") is not True
            ]
        ),
        "record_type_warnings": sorted(
            (
                {k: w.get(k) for k in ("accession", "uid", "record_type", "component_count", "reason")}
                for w in (warnings if isinstance(warnings, list) else [])
                if isinstance(w, dict)
            ),
            key=lambda w: (str(w.get("accession")), str(w.get("uid"))),
        ),
    }


def _extract_clinical_content(document: Dict[str, Any], version: int = LEGACY_CONTENT_HASH_VERSION) -> Dict[str, Any]:
    """
    Extract clinical content from a report document for hashing.

    INCLUDES:
    - variants (all fields: chrom, pos, ref, alt)
    - normalization results
    - gnomad, dbsnp, clinvar, clingen, alphamissense data
    - interpretation_result and candidate_interpretation (clinical reports)
    - acmg_classification, confidence, priority
    - case_prioritization
    - Evidence and recommendations
    - conflict_resolution
    - overrides (clinician classification changes)
    - version 2 only: the per-variant ClinVar attestation and the context-only data sources (see module docstring)

    EXCLUDES:
    - timestamps (generated_at, reviewed_at, reviewed_by, created, exported, etc.)
    - file paths (input_vcf, output_dir)
    - review_status (workflow state, not clinical content)
    - vcf_samples (patient identifier, not clinical classification)
    - variant_count (redundant, computed from variants array length)
    - assembly (could include, but excluded from hash to allow re-exports with different builds)
    - Any audit/administrative fields
    """
    clinical_content: Dict[str, Any] = {"variants": []}
    if version >= 2:
        # Domain separation: a version-2 digest can never equal a version-1 digest of the same document.
        clinical_content["content_hash_schema"] = f"geper-content-v{version}"

    # Extract variants - only clinical interpretation, not metadata about the processing
    for variant_result in document.get("variants", []):
        # Extract the essential clinical fields from each variant
        clinical_variant = {}

        # Variant coordinates (these don't change, are essential for identification)
        if "variant" in variant_result:
            variant = variant_result["variant"]
            clinical_variant["variant"] = {
                k: v for k, v in variant.items() if k in ("chrom", "pos", "ref", "alt", "id", "variant_type")
            }

        # Normalization: only keep the actual HGVS results, not processing metadata
        if "normalization" in variant_result:
            norm = variant_result["normalization"]
            clinical_variant["normalization_hgvs_g"] = norm.get("hgvs_g")
            clinical_variant["normalization_hgvs_c"] = norm.get("hgvs_c")

        # Population frequencies
        if "gnomad" in variant_result:
            gnomad = variant_result["gnomad"]
            clinical_variant["gnomad"] = {k: v for k, v in gnomad.items() if k in ("found", "global_af")}

        if "dbsnp" in variant_result:
            dbsnp = variant_result["dbsnp"]
            clinical_variant["dbsnp"] = {k: v for k, v in dbsnp.items() if k in ("found", "rsid")}

        # ClinVar clinical significance
        if "clinvar" in variant_result:
            clinvar = variant_result["clinvar"] or {}
            if "primary_record" in clinvar:
                # `primary_record` is None whenever ClinVar has no individual-variant record for this
                # variant (not_found, position_only, context-only, conflicting). `None.get(...)` here
                # made the content hash -- and so sign-off approval -- raise for such a variant; the C2
                # record-type policy makes that case more common (a variant that only sits inside a
                # haplotype or diplotype now has no primary record). Hashes of documents whose primary
                # record is present are unchanged.
                primary = clinvar["primary_record"] or {}
                clinical_variant["clinvar"] = {
                    "clinical_significance": primary.get("clinical_significance"),
                    "review_status": primary.get("review_status"),
                }

        # ClinGen gene validity
        if "clingen" in variant_result:
            clingen = variant_result["clingen"]
            clinical_variant["clingen"] = {
                k: v for k, v in clingen.items() if k in ("found", "gene_symbol", "clinical_validity_summary")
            }

        # AlphaMissense predictions
        if "alphamissense" in variant_result:
            alphamissense = variant_result["alphamissense"]
            clinical_variant["alphamissense"] = {
                k: v for k, v in alphamissense.items() if k in ("found", "am_class", "am_pathogenicity")
            }

        # Interpretation results (gene symbol, ACMG classification)
        if "interpretation_result" in variant_result:
            interp = variant_result["interpretation_result"]
            clinical_variant["interpretation_result"] = {
                k: v for k, v in interp.items() if k in ("gene_symbol", "acmg_classification")
            }

        # Clinical report (candidate_interpretation or clinical_report alias)
        # This is the key clinical decision point
        clinical_report = variant_result.get("candidate_interpretation") or variant_result.get("clinical_report")
        if clinical_report:
            cr = {}

            # ACMG classification and criteria
            if "acmg_classification" in clinical_report:
                acmg = clinical_report["acmg_classification"]
                cr["acmg_classification"] = {
                    "classification": acmg.get("classification"),
                    "triggered_criteria": [
                        {"code": c.get("code"), "strength": c.get("strength"), "direction": c.get("direction")}
                        for c in acmg.get("triggered_criteria", [])
                    ],
                    "not_evaluated_count": acmg.get("not_evaluated_count"),
                }

                # Clinician override (if present)
                if "clinician_override" in acmg:
                    override = acmg["clinician_override"]
                    cr["clinician_override"] = {
                        "original_classification": override.get("original_classification"),
                        "new_classification": override.get("new_classification"),
                        "reason": override.get("reason"),
                    }

            # Confidence assessment
            if "confidence" in clinical_report:
                conf = clinical_report["confidence"]
                cr["confidence"] = {
                    "score": conf.get("score"),
                    "label": conf.get("label"),
                    "pending": conf.get("pending", True),
                }

            # Priority assessment
            if "priority" in clinical_report:
                priority = clinical_report["priority"]
                cr["priority"] = {
                    "score": priority.get("score"),
                    "category": priority.get("category"),
                    "rank": priority.get("rank"),
                    "pending": priority.get("pending", True),
                }

            # Evidence and recommendations
            if "supporting_evidence" in clinical_report:
                cr["supporting_evidence"] = clinical_report["supporting_evidence"]
            if "conflicting_evidence" in clinical_report:
                cr["conflicting_evidence"] = clinical_report["conflicting_evidence"]
            if "recommendations" in clinical_report:
                cr["recommendations"] = clinical_report["recommendations"]
            if "evidence_sources" in clinical_report:
                cr["evidence_sources"] = clinical_report["evidence_sources"]
            if version >= 2:
                cr["context_data_sources"] = clinical_report.get("context_data_sources") or []

            # Conflict resolution (Phase 6 feature)
            if "conflict_resolution" in clinical_report:
                conflict = clinical_report["conflict_resolution"]
                cr["conflict_resolution"] = {
                    "severity": conflict.get("severity"),
                    "description": conflict.get("description"),
                }

            clinical_variant["clinical_report"] = cr

        # Case prioritization (HPO-based ranking)
        if "case_prioritization" in variant_result:
            case_prio = variant_result["case_prioritization"]
            clinical_variant["case_prioritization"] = {
                "case_rank": case_prio.get("case_rank"),
                "case_rank_score": case_prio.get("case_rank_score"),
                "phenotype_match_score": (
                    case_prio.get("phenotype_match", {}).get("score")
                    if isinstance(case_prio.get("phenotype_match"), dict)
                    else case_prio.get("phenotype_match_score")
                ),
                "tier": case_prio.get("tier"),
                "reason": case_prio.get("reason"),
            }

        # All overrides (historical record of clinician changes)
        if "overrides" in variant_result and variant_result["overrides"]:
            clinical_variant["overrides"] = [
                {
                    "original_classification": o.get("original_classification"),
                    "new_classification": o.get("new_classification"),
                    "reason": o.get("reason"),
                }
                for o in variant_result["overrides"]
            ]

        if version >= 2:
            clinical_variant["clinvar_attestation"] = _clinvar_attestation(variant_result)

        clinical_content["variants"].append(clinical_variant)

    # Include run-level clinical information
    # Caveats are clinical warnings that affect interpretation
    if "caveats" in document:
        clinical_content["caveats"] = document["caveats"]

    # QC metrics if present (these affect clinical validity)
    if "qc_metrics" in document:
        clinical_content["qc_metrics"] = document["qc_metrics"]

    # Assembly matters for variant interpretation
    if "assembly" in document:
        clinical_content["assembly"] = document["assembly"]

    return clinical_content


def compute_content_hash(document: Dict[str, Any], version: Optional[int] = None) -> str:
    """
    Compute a deterministic SHA-256 hash of clinical content.

    This hash:
    - Is computed from clinical content only (findings, classifications, interpretations)
    - Excludes workflow state (review_status, timestamps, file paths)
    - Must be identical when run multiple times on the same report
    - Must change if ANY clinical content is modified

    The hash is stored at approval time and re-verified at export time.
    Mismatch indicates the report's clinical content changed after approval.

    Args:
        document: The geper_results.json document dict
        version: the hash version to compute (see the module docstring). Default: the version the document
            declares in `content_hash_version`, or the legacy version 1 when it declares none -- so a
            document approved before versioning is hashed exactly as it always was. `approve()` sets
            `content_hash_version` to `CONTENT_HASH_VERSION` before it computes the hash it stores.

    Returns:
        The SHA-256 hex digest (64 hex characters)

    Raises:
        ValueError: for a version this code does not know.
    """
    resolved = content_hash_version_of(document) if version is None else version
    if resolved not in SUPPORTED_CONTENT_HASH_VERSIONS:
        raise ValueError(f"Unsupported content hash version: {document.get('content_hash_version')!r}")
    clinical_content = _extract_clinical_content(document, resolved)
    serialized = _serialize_clinical_content(clinical_content)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return digest


def verify_content_hash(document: Dict[str, Any], stored_hash: Optional[str], version: Optional[int] = None) -> bool:
    """
    Verify that a document's clinical content matches its stored hash.

    The hash is recomputed with the version the document declares (`content_hash_version`; none means the
    legacy version 1), so a report approved before versioning still verifies and a version-2 report is
    checked against its ClinVar attestation. A declared version this code does not know never verifies
    (fail closed). Nothing is rewritten.

    Args:
        document: The geper_results.json document dict
        stored_hash: The hash stored at approval time (from document["content_hash"])
        version: override the version to verify against (tests/tools); default as above

    Returns:
        True if the content hash matches, False otherwise

    Raises:
        SignoffError if content hash verification fails (via calling code)
    """
    if not stored_hash:
        # No hash stored - this shouldn't happen on an approved report
        return False

    resolved = content_hash_version_of(document) if version is None else version
    if resolved not in SUPPORTED_CONTENT_HASH_VERSIONS:
        return False
    current_hash = compute_content_hash(document, resolved)
    return current_hash == stored_hash
