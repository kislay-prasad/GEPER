"""
pipeline/reporting/stage.py
─────────────────────────────
ReportingStage — produces one sample-level clinical report (Task 6).

Aggregates:
  * QC summary (from FastqStats or a pre-computed dict)
  * Alignment statistics (from AlignmentResult or dict)
  * Variant statistics (from VariantCallingResult or dict)
  * Annotation summary (from AnnotationResult or dict)
  * ACMG classification results (dict passed in by the caller)
  * Final annotated variant list

Output formats:
  * ``report.json``  — structured machine-readable (always written)
  * ``report.html``  — human-readable clinical HTML report (always written;
                        does not require WeasyPrint or wkhtmltopdf)
  * ``report.pdf``   — optional; written only if ``generate_pdf=True`` in
                        config AND either WeasyPrint or wkhtmltopdf is available

Only one report is generated per *sample*, not one per variant.  The
variant list is embedded inside the single sample report.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from geper.pipeline.fastq.pipeline import FastqPipelineError

from pipeline.reporting.clinical_sections import (
    normalize_patient_metadata,
    qc_status_summary,
    variant_dashboard,
    clinical_interpretation,
    merge_variants_with_acmg,
)
from pipeline.reporting import pdf_report as _pdf_report_mod
from pipeline.pgx.stage import PGX_VALIDATION_CAVEAT

logger = logging.getLogger("geper.pipeline.reporting.stage")

# Single source of truth for the pipeline version string, shared by the
# JSON payload, HTML report, and PDF report so they can never disagree.
PIPELINE_VERSION = "GEPER v8"

# Shared ACMG classification -> style mapping. HTML uses the CSS-string
# values directly; pipeline/reporting/pdf_report.py uses the same keys
# with reportlab Color objects (see ACMG_CLASS_COLORS there) so both
# renderers color-code identically without duplicating the tier list.
ACMG_CLASS_STYLES = {
    "Pathogenic": "background:#f8d7da;color:#721c24;",
    "Likely_Pathogenic": "background:#ffe8cc;color:#7d4e00;",
    "Uncertain_Significance": "background:#fff3cd;color:#856404;",
    "Likely_Benign": "background:#cfe2ff;color:#084298;",
    "Benign": "background:#d1e7dd;color:#0f5132;",
}


# ─── HTML template ────────────────────────────────────────────────────────────

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{pipeline_version} Clinical Report — {sample_id}</title>
<style>
  :root {{
    --brand: #1a4a8a; --brand-light: #2a6ab5; --ink: #1f2937; --muted: #6b7280;
    --border: #e2e8f0; --bg-soft: #f7f9fc;
    --pass-bg: #d4edda; --pass-fg: #155724;
    --warn-bg: #fff3cd; --warn-fg: #856404;
    --fail-bg: #f8d7da; --fail-fg: #721c24;
    --unknown-bg: #e2e3e5; --unknown-fg: #383d41;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, Helvetica, Arial, sans-serif;
    margin: 0; padding: 0 0 60px 0; color: var(--ink); background: #fff;
    line-height: 1.5; font-size: 15px;
  }}
  .report-container {{ max-width: 1100px; margin: 0 auto; padding: 32px 24px; }}
  .report-header {{
    display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline;
    border-bottom: 3px solid var(--brand); padding-bottom: 14px; margin-bottom: 20px;
  }}
  .report-header h1 {{ color: var(--brand); margin: 0; font-size: 1.7em; font-weight: 700; }}
  .report-header .meta {{ color: var(--muted); font-size: 0.85em; text-align: right; }}
  h2.section-title {{
    color: var(--brand-light); font-size: 1.15em; font-weight: 700;
    border-left: 4px solid var(--brand-light); padding-left: 10px;
    margin: 30px 0 10px 0;
  }}
  .section {{ margin-bottom: 26px; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 6px; font-size: 0.92em; }}
  th {{ background: var(--brand-light); color: #fff; padding: 7px 10px; text-align: left; font-weight: 600; }}
  td {{ border: 1px solid var(--border); padding: 6px 10px; vertical-align: top; }}
  tr:nth-child(even) td {{ background: var(--bg-soft); }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-weight: 700; font-size: 0.85em; }}
  .badge.PASS {{ background: var(--pass-bg); color: var(--pass-fg); }}
  .badge.FAIL {{ background: var(--fail-bg); color: var(--fail-fg); }}
  .badge.WARNING {{ background: var(--warn-bg); color: var(--warn-fg); }}
  .badge.UNKNOWN {{ background: var(--unknown-bg); color: var(--unknown-fg); }}
  .patient-card {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 10px 24px; background: var(--bg-soft); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px 20px; margin-bottom: 4px;
  }}
  .patient-card .field-label {{ color: var(--muted); font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.03em; }}
  .patient-card .field-value {{ font-weight: 600; }}
  .dashboard {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: 12px; margin-top: 10px;
  }}
  .dash-tile {{
    border: 1px solid var(--border); border-radius: 8px; padding: 14px 10px; text-align: center;
  }}
  .dash-tile .dash-value {{ font-size: 1.7em; font-weight: 800; display: block; }}
  .dash-tile .dash-label {{ font-size: 0.78em; color: var(--muted); text-transform: uppercase; letter-spacing: 0.02em; }}
  .dash-tile.Pathogenic {{ background: var(--fail-bg); }}
  .dash-tile.Pathogenic .dash-value {{ color: var(--fail-fg); }}
  .dash-tile.Likely_Pathogenic {{ background: #ffe8cc; }}
  .dash-tile.Likely_Pathogenic .dash-value {{ color: #7d4e00; }}
  .dash-tile.VUS {{ background: var(--warn-bg); }}
  .dash-tile.VUS .dash-value {{ color: var(--warn-fg); }}
  .dash-tile.Benign {{ background: var(--pass-bg); }}
  .dash-tile.Benign .dash-value {{ color: var(--pass-fg); }}
  .interpretation-box {{
    background: var(--bg-soft); border-left: 4px solid var(--brand-light);
    border-radius: 4px; padding: 16px 20px; margin-top: 8px;
  }}
  .interpretation-box p {{ margin: 0 0 10px 0; }}
  .interpretation-box p:last-child {{ margin-bottom: 0; }}
  footer.report-footer {{
    max-width: 1100px; margin: 40px auto 0 auto; padding: 14px 24px;
    border-top: 1px solid var(--border); font-size: 0.75em; color: var(--muted);
  }}
  @media print {{
    body {{ padding: 0; font-size: 12px; }}
    .section {{ page-break-inside: avoid; }}
    table {{ font-size: 0.85em; }}
    a[href]::after {{ content: none !important; }}
  }}
  @media (max-width: 640px) {{
    .report-header {{ flex-direction: column; align-items: flex-start; }}
    .report-header .meta {{ text-align: left; margin-top: 6px; }}
    table, thead, tbody, th, td, tr {{ display: block; }}
    th {{ position: absolute; left: -9999px; }}
    td {{ border: none; border-bottom: 1px solid var(--border); }}
  }}
</style>
</head>
<body>
<div class="report-container">

<div class="report-header">
  <h1>{pipeline_version} — Clinical Genomic Report</h1>
  <div class="meta">Generated: {generated_at}<br/>Reference genome: {reference_genome}</div>
</div>

<div class="section">
<h2 class="section-title">Patient Information</h2>
<div class="patient-card">
  <div><div class="field-label">Sample ID</div><div class="field-value">{sample_id}</div></div>
  <div><div class="field-label">Patient Name</div><div class="field-value">{patient_name}</div></div>
  <div><div class="field-label">Date of Birth</div><div class="field-value">{patient_dob}</div></div>
  <div><div class="field-label">Sex</div><div class="field-value">{patient_sex}</div></div>
  <div><div class="field-label">Ordering Physician</div><div class="field-value">{patient_physician}</div></div>
</div>
</div>

<div class="section">
<h2 class="section-title">Sequencing QC Summary</h2>
{qc_status_table}
</div>

<div class="section">
<h2 class="section-title">Variant Summary Dashboard</h2>
<div class="dashboard">
  <div class="dash-tile"><span class="dash-value">{dash_total}</span><span class="dash-label">Total Variants</span></div>
  <div class="dash-tile Pathogenic"><span class="dash-value">{dash_pathogenic}</span><span class="dash-label">Pathogenic</span></div>
  <div class="dash-tile Likely_Pathogenic"><span class="dash-value">{dash_likely_pathogenic}</span><span class="dash-label">Likely Pathogenic</span></div>
  <div class="dash-tile VUS"><span class="dash-value">{dash_vus}</span><span class="dash-label">VUS</span></div>
  <div class="dash-tile Benign"><span class="dash-value">{dash_benign_total}</span><span class="dash-label">Benign / Likely Benign</span></div>
  <div class="dash-tile"><span class="dash-value">{dash_pgx}</span><span class="dash-label">PGx Findings</span></div>
</div>
<p class="meta" style="margin-top:10px;">Ancestry: <strong>{dash_ancestry}</strong></p>
</div>

<div class="section">
<h2 class="section-title">Clinical Interpretation</h2>
<div class="interpretation-box">
<p>{interpretation_summary}</p>
<p><strong>Recommended Follow-up:</strong> {interpretation_follow_up}</p>
</div>
</div>

<div class="section">
<h2 class="section-title">Alignment Statistics</h2>
{alignment_table}
</div>

<div class="section">
<h2 class="section-title">Variant Calling Statistics</h2>
{variant_stats_table}
</div>

<div class="section">
<h2 class="section-title">ACMG Classification Results</h2>
{acmg_table}
</div>

<div class="section">
<h2 class="section-title">Annotation Summary</h2>
{annotation_summary_table}
</div>

<div class="section">
<h2 class="section-title">Variant Detail ({n_variants} PASS variants)</h2>
{variants_table}
</div>

<div class="section">
<h2 class="section-title">Pharmacogenomics (PGx)</h2>
<p style="color:#777;font-size:0.85em;"><em>{pgx_caveat}</em></p>
{pgx_section}
</div>


<div class="section">
<h2 class="section-title">Ancestry Inference</h2>
{ancestry_section}
</div>

</div>

<footer class="report-footer">
  <p>{lab_disclaimer}</p>
  <p>Reference genome: {reference_genome} &nbsp;|&nbsp; Pipeline: {pipeline_version} &nbsp;|&nbsp;
  Generated: {generated_at}</p>
</footer>

</body>
</html>
"""


def _qc_status_to_html_table(qc_rows: List[Dict[str, Any]]) -> str:
    """Render sequencing QC rows (each with label/value/status) with a
    color-coded PASS/WARNING/FAIL/UNKNOWN badge."""
    if not qc_rows:
        return "<p><em>No QC data available.</em></p>"
    rows_html = "".join(
        f"<tr><td>{r['label']}</td><td>{r['value']}</td>"
        f'<td><span class="badge {r["status"]}">{r["status"]}</span></td></tr>'
        for r in qc_rows
    )
    return f"<table><tr><th>Metric</th><th>Value</th><th>Status</th></tr>{rows_html}</table>"


def _dict_to_html_table(data: Dict[str, Any], title: Optional[str] = None) -> str:
    """Render a flat dict as an HTML two-column table."""
    rows = "".join(f"<tr><td><strong>{k}</strong></td><td>{v}</td></tr>" for k, v in data.items())
    return f"<table><tr><th>Field</th><th>Value</th></tr>{rows}</table>"


def _acmg_to_html_table(acmg_results: Optional[List[Dict]]) -> str:
    """Render ACMG classification results as a styled HTML table.

    ISSUE 2 FIX: the "Exploratory Tier"/"Exploratory Score" columns come
    from EvidenceAggregator's composite ranking metric, which deliberately
    re-weights ClinVar/computational evidence already folded into the
    ACMG score (see pipeline/evidence/aggregator.py for the rationale).
    To avoid this being misread as a second, independent clinical
    confidence score, the columns are labelled "Exploratory" (not
    "Evidence"/"Composite") and the aggregator's own disclaimer string is
    rendered directly beneath the table.
    """
    if not acmg_results:
        return "<p>ACMG classification not available.</p>"

    _CLASS_STYLES = ACMG_CLASS_STYLES

    headers = [
        "Variant",
        "Gene",
        "ACMG Class",
        "ACMG Score",
        "Criteria Met",
        "Criteria Unknown",
        "ClinVar",
        "gnomAD",
        "Exploratory Tier*",
        "Exploratory Score*",
    ]
    header_html = "".join(f"<th>{h}</th>" for h in headers)

    rows_html = ""
    disclaimer = None
    for item in acmg_results:
        chrom = item.get("chrom", "")
        pos = item.get("pos", "")
        ref = item.get("ref", "")
        alt = item.get("alt", "")
        variant_str = f"{chrom}:{pos} {ref}>{alt}" if chrom else "—"
        # FIX (Issue 7): never silently render a null gene as a bare dash —
        # always state why it's unavailable.
        gene = item.get("gene")
        if gene:
            gene_cell = gene
        else:
            reason = item.get("gene_unavailable_reason") or "Unknown reason"
            gene_cell = f"<em>Gene annotation unavailable<br/>Reason: {reason}</em>"
        classification = item.get("classification", "Uncertain_Significance")
        acmg_score = item.get("score", "")
        criteria_met = ", ".join(item.get("criteria_met") or []) or "—"
        # FIX (Issue 6): surface criteria that could not be evaluated
        # (Unknown / Insufficient Data) distinctly from "not met".
        criteria_unknown = ", ".join(item.get("criteria_unknown") or []) or "—"
        # FIX (Issue 7): ClinVar/gnomAD cells always carry a reason when
        # the value itself is unavailable, rather than a bare blank/dash.
        clinvar_reason = item.get("clinvar_unavailable_reason")
        clinvar_cell = clinvar_reason or "Found"
        gnomad_reason = item.get("gnomad_unavailable_reason")
        if gnomad_reason:
            gnomad_cell = gnomad_reason
        else:
            gnomad_af = item.get("gnomad_af")
            gnomad_af_popmax = item.get("gnomad_af_popmax")
            if gnomad_af is not None:
                gnomad_cell = f"AF: {gnomad_af:.2e}"
                if gnomad_af_popmax is not None:
                    gnomad_cell += f" (popmax: {gnomad_af_popmax:.2e})"
            else:
                # "found" status without a numeric AF is unexpected but
                # must never render a bare blank cell (Issue 7 principle).
                gnomad_cell = "Found (frequency not available)"
        final_tier = item.get("final_tier", "—")
        composite = item.get("composite_score", "—")
        if disclaimer is None and item.get("disclaimer"):
            disclaimer = item["disclaimer"]

        cls_style = _CLASS_STYLES.get(classification, "")
        cls_cell = f'<td style="{cls_style}">{classification}</td>'

        rows_html += (
            f"<tr>"
            f"<td>{variant_str}</td>"
            f"<td>{gene_cell}</td>"
            f"{cls_cell}"
            f"<td>{acmg_score}</td>"
            f"<td>{criteria_met}</td>"
            f"<td>{criteria_unknown}</td>"
            f"<td>{clinvar_cell}</td>"
            f"<td>{gnomad_cell}</td>"
            f"<td>{final_tier}</td>"
            f"<td>{composite}</td>"
            f"</tr>"
        )

    table_html = f"<table><tr>{header_html}</tr>{rows_html}</table>"

    # Fall back to the aggregator's canonical disclaimer text if no result
    # row happened to carry one (e.g. all variants errored before evidence
    # aggregation ran), so the warning is never silently dropped.
    if disclaimer is None:
        from pipeline.evidence.aggregator import EvidenceResult

        disclaimer = EvidenceResult().disclaimer

    footnote = (
        f'<p class="evidence-disclaimer" '
        f'style="font-size:0.85em;color:#555;margin-top:4px;">'
        f"<strong>* Exploratory Tier / Exploratory Score:</strong> {disclaimer}"
        f"</p>"
    )

    return table_html + footnote


def _annotation_summary_to_html(ann_d: Dict) -> str:
    """Render the annotation-stage summary, including any symbolic ALT=*
    placeholder variants that were filtered out before annotation/ACMG.

    FIX (Issue 5 / Issue 8): these must be visible in the report, not
    silently dropped — the annotation stage records why each one was
    skipped.
    """
    summary = {k: v for k, v in ann_d.items() if k not in ("variants", "skipped_symbolic")}
    table = (
        _dict_to_html_table(summary)
        if summary
        else "<p><em>No annotation summary available.</em></p>"
    )

    skipped = ann_d.get("skipped_symbolic") or []
    if not skipped:
        return table

    rows = "".join(
        f"<tr><td>{s.get('chrom', '')}:{s.get('pos', '')}</td>"
        f"<td>{s.get('ref', '')}</td><td>{s.get('alt', '')}</td>"
        f"<td>{s.get('reason', '')}</td></tr>"
        for s in skipped
    )
    skipped_table = (
        f"<p><strong>{len(skipped)} variant(s) skipped before annotation "
        f"(symbolic ALT=* placeholders):</strong></p>"
        f"<table><tr><th>Position</th><th>REF</th><th>ALT</th><th>Reason</th></tr>{rows}</table>"
    )
    return table + skipped_table


def _variants_to_html_table(
    variants: List[Dict], gene_unavailable_reason: str = "Gene annotation unavailable"
) -> str:
    """Render the variant list as an HTML table."""
    if not variants:
        return "<p><em>No PASS variants.</em></p>"

    headers = [
        "CHROM",
        "POS",
        "REF",
        "ALT",
        "QUAL",
        "Gene",
        "Transcript",
        "HGVS",
        "Consequence",
        "Zygosity",
        "GT",
        "DP",
        "AD",
    ]
    header_html = "".join(f"<th>{h}</th>" for h in headers)

    rows_html = ""
    for v in variants:
        # FIX (Issue 7): never silently render a null gene as a bare
        # "intergenic" guess — state the actual reason it's unavailable.
        gene_cell = (
            v.get("gene_name")
            or f"<em>Gene annotation unavailable<br/>Reason: {gene_unavailable_reason}</em>"
        )
        cells = [
            v.get("chrom", ""),
            v.get("pos", ""),
            v.get("ref", ""),
            v.get("alt", ""),
            v.get("qual", ""),
            gene_cell,
            v.get("transcript_id") or "",
            v.get("hgvs", ""),
            v.get("consequence") or "—",
            v.get("zygosity", ""),
            v.get("gt", ""),
            v.get("dp", ""),
            v.get("ad", ""),
        ]
        row = "".join(f"<td>{c}</td>" for c in cells)
        rows_html += f"<tr>{row}</tr>"

    return f"<table><tr>{header_html}</tr>{rows_html}</table>"


def _pgx_to_html_section(pgx_result: Any) -> str:
    """Render PGx annotations as an HTML table section."""
    if not pgx_result:
        return "<p><em>PGx analysis not performed.</em></p>"
    annotations = (
        getattr(pgx_result, "annotations", None) or pgx_result.get("annotations", [])
        if isinstance(pgx_result, dict)
        else []
    )
    if not annotations:
        return "<p><em>No PGx annotations available.</em></p>"
    header = "<tr><th>Gene</th><th>Diplotype</th><th>Phenotype</th><th>Activity Score</th><th>Drug Implications</th><th>Evidence</th></tr>"
    rows = ""
    for ann in annotations:
        if isinstance(ann, dict):
            gene = ann.get("gene", "")
            diplotype = ann.get("diplotype", "")
            phenotype = ann.get("phenotype", "")
            score = ann.get("activity_score")
            drugs = ann.get("affected_drugs", [])
            evidence = ann.get("evidence_level", "")
        else:
            gene = getattr(ann, "gene", "")
            diplotype = getattr(ann, "diplotype", "")
            phenotype = getattr(ann, "phenotype", "")
            score = getattr(ann, "activity_score", None)
            drugs = getattr(ann, "affected_drugs", [])
            evidence = getattr(ann, "evidence_level", "")
        drug_str = (
            "; ".join(f"{d.get('drug', '')}: {d.get('implication', '')}" for d in drugs) or "None"
        )
        score_str = f"{score:.1f}" if score is not None else "N/A"
        rows += f"<tr><td>{gene}</td><td>{diplotype}</td><td>{phenotype}</td><td>{score_str}</td><td style='font-size:0.85em'>{drug_str}</td><td>{evidence}</td></tr>"
    return f"<table>{header}{rows}</table>"


def _ancestry_to_html_section(ancestry_result: Any) -> str:
    """Render Ancestry inference result as an HTML section."""
    if not ancestry_result:
        return "<p><em>Ancestry analysis not performed.</em></p>"
    if isinstance(ancestry_result, dict):
        primary = ancestry_result.get("primary_population", "Unknown")
        probs = ancestry_result.get("population_probabilities", {})
        confidence = ancestry_result.get("confidence", "Unknown")
        called = ancestry_result.get("markers_called", 0)
        evaluated = ancestry_result.get("markers_evaluated", 0)
    else:
        primary = getattr(ancestry_result, "primary_population", "Unknown")
        probs = getattr(ancestry_result, "population_probabilities", {})
        confidence = getattr(ancestry_result, "confidence", "Unknown")
        called = getattr(ancestry_result, "markers_called", 0)
        evaluated = getattr(ancestry_result, "markers_evaluated", 0)
    prob_rows = "".join(
        f"<tr><td>{pop}</td><td>{prob:.3f}</td></tr>"
        for pop, prob in sorted(probs.items(), key=lambda x: -x[1])
    )
    return f"""
    <p><strong>Primary Population:</strong> {primary} &nbsp;|&nbsp;
    <strong>Confidence:</strong> {confidence} &nbsp;|&nbsp;
    <strong>Markers:</strong> {called}/{evaluated} called</p>
    <table><tr><th>Population</th><th>Probability</th></tr>{prob_rows}</table>
    """


# ─── Stage result ─────────────────────────────────────────────────────────────


@dataclass
class ReportResult:
    sample_id: str = ""
    json_path: str = ""
    html_path: str = ""
    pdf_path: Optional[str] = None
    elapsed_seconds: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "sample_id": self.sample_id,
            "json_path": self.json_path,
            "html_path": self.html_path,
            "pdf_path": self.pdf_path,
            "elapsed_seconds": self.elapsed_seconds,
        }


# ─── Stage ────────────────────────────────────────────────────────────────────


class ReportingStage:
    """Generate a single sample-level clinical report.

    Configuration (under ``cfg["reporting"]``):
        output_dir     str   Default output directory.
        generate_pdf   bool  Try to render PDF (default True).

    Args (``run``):
        sample_id:       Sample identifier.
        output_dir:      Directory for report files (overrides config default).
        qc_summary:      Dict or object with ``.to_dict()`` from FastqValidator.
        alignment_stats: Dict or AlignmentResult.
        variant_stats:   Dict or VariantCallingResult.
        annotation_result: Dict or AnnotationResult.
        acmg_results:    Dict of ACMG classification output (optional).
    """

    def __init__(self, cfg: Optional[Dict] = None) -> None:
        self._full_cfg = cfg or {}
        self._cfg = (cfg or {}).get("reporting", {}) or {}
        self._default_output_dir: Optional[str] = self._cfg.get("output_dir", None) or None
        self._generate_pdf: bool = bool(self._cfg.get("generate_pdf", True))

    @staticmethod
    def _gene_unavailable_reason(cfg: Dict) -> str:
        """FIX (Issue 7): determine why a variant's gene annotation might be
        missing, so reports can state a specific reason instead of a bare
        null/dash. Mirrors the identical logic in
        pipeline/orchestration/shared.py (kept in sync deliberately —
        both need the same answer for the same config).
        """
        vep_enabled = bool((cfg.get("vep") or {}).get("enabled", True))
        if not vep_enabled:
            return "VEP disabled"
        gff_configured = bool(
            (cfg.get("annotation") or {}).get("refseq_gff")
            or (cfg.get("rna_analysis") or {}).get("refseq_gff")
        )
        if not gff_configured:
            return "No GFF3/VEP annotation source configured"
        return "Variant not resolved to a known gene (intergenic or annotation gap)"

    @staticmethod
    def _to_dict(obj: Any) -> Dict:
        """Coerce any input (already a dict, dataclass, or ``to_dict``-able) to dict."""
        if obj is None:
            return {}
        if isinstance(obj, dict):
            return obj
        if hasattr(obj, "to_dict"):
            return obj.to_dict()
        return vars(obj)

    def run(
        self,
        sample_id: str,
        output_dir: Optional[str] = None,
        qc_summary: Any = None,
        alignment_stats: Any = None,
        variant_stats: Any = None,
        annotation_result: Any = None,
        acmg_results: Optional[Dict] = None,
        pgx_result: Any = None,
        ancestry_result: Any = None,
        reference_versions: Optional[Dict] = None,  # FIX 14
        patient_metadata: Optional[Dict] = None,  # Issue 3: optional patient metadata JSON
    ) -> ReportResult:
        """Generate the report and write files to *output_dir*.

        Returns:
            ``ReportResult`` with paths to generated files.
        """
        t0 = time.time()
        effective_dir = output_dir or self._default_output_dir
        if effective_dir is None:
            raise FastqPipelineError(
                "reporting.output_dir must be set in config — no default output directory "
                "is used in clinical mode.",
                stage="reporting",
            )
        out = Path(effective_dir)
        out.mkdir(parents=True, exist_ok=True)

        qc_d = self._to_dict(qc_summary)
        align_d = self._to_dict(alignment_stats)
        vc_d = self._to_dict(variant_stats)
        ann_d = self._to_dict(annotation_result)
        acmg_d = acmg_results or []

        # PGx and Ancestry sections
        pgx_d = (
            pgx_result.to_dict()
            if pgx_result is not None and hasattr(pgx_result, "to_dict")
            else {}
        )
        ancestry_d = (
            ancestry_result.to_dict()
            if ancestry_result is not None and hasattr(ancestry_result, "to_dict")
            else {}
        )

        # Extract variant list from annotation result
        variants: List[Dict] = []
        if "variants" in ann_d and isinstance(ann_d["variants"], list):
            variants = [
                v if isinstance(v, dict) else (v.to_dict() if hasattr(v, "to_dict") else vars(v))
                for v in ann_d["variants"]
            ]

        # ── JSON report ──────────────────────────────────────────────────────
        report_payload = {
            "sample_id": sample_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "pipeline": PIPELINE_VERSION,
            "reference_versions": reference_versions or {},  # FIX 14
            "qc_summary": qc_d,
            "alignment_statistics": align_d,
            "variant_statistics": vc_d,
            "annotation_summary": {k: v for k, v in ann_d.items() if k != "variants"},
            "acmg_evidence": acmg_d,
            "acmg_results": acmg_d,
            "variants": variants,
            "pharmacogenomics": pgx_d,
            "ancestry": ancestry_d,
        }
        json_path = str(out / "report.json")
        Path(json_path).write_text(json.dumps(report_payload, indent=2))
        logger.info("[%s] Report JSON written: %s", sample_id, json_path)

        # ── HTML report ──────────────────────────────────────────────────────
        html_path = str(out / "report.html")
        generated_at_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        reference_genome = (reference_versions or {}).get("reference_genome") or (
            (reference_versions or {}).get("reference") or "Not specified"
        )
        lab_disclaimer = (
            "This report is generated by Bij AI, an in-development bioinformatics pipeline "
            "that assists qualified clinicians and pathologists. It produces a draft "
            "classification requiring qualified human review and final sign-off before "
            "any clinical use, and does not independently provide final clinical "
            "interpretation."
        )

        patient_meta = normalize_patient_metadata(patient_metadata)
        acmg_list = acmg_d if isinstance(acmg_d, list) else []
        qc_thresholds = (self._full_cfg.get("reporting") or {}).get("qc_thresholds")
        qc_rows = qc_status_summary(qc_d, align_d, qc_thresholds)
        dashboard = variant_dashboard(acmg_list, pgx_d, ancestry_d)
        interpretation = clinical_interpretation(dashboard)
        merged_variants = merge_variants_with_acmg(variants, acmg_list)

        html_content = _HTML_TEMPLATE.format(
            sample_id=sample_id,
            generated_at=generated_at_str,
            pipeline_version=PIPELINE_VERSION,
            reference_genome=reference_genome,
            patient_name=patient_meta["name"],
            patient_dob=patient_meta["dob"],
            patient_sex=patient_meta["sex"],
            patient_physician=patient_meta["physician"],
            qc_status_table=_qc_status_to_html_table(qc_rows),
            dash_total=dashboard["total_variants"],
            dash_pathogenic=dashboard["pathogenic"],
            dash_likely_pathogenic=dashboard["likely_pathogenic"],
            dash_vus=dashboard["vus"],
            dash_benign_total=dashboard["benign"] + dashboard["likely_benign"],
            dash_pgx=dashboard["pgx_findings"],
            dash_ancestry=dashboard["ancestry_summary"],
            interpretation_summary=interpretation["summary"],
            interpretation_follow_up=interpretation["follow_up"],
            alignment_table=_dict_to_html_table(
                {k: v for k, v in align_d.items() if k != "commands_run"}
            ),
            variant_stats_table=_dict_to_html_table(
                {k: v for k, v in vc_d.items() if k not in ("filter_thresholds",)}
            ),
            acmg_table=_acmg_to_html_table(acmg_d if isinstance(acmg_d, list) else None),
            annotation_summary_table=_annotation_summary_to_html(ann_d),
            variants_table=_variants_to_html_table(
                variants, self._gene_unavailable_reason(self._full_cfg)
            ),
            n_variants=len(variants),
            pgx_section=_pgx_to_html_section(pgx_result),
            pgx_caveat=PGX_VALIDATION_CAVEAT,
            ancestry_section=_ancestry_to_html_section(ancestry_result),
            lab_disclaimer=lab_disclaimer,
        )
        Path(html_path).write_text(html_content)
        logger.info("[%s] Report HTML written: %s", sample_id, html_path)

        # ── PDF (optional) ───────────────────────────────────────────────────
        pdf_path: Optional[str] = None
        if self._generate_pdf:
            pdf_path = self._try_render_pdf(
                html_path,
                str(out / "report.pdf"),
                sample_id,
                patient_meta=patient_meta,
                qc_rows=qc_rows,
                dashboard=dashboard,
                interpretation=interpretation,
                merged_variants=merged_variants,
                pgx_annotations=pgx_d.get("annotations") if isinstance(pgx_d, dict) else None,
                ancestry_summary=dashboard["ancestry_summary"],
                reference_genome=reference_genome,
                pipeline_version=PIPELINE_VERSION,
                generated_at=generated_at_str,
                lab_disclaimer=lab_disclaimer,
            )

        result = ReportResult(
            sample_id=sample_id,
            json_path=json_path,
            html_path=html_path,
            pdf_path=pdf_path,
            elapsed_seconds=round(time.time() - t0, 3),
        )
        logger.info(
            "[%s] ReportingStage complete in %.2fs: JSON=%s HTML=%s PDF=%s",
            sample_id,
            result.elapsed_seconds,
            json_path,
            html_path,
            pdf_path or "<not generated>",
        )
        return result

    def _try_render_pdf(
        self,
        html_path: str,
        pdf_path: str,
        sample_id: str,
        **clinical_pdf_kwargs: Any,
    ) -> Optional[str]:
        """Attempt PDF generation via ReportLab first (primary — pure
        Python, no system dependency, built natively from the same
        structured data as the HTML report), then WeasyPrint, then
        wkhtmltopdf (secondary fallbacks that convert the already-
        written HTML file, kept only in case ReportLab itself is
        unavailable in some environment).

        Returns the PDF path on success, or ``None`` with a warning log
        if nothing is available — PDF is optional, not a blocker.
        """
        try:
            _pdf_report_mod.render_clinical_pdf(
                pdf_path, sample_id=sample_id, **clinical_pdf_kwargs
            )
            logger.info("[%s] PDF written via ReportLab: %s", sample_id, pdf_path)
            return pdf_path
        except _pdf_report_mod.ReportLabUnavailableError:
            logger.info(
                "[%s] reportlab not installed — falling back to WeasyPrint/wkhtmltopdf "
                "(HTML-to-PDF conversion) for report.pdf.",
                sample_id,
            )
        except Exception as exc:
            logger.warning(
                "[%s] ReportLab PDF generation failed (%s) — falling back to "
                "WeasyPrint/wkhtmltopdf.",
                sample_id,
                exc,
            )

        # Try WeasyPrint (Python library)
        try:
            from weasyprint import HTML  # type: ignore

            HTML(filename=html_path).write_pdf(pdf_path)
            logger.info("[%s] PDF written via WeasyPrint: %s", sample_id, pdf_path)
            return pdf_path
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("[%s] WeasyPrint PDF generation failed: %s", sample_id, exc)

        # Try wkhtmltopdf (system binary)
        if shutil.which("wkhtmltopdf"):
            try:
                subprocess.run(
                    ["wkhtmltopdf", html_path, pdf_path],
                    capture_output=True,
                    check=True,
                )
                logger.info("[%s] PDF written via wkhtmltopdf: %s", sample_id, pdf_path)
                return pdf_path
            except subprocess.CalledProcessError as exc:
                logger.warning("[%s] wkhtmltopdf failed: %s", sample_id, exc.stderr[:500])

        logger.warning(
            "[%s] PDF generation skipped: neither ReportLab, WeasyPrint, nor "
            "wkhtmltopdf produced a report.pdf. Install reportlab (pip install "
            "reportlab) to enable PDF reports.",
            sample_id,
        )
        return None
