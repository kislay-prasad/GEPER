"""
Short-form clinical PDF report -- the compact companion to the full
report `report/summary.py::generate_pdf` produces.

Same input, same run, same already-computed data: this module renders
the exact `document` shape `report/json_builder.py::JSONResultBuilder.build()`
produces (or the single-`variant_result` ad hoc shape `generate_pdf`
also accepts), and reads only fields the full report already reads --
`clinical_report["executive_summary"]`, `["acmg_classification"]`,
`["confidence"]`, `interpretation_result["gene_symbol"]`,
`normalization["hgvs_c"]/["hgvs_g"]`, and `case_prioritization`.
Nothing here re-derives ACMG classification, confidence, evidence, or
phenotype ranking, and nothing here gathers new evidence; it is purely
a second rendering of what the pipeline already computed. Everything
it prints in prose is a substring of, or a direct field read from,
that data (see `_short_interpretation`).

Styling target: a traditional single-page clinical genetics laboratory
report -- a patient/sample identity block, one compact results block
per variant (gene, HGVS, classification, confidence, a 2-4 sentence
plain-language interpretation), any reviewer-attention flags in one
short line, a dual sign-off (Clinical Scientist / Consultant Clinical
Scientist, the convention used by real genetics labs), and a pointer
to the full report as a companion document. Deliberately NOT a
condensed copy of the long report: no QC table, no ACMG criteria
table, no supporting-evidence list, no limitations list, no PDF
outline -- all of that lives in the full report, which this one names.

Entry point: `generate_short_pdf(document, output_path,
patient_meta=None, run_id=None, logo_path=None,
companion_filename="geper_report_full.pdf")`.

Several helpers are imported from `report/summary.py` rather than
reimplemented (patient-metadata parsing, Sample/Run ID derivation,
reviewer-flag detection, logo loading, the paginating canvas, the
legal disclaimer text). That import direction is deliberate: the two
reports must agree on identity fields, flags, and disclaimer wording,
and duplicating any of it here is exactly how the two documents would
silently drift apart. `report/summary.py` is left completely
unmodified by this feature.

Compliance note: identical to `report/summary.py`'s -- this module
renders whatever patient_meta it is given and implements none of the
India DPDP Act 2023's consent/retention requirements itself. See
`report/summary.py::_parse_patient_meta`'s docstring.
"""

from __future__ import annotations

import functools
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from report.summary import (
    _DEIDENTIFIED_LABEL,
    _DISCLAIMER_TEXT,
    _MARGIN,
    _PAGE_W,
    _NumberedCanvas,
    _consent_value_label,
    _derive_run_id,
    _derive_sample_id,
    _icmr_ai_disclosure_footer_text,
    _load_cropped_logo_image,
    _parse_patient_meta,
    _resolve_logo_path,
    _variant_reviewer_flags,
)
from utils.logger import get_logger
from utils.timezone_utils import format_ist

logger = get_logger(__name__)

_NAVY = colors.HexColor("#1a3c5e")
_GRID = colors.HexColor("#cccccc")

# How many sentences of the already-written executive summary this
# report carries over. The brief asks for "2-4 sentences, plain
# language, similar tone to the existing Executive Summary but even
# more compressed" -- taking the summary's own leading sentences is
# the only way to satisfy "compressed version of that paragraph"
# without writing new clinical prose here, which this module must not
# do (it has no evidence of its own to write from). Three is the
# midpoint of the requested range.
_INTERPRETATION_SENTENCES = 3

# Sentence split on terminal punctuation followed by whitespace. Kept
# deliberately simple: the input is already-generated summary prose
# from `report/clinical_report_builder.py`, not arbitrary text, and a
# mis-split here can only ever mean a slightly longer or shorter
# excerpt -- never a wrong clinical statement, since every sentence
# kept is verbatim.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")

_COMPANION_NOTE = (
    "This is a summary report. The full detailed GEPER report for this run -- including the "
    "sequencing QC table, the complete ACMG/AMP criteria applied to each variant with their "
    "rationale, all supporting evidence, and the stated limitations of each finding -- is "
    "available as a companion document ({companion}) generated from the same analysis run and "
    "filed alongside this one. This summary must be read in conjunction with it."
)


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------


def _build_short_stylesheet() -> Dict[str, ParagraphStyle]:
    """Tighter than `report/summary.py::_build_stylesheet` (smaller sizes, less leading) -- this document's whole purpose is fitting on roughly one page per variant."""
    base = getSampleStyleSheet()
    return {
        "ReportTitle": ParagraphStyle(
            "GeperShortTitle", parent=base["Title"], fontSize=14, textColor=_NAVY, spaceAfter=0, alignment=0
        ),
        "SectionHeading": ParagraphStyle(
            "GeperShortSectionHeading",
            parent=base["Heading2"],
            fontSize=10,
            textColor=_NAVY,
            spaceBefore=4,
            spaceAfter=2,
        ),
        "VariantHeading": ParagraphStyle(
            "GeperShortVariantHeading",
            parent=base["Heading3"],
            fontSize=9.5,
            textColor=_NAVY,
            spaceBefore=3,
            spaceAfter=1,
        ),
        "BodyText": ParagraphStyle("GeperShortBody", parent=base["BodyText"], fontSize=8.5, leading=11, spaceAfter=0),
        "TableLabel": ParagraphStyle(
            "GeperShortTableLabel", parent=base["BodyText"], fontSize=8, leading=10, fontName="Helvetica-Bold"
        ),
        "TableValue": ParagraphStyle("GeperShortTableValue", parent=base["BodyText"], fontSize=8, leading=10),
        "Flag": ParagraphStyle(
            "GeperShortFlag",
            parent=base["BodyText"],
            fontSize=8,
            leading=10,
            textColor=colors.HexColor("#9a6a00"),
            fontName="Helvetica-Bold",
        ),
        "Footnote": ParagraphStyle(
            "GeperShortFootnote", parent=base["BodyText"], fontSize=7, leading=9, textColor=colors.grey
        ),
        "SignoffRole": ParagraphStyle(
            "GeperShortSignoffRole", parent=base["BodyText"], fontSize=8, leading=10, fontName="Helvetica-Bold"
        ),
    }


# ---------------------------------------------------------------------------
# Field extraction (read-only over data the full report already has)
# ---------------------------------------------------------------------------


def _variant_locus(variant_result: Dict[str, Any]) -> str:
    variant = variant_result.get("variant") or {}
    return f"{variant.get('chrom')}:{variant.get('pos')} {variant.get('ref')}>{variant.get('alt')}"


def _variant_hgvs(variant_result: Dict[str, Any]) -> str:
    """
    Best available HGVS notation for this variant, from the same
    `normalization` stage dict the JSON document already carries
    (`pipeline/orchestrator.py::_run_normalization_stage` /
    `_attach_hgvs_c`): transcript-level HGVS.c when the transcript
    stage resolved structure for it, otherwise genomic HGVS.g,
    otherwise the plain chrom:pos ref>alt locus. Never synthesizes
    notation of its own.
    """
    normalization = variant_result.get("normalization") or {}
    return normalization.get("hgvs_c") or normalization.get("hgvs_g") or _variant_locus(variant_result)


def _variant_gene(variant_result: Dict[str, Any]) -> Optional[str]:
    return (variant_result.get("interpretation_result") or {}).get("gene_symbol")


def _classification_text(clinical: Optional[Dict[str, Any]]) -> str:
    return ((clinical or {}).get("acmg_classification") or {}).get("classification") or "Not classified"


def _confidence_text(clinical: Optional[Dict[str, Any]]) -> str:
    """Same confidence rendering the full report's Clinician Summary table uses (`report/summary.py::_build_clinician_summary_table`), so the two documents never disagree on a variant's confidence."""
    confidence = (clinical or {}).get("confidence") or {}
    if confidence.get("pending", True):
        return "Pending"
    score = confidence.get("score")
    label = confidence.get("label") or "n/a"
    if isinstance(score, (int, float)):
        return f"{label} ({score:.0f}%)"
    return str(label)


def _short_interpretation(clinical: Optional[Dict[str, Any]]) -> str:
    """
    The 2-4 sentence plain-language interpretation paragraph: the
    leading `_INTERPRETATION_SENTENCES` sentences of the executive
    summary `report/clinical_report_builder.py` already wrote for this
    variant, verbatim.

    Verbatim excerpting rather than paraphrase is a correctness
    requirement, not a shortcut -- this module has no evidence of its
    own to reason from, so any rewording it invented would be a
    clinical claim nothing in the pipeline actually supports, and could
    silently contradict the full report's own wording for the same
    variant. Truncation is signalled with an ellipsis so a reader can
    see the paragraph continues in the companion document.

    Falls back to the same "data gap, not a benign finding" framing
    `report/summary.py::_build_variant_section` uses when no clinical
    interpretation exists for the variant at all.
    """
    if not clinical:
        return (
            "No clinical interpretation is available for this variant. This is reported as a "
            "data gap, not a benign finding."
        )
    text = (clinical.get("executive_summary") or "").strip()
    if not text:
        return (
            "No interpretive summary was generated for this variant; see the full report for the underlying evidence."
        )

    sentences = [s for s in _SENTENCE_END_RE.split(text) if s.strip()]
    if len(sentences) <= _INTERPRETATION_SENTENCES:
        return text
    return " ".join(sentences[:_INTERPRETATION_SENTENCES]).rstrip() + " [...]"


_ORDERING_NOTE_TEXT = (
    "Findings below are ordered by case-level phenotype-match rank, not VCF order -- each "
    "“Finding N” label is still the variant's original finding number."
)


def _ordered_variants(variants: List[Dict[str, Any]]) -> List[tuple]:
    """
    `(original_finding_number, variant_result)` pairs, ordered exactly
    the way the full report's Clinician Summary table orders its rows:
    by case-level phenotype rank when `case_prioritization` is present
    for this run, otherwise VCF order. The printed finding number is
    always the original 1-based one either way, so a variant carries
    the same "Finding N" label in both documents.
    """
    indexed = list(enumerate(variants, start=1))
    if not any(isinstance(vr.get("case_prioritization"), dict) for vr in variants):
        return indexed

    def _rank_key(pair):
        idx, vr = pair
        rank = (vr.get("case_prioritization") or {}).get("case_rank")
        return (rank is None, rank if rank is not None else 0, idx)

    return sorted(indexed, key=_rank_key)


# ---------------------------------------------------------------------------
# Flowable builders
# ---------------------------------------------------------------------------


def _build_short_header(logo_path: Optional[str], styles: Dict[str, ParagraphStyle]) -> List[Any]:
    """Compact letterhead line: optional logo plus the report title. Same graceful degradation as the full report -- a missing or corrupt logo logs and falls back to the text-only title, never fails the render."""
    content_width = _PAGE_W - 2 * _MARGIN
    title = Paragraph("GEPER Clinical Genomic Summary Report", styles["ReportTitle"])
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
    except Exception as exc:  # noqa: BLE001 - branding must never break report generation
        logger.warning(f"Could not load report logo from '{resolved}' ({exc}); rendering the text-only header instead.")
        return [title]

    gap = 4 * mm
    header = Table([[logo, title]], colWidths=[logo_w + gap, content_width - logo_w - gap], hAlign="LEFT")
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return [header]


def _build_identity_block(
    patient: Dict[str, Any],
    sample_id: str,
    run_id: str,
    assembly: Optional[str],
    variant_count: int,
    styles: Dict[str, ParagraphStyle],
) -> Table:
    """
    Patient/sample identity block -- the same fields the full report's
    header table carries (`report/summary.py::_build_patient_header_table`,
    including its de-identified fallback behaviour), laid out as a
    two-column-pair grid so it occupies about half the vertical space.
    """
    lbl, val = styles["TableLabel"], styles["TableValue"]
    pairs: List[tuple] = []

    if patient["deidentified"]:
        pairs.append(("Patient", _DEIDENTIFIED_LABEL))
    else:
        pairs.append(("Patient Name", patient["patient_name"]))
        pairs.append(("Date of Birth", patient["dob"] or "Not provided"))
        pairs.append(("Gender", patient["gender"] or "Not provided"))
        pairs.append(("Referring Physician", patient["physician"] or "Not provided"))

    consent = patient.get("consent")
    if consent:
        # DPDP Act 2023 consent-metadata rows -- same data, same "only
        # shown when actually supplied" rule as the full report's
        # `report/summary.py::_consent_rows` (deliberately independent
        # of `patient["deidentified"]`; see `_parse_consent`'s
        # docstring for why a de-identified sample can still carry a
        # real consent record).
        pairs.append(("Consent -- Clinical Reporting", _consent_value_label(consent["clinical_reporting"])))
        pairs.append(("Consent -- Research Use", _consent_value_label(consent["research"])))
        if consent.get("timestamp"):
            pairs.append(("Consent Recorded", consent["timestamp"]))

    pairs.append(("Sample ID", sample_id))
    pairs.append(("Run ID", run_id))
    pairs.append(("Reference Build", assembly or "Not specified"))
    pairs.append(("Variants Reported", str(variant_count)))
    # IST for the same reason the full report uses it (Indian
    # hospitals); the underlying timestamp is still UTC.
    pairs.append(("Report Generated", format_ist(datetime.now(timezone.utc))))

    rows: List[List[Any]] = []
    for i in range(0, len(pairs), 2):
        chunk = pairs[i : i + 2]
        row = [Paragraph(chunk[0][0], lbl), Paragraph(chunk[0][1], val)]
        if len(chunk) == 2:
            row += [Paragraph(chunk[1][0], lbl), Paragraph(chunk[1][1], val)]
        else:
            row += [Paragraph("", lbl), Paragraph("", val)]
        rows.append(row)

    table = Table(rows, colWidths=[32 * mm, 50 * mm, 32 * mm, 51 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, _GRID),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2f6")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#eef2f6")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return table


def _build_variant_block(idx: int, variant_result: Dict[str, Any], styles: Dict[str, ParagraphStyle]) -> List[Any]:
    """
    One variant's compact result block: a heading line (finding number,
    gene, HGVS), a one-row result strip (classification / confidence /
    locus), the short interpretation paragraph, and -- only when there
    are any -- a single-line reviewer-attention flag list.

    Wrapped in `KeepTogether` by the caller's story assembly so a
    single variant's block never splits across a page boundary; that,
    plus the absence of any evidence/criteria tables, is what keeps
    this at roughly one variant per third of a page rather than the
    full report's multiple pages each.
    """
    clinical = variant_result.get("clinical_report")
    gene = _variant_gene(variant_result)
    hgvs = _variant_hgvs(variant_result)
    heading = f"Finding {idx}: {gene + ' ' if gene else ''}{hgvs}"

    lbl, val = styles["TableLabel"], styles["TableValue"]
    strip = Table(
        [
            [
                Paragraph("Gene", lbl),
                Paragraph(gene or "Not resolved", val),
                Paragraph("Classification", lbl),
                Paragraph(_classification_text(clinical), val),
                Paragraph("Confidence", lbl),
                Paragraph(_confidence_text(clinical), val),
            ]
        ],
        colWidths=[13 * mm, 25 * mm, 24 * mm, 40 * mm, 22 * mm, 41 * mm],
        hAlign="LEFT",
    )
    strip.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, _GRID),
                ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#eef2f6")),
                ("BACKGROUND", (2, 0), (2, 0), colors.HexColor("#eef2f6")),
                ("BACKGROUND", (4, 0), (4, 0), colors.HexColor("#eef2f6")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )

    flow: List[Any] = [
        Paragraph(heading, styles["VariantHeading"]),
        strip,
        Spacer(1, 1.5 * mm),
        Paragraph(_short_interpretation(clinical), styles["BodyText"]),
    ]

    # Clinician override (geper/review/signoff.py's "override" command) --
    # layered on top of, never substituting for, GEPER's own
    # classification in the strip above: both are shown, explicitly
    # labelled. Absent for every variant no clinician has overridden.
    override = ((clinical or {}).get("acmg_classification") or {}).get("clinician_override")
    if override:
        flow.append(Spacer(1, 1 * mm))
        flow.append(
            Paragraph(
                f"Clinician override -- GEPER: {override.get('original_classification') or 'Not classified'}; "
                f"Override: {override.get('new_classification')} -- {override.get('reason')}",
                styles["Flag"],
            )
        )

    # Same flag detection the full report's summary page uses, so the
    # two documents can never disagree about which findings need a
    # reviewer's attention. "!" rather than a warning-sign glyph for
    # the same encoding reason documented in report/summary.py.
    flags = _variant_reviewer_flags(variant_result, clinical)
    if flags:
        flow.append(Spacer(1, 1 * mm))
        flow.append(Paragraph("! Reviewer attention: " + "; ".join(flags) + ".", styles["Flag"]))

    flow.append(Spacer(1, 3 * mm))
    return flow


def _build_signoff_block(styles: Dict[str, ParagraphStyle]) -> List[Any]:
    """
    Dual sign-off: Clinical Scientist and Consultant Clinical
    Scientist, side by side -- the two-signature convention diagnostic
    genetics laboratories use (an authoring scientist plus an
    authorising consultant). Distinct from the full report's single
    "Chief Pathologist / Medical Director" block; both are unsigned
    rule/date lines, neither asserts that anyone has actually signed.

    Only the heading + signature table are wrapped in `KeepTogether`
    (so a signature line itself never splits across a page break); the
    trailing disclaimer paragraph is intentionally left outside that
    atomic unit. It is plain legal boilerplate that reads fine even if
    it starts on the next page or word-wraps across a page boundary,
    and folding it into the same KeepTogether as the signature table
    was inflating the block's "must all fit together" height by the
    disclaimer's own ~40pt for no visual benefit -- on a page with
    genuine but tight remaining room (e.g. a 5-variant run), that was
    enough to push the whole block, disclaimer included, onto an
    otherwise near-empty next page.
    """
    line = "_" * 32
    lbl, val = styles["SignoffRole"], styles["TableValue"]
    table = Table(
        [
            [Paragraph("Clinical Scientist", lbl), Paragraph("Consultant Clinical Scientist", lbl)],
            [Paragraph(line, val), Paragraph(line, val)],
            [Paragraph("Name / Signature", styles["Footnote"]), Paragraph("Name / Signature", styles["Footnote"])],
            [Paragraph(line, val), Paragraph(line, val)],
            [Paragraph("Date", styles["Footnote"]), Paragraph("Date", styles["Footnote"])],
        ],
        colWidths=[82 * mm, 83 * mm],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )
    return [
        KeepTogether(
            [
                Spacer(1, 4 * mm),
                Paragraph("Authorisation", styles["SectionHeading"]),
                table,
            ]
        ),
        Spacer(1, 3 * mm),
        Paragraph(_DISCLAIMER_TEXT, styles["Footnote"]),
    ]


def _make_page_decoration(header_label: str):
    """One running rule + muted identity line on every page (this document is short enough that a distinct first-page treatment would be noise)."""

    def _draw(canvas, _doc) -> None:
        canvas.saveState()
        canvas.setStrokeColor(_NAVY)
        canvas.setLineWidth(1.0)
        canvas.line(_MARGIN, A4[1] - 14 * mm, _PAGE_W - _MARGIN, A4[1] - 14 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(_MARGIN, A4[1] - 12 * mm, f"Summary Report -- {header_label}")
        canvas.restoreState()

    return _draw


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def generate_short_pdf(
    document: Dict[str, Any],
    output_path: str,
    patient_meta: Optional[Union[Dict[str, Any], str]] = None,
    run_id: Optional[str] = None,
    logo_path: Optional[str] = None,
    companion_filename: str = "geper_report_full.pdf",
) -> str:
    """
    Render `document` as the short-form clinical summary PDF at
    `output_path` and return that path.

    Accepts exactly the same two input shapes
    `report/summary.py::generate_pdf` accepts (the full JSON document,
    or a single `variant_result` dict detected by the absence of a
    "variants" key alongside the presence of a "variant" key), and the
    same `patient_meta` / `run_id` / `logo_path` semantics -- see that
    function's docstring. `qc_metrics` is deliberately NOT accepted:
    the QC table belongs to the full report, and a short-form clinical
    summary that reprinted it would be the "condensed copy of the long
    report" this document exists to avoid.

    `companion_filename` is the name printed in the companion-document
    note; the caller passes whatever it actually wrote the full report
    as, so the note never names a file that isn't there.

    Raises on a genuine ReportLab rendering failure, same as
    `generate_pdf` -- for the same reason (a silently corrupt clinical
    PDF is worse than a loud failure). The orchestrator wraps the call
    so a failure here cannot invalidate an otherwise-successful run.
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

    styles = _build_short_stylesheet()

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        topMargin=20 * mm,
        # Bumped from 18mm: room for the mandatory ICMR AI-disclosure
        # footer this report shares with the full report (both use
        # `_NumberedCanvas` -- see that class's docstring in
        # report/summary.py) above the existing rule/page-number band.
        bottomMargin=28 * mm,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        title="GEPER Clinical Genomic Summary Report",
    )

    story: List[Any] = list(_build_short_header(logo_path, styles))
    story.append(Spacer(1, 3 * mm))
    story.append(_build_identity_block(patient, sample_id, resolved_run_id, assembly, len(variants), styles))
    story.append(Spacer(1, 4 * mm))

    story.append(Paragraph("Result", styles["SectionHeading"]))
    if any(isinstance(vr.get("case_prioritization"), dict) for vr in variants):
        # Same reordering the full report's Clinician Summary table
        # applies and explains (report/summary.py::_build_clinician_summary_table);
        # reusing its wording so a reader who has seen one report
        # recognizes the other's explanation for the same behaviour.
        story.append(Paragraph(_ORDERING_NOTE_TEXT, styles["Footnote"]))
        story.append(Spacer(1, 1.5 * mm))
    if variants:
        for idx, variant_result in _ordered_variants(variants):
            story.append(KeepTogether(_build_variant_block(idx, variant_result, styles)))
    else:
        story.append(Paragraph("No variants were analysed in this run; no findings are reported.", styles["BodyText"]))

    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(_COMPANION_NOTE.format(companion=companion_filename), styles["Footnote"]))
    story.extend(_build_signoff_block(styles))

    decoration = _make_page_decoration(header_label)
    # Mandatory ICMR AI-disclosure footer (see `_NumberedCanvas`'s
    # docstring in report/summary.py) -- same mechanism as the full
    # report, so both output formats can never drift on this text.
    doc.build(
        story,
        onFirstPage=decoration,
        onLaterPages=decoration,
        canvasmaker=functools.partial(_NumberedCanvas, footer_text=_icmr_ai_disclosure_footer_text(patient)),
    )
    logger.info(f"Wrote short-form clinical PDF report to '{output_path}' ({len(variants)} variant finding(s)).")
    return output_path
