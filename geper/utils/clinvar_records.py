"""
ClinVar record-type policy (ClinVar audit C2).

ClinVar stores more than single variants. An E-utilities `esummary` entry carries an
`obj_type` and a `variation_set` (its component variants). A haplotype, a diplotype or a
compound heterozygote is ONE record whose classification belongs to the combination; every
component is listed in its `variation_set`, so a component's genomic coordinates also "match"
that record. Treating such a record as the component's own classification attached a
haplotype's Likely pathogenic, or a diplotype's "drug response (practice guideline)", to a
single variant.

Evidence policy (approved), version `EVIDENCE_POLICY_VERSION`:

* ONLY a record that is (a) an explicitly recognised single-allele type, (b) made of exactly one
  component, (c) allele-matched to the queried variant and (d) carries an individual-variant
  classification is ELIGIBLE. Eligible records are the only ones allowed to feed ACMG/BP6,
  PS1/PM5, confidence, priority, the legacy summary and pathogenicity-conflict scoring.
* Every other record that contains the allele is CONTEXT ONLY: kept, labelled with its record
  type, shown to reviewers, never scored, never raising a pathogenicity flag.
* An unrecognised or missing `obj_type` is context only (fail-safe: a new NCBI type can never
  silently become evidence).
* "no classification for the single variant" is not a classification: it is never read as
  Benign, Pathogenic or Likely pathogenic, and such a record is context only.

This module is pure (no I/O) so the policy can be tested without the network. Definitions and
their official sources are in `geper/CLINVAR_RECORD_TYPES.md`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

EVIDENCE_POLICY_VERSION = "clinvar-single-allele-v1"

# Record roles. Only ROLE_VARIANT_RECORD can ever be eligible.
ROLE_VARIANT_RECORD = "variant_record"
ROLE_VARIANT_RECORD_UNCLASSIFIED = "variant_record_unclassified"
ROLE_STAR_ALLELE_RECORD = "star_allele_record"
ROLE_MULTI_VARIANT_RECORD = "multi_variant_record"
ROLE_UNRECOGNIZED_RECORD_TYPE = "unrecognized_record_type"

# ClinVar `obj_type` values observed live for single sequence changes (the XSD's SimpleAllele)
# that can be matched through a canonical SPDI. Deliberately explicit: anything not listed here
# (including the generic "Variation") is context only.
SINGLE_ALLELE_TYPES = frozenset(
    {
        "single nucleotide variant",
        "Deletion",
        "Duplication",
        "Insertion",
        "Indel",
        "Microsatellite",
        "Inversion",
        "Tandem duplication",
    }
)

# A haplotype defined by one variant (a pharmacogenomic star allele such as CYP2C19*23). It describes
# one allele, but as a haplotype and with a drug-response classification, so it is context only.
# Both spellings are values of the ClinVar XSD's `typeHaplotypeVariationType`.
STAR_ALLELE_TYPES = frozenset({"Haplotype, single variant", "Haplotype defined by a single variant"})

# Records whose classification belongs to a combination of variants (or whose components are
# not individually classified). "Genotype" is listed for completeness: it is the XSD element
# name and not an `obj_type` observed in esummary.
MULTI_VARIANT_TYPES = frozenset(
    {
        "Haplotype",
        "Diplotype",
        "Genotype",
        "CompoundHeterozygote",
        "Phase unknown",
        "Distinct chromosomes",
    }
)

# Classification texts that mean "this variant itself has no classification of its own".
NO_INDIVIDUAL_CLASSIFICATION = frozenset({"no classification for the single variant"})

_ROLE_LABELS = {
    ROLE_VARIANT_RECORD: "individual-variant record",
    ROLE_VARIANT_RECORD_UNCLASSIFIED: "individual-variant record with no classification of its own",
    ROLE_STAR_ALLELE_RECORD: "pharmacogenomic star-allele record",
    ROLE_MULTI_VARIANT_RECORD: "multi-variant record",
    ROLE_UNRECOGNIZED_RECORD_TYPE: "record of unrecognised type",
}


def as_text(value: Any) -> Optional[str]:
    """
    `value` when it is a string, otherwise None. ClinVar fields are strings; this keeps a malformed
    external value (a number, a list, an object) from raising inside the record-type policy. A
    non-string is treated exactly like a missing value, so it can never make a record eligible.
    """
    return value if isinstance(value, str) else None


def has_individual_classification(classification: Any) -> bool:
    """False for a missing/blank/non-text classification and for ClinVar's "no classification for the single variant"."""
    text = (as_text(classification) or "").strip().lower()
    return bool(text) and text not in NO_INDIVIDUAL_CLASSIFICATION


def classify_record_role(record_type: Any, component_count: Optional[int], classification: Any) -> str:
    """
    Role of one ClinVar record, from its `obj_type`, its number of components and its germline
    classification text. Allele matching is separate (see `ClinVarClient._variant_match`).
    A missing, non-text or unhashable `record_type` is unrecognised (context only).
    """
    record_type = as_text(record_type)
    if record_type in MULTI_VARIANT_TYPES:
        return ROLE_MULTI_VARIANT_RECORD
    if record_type in STAR_ALLELE_TYPES:
        return ROLE_STAR_ALLELE_RECORD
    if record_type in SINGLE_ALLELE_TYPES and component_count == 1:
        if has_individual_classification(classification):
            return ROLE_VARIANT_RECORD
        return ROLE_VARIANT_RECORD_UNCLASSIFIED
    return ROLE_UNRECOGNIZED_RECORD_TYPE


def is_single_allele_record(record_type: Any, component_count: Optional[int]) -> bool:
    """True for a recognised single-allele type with exactly one component, whatever its classification."""
    return as_text(record_type) in SINGLE_ALLELE_TYPES and component_count == 1


def unrecognized_record_reason(record_type: Any, component_count: Optional[int]) -> Optional[str]:
    """
    Why a record is in the unrecognised role, or None when its role is a recognised one. Used for the
    structured warning that names an unknown or missing ClinVar record type (C2 follow-up): the record
    stays context only, and the pipeline never fails because of it.
    """
    if record_type is None or (isinstance(record_type, str) and not record_type.strip()):
        return "missing_record_type"
    text = as_text(record_type)
    if text is None:
        return "non_text_record_type"
    if text in MULTI_VARIANT_TYPES or text in STAR_ALLELE_TYPES:
        return None
    if text in SINGLE_ALLELE_TYPES:
        return None if component_count == 1 else "unexpected_component_count"
    return "unrecognized_record_type"


def is_eligible(role: str, variant_match: Optional[bool]) -> bool:
    """A record is eligible individual-variant evidence only when its role is `variant_record` AND the allele matched."""
    return role == ROLE_VARIANT_RECORD and variant_match is True


def role_label(role: str, record_type: Optional[str] = None) -> str:
    """Human label for a role, naming the ClinVar record type where it adds information."""
    base = _ROLE_LABELS.get(role, _ROLE_LABELS[ROLE_UNRECOGNIZED_RECORD_TYPE])
    return f"{base} ({record_type})" if record_type and role != ROLE_VARIANT_RECORD else base


# ClinVar star ratings, used for DISPLAY ORDERING only (never to choose between conflicting
# classifications). Unknown statuses rank 0. Kept in step with `pipeline/ps1_pm5/models.py`
# (a test asserts the two agree).
_REVIEW_RANK = {
    "practice guideline": 4,
    "reviewed by expert panel": 3,
    "criteria provided, multiple submitters, no conflicts": 2,
    "criteria provided, single submitter": 1,
}


def review_rank(review_status: Any) -> int:
    return _REVIEW_RANK.get((as_text(review_status) or "").strip().lower(), 0)


def _evaluated(value: Any) -> datetime:
    text = as_text(value)
    if not text:
        return datetime.min
    try:
        return datetime.strptime(text.strip(), "%Y/%m/%d %H:%M")
    except ValueError:
        return datetime.min


def order_records(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deterministic DISPLAY order: higher review rank first, then later evaluation date, then
    accession. It never decides which of several conflicting classifications is "the" answer.
    """
    ranked = sorted(records, key=lambda r: str(r.get("accession") or ""))
    ranked.sort(key=lambda r: _evaluated(r.get("last_evaluated")), reverse=True)
    ranked.sort(key=lambda r: review_rank(r.get("review_status")), reverse=True)
    return ranked


def classifications_conflict(records: Iterable[Dict[str, Any]]) -> bool:
    """True when eligible records carry different classification texts (compared case-insensitively)."""
    texts = {(as_text(r.get("clinical_significance")) or "").strip().lower() for r in records}
    return len(texts) > 1


def summarize_context(context_records: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Count of context records by their ClinVar record type (for compact reviewer-facing summaries)."""
    counts: Dict[str, int] = {}
    for record in context_records:
        key = as_text(record.get("record_type")) or "unrecognised type"
        counts[key] = counts.get(key, 0) + 1
    return counts


def context_clause(clinvar_result: Optional[Dict[str, Any]]) -> str:
    """
    A sentence about context-only records for position-only/matched notes, or "" when there are none
    (so wording for results without context, and for results stored before this policy, is unchanged).
    """
    contexts = (clinvar_result or {}).get("context_records") or []
    if not contexts:
        return ""
    by_type = ", ".join(f"{k} x{v}" for k, v in sorted(summarize_context(contexts).items()))
    return (
        f" {len(contexts)} multi-variant, star-allele or unclassified ClinVar record(s) contain this allele "
        f"({by_type}) but are context only and are not evidence about this variant."
    )


def conflict_clause(clinvar_result: Optional[Dict[str, Any]]) -> str:
    """Wording for several eligible records with different classifications ("" when not in conflict)."""
    if not (clinvar_result or {}).get("individual_records_conflict"):
        return ""
    texts = sorted(
        {
            (as_text(r.get("clinical_significance")) or "unspecified")
            for r in (clinvar_result or {}).get("matched_records") or []
        }
    )
    return (
        f"ClinVar holds {len((clinvar_result or {}).get('matched_records') or [])} individual-variant records for this "
        f"variant with conflicting classifications ({'; '.join(texts)}); none is used as a single classification."
    )


def record_type_warning(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Structured warning for ONE parsed ClinVar record that contains the queried allele but whose record
    type is missing, unknown, or inconsistent with its component count -- None for every record whose
    type is recognised. Such a record stays context only (fail-safe); the warning names the accession
    and the type so a new NCBI record type is noticed instead of silently demoting evidence.
    """
    if record.get("record_type_malformed"):  # NCBI sent an `obj_type` that is not text (the client dropped it)
        reason: Optional[str] = "non_text_record_type"
    else:
        reason = unrecognized_record_reason(record.get("record_type"), record.get("component_count"))
    if reason is None or record.get("variant_match") is not True:
        return None
    return {
        "accession": record.get("accession"),
        "uid": record.get("uid"),
        "record_type": as_text(record.get("record_type")) or None,
        "component_count": record.get("component_count"),
        "reason": reason,
        "treated_as": "context_only",
    }


_WARNING_REASON_TEXT = {
    "missing_record_type": "has no record type",
    "non_text_record_type": "has a record type that is not text",
    "unrecognized_record_type": "has an unrecognised record type",
    "unexpected_component_count": "has an unexpected number of components for its record type",
}


def record_type_warning_message(warning: Dict[str, Any]) -> str:
    """One-line reviewer-facing text for a `record_type_warning` (shown with the variant's stage warnings)."""
    reason = _WARNING_REASON_TEXT.get(str(warning.get("reason")), "has an unexpected record type")
    type_text = f" '{warning['record_type']}'" if warning.get("record_type") else ""
    return (
        f"ClinVar record {warning.get('accession') or warning.get('uid') or '(no accession)'} {reason}{type_text}; "
        "kept as context only and not used as evidence for this variant."
    )


def reviewer_attention_text(clinvar_result: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    The explicit reviewer-attention statement for several eligible ClinVar records with conflicting
    classifications, or None when there is no such conflict. Display/attestation only -- it never feeds
    an ACMG criterion, a score or a threshold.
    """
    if not (clinvar_result or {}).get("individual_records_conflict"):
        return None
    matched = (clinvar_result or {}).get("matched_records") or []
    texts = sorted({(as_text(r.get("clinical_significance")) or "unspecified") for r in matched})
    return (
        f"ClinVar holds {len(matched)} individual-variant records for this exact variant with conflicting "
        f"classifications ({'; '.join(texts)}); none is used as a single classification and a reviewer must "
        "assess them."
    )
