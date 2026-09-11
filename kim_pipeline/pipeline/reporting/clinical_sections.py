"""
pipeline/reporting/clinical_sections.py
────────────────────────────────────────
Pure, dependency-light helper functions that build the *data* for the
new clinical-report sections (Issue 3): patient metadata, QC
PASS/WARNING indicators, the variant summary dashboard, a plain-language
clinical interpretation, and a merged gene/transcript/HGVS + ACMG
variant view.

Kept separate from pipeline/reporting/stage.py (which owns HTML/PDF
*rendering*) so each piece of report logic is independently unit
testable and so stage.py's diff for this feature stays reviewable.

Nothing here touches alignment, variant calling, annotation, or ACMG
classification logic — it only reads already-computed dicts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pipeline.annotation.stage import CONSEQUENCE_NOT_DETERMINED


# ─── What this report is ───────────────────────────────────────────────────────

# Ratified wording (2026-09-11, Option B): names THIS tree as a component of
# Bij AI rather than as the whole product. One constant, rendered by both the
# HTML report (stage.py) and the ReportLab PDF (pdf_report.py), so the two
# cannot drift. Clinician-facing text -- change only with human sign-off.
SCOPE_LINE = (
    "Sequencing analysis from FASTQ — produced by the Bij AI sequencing-analysis "
    "component (Kim): QC, alignment and variant calling, with ACMG classification "
    "of the variants called here."
)


# ─── Patient metadata ───────────────────────────────────────────────────────────

_DEIDENTIFIED_LABEL = "De-identified / Research Sample"


def normalize_patient_metadata(patient_metadata: Optional[Dict]) -> Dict[str, str]:
    """Normalize optional patient metadata (name/DOB/sex/physician) with
    a safe, explicit fallback when absent or incomplete.

    Never raises — malformed input (wrong type, missing keys) degrades
    to the de-identified fallback for the missing field(s) individually,
    not the whole block, so a partially-provided metadata dict (e.g.
    physician name known, patient anonymized) still renders sensibly.
    """
    meta = patient_metadata if isinstance(patient_metadata, dict) else {}

    def _field(key: str, empty_label: str = _DEIDENTIFIED_LABEL) -> str:
        val = meta.get(key)
        if val is None:
            return empty_label
        val_str = str(val).strip()
        return val_str if val_str else empty_label

    is_deidentified = not any(
        meta.get(k) not in (None, "") for k in ("name", "dob", "sex", "physician")
    )

    return {
        "name": _field("name"),
        "dob": _field("dob", "Not provided"),
        "sex": _field("sex", "Not provided"),
        "physician": _field("physician", "Not provided"),
        "is_deidentified": is_deidentified,
    }


# ─── QC status indicators ───────────────────────────────────────────────────────

# Defaults chosen to match commonly-used clinical WGS/WES thresholds;
# override via cfg["reporting"]["qc_thresholds"] if a lab has its own SOP.
_DEFAULT_QC_THRESHOLDS = {
    "mean_depth": {"pass_min": 20.0, "warn_min": 10.0},
    "pct_mapped": {"pass_min": 95.0, "warn_min": 90.0},
    "q30_fraction": {"pass_min": 0.80, "warn_min": 0.70},
}


def _status_for(value: Optional[float], thresholds: Dict[str, float]) -> str:
    if value is None:
        return "UNKNOWN"
    if value >= thresholds["pass_min"]:
        return "PASS"
    if value >= thresholds["warn_min"]:
        return "WARNING"
    return "FAIL"


def qc_status_summary(
    qc_d: Optional[Dict],
    align_d: Optional[Dict],
    thresholds: Optional[Dict] = None,
) -> List[Dict[str, Any]]:
    """Build the sequencing QC summary rows (coverage, read count,
    mapping rate, Q30) each with a PASS/WARNING/FAIL/UNKNOWN indicator.

    Accepts whatever shape ReportingStage.run() already receives for
    qc_summary/alignment_stats (flat dicts, matching FastqStats /
    AlignmentMetrics .to_dict() field names) — missing fields degrade to
    UNKNOWN rather than raising, since QC data is often partial in
    early-pipeline or VCF-only runs.
    """
    qc_d = qc_d or {}
    align_d = align_d or {}
    th = {**_DEFAULT_QC_THRESHOLDS, **(thresholds or {})}

    read_count = align_d.get("total_reads")
    if read_count is None:
        read_count = qc_d.get("total_records")

    mean_depth = align_d.get("mean_depth")
    pct_mapped = align_d.get("pct_mapped")
    q30_fraction = qc_d.get("q30_fraction")

    rows = [
        {
            "label": "Read Count",
            "value": f"{read_count:,}" if isinstance(read_count, (int, float)) else "N/A",
            "status": "PASS" if read_count else "UNKNOWN",
        },
        {
            "label": "Mean Coverage",
            "value": f"{mean_depth:.1f}x" if isinstance(mean_depth, (int, float)) else "N/A",
            "status": _status_for(mean_depth, th["mean_depth"]),
        },
        {
            "label": "Mapping Rate",
            "value": f"{pct_mapped:.2f}%" if isinstance(pct_mapped, (int, float)) else "N/A",
            "status": _status_for(pct_mapped, th["pct_mapped"]),
        },
        {
            "label": "Q30 Bases",
            "value": f"{q30_fraction * 100:.1f}%"
            if isinstance(q30_fraction, (int, float))
            else "N/A",
            "status": _status_for(q30_fraction, th["q30_fraction"]),
        },
    ]
    return rows


# ─── Variant summary dashboard ──────────────────────────────────────────────────

_CLASSIFICATION_LABELS = [
    "Pathogenic",
    "Likely_Pathogenic",
    "Uncertain_Significance",
    "Likely_Benign",
    "Benign",
]


def variant_dashboard(
    acmg_d: Optional[List[Dict]],
    ancestry_d: Optional[Dict],
) -> Dict[str, Any]:
    """Aggregate counts for the top-of-report dashboard: total variants,
    a count per ACMG tier, and a one-line ancestry summary."""
    acmg_d = acmg_d or []
    ancestry_d = ancestry_d or {}

    counts = {label: 0 for label in _CLASSIFICATION_LABELS}
    for item in acmg_d:
        cls = (item or {}).get("classification")
        if cls in counts:
            counts[cls] += 1

    ancestry_primary = ancestry_d.get("primary_population")
    ancestry_confidence = ancestry_d.get("confidence")
    if ancestry_primary:
        ancestry_summary = f"{ancestry_primary} (confidence: {ancestry_confidence or 'Unknown'})"
    else:
        ancestry_summary = "Not performed"

    return {
        "total_variants": len(acmg_d),
        "pathogenic": counts["Pathogenic"],
        "likely_pathogenic": counts["Likely_Pathogenic"],
        "vus": counts["Uncertain_Significance"],
        "likely_benign": counts["Likely_Benign"],
        "benign": counts["Benign"],
        "ancestry_summary": ancestry_summary,
    }


# ─── Clinical interpretation ────────────────────────────────────────────────────


def clinical_interpretation(dashboard: Dict[str, Any]) -> Dict[str, str]:
    """Plain-language summary + recommended follow-up, derived purely
    from the dashboard counts already computed above — this restates
    the ACMG classifier's own output in prose, it does not re-derive or
    override any classification.
    """
    actionable = dashboard.get("pathogenic", 0) + dashboard.get("likely_pathogenic", 0)
    vus = dashboard.get("vus", 0)
    total = dashboard.get("total_variants", 0)

    if total == 0:
        summary = "No variants met inclusion criteria for ACMG classification in this sample."
    elif actionable > 0:
        summary = (
            f"Of {total} classified variant(s), {actionable} were classified as "
            f"Pathogenic or Likely Pathogenic and may be clinically actionable. "
            f"{vus} variant(s) were of uncertain significance."
        )
    elif vus > 0:
        summary = (
            f"Of {total} classified variant(s), none reached Pathogenic or Likely "
            f"Pathogenic; {vus} were classified as Variants of Uncertain Significance."
        )
    else:
        summary = (
            f"All {total} classified variant(s) were classified as Benign or Likely "
            f"Benign — no clinically actionable findings in this analysis."
        )

    if actionable > 0:
        follow_up = (
            "Correlate Pathogenic/Likely Pathogenic finding(s) with the patient's "
            "clinical presentation and family history. Genetic counseling referral "
            "is recommended. Confirmatory orthogonal testing (e.g. Sanger sequencing) "
            "is recommended prior to clinical action, per standard laboratory practice."
        )
    elif vus > 0:
        follow_up = (
            "Variant(s) of Uncertain Significance do not currently support a clinical "
            "management change on their own. Consider periodic re-review as new "
            "evidence (ClinVar, literature) becomes available, and correlate with "
            "phenotype/family history if clinically indicated."
        )
    else:
        follow_up = (
            "No specific follow-up is indicated by this analysis alone. Clinical "
            "correlation remains at the ordering physician's discretion."
        )

    return {"summary": summary, "follow_up": follow_up}


# ─── Variant + ACMG merge ───────────────────────────────────────────────────────


def _variant_key(chrom: Any, pos: Any, ref: Any, alt: Any) -> str:
    return f"{str(chrom).lstrip('chr').upper()}:{pos}:{str(ref).upper()}:{str(alt).upper()}"


def merge_variants_with_acmg(
    variants: Optional[List[Dict]],
    acmg_d: Optional[List[Dict]],
) -> List[Dict]:
    """Join the annotation-stage variant list (gene/transcript/HGVS/
    consequence) with the ACMG evidence-batch results (classification/
    criteria/gnomAD/ClinVar) on (chrom, pos, ref, alt), so the clinical
    variant table can show both in one row without either producer
    needing to know about the other.

    Variants present in one list but not the other are still included
    (annotation-only or ACMG-only rows), never silently dropped.
    """
    variants = variants or []
    acmg_d = acmg_d or []

    acmg_by_key = {
        _variant_key(a.get("chrom"), a.get("pos"), a.get("ref"), a.get("alt")): a
        for a in acmg_d
        if a.get("chrom") is not None
    }
    seen_keys = set()
    merged: List[Dict] = []

    for v in variants:
        key = _variant_key(v.get("chrom"), v.get("pos"), v.get("ref"), v.get("alt"))
        seen_keys.add(key)
        row = dict(v)
        acmg_row = acmg_by_key.get(key)
        if acmg_row:
            row["acmg"] = acmg_row
        merged.append(row)

    # ACMG rows with no matching annotation entry (e.g. annotation was
    # skipped for that variant) still need to be visible in the report.
    for key, a in acmg_by_key.items():
        if key not in seen_keys:
            merged.append(
                {
                    "chrom": a.get("chrom"),
                    "pos": a.get("pos"),
                    "ref": a.get("ref"),
                    "alt": a.get("alt"),
                    "gene_name": a.get("gene"),
                    "acmg": a,
                }
            )

    return merged


# ─── Consequence display ────────────────────────────────────────────────────────


def consequence_display_label(raw: Optional[str]) -> str:
    """Map a stored consequence value to what a clinician reads.

    Explicit mapping, not a truthy fallback -- CONSEQUENCE_NOT_DETERMINED
    is a non-empty string and therefore truthy; `raw or "—"` would let it
    pass through unchanged, which is the exact defect this function
    exists to prevent.
    """
    if raw == CONSEQUENCE_NOT_DETERMINED:
        return "Not determined"
    return raw or "—"
