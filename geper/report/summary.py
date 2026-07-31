"""
Clinical-grade PDF report generator.

Renders the same JSON document shape `report/json_builder.py::JSONResultBuilder.build()`
produces as a multi-page, ReportLab-based PDF suitable for a hospital
laboratory's diagnostic record: a title/logo header (first page only --
see `_build_report_header`), a patient/sample header, a sequencing
QC status table, one section per variant finding (reusing
`report/clinical_report_builder.py`'s already-computed evidence --
nothing here re-derives ACMG classification or evidence from raw
provider dicts a second time), a pathologist sign-off block, and a
standard legal disclaimer.

Entry point: `generate_pdf(document, output_path, patient_meta=None,
qc_metrics=None, run_id=None, logo_path=None)`.

Compliance note (India DPDP Act 2023, not HIPAA/GDPR -- this is an
India-market product): this module parses and renders whatever
patient_meta it is given; it does not itself implement the DPDP Act's
consent, purpose-limitation, data-minimization, or retention/deletion
requirements, and makes no claim of compliance anywhere in its code or
rendered output. Real compliance sign-off must come from someone
qualified to review this against DPDP Act requirements (and any
applicable state/ICMR clinical-lab regulations) before production/
hospital use -- see `_parse_patient_meta`'s docstring for specifics.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Union

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from config import CONFIG
from utils.logger import get_logger

logger = get_logger(__name__)

_DEIDENTIFIED_LABEL = "De-identified / Research Sample"

_PAGE_W, _PAGE_H = A4
_MARGIN = 20 * mm

# Header logo's height is derived at render time from the title
# Paragraph's own measured height (see `_build_report_header`), not a
# fixed constant -- that's what makes "roughly matches the title text
# next to it" literally true rather than approximate, and keeps it
# correct automatically if the title style ever changes. This gap is
# the only fixed measurement: horizontal breathing room between the
# logo and the title text that follows it.
_LOGO_TITLE_GAP = 4 * mm

_DISCLAIMER_TEXT = (
    "Limitations and Disclaimer: This test was developed and its performance characteristics "
    "determined by the Geper Genomic Analysis Pipeline. It is intended for clinical use in "
    "conjunction with other clinical and diagnostic findings. Decisions regarding patient care "
    "should not be based solely on this report. All clinical decisions must be made by a "
    "qualified healthcare professional."
)

# Mock QC metrics, used only when the caller supplies none. GEPER's
# pipeline consumes an already-called VCF, not raw FASTQ/BAM, so it
# cannot compute these itself -- see QCReportConfig's docstring
# (config.py). Never presented as a real result: the rendered report
# footnotes this explicitly whenever it is used (see `_build_qc_flowables`).
_MOCK_QC_METRICS: Dict[str, Dict[str, Any]] = {
    "mean_coverage_depth": {"value": 42.5, "unit": "x"},
    "bases_at_20x": {"value": 96.8, "unit": "%"},
    "q30_score": {"value": 93.2, "unit": "%"},
}

_QC_METRIC_LABELS = {
    "mean_coverage_depth": "Mean Coverage Depth",
    "bases_at_20x": "Bases at >20x Coverage",
    "q30_score": "Q30 Score",
}

_QC_METRIC_ORDER = ("mean_coverage_depth", "bases_at_20x", "q30_score")


def _qc_threshold_pass_min(metric_key: str) -> float:
    """PASS/WARNING threshold for one QC metric -- see QCReportConfig's docstring (config.py) for why these are placeholders, configurable via GEPER_QC_* env vars."""
    return {
        "mean_coverage_depth": CONFIG.qc_report.MEAN_COVERAGE_DEPTH_PASS_MIN,
        "bases_at_20x": CONFIG.qc_report.BASES_AT_20X_PASS_MIN,
        "q30_score": CONFIG.qc_report.Q30_SCORE_PASS_MIN,
    }[metric_key]


def _qc_status(metric_key: str, value: float) -> str:
    return "PASS" if value >= _qc_threshold_pass_min(metric_key) else "WARNING"


# ---------------------------------------------------------------------------
# Patient metadata parsing
# ---------------------------------------------------------------------------

def _parse_patient_meta(patient_meta: Optional[Union[Dict[str, Any], str]]) -> Dict[str, Any]:
    """
    Accepts a dict, a path to a JSON file, or None. Returns
    {"patient_name", "dob", "gender", "physician", "deidentified"}.

    Never raises: a missing file, a corrupt/malformed JSON body, or a
    body without a usable patient_name all fall back to the safe
    de-identified default -- exactly per spec ("a corrupt JSON file
    never crashes the pipeline" / "if absent, cleanly default"). The
    parse failure is logged for operators; it is deliberately NOT
    surfaced in the rendered report itself (a clinical document should
    show the safe fallback state, not internal parsing diagnostics).

    India DPDP Act 2023 note (not HIPAA/GDPR -- this is an India-market
    product): patient_name/dob/gender/physician are "personal data"
    under the DPDP Act once a real (non-de-identified) patient_meta is
    supplied. This function only parses and hands back what it is
    given -- it does not implement or enforce the DPDP Act's consent
    capture, purpose-limitation, data-minimization, or retention/
    deletion requirements, none of which can be satisfied inside a PDF
    renderer; those belong further upstream (the system that collects
    and stores patient_meta in the first place). Real compliance
    sign-off must come from someone qualified to review the full data
    flow against DPDP Act requirements before production/hospital
    deployment -- do not treat this comment, or the presence of a
    de-identified fallback, as that sign-off.
    """
    defaults = {
        "patient_name": _DEIDENTIFIED_LABEL,
        "dob": None,
        "gender": None,
        "physician": None,
        "deidentified": True,
    }
    if not patient_meta:
        return defaults

    try:
        if isinstance(patient_meta, dict):
            raw = patient_meta
        else:
            with open(patient_meta, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError("patient metadata JSON must be an object")
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(f"Could not parse patient metadata ({exc}); rendering the de-identified default instead.")
        return defaults

    name = (raw.get("patient_name") or "").strip()
    if not name:
        return defaults  # no usable name -> treat exactly like "absent", per spec

    return {
        "patient_name": name,
        "dob": (raw.get("dob") or "").strip() or None,
        "gender": (raw.get("gender") or "").strip() or None,
        "physician": (raw.get("physician") or "").strip() or None,
        "deidentified": False,
    }


# ---------------------------------------------------------------------------
# Sample ID / Run ID derivation
# ---------------------------------------------------------------------------

def _derive_sample_id(document: Dict[str, Any]) -> str:
    """
    GEPER's pipeline has no first-class "Sample ID" concept of its own
    -- this derives one from the input VCF's real genotype sample
    column name(s) (`document["vcf_samples"]`, from the VCF header)
    when present, or its filename when the VCF carries no genotype
    columns at all (common for research/annotation-only VCFs). A real
    LIMS/hospital deployment should supply its own authoritative sample
    identifier rather than rely on this fallback.
    """
    samples = document.get("vcf_samples") or []
    if samples:
        return ", ".join(samples)
    input_vcf = document.get("input_vcf") or ""
    stem = os.path.splitext(os.path.basename(input_vcf))[0]
    return stem or "UNKNOWN-SAMPLE"


def _derive_run_id(document: Dict[str, Any], run_id: Optional[str]) -> str:
    """
    GEPER assigns no run/accession ID anywhere else in the pipeline --
    if the caller doesn't supply one, this derives a stable one from
    the document's own `generated_at` timestamp (so re-rendering the
    same already-written geper_results.json reproduces the same Run ID
    rather than a new one each time). A real LIMS/hospital deployment
    should supply its own authoritative run/accession ID via
    `generate_pdf(run_id=...)` instead of relying on this fallback.
    """
    if run_id:
        return run_id
    generated_at = document.get("generated_at") or datetime.now(timezone.utc).isoformat()
    compact = "".join(ch for ch in generated_at if ch.isalnum())
    return f"GEPER-RUN-{compact[:14]}"


# ---------------------------------------------------------------------------
# Two-pass "Page X of Y" canvas
# ---------------------------------------------------------------------------

class _NumberedCanvas(Canvas):
    """
    Standard two-pass ReportLab pagination recipe: every `showPage()`
    call is intercepted and the page's state buffered instead of being
    flushed immediately, so the true total page count is only known
    once the whole document has been laid out. `save()` then replays
    each buffered page, drawing the "Page X of Y" footer (now that Y is
    known) before actually emitting it.

    This composes with `SimpleDocTemplate.build(onFirstPage=...,
    onLaterPages=...)`: those callbacks draw everything else that
    varies by page (the persistent Sample ID header on later pages)
    once per page, before this class's `showPage()` buffers that
    already-drawn content -- the two drawing paths don't conflict.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states: List[Dict[str, Any]] = []

    def showPage(self) -> None:
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_pagination_footer(total_pages)
            super().showPage()
        super().save()

    def _draw_pagination_footer(self, total_pages: int) -> None:
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.grey)
        self.drawRightString(_PAGE_W - _MARGIN, 12 * mm, f"Page {self._pageNumber} of {total_pages}")
        self.setStrokeColor(colors.lightgrey)
        self.line(_MARGIN, 16 * mm, _PAGE_W - _MARGIN, 16 * mm)


def _make_first_page_decoration():
    """Page 1's running furniture: a single top rule under the title (the patient/QC detail is drawn as ordinary flowables, not canvas-drawn, since it only ever appears once)."""

    def _draw(canvas: Canvas, _doc: SimpleDocTemplate) -> None:
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#1a3c5e"))
        canvas.setLineWidth(1.2)
        canvas.line(_MARGIN, _PAGE_H - 14 * mm, _PAGE_W - _MARGIN, _PAGE_H - 14 * mm)
        canvas.restoreState()

    return _draw


def _make_later_page_decoration(header_label: str):
    """Pages 2+: small, muted running header carrying the Sample ID (or patient name, if provided) -- per spec, only on pages after page 1."""

    def _draw(canvas: Canvas, _doc: SimpleDocTemplate) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.grey)
        canvas.drawString(_MARGIN, _PAGE_H - 12 * mm, f"GEPER Clinical Genomic Report -- {header_label}")
        canvas.setStrokeColor(colors.lightgrey)
        canvas.line(_MARGIN, _PAGE_H - 14 * mm, _PAGE_W - _MARGIN, _PAGE_H - 14 * mm)
        canvas.restoreState()

    return _draw


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------

def _build_stylesheet() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    navy = colors.HexColor("#1a3c5e")
    return {
        "ReportTitle": ParagraphStyle("GeperReportTitle", parent=base["Title"], fontSize=16, textColor=navy, spaceAfter=2),
        "SectionHeading": ParagraphStyle("GeperSectionHeading", parent=base["Heading2"], fontSize=12, textColor=navy, spaceBefore=6, spaceAfter=3),
        "BodyText": ParagraphStyle("GeperBodyText", parent=base["BodyText"], fontSize=9.5, leading=13),
        "BulletText": ParagraphStyle("GeperBulletText", parent=base["BodyText"], fontSize=9, leading=12, leftIndent=10),
        "TableHeader": ParagraphStyle("GeperTableHeader", parent=base["BodyText"], fontSize=9, textColor=colors.white, fontName="Helvetica-Bold"),
        "TableLabel": ParagraphStyle("GeperTableLabel", parent=base["BodyText"], fontSize=9, fontName="Helvetica-Bold"),
        "TableValue": ParagraphStyle("GeperTableValue", parent=base["BodyText"], fontSize=9),
        "TableValueSmall": ParagraphStyle("GeperTableValueSmall", parent=base["BodyText"], fontSize=8, leading=10),
        "StatusPass": ParagraphStyle("GeperStatusPass", parent=base["BodyText"], fontSize=9, textColor=colors.HexColor("#1a7a35"), fontName="Helvetica-Bold"),
        "StatusWarn": ParagraphStyle("GeperStatusWarn", parent=base["BodyText"], fontSize=9, textColor=colors.HexColor("#9a6a00"), fontName="Helvetica-Bold"),
        "Footnote": ParagraphStyle("GeperFootnote", parent=base["BodyText"], fontSize=7.5, textColor=colors.grey, leading=10),
        "SignoffTitle": ParagraphStyle("GeperSignoffTitle", parent=base["Heading3"], fontSize=10),
        "Disclaimer": ParagraphStyle("GeperDisclaimer", parent=base["BodyText"], fontSize=7.5, textColor=colors.grey, leading=10),
    }


_TABLE_GRID_COLOR = colors.HexColor("#cccccc")


# ---------------------------------------------------------------------------
# Flowable builders
# ---------------------------------------------------------------------------

def _resolve_logo_path(logo_path: Optional[str]) -> Optional[str]:
    """
    Which logo file (if any) `_build_report_header` should attempt to
    load, per call.

    An explicit `generate_pdf(logo_path=...)` argument always wins, so
    a caller can either force a specific logo (a white-label
    deployment's own mark) or force no logo at all for one call by
    passing `logo_path=""` -- distinct from passing nothing, which
    defers to `CONFIG.report_branding` (GEPER's own default mark,
    globally enabled/disabled/repointed via
    GEPER_REPORT_LOGO_ENABLED / GEPER_REPORT_LOGO_PATH, see
    `config.py::ReportBrandingConfig`). Returning None here only means
    "don't try" -- it says nothing about whether a path, if resolved,
    actually points to a loadable image; that's `_build_report_header`'s
    job, with its own graceful fallback.
    """
    if logo_path is not None:
        return logo_path or None
    if not CONFIG.report_branding.ENABLED:
        return None
    return CONFIG.report_branding.LOGO_PATH or None


def _load_cropped_logo_image(path: str) -> BytesIO:
    """
    Open the logo at `path` and, when it carries transparency, crop it
    to the bounding box of its actual visible (non-transparent)
    content before returning it as an in-memory PNG.

    Why this matters for sizing: an icon-style logo is routinely saved
    on a much larger transparent canvas than the mark itself (GEPER's
    own `assets/logo.png` is a 500x500 canvas whose visible content is
    only ~213px tall, roughly 43% of the canvas height). Sizing a
    bounding box that includes that invisible padding to "match the
    title's height" (as an earlier version of this function did)
    therefore actually renders the *visible* mark at well under half
    that height -- which is exactly the "tiny icon" bug this was
    written to fix. Cropping first means the height this module
    computes always corresponds to what a viewer actually sees, for
    GEPER's own mark or any white-label logo a hospital supplies,
    regardless of how much padding its source file happens to carry.

    Returns the original image bytes unchanged (still as a BytesIO, so
    the caller has one consistent input type) when there is no alpha
    channel, or when the alpha channel's bounding box can't be
    determined (fully transparent or fully opaque image -- `getbbox()`
    returns None in both cases, and there is nothing meaningful to
    crop to).
    """
    with PILImage.open(path) as img:
        img.load()  # force the read to happen inside this `with` block, before the file handle closes
        has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        if has_alpha:
            rgba = img.convert("RGBA")
            bbox = rgba.split()[-1].getbbox()
            if bbox is not None:
                img = rgba.crop(bbox)
        buf = BytesIO()
        img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _build_report_header(logo_path: Optional[str], styles: Dict[str, ParagraphStyle]) -> List[Any]:
    """
    First-page report header: the title, with a logo placed to its
    left when one is available and loadable.

    This is a normal flowable placed as the very first entry of the
    story, not a canvas-drawn watermark/background -- it sits in the
    document flow alongside the title text exactly like the Sample ID/
    Run ID table and disclaimer that follow it, and it never repeats
    on later pages simply because it is only ever added once, at the
    top of the story: `SimpleDocTemplate` always starts the story at
    the top of page 1, so "first page only" falls out of that
    placement for free, without any page-number bookkeeping.

    Sizing: the logo's target height is the title Paragraph's own
    measured height (`title.wrap(...)`, at the full content width so
    it's measured as the single line it actually renders as) -- not a
    hardcoded constant -- so it is a literal height match, not an
    approximation, and stays correct if the title style ever changes.
    The logo is cropped to its visible content first (see
    `_load_cropped_logo_image`) so that measured height corresponds to
    what's actually visible, not an oversized transparent canvas.
    Width is always derived from the (cropped) image's own aspect
    ratio, so a non-square white-label logo is never stretched.

    Never raises. A missing file, an unreadable path, a corrupt image,
    or any other load failure is caught here and logged -- report
    generation must never fail because branding didn't load; it
    degrades to the exact plain-title header this codebase rendered
    before this feature existed.
    """
    content_width = _PAGE_W - 2 * _MARGIN
    title = Paragraph("GEPER Clinical Genomic Analysis Report", styles["ReportTitle"])
    _, title_height = title.wrap(content_width, 1000)

    resolved = _resolve_logo_path(logo_path)
    if not resolved:
        return [title]

    try:
        image_buf = _load_cropped_logo_image(resolved)
        native_w, native_h = ImageReader(image_buf).getSize()
        if not native_w or not native_h:
            raise ValueError(f"reported image dimensions were {native_w}x{native_h}")
        logo_h = title_height
        logo_w = logo_h * (native_w / native_h)
        image_buf.seek(0)
        logo = Image(image_buf, width=logo_w, height=logo_h)
    except Exception as exc:  # noqa: BLE001 -- any failure here must degrade to the text-only header, never crash report generation
        logger.warning(f"Could not load report logo from '{resolved}' ({exc}); rendering the text-only header instead.")
        return [title]

    logo_column_width = logo_w + _LOGO_TITLE_GAP
    header_table = Table(
        [[logo, title]],
        colWidths=[logo_column_width, content_width - logo_column_width],
        hAlign="LEFT",
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return [header_table]

def _build_patient_header_table(
    patient: Dict[str, Any], sample_id: str, run_id: str, assembly: Optional[str], styles: Dict[str, ParagraphStyle]
) -> Table:
    lbl, val = styles["TableLabel"], styles["TableValue"]
    rows: List[List[Paragraph]] = []

    if patient["deidentified"]:
        # Per spec: absent patient_meta -> print only the safe label
        # plus Sample ID / Run ID, no DOB/Gender/Physician rows at all.
        rows.append([Paragraph("Patient", lbl), Paragraph(_DEIDENTIFIED_LABEL, val)])
    else:
        rows.append([Paragraph("Patient Name", lbl), Paragraph(patient["patient_name"], val)])
        rows.append([Paragraph("Date of Birth", lbl), Paragraph(patient["dob"] or "Not provided", val)])
        rows.append([Paragraph("Gender", lbl), Paragraph(patient["gender"] or "Not provided", val)])
        rows.append([Paragraph("Referring Physician", lbl), Paragraph(patient["physician"] or "Not provided", val)])

    rows.append([Paragraph("Sample ID", lbl), Paragraph(sample_id, val)])
    rows.append([Paragraph("Run ID", lbl), Paragraph(run_id, val)])
    rows.append([Paragraph("Genome Reference Build", lbl), Paragraph(assembly or "Not specified", val)])
    rows.append([Paragraph("Report Generated", lbl), Paragraph(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), val)])

    table = Table(rows, colWidths=[55 * mm, 110 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, _TABLE_GRID_COLOR),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2f6")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _build_qc_flowables(qc_metrics: Optional[Dict[str, float]], styles: Dict[str, ParagraphStyle]) -> List[Any]:
    """Sequencing QC status table -- PASS/WARNING against `CONFIG.qc_report`'s configurable thresholds (see that class's docstring for why these are placeholders)."""
    is_mock = not qc_metrics
    resolved: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in _MOCK_QC_METRICS.items()}
    if qc_metrics:
        for key, value in qc_metrics.items():
            if key in resolved:
                resolved[key]["value"] = value

    header = [
        Paragraph("Metric", styles["TableHeader"]),
        Paragraph("Result", styles["TableHeader"]),
        Paragraph("Threshold (PASS ≥)", styles["TableHeader"]),
        Paragraph("Status", styles["TableHeader"]),
    ]
    rows = [header]
    row_statuses: List[str] = []
    for key in _QC_METRIC_ORDER:
        value = resolved[key]["value"]
        unit = resolved[key]["unit"]
        threshold = _qc_threshold_pass_min(key)
        status = _qc_status(key, value)
        row_statuses.append(status)
        rows.append([
            Paragraph(_QC_METRIC_LABELS[key], styles["TableLabel"]),
            Paragraph(f"{value:g}{unit}", styles["TableValue"]),
            Paragraph(f"{threshold:g}{unit}", styles["TableValue"]),
            Paragraph(status, styles["StatusPass"] if status == "PASS" else styles["StatusWarn"]),
        ])

    table = Table(rows, colWidths=[55 * mm, 35 * mm, 40 * mm, 30 * mm], hAlign="LEFT")
    style_cmds = [
        ("GRID", (0, 0), (-1, -1), 0.4, _TABLE_GRID_COLOR),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3c5e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for i, status in enumerate(row_statuses, start=1):
        bg = colors.HexColor("#e3f6e8") if status == "PASS" else colors.HexColor("#fdf1d6")
        style_cmds.append(("BACKGROUND", (0, i), (-1, i), bg))
    table.setStyle(TableStyle(style_cmds))

    flowables: List[Any] = [table]
    if is_mock:
        flowables.append(Spacer(1, 2 * mm))
        flowables.append(Paragraph(
            "Note: no run-level QC metrics were supplied to this report; the values above are "
            "illustrative placeholders only, not a real sequencing QC result. Pass real values via "
            "generate_pdf(qc_metrics={...}) from the upstream sequencing/alignment pipeline.",
            styles["Footnote"],
        ))
    return flowables


def _build_variant_section(idx: int, variant_result: Dict[str, Any], styles: Dict[str, ParagraphStyle]) -> List[Any]:
    """Renders one variant's already-computed `clinical_report` dict (see report/clinical_report_builder.py) -- no evidence is re-derived here."""
    variant = variant_result.get("variant", {})
    locus = f"{variant.get('chrom')}:{variant.get('pos')} {variant.get('ref')}>{variant.get('alt')}"
    clinical = variant_result.get("clinical_report")

    flow: List[Any] = [Spacer(1, 4 * mm), Paragraph(f"Finding {idx}: {locus}", styles["SectionHeading"])]

    if not clinical:
        flow.append(Paragraph(
            "No clinical interpretation is available for this variant (the interpretation engine "
            "did not produce a result for it). This is reported as a data gap, not a benign finding.",
            styles["BodyText"],
        ))
        return flow

    flow.append(Paragraph(clinical["executive_summary"], styles["BodyText"]))

    acmg = clinical.get("acmg_classification") or {}
    if acmg.get("classification"):
        flow.append(Paragraph(f"<b>ACMG/AMP Classification:</b> {acmg['classification']}", styles["BodyText"]))

    confidence = clinical.get("confidence") or {}
    if not confidence.get("pending") and confidence.get("label"):
        flow.append(Paragraph(f"<b>Confidence:</b> {confidence['label']}", styles["BodyText"]))

    triggered = acmg.get("triggered_criteria") or []
    if triggered:
        rows = [[Paragraph("Criterion", styles["TableHeader"]), Paragraph("Strength", styles["TableHeader"]), Paragraph("Rationale", styles["TableHeader"])]]
        for crit in triggered:
            rows.append([
                Paragraph(str(crit.get("code") or ""), styles["TableValue"]),
                Paragraph(str(crit.get("strength") or ""), styles["TableValue"]),
                Paragraph(str(crit.get("rationale") or ""), styles["TableValueSmall"]),
            ])
        table = Table(rows, colWidths=[22 * mm, 25 * mm, 113 * mm], hAlign="LEFT")
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.3, _TABLE_GRID_COLOR),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3c5e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        flow.append(Spacer(1, 2 * mm))
        flow.append(table)

    supporting = clinical.get("supporting_evidence") or []
    if supporting:
        flow.append(Spacer(1, 2 * mm))
        flow.append(Paragraph("<b>Supporting Evidence:</b>", styles["BodyText"]))
        flow.extend(Paragraph(f"• {item}", styles["BulletText"]) for item in supporting)

    limitations = clinical.get("limitations") or []
    if limitations:
        flow.append(Spacer(1, 2 * mm))
        flow.append(Paragraph("<b>Limitations:</b>", styles["BodyText"]))
        flow.extend(Paragraph(f"• {item}", styles["BulletText"]) for item in limitations)

    return flow


def _build_signoff_block(styles: Dict[str, ParagraphStyle]) -> KeepTogether:
    """
    Signature + date lines, followed immediately by the standard legal
    disclaimer. Wrapped in `KeepTogether` so the two never split across
    a page break -- ReportLab's flowable layout flows top-down and has
    no first-class "pin to the bottom of the last page" primitive, so
    this is the idiomatic way to say "keep this block intact and let it
    land wherever it naturally falls" rather than a false promise of
    exact bottom-of-page placement.
    """
    line = "_" * 45
    sig_table = Table(
        [
            [Paragraph("Signature:", styles["TableLabel"]), Paragraph(line, styles["TableValue"])],
            [Paragraph("Date:", styles["TableLabel"]), Paragraph(line, styles["TableValue"])],
        ],
        colWidths=[28 * mm, 137 * mm],
        hAlign="LEFT",
    )
    sig_table.setStyle(TableStyle([
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    return KeepTogether([
        Spacer(1, 10 * mm),
        Paragraph("Chief Pathologist / Medical Director", styles["SignoffTitle"]),
        Spacer(1, 4 * mm),
        sig_table,
        Spacer(1, 8 * mm),
        Paragraph(_DISCLAIMER_TEXT, styles["Disclaimer"]),
    ])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def generate_pdf(
    document: Dict[str, Any],
    output_path: str,
    patient_meta: Optional[Union[Dict[str, Any], str]] = None,
    qc_metrics: Optional[Dict[str, float]] = None,
    run_id: Optional[str] = None,
    logo_path: Optional[str] = None,
) -> str:
    """
    Render `document` (the JSON document shape
    `report/json_builder.py::JSONResultBuilder.build()` produces --
    keys: geper_version, generated_at, input_vcf, assembly, vcf_samples,
    variant_count, variants[]) as a clinical-grade, multi-page PDF.

    A single variant_result dict (one entry of `document["variants"]`,
    e.g. for ad hoc testing against one already-annotated variant) is
    also accepted directly -- detected by the absence of a "variants"
    key alongside the presence of a "variant" key.

    `patient_meta`: dict, path to a JSON file, or None -- see
    `_parse_patient_meta`'s docstring for the DPDP Act framing and
    de-identified fallback.
    `qc_metrics`: optional {"mean_coverage_depth": float,
    "bases_at_20x": float, "q30_score": float}; see
    `config.py::QCReportConfig`'s docstring for why GEPER cannot
    compute these itself from a VCF-only pipeline.
    `run_id`: optional caller-supplied run/accession identifier; see
    `_derive_run_id`'s docstring for the fallback when not given.
    `logo_path`: optional per-call override for the header logo image
    (any format ReportLab/PIL can decode -- PNG-with-alpha is the
    common case). None (the default) defers to
    `CONFIG.report_branding` (GEPER's own default mark, or a
    white-label deployment's, via GEPER_REPORT_LOGO_PATH /
    GEPER_REPORT_LOGO_ENABLED); pass "" explicitly to force no logo
    for just this call regardless of that config. See
    `_build_report_header`'s docstring for the placement (first page
    only, alongside the title, never a watermark) and the graceful
    fallback when the file is missing or fails to load.

    Raises on a genuine ReportLab rendering failure (unlike every other
    optional-evidence integration in this codebase, which degrades
    gracefully) -- silently emitting a corrupt/truncated PDF would be
    worse than a loud failure for a document a hospital will file.
    `patient_meta` parsing failures and logo load failures are the
    things this function swallows (see `_parse_patient_meta` and
    `_build_report_header`), per spec.
    """
    if "variants" in document:
        variants = document.get("variants") or []
    elif "variant" in document:
        variants = [document]
    else:
        variants = []

    patient = _parse_patient_meta(patient_meta)
    sample_id = _derive_sample_id(document)
    resolved_run_id = _derive_run_id(document, run_id)
    assembly = document.get("assembly")
    header_label = patient["patient_name"] if not patient["deidentified"] else sample_id

    styles = _build_stylesheet()

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        topMargin=22 * mm,
        bottomMargin=22 * mm,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        title="GEPER Clinical Genomic Analysis Report",
    )

    story: List[Any] = list(_build_report_header(logo_path, styles))
    story.extend([
        Spacer(1, 4 * mm),
        _build_patient_header_table(patient, sample_id, resolved_run_id, assembly, styles),
        Spacer(1, 6 * mm),
        Paragraph("Sequencing Quality Control Metrics", styles["SectionHeading"]),
    ])
    story.extend(_build_qc_flowables(qc_metrics, styles))
    story.append(Spacer(1, 4 * mm))

    for idx, variant_result in enumerate(variants, start=1):
        story.extend(_build_variant_section(idx, variant_result, styles))

    story.append(_build_signoff_block(styles))

    doc.build(
        story,
        onFirstPage=_make_first_page_decoration(),
        onLaterPages=_make_later_page_decoration(header_label),
        canvasmaker=_NumberedCanvas,
    )
    logger.info(f"Wrote clinical PDF report to '{output_path}' ({len(variants)} variant finding(s)).")
    return output_path
