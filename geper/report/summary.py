"""
Clinical-grade PDF report generator.

Renders the same JSON document shape `report/json_builder.py::JSONResultBuilder.build()`
produces as a multi-page, ReportLab-based PDF suitable for a hospital
laboratory's diagnostic record: a title/logo header (first page only --
see `_build_report_header`), a one-page skimmable Clinician Summary
(`_build_clinician_summary_flowables` -- sample identity, one compact row
per variant, and any reviewer-attention flags, ending in a page break; see
that function's docstring for how it differs from each variant's own
"Executive Summary" paragraph), a patient/sample header, a sequencing
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

import functools
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
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus.flowables import Flowable

from annotation.thousand_genomes_sas import DIASPORA_DISCLOSURE, SAMPLE_SIZE_DISCLOSURE
from config import CONFIG
from pipeline.provenance import EVIDENCE_SOURCE_TO_PROVENANCE_PREFIX
from utils.logger import get_logger
from utils.service_health import HEALTH
from utils.timezone_utils import format_ist

logger = get_logger(__name__)

_DEIDENTIFIED_LABEL = "De-identified / Research Sample"

_PAGE_W, _PAGE_H = A4
_MARGIN = 20 * mm

# Footer vertical layout, bottom-up: page number (bottom-most, right-
# aligned) -- unchanged in spirit from before this feature -- then the
# mandatory AI-disclosure line(s) above it, then the separating rule at
# the top of the footer band. `_FOOTER_RULE_Y` sits well below every
# caller's `bottomMargin` (see `generate_pdf`/`generate_short_pdf`) so
# body flowables never collide with it; `_FOOTER_PAGE_NUM_Y` stays
# comfortably below `_FOOTER_RULE_Y` even for a 3-line wrapped
# disclosure (a physician string long enough to wrap that far is
# already an edge case, but should still never overlap the page number).
_FOOTER_RULE_Y = 22 * mm
_FOOTER_PAGE_NUM_Y = 8 * mm

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

# Mandatory ICMR-style AI-disclosure footer, printed on EVERY page of
# every GEPER clinical PDF (see `_icmr_ai_disclosure_footer_text` for
# how the "reviewed" variant is built, and `_NumberedCanvas` for how it
# is actually drawn on each page alongside the existing "Page X of Y").
# Never gated behind any config flag -- there is no "skip footer" or
# "minimal report" option anywhere in this codebase (confirmed by
# reading `config.py` and every `generate_pdf`/`generate_short_pdf`
# call site) for this to be accidentally exempted from; if one is ever
# added, it must not apply here.
_ICMR_DRAFT_FOOTER_TEXT = "DRAFT -- NOT FOR PATIENT USE. Awaiting clinical review."


def _icmr_ai_disclosure_footer_text(patient: Dict[str, Any]) -> str:
    """
    The mandatory per-page AI-disclosure footer text for this report.

    Reuses `patient["physician"]` -- the one reviewing-clinician field
    `_parse_patient_meta` already parses out of `--patient-meta` (see
    that function's docstring) -- as the sole signal for "has a
    clinician actually reviewed this run", rather than inventing a new
    input path GEPER doesn't otherwise have. GEPER has no separate
    medical-registration-number or hospital/lab-name field anywhere in
    `patient_meta` (see `patient_metadata.example.json`: `physician` is
    the one free-text clinician field, e.g. "Dr. A. Sharma, MD"), so
    rather than printing the requested template's
    "[Medical Registration Number], [Hospital/Lab Name]" placeholders
    as literal bracketed text that could be mistaken for real,
    fabricated-looking identifiers, this renders only the identifying
    string GEPER actually has. A deployment that wants those additional
    identifiers rendered should supply them as part of `physician`
    (e.g. "Dr. A. Sharma, MD, Reg. No. 12345, ABC Diagnostics") --
    still exactly the same one input field, no new path.

    Before any reviewing-clinician info exists at all (`physician` is
    `None` -- `--patient-meta` omitted, a corrupt/malformed file, or a
    file that supplies `patient_name` but not `physician`), this
    returns `_ICMR_DRAFT_FOOTER_TEXT` instead: a report nobody has
    attested to reviewing must never look indistinguishable, at a
    glance, from one that's actually ready for clinical use.
    """
    physician = (patient or {}).get("physician")
    if not physician:
        return _ICMR_DRAFT_FOOTER_TEXT
    return (
        f"This report was generated using AI-assisted genomic interpretation and has been "
        f"reviewed by {physician}. This is not a standalone diagnosis."
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


def _parse_consent(raw_consent: Any) -> Optional[Dict[str, Any]]:
    """
    DPDP Act 2023 consent-metadata capture -- deliberately small: this
    function only reads whatever `{"clinical_reporting": bool,
    "research": bool, "timestamp": str}` object (if any) was supplied
    under `patient_meta["consent"]` and hands it back verbatim
    (type-validated field by field). It does NOT implement consent
    storage, an erasure/withdrawal workflow, an IP-address/hash audit
    trail, or any database -- those are real DPDP Act requirements, but
    belong to a much larger patient-intake/storage-lifecycle system
    that doesn't exist in GEPER yet and shouldn't be bolted on here
    speculatively. This is metadata capture only, not a compliance
    guarantee -- the actual consent process, its storage, and its
    retention remain the deploying lab's responsibility (same framing
    `_parse_patient_meta`'s own docstring already uses for
    patient_name/dob/gender/physician).

    Returns `None` -- never a fabricated `{"clinical_reporting": False,
    ...}` -- whenever no usable consent object was supplied at all: no
    `"consent"` key, a non-dict value, or a dict where every one of the
    three fields was itself missing/malformed. Deliberately independent
    of `patient_name`: unlike the DOB/Gender/Physician fields below
    (which `_parse_patient_meta` only returns for a *named*, non-de-
    identified patient), a de-identified research sample can still
    carry a genuine, documented consent record -- consent tracking and
    patient identification are two different DPDP Act concerns, and
    conflating them would silently drop real consent data for GEPER's
    own default "de-identified / research sample" framing (see this
    module's own disclaimer text), the single most common case this
    field needs to cover.

    Each of `clinical_reporting`/`research` is independently `None`
    (not coerced to `False`) when that one field wasn't stated as an
    actual JSON boolean -- a stray string like `"true"` is a type
    mismatch, not evidence of consent either way, so it is treated as
    "not stated" rather than guessed. `timestamp` is passed through
    exactly as supplied (GEPER never fabricates a "when consent was
    given" value from its own render-time clock -- that would
    misrepresent when the patient actually consented).
    """
    if not isinstance(raw_consent, dict):
        return None

    def _as_bool(value: Any) -> Optional[bool]:
        return value if isinstance(value, bool) else None

    def _as_str(value: Any) -> Optional[str]:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return None

    clinical_reporting = _as_bool(raw_consent.get("clinical_reporting"))
    research = _as_bool(raw_consent.get("research"))
    timestamp = _as_str(raw_consent.get("timestamp"))

    if clinical_reporting is None and research is None and timestamp is None:
        return None  # a "consent" object was present but carried nothing usable -- same as absent

    return {"clinical_reporting": clinical_reporting, "research": research, "timestamp": timestamp}


def _parse_patient_meta(patient_meta: Optional[Union[Dict[str, Any], str]]) -> Dict[str, Any]:
    """
    Accepts a dict, a path to a JSON file, or None. Returns
    {"patient_name", "dob", "gender", "physician", "deidentified",
    "consent"}.

    Never raises: a missing file, a corrupt/malformed JSON body, or a
    body without a usable patient_name all fall back to the safe
    de-identified default -- exactly per spec ("a corrupt JSON file
    never crashes the pipeline" / "if absent, cleanly default"). The
    parse failure is logged for operators; it is deliberately NOT
    surfaced in the rendered report itself (a clinical document should
    show the safe fallback state, not internal parsing diagnostics).

    `consent` and `physician`: resolved regardless of whether
    `patient_name` was usable, so both are computed before the name-gate
    below and threaded into both the de-identified and named return
    shapes. Two independent reasons this matters, one per field:

      - `consent` (see `_parse_consent`'s docstring): consent tracking
        is independent of patient identification -- a de-identified
        research sample can still carry a real, documented consent
        record.
      - `physician`: REVIEWING-CLINICIAN identity is a different
        concern from PATIENT identity. GEPER's stated default is a
        de-identified/research sample (see this module's own
        disclaimer text) -- that must remain the common case a lab can
        stay in while still having a named clinician review and sign
        off on a report (see `_icmr_ai_disclosure_footer_text`, which
        reads exactly this field to decide "DRAFT" vs "reviewed by
        {physician}"). Coupling `physician` to `patient_name` would
        force a lab to fabricate or attach a real patient name purely
        to unlock sign-off on an otherwise-intentionally-de-identified
        run -- backwards from what de-identification is for. (Earlier
        versions of this function dropped `physician` in the de-
        identified branch, matching every other identity field; that
        turned out to be wrong for exactly this reason once a real
        sign-off workflow needed to depend on it -- see
        `geper/review/signoff.py`'s `approve` command.)

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
    defaults: Dict[str, Any] = {
        "patient_name": _DEIDENTIFIED_LABEL,
        "dob": None,
        "gender": None,
        "physician": None,
        "deidentified": True,
        "consent": None,
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

    consent = _parse_consent(raw.get("consent"))
    physician = (raw.get("physician") or "").strip() or None

    name = (raw.get("patient_name") or "").strip()
    if not name:
        # no usable name -> treat exactly like "absent" for every
        # PATIENT-identity field (dob/gender), per spec -- except
        # `consent` and `physician`, both independent of patient
        # identity (see this function's docstring), which must still
        # come through.
        result = dict(defaults)
        result["consent"] = consent
        result["physician"] = physician
        return result

    return {
        "patient_name": name,
        "dob": (raw.get("dob") or "").strip() or None,
        "gender": (raw.get("gender") or "").strip() or None,
        "physician": physician,
        "deidentified": False,
        "consent": consent,
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

    `footer_text`: the mandatory ICMR-style AI-disclosure line drawn on
    every page alongside "Page X of Y" (see
    `_icmr_ai_disclosure_footer_text`) -- part of THIS class, not a
    separate mechanism, precisely because this two-pass buffering is
    what already guarantees "every single page, unconditionally" for
    the pagination footer; reusing it is what gives the same guarantee
    here rather than building a second, possibly-divergent page-decorator
    path. Passed in via `functools.partial(_NumberedCanvas,
    footer_text=...)` as the `canvasmaker` argument to
    `SimpleDocTemplate.build()` (see `generate_pdf`/
    `report/summary_short.py::generate_short_pdf`, both of which share
    this one class) since `canvasmaker` is invoked as a plain callable,
    not a pre-built instance -- `functools.partial` is the standard way
    to bind that one extra argument without needing a bespoke subclass
    per call site.
    """

    def __init__(self, *args, footer_text: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states: List[Dict[str, Any]] = []
        self._icmr_footer_text = footer_text

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
        content_width = _PAGE_W - 2 * _MARGIN
        footer_font_size = 6.5
        footer_line_height = 8

        self.setStrokeColor(colors.lightgrey)
        self.line(_MARGIN, _FOOTER_RULE_Y, _PAGE_W - _MARGIN, _FOOTER_RULE_Y)

        # Mandatory AI-disclosure line(s) -- always drawn, on every
        # page, unconditionally (see `_icmr_ai_disclosure_footer_text`'s
        # docstring for why this can never be blank: even the "no
        # reviewing clinician yet" state still returns real text).
        self.setFont("Helvetica-Bold", footer_font_size)
        self.setFillColor(colors.black)
        lines = simpleSplit(self._icmr_footer_text, "Helvetica-Bold", footer_font_size, content_width)
        y = _FOOTER_RULE_Y - footer_line_height
        for line in lines:
            self.drawCentredString(_PAGE_W / 2, y, line)
            y -= footer_line_height

        self.setFont("Helvetica", 8)
        self.setFillColor(colors.grey)
        self.drawRightString(_PAGE_W - _MARGIN, _FOOTER_PAGE_NUM_Y, f"Page {self._pageNumber} of {total_pages}")


# ---------------------------------------------------------------------------
# PDF bookmarks (outline/TOC)
# ---------------------------------------------------------------------------


class _Bookmark(Flowable):
    """
    A zero-size flowable that, when the document layout engine reaches it
    in the normal top-to-bottom flow, marks the current page as the
    target of a named PDF bookmark and adds a matching entry to the PDF's
    outline (the navigation panel most PDF viewers show in a sidebar).

    This is the standard ReportLab/Platypus recipe for outline entries --
    `Canvas.bookmarkPage`/`Canvas.addOutlineEntry` are canvas-level calls,
    and a flowable's `draw()` is the only place in Platypus's flowable
    API that has the live canvas for the exact page this content actually
    lands on (which page that turns out to be depends on everything laid
    out before it, so it cannot be known any earlier). `width`/`height`
    are both 0 so inserting this never shifts any surrounding layout --
    it occupies no visible space itself.

    Compatible with `_NumberedCanvas`'s two-pass buffering (`showPage`
    snapshots `self.__dict__`, `save` replays it) because `bookmarkPage`/
    `addOutlineEntry` mutate the canvas's underlying PDF document object
    directly, not the per-page graphics-state attributes that trick
    snapshots and restores -- the same reason page content itself
    survives that buffering unaffected.

    `key` must be unique across the whole document; `title` is the
    (non-unique) label shown in the outline; `level` follows
    `addOutlineEntry`'s own rule (may repeat or drop to any shallower
    level freely, but may only go one level deeper than the last entry
    at a time) -- see that method's docstring for the full rule.
    """

    def __init__(self, key: str, title: str, level: int = 0):
        Flowable.__init__(self)
        self.key = key
        self.title = title
        self.level = level
        self.width = 0
        self.height = 0

    def wrap(self, availWidth, availHeight):
        return 0, 0

    def draw(self):
        self.canv.bookmarkPage(self.key)
        self.canv.addOutlineEntry(self.title, self.key, level=self.level, closed=False)


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
        "ReportTitle": ParagraphStyle(
            "GeperReportTitle", parent=base["Title"], fontSize=16, textColor=navy, spaceAfter=2
        ),
        "SectionHeading": ParagraphStyle(
            "GeperSectionHeading", parent=base["Heading2"], fontSize=12, textColor=navy, spaceBefore=6, spaceAfter=3
        ),
        "BodyText": ParagraphStyle("GeperBodyText", parent=base["BodyText"], fontSize=9.5, leading=13),
        "BulletText": ParagraphStyle("GeperBulletText", parent=base["BodyText"], fontSize=9, leading=12, leftIndent=10),
        "TableHeader": ParagraphStyle(
            "GeperTableHeader", parent=base["BodyText"], fontSize=9, textColor=colors.white, fontName="Helvetica-Bold"
        ),
        "TableLabel": ParagraphStyle("GeperTableLabel", parent=base["BodyText"], fontSize=9, fontName="Helvetica-Bold"),
        "TableValue": ParagraphStyle("GeperTableValue", parent=base["BodyText"], fontSize=9),
        "TableValueSmall": ParagraphStyle("GeperTableValueSmall", parent=base["BodyText"], fontSize=8, leading=10),
        "StatusPass": ParagraphStyle(
            "GeperStatusPass",
            parent=base["BodyText"],
            fontSize=9,
            textColor=colors.HexColor("#1a7a35"),
            fontName="Helvetica-Bold",
        ),
        "StatusWarn": ParagraphStyle(
            "GeperStatusWarn",
            parent=base["BodyText"],
            fontSize=9,
            textColor=colors.HexColor("#9a6a00"),
            fontName="Helvetica-Bold",
        ),
        "Footnote": ParagraphStyle(
            "GeperFootnote", parent=base["BodyText"], fontSize=7.5, textColor=colors.grey, leading=10
        ),
        "SignoffTitle": ParagraphStyle("GeperSignoffTitle", parent=base["Heading3"], fontSize=10),
        "Disclaimer": ParagraphStyle(
            "GeperDisclaimer", parent=base["BodyText"], fontSize=7.5, textColor=colors.grey, leading=10
        ),
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
    header_table.setStyle(
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
    return [header_table]


def _consent_value_label(value: Optional[bool]) -> str:
    """ "Yes"/"No"/"Not stated" -- matches the "Not provided" convention `_build_patient_header_table` already uses for a missing DOB/Gender/Physician, applied to a tri-state (True/False/None) field instead of a missing string."""
    if value is None:
        return "Not stated"
    return "Yes" if value else "No"


def _consent_rows(patient: Dict[str, Any], lbl: ParagraphStyle, val: ParagraphStyle) -> List[List[Paragraph]]:
    """
    DPDP Act 2023 consent-metadata rows -- shared by the full report's
    `_build_patient_header_table` and the short report's
    `_build_identity_block`-equivalent construction in
    report/summary_short.py, both of which pass this the same `patient`
    dict `_parse_patient_meta` already produced. Returns `[]` (no rows
    at all -- not three "Not stated" rows) when no `consent` object was
    supplied in `patient_meta`, matching `_parse_consent`'s "absent,
    not fabricated" contract: a report with no consent data at all
    should not manufacture the appearance of a compliance record where
    none exists. Deliberately independent of `patient["deidentified"]`
    -- see `_parse_consent`'s docstring for why a de-identified sample
    can still carry a real, documented consent record.
    """
    consent = patient.get("consent")
    if not consent:
        return []
    rows = [
        [
            Paragraph("Consent -- Clinical Reporting", lbl),
            Paragraph(_consent_value_label(consent["clinical_reporting"]), val),
        ],
        [Paragraph("Consent -- Research Use", lbl), Paragraph(_consent_value_label(consent["research"]), val)],
    ]
    if consent.get("timestamp"):
        rows.append([Paragraph("Consent Recorded", lbl), Paragraph(consent["timestamp"], val)])
    return rows


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

    rows.extend(_consent_rows(patient, lbl, val))

    rows.append([Paragraph("Sample ID", lbl), Paragraph(sample_id, val)])
    rows.append([Paragraph("Run ID", lbl), Paragraph(run_id, val)])
    rows.append([Paragraph("Genome Reference Build", lbl), Paragraph(assembly or "Not specified", val)])
    # Displayed in IST (report is for Indian hospitals) -- the
    # underlying timestamp is still generated in UTC
    # (`datetime.now(timezone.utc)`) and only converted for this
    # human-facing label; nothing stored/logged changes. See
    # `utils/timezone_utils.py`.
    rows.append([Paragraph("Report Generated", lbl), Paragraph(format_ist(datetime.now(timezone.utc)), val)])

    table = Table(rows, colWidths=[55 * mm, 110 * mm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, _TABLE_GRID_COLOR),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2f6")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
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
        rows.append(
            [
                Paragraph(_QC_METRIC_LABELS[key], styles["TableLabel"]),
                Paragraph(f"{value:g}{unit}", styles["TableValue"]),
                Paragraph(f"{threshold:g}{unit}", styles["TableValue"]),
                Paragraph(status, styles["StatusPass"] if status == "PASS" else styles["StatusWarn"]),
            ]
        )

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
        flowables.append(
            Paragraph(
                "Note: no run-level QC metrics were supplied to this report; the values above are "
                "illustrative placeholders only, not a real sequencing QC result. Pass real values via "
                "generate_pdf(qc_metrics={...}) from the upstream sequencing/alignment pipeline.",
                styles["Footnote"],
            )
        )
    return flowables


# ---------------------------------------------------------------------------
# One-page clinician summary
# ---------------------------------------------------------------------------
#
# A single skimmable front page (sample identity, then one compact row per
# variant, then any reviewer-attention flags), placed before the existing
# per-variant detail sections and everything after it in `generate_pdf` --
# nothing below this block is touched by this feature. Distinct from each
# variant's own "Executive Summary" (`clinical_report["executive_summary"]`,
# rendered inside its full "Finding N" section, one paragraph of prose) --
# this page is a run-level dashboard meant to be read in well under 30
# seconds, not prose: short table cells, not paragraphs, and only the
# highest-signal 2-3 evidence items per variant, not the full supporting-
# evidence list.
#
# This is pure synthesis over data `clinical_report`/`interpretation_result`
# /`provenance` already computed -- nothing here re-derives ACMG
# classification, confidence, or evidence, matching every other builder in
# this module and in report/clinical_report_builder.py.


def _normalize_evidence_text(item: Any) -> str:
    """Same `dict-or-plain-string` normalization `report/report_generator.py`'s
    Markdown renderer already applies to `supporting_evidence` entries."""
    if isinstance(item, dict):
        return item.get("text") or ""
    return str(item) if item is not None else ""


def _variant_reviewer_flags(variant_result: Dict[str, Any], clinical: Optional[Dict[str, Any]]) -> List[str]:
    """
    Short, plain-language reasons this one variant's finding may need a
    reviewer's attention before sign-off -- deliberately narrow (not every
    caveat the full report carries, e.g. routine "not yet scored" pending
    states are left to the detailed sections) so this stays a genuine
    signal on a 30-second skim, not noise.

      - No clinical interpretation could be built at all (a data gap, not
        a benign finding -- see `_build_variant_section`'s identical
        framing for the full-detail section).
      - A real (Minor/Moderate/Major) evidence conflict was detected --
        `clinical_report["conflict_resolution"]["severity"]`, the same
        field the Conflict Resolution Engine (Phase 6) computes.
      - Gene resolution came back genuinely ambiguous (multiple candidate
        genes overlap this position and could not be disambiguated) --
        see `pipeline/orchestrator.py::GeperPipeline._with_gene_resolution_context`
        and `pipeline/clingen/utils.py::GeneResolutionStatus`. Checked
        against the ClinGen and transcript-structure stages, the two
        stages that surface this status onto their own result dict.
    """
    if not clinical:
        return ["No clinical interpretation available"]

    flags: List[str] = []

    severity = (clinical.get("conflict_resolution") or {}).get("severity")
    if severity in ("Minor", "Moderate", "Major"):
        flags.append(f"Conflicting evidence ({severity})")

    for stage_key in ("clingen", "transcript"):
        if (variant_result.get(stage_key) or {}).get("gene_resolution_status") == "ambiguous":
            flags.append("Ambiguous gene resolution")
            break

    return flags


def _provenance_gap_sources(document: Dict[str, Any], variants: List[Dict[str, Any]]) -> List[str]:
    """
    Data sources this report actually cited as evidence (union of every
    variant's `clinical_report["evidence_sources"]`) for which
    `pipeline/provenance.py` recorded `status: "unknown"` -- consulted,
    but no version/release/hash identifier could be pinned down. This is
    the genuinely actionable reproducibility gap for a reviewer ("this
    finding used ClinVar, but which ClinVar snapshot cannot be
    determined"), as opposed to `status: "not_consulted"` (expected for
    every source this run never needed) or `"timestamp_only"`/
    `"hash_only"` (a real, if less specific, identifier does exist) --
    neither of those is a gap worth interrupting a 30-second skim for.

    Deliberately does not flag a source with NO matching evidence_sources
    entry at all (i.e. never cited by anything in this report) even if
    its own provenance status is "unknown" -- an uncited source's version
    gap cannot affect anything a reviewer is being asked to sign off on
    here. `document["provenance"]` (run-level, not per-variant) is read
    from `document` directly since it is absent entirely from the
    single-variant ad hoc calling convention `generate_pdf` also accepts
    (see its docstring) -- `variants` is passed in already resolved by
    the caller so both calling conventions share this one code path.
    """
    cited: set = set()
    for variant_result in variants:
        clinical = variant_result.get("clinical_report") or {}
        cited.update(clinical.get("evidence_sources") or [])
    if not cited:
        return []

    prefixes = {
        EVIDENCE_SOURCE_TO_PROVENANCE_PREFIX[name] for name in cited if name in EVIDENCE_SOURCE_TO_PROVENANCE_PREFIX
    }
    if not prefixes:
        return []

    gaps: List[str] = []
    for record in document.get("provenance") or []:
        source = record.get("source") or ""
        if record.get("status") == "unknown" and any(source.startswith(p) for p in prefixes):
            gaps.append(source)
    return gaps


def _offline_sources_caveat_text() -> Optional[str]:
    """
    Run-level, not per-finding: which external evidence sources (see
    `utils/service_health.py`) were confirmed offline at startup and
    therefore skipped for every variant in this run, rather than
    genuinely queried and found to have nothing. Read once here so any
    "not evaluated" or "not found" text for one of these sources
    elsewhere in the report is not mistaken for a completed, negative
    search -- the same reasoning
    `report/clinical_report_builder.py::_indian_population_frequency`
    documents for the per-finding IndiGenomes case. Returns `None` when
    every configured source was reachable at startup (the common case,
    no caveat needed).
    """
    offline = HEALTH.offline_services()
    if not offline:
        return None
    return (
        "The following data source(s) were unreachable during this analysis run and were not queried for "
        "any variant in this report: " + ", ".join(offline) + '. Any finding reported as "not evaluated" '
        "or lacking data from these sources reflects a data-collection gap for this run, not a confirmed "
        "absence -- it should not be treated as a negative result."
    )


def _build_clinician_summary_table(variants: List[Dict[str, Any]], styles: Dict[str, ParagraphStyle]) -> Table:
    val, small = styles["TableValue"], styles["TableValueSmall"]

    # Case-level phenotype ranking (pipeline/case_prioritization.py) is
    # additive and only present when the patient supplied HPO terms
    # this run -- when it's absent for every variant, this table
    # renders exactly as it did before that feature existed (VCF
    # order, no extra column). `idx` below is always the ORIGINAL
    # 1-based finding number (matches the numbered "Finding N"
    # sections further down the PDF) even when row DISPLAY order is
    # reshuffled by case rank -- only the row's position in the table
    # changes, never the number printed in its "#" cell, so a reader
    # can always cross-reference a row back to its detailed section.
    has_case_ranking = any(isinstance(vr.get("case_prioritization"), dict) for vr in variants)

    indexed = list(enumerate(variants, start=1))
    if has_case_ranking:

        def _rank_key(pair):
            idx, vr = pair
            cp = vr.get("case_prioritization") or {}
            rank = cp.get("case_rank")
            return (rank is None, rank if rank is not None else 0, idx)

        indexed.sort(key=_rank_key)

    header_labels = ["#", "Variant / Gene", "Classification", "Confidence"]
    if has_case_ranking:
        header_labels.append("Phenotype Match")
    header_labels += ["Top Evidence", "Attention"]
    header = [Paragraph(h, styles["TableHeader"]) for h in header_labels]
    rows: List[List[Paragraph]] = [header]
    flag_rows: List[int] = []  # 1-based row indices (into `rows`) carrying at least one reviewer flag

    for row_num, (idx, variant_result) in enumerate(indexed, start=1):
        variant = variant_result.get("variant", {})
        locus = f"{variant.get('chrom')}:{variant.get('pos')} {variant.get('ref')}>{variant.get('alt')}"
        clinical = variant_result.get("clinical_report")
        gene = (variant_result.get("interpretation_result") or {}).get("gene_symbol")
        gene_line = f"{locus}<br/><b>{gene}</b>" if gene else locus

        acmg = (clinical or {}).get("acmg_classification") or {}
        classification = acmg.get("classification") or "Not classified"

        confidence = (clinical or {}).get("confidence") or {}
        if confidence.get("pending", True):
            confidence_text = "Pending"
        else:
            score = confidence.get("score")
            score_text = f"{score:.0f}%" if isinstance(score, (int, float)) else ""
            confidence_text = f"{confidence.get('label') or 'n/a'}" + (f" ({score_text})" if score_text else "")

        top_evidence = [
            _normalize_evidence_text(item) for item in ((clinical or {}).get("supporting_evidence") or [])[:3]
        ]
        top_evidence = [t for t in top_evidence if t]
        evidence_text = "<br/>".join(f"• {t}" for t in top_evidence) if top_evidence else "—"

        flags = _variant_reviewer_flags(variant_result, clinical)
        # "!" not the Unicode warning-sign glyph (U+26A0): Helvetica's
        # WinAnsiEncoding (what ReportLab's built-in fonts use) has no
        # glyph for U+26A0, unlike "•"/em dash above which both are
        # already in that encoding and already used elsewhere in this
        # module -- an unsupported glyph would render as a blank/tofu
        # box in the actual PDF, not just in a text-extraction tool.
        flags_text = "<br/>".join(f"! {f}" for f in flags) if flags else "—"
        if flags:
            # `row_num` (1-based, into `rows`) is this row's actual
            # table position -- NOT `idx`, which is the original
            # finding number and may differ once rows are reordered
            # by case rank above.
            flag_rows.append(row_num)

        row = [
            Paragraph(str(idx), val),
            Paragraph(gene_line, small),
            Paragraph(classification, small),
            Paragraph(confidence_text, small),
        ]
        if has_case_ranking:
            cp = variant_result.get("case_prioritization") or {}
            pm = cp.get("phenotype_match") or {}
            pm_score = pm.get("score")
            if pm_score is not None:
                top_terms = sorted(
                    (m for m in (pm.get("term_matches") or []) if m.get("similarity", 0) > 0),
                    key=lambda m: -m.get("similarity", 0),
                )[:2]
                term_text = (
                    "; ".join(
                        f"{m.get('matched_gene_term_name') or m.get('matched_gene_term_id')} ({m['match_type']})"
                        for m in top_terms
                    )
                    or "no term overlap"
                )
                pm_text = f"{pm_score:.0f}/100<br/>{term_text}"
            else:
                pm_text = "No HPO data<br/>for this gene"
            row.append(Paragraph(pm_text, small))
        row += [
            Paragraph(evidence_text, small),
            Paragraph(flags_text, styles["StatusWarn"] if flags else small),
        ]
        rows.append(row)

    if has_case_ranking:
        col_widths = [8 * mm, 30 * mm, 25 * mm, 17 * mm, 30 * mm, 40 * mm, 20 * mm]
    else:
        col_widths = [8 * mm, 35 * mm, 30 * mm, 20 * mm, 53 * mm, 24 * mm]
    table = Table(rows, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    style_cmds = [
        ("GRID", (0, 0), (-1, -1), 0.4, _TABLE_GRID_COLOR),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3c5e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for row_idx in flag_rows:
        style_cmds.append(("BACKGROUND", (0, row_idx), (-1, row_idx), colors.HexColor("#fdf1d6")))
    table.setStyle(TableStyle(style_cmds))
    return table


def _build_clinician_summary_flowables(
    document: Dict[str, Any],
    variants: List[Dict[str, Any]],
    sample_id: str,
    run_id: str,
    assembly: Optional[str],
    styles: Dict[str, ParagraphStyle],
) -> List[Any]:
    """
    The one-page front-page summary itself: identity strip, one compact
    row per variant, then a short reviewer-attention callout. Ends with a
    `PageBreak()` so the existing detailed sections (patient header, QC,
    per-variant findings -- built by the unmodified functions below)
    always start on a fresh page, exactly as before this feature existed.

    Designed to fit one page for a typical run (a handful of variants);
    for an unusually large batch this table will legitimately spill onto
    a second page like any other ReportLab flowable -- silently truncating
    real findings to force a hard one-page limit would hide clinically
    relevant results, which is worse than an honest overflow.

    `variants` is the same already-resolved list `generate_pdf` builds
    (handles both the normal `document["variants"]` shape and the single-
    variant ad hoc calling convention -- see that function's docstring)
    so this never has to special-case the input shape itself.
    """
    has_case_ranking = any(isinstance(vr.get("case_prioritization"), dict) for vr in variants)
    footnote_text = (
        "Rows below are ordered by case-level phenotype-match rank (best explains the patient's "
        "observed symptoms first), not VCF order -- the “#” column is each variant's original "
        "finding number; see the matching numbered “Finding” section below for full detail. "
        "This ranking is a triage aid only and does not affect ACMG classification."
        if has_case_ranking
        else "One row per variant finding; see the numbered “Finding” sections below for full detail on any of them."
    )
    flow: List[Any] = [
        _Bookmark("bm_clinician_summary", "Clinician Summary"),
        Paragraph("Clinician Summary", styles["SectionHeading"]),
        Paragraph(footnote_text, styles["Footnote"]),
        Spacer(1, 2 * mm),
    ]

    identity = (
        f"<b>Sample:</b> {sample_id} &nbsp;&nbsp; <b>Run:</b> {run_id} &nbsp;&nbsp; "
        f"<b>Reference build:</b> {assembly or 'not specified'} &nbsp;&nbsp; "
        f"<b>Variants analyzed:</b> {len(variants)}"
    )
    flow.append(Paragraph(identity, styles["BodyText"]))
    flow.append(Spacer(1, 3 * mm))

    if variants:
        flow.append(_build_clinician_summary_table(variants, styles))
        flow.append(
            Paragraph(
                "Confidence reflects how complete the available evidence is, not how certain each classification is.",
                styles["Footnote"],
            )
        )
    else:
        flow.append(Paragraph("No variants were analyzed in this run.", styles["BodyText"]))
    flow.append(Spacer(1, 3 * mm))

    attention_lines: List[str] = []
    for idx, variant_result in enumerate(variants, start=1):
        clinical = variant_result.get("clinical_report")
        flags = _variant_reviewer_flags(variant_result, clinical)
        if flags:
            variant = variant_result.get("variant", {})
            locus = f"{variant.get('chrom')}:{variant.get('pos')} {variant.get('ref')}>{variant.get('alt')}"
            attention_lines.append(f"Finding {idx} ({locus}): {'; '.join(flags)}")
    gap_sources = _provenance_gap_sources(document, variants)
    if gap_sources:
        attention_lines.append(
            "Data-source version not determinable for cited evidence from: " + ", ".join(sorted(gap_sources)) + "."
        )
    offline_caveat = _offline_sources_caveat_text()
    if offline_caveat:
        attention_lines.append(offline_caveat)

    # Heading + its first line of content are wrapped in `KeepTogether` so
    # the heading can never be stranded alone at the bottom of a page with
    # its content starting fresh on the next one (reproduced with a
    # 12-variant run: the heading alone was the last line of page 1, every
    # bullet started page 2). Only the first line is included -- not the
    # whole (open-ended) attention list -- so a long list still flows
    # normally across a page break rather than being forced, as a block,
    # onto a near-empty new page the way the sign-off block used to be
    # (see `_build_signoff_block`'s docstring for that failure mode).
    if attention_lines:
        first, rest = attention_lines[0], attention_lines[1:]
        flow.append(
            KeepTogether(
                [
                    Paragraph("Reviewer Attention", styles["SectionHeading"]),
                    Paragraph(f"! {first}", styles["BulletText"]),
                ]
            )
        )
        flow.extend(Paragraph(f"! {line}", styles["BulletText"]) for line in rest)
    else:
        flow.append(
            KeepTogether(
                [
                    Paragraph("Reviewer Attention", styles["SectionHeading"]),
                    Paragraph(
                        "No conflicts, ambiguous gene resolution, or evidence-provenance gaps flagged for this run.",
                        styles["BodyText"],
                    ),
                ]
            )
        )

    flow.append(PageBreak())
    return flow


def _build_1000_genomes_sas_fallback_flowables(ipf: Dict[str, Any], styles: Dict[str, ParagraphStyle]) -> List[Any]:
    """
    Renders the 1000 Genomes SAS fallback -- called only when
    `ipf["fallback_shown"]` is True (IndiGenomes confirmed offline this
    run; see `report/clinical_report_builder.py::_indian_population_frequency`
    and `annotation/thousand_genomes_sas.py`'s module docstring). Both
    mandatory disclosures (`SAMPLE_SIZE_DISCLOSURE`/`DIASPORA_DISCLOSURE`
    -- the same shared text `report/report_generator.py`'s Markdown
    path renders, so the two output formats never drift on wording) are
    always shown whenever this fallback has anything to report at all --
    found, not found, or errored -- not only on the "found" path.
    """
    flow: List[Any] = [
        Paragraph(
            "• 1000 Genomes (South Asian, SAS) -- fallback source, IndiGenomes was unavailable this run:",
            styles["BulletText"],
        )
    ]
    if ipf.get("fallback_available"):
        pooled = ipf.get("fallback_sas_pooled") or {}
        if pooled.get("af") is not None:
            flow.append(
                Paragraph(
                    f"&nbsp;&nbsp;&nbsp;&nbsp;- Pooled SAS: AF = {pooled.get('af'):.2e} "
                    f"(AC={pooled.get('ac')}, AN={pooled.get('an')})",
                    styles["BulletText"],
                )
            )
        labels = ipf.get("fallback_population_labels") or {}
        sizes = ipf.get("fallback_sample_sizes") or {}
        for code, sub in (ipf.get("fallback_sub_populations") or {}).items():
            label = labels.get(code, code)
            n = sizes.get(code)
            af = sub.get("af")
            af_text = f"{af:.2e}" if af is not None else "n/a"
            flow.append(
                Paragraph(
                    f"&nbsp;&nbsp;&nbsp;&nbsp;- {code} ({label}, n={n}): AF = {af_text} "
                    f"(AC={sub.get('ac')}, AN={sub.get('an')})",
                    styles["BulletText"],
                )
            )
    elif ipf.get("fallback_error"):
        flow.append(
            Paragraph(
                f"&nbsp;&nbsp;&nbsp;&nbsp;- Lookup failed (external service issue: {ipf['fallback_error']}).",
                styles["BulletText"],
            )
        )
    else:
        flow.append(
            Paragraph(
                "&nbsp;&nbsp;&nbsp;&nbsp;- Variant not found in this fallback source either.", styles["BulletText"]
            )
        )
    flow.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;! {SAMPLE_SIZE_DISCLOSURE}", styles["StatusWarn"]))
    flow.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;! {DIASPORA_DISCLOSURE}", styles["StatusWarn"]))
    return flow


def _build_indian_population_frequency_flowables(
    clinical: Dict[str, Any], styles: Dict[str, ParagraphStyle]
) -> List[Any]:
    """
    India-deployment feature: gnomAD South Asian (SAS) AF alongside
    IndiGenomes' own India-specific cohort AF (see
    `report/clinical_report_builder.py::_indian_population_frequency`
    for where this dict comes from and why the two figures are never
    merged into one number), plus a "Common in Indian populations"
    flag when either exceeds the configured threshold. Renders nothing
    at all only when there is truly no signal from either source --
    gnomAD SAS has no figure and IndiGenomes was genuinely queried
    with no matching record (a real, meaningful absence). Any other
    IndiGenomes outcome (confirmed offline this run, a genuine lookup
    error, or disabled/GRCh37-skipped) is always rendered explicitly,
    even when gnomAD alone would otherwise fill the section --
    silently omitting IndiGenomes in those cases would look identical
    to "queried, nothing found" to a reader, which is exactly the
    ambiguity this section must not create (see
    `report/clinical_report_builder.py::_indian_population_frequency`
    for where `indigenomes_offline`/`indigenomes_error`/
    `indigenomes_skipped_reason` come from).
    """
    ipf = clinical.get("indian_population_frequency") or {}
    gnomad_sas_af = ipf.get("gnomad_af_sas")
    indigenomes_available = ipf.get("indigenomes_available")
    indigenomes_offline = ipf.get("indigenomes_offline")
    indigenomes_error = ipf.get("indigenomes_error")
    indigenomes_skipped_reason = ipf.get("indigenomes_skipped_reason")
    indigenomes_flagged = bool(
        indigenomes_available or indigenomes_offline or indigenomes_error or indigenomes_skipped_reason
    )
    if gnomad_sas_af is None and not indigenomes_flagged:
        return []

    flow: List[Any] = [Spacer(1, 2 * mm), Paragraph("<b>Indian Population Frequency:</b>", styles["BodyText"])]
    if gnomad_sas_af is not None:
        flow.append(Paragraph(f"• gnomAD (South Asian, SAS): AF = {gnomad_sas_af:.2e}", styles["BulletText"]))
    if indigenomes_available:
        flow.append(
            Paragraph(
                f"• IndiGenomes: AF = {ipf.get('indigenomes_af'):.2e} "
                f"(AC={ipf.get('indigenomes_ac')}, AN={ipf.get('indigenomes_an')})",
                styles["BulletText"],
            )
        )
    elif indigenomes_offline:
        flow.append(
            Paragraph(
                "• IndiGenomes: not evaluated -- IndiGenomes was unreachable during this analysis run "
                "(not queried). This is a data-collection gap for this run, not evidence of an absent record.",
                styles["BulletText"],
            )
        )
    elif indigenomes_error:
        flow.append(
            Paragraph(
                f"• IndiGenomes: lookup failed (external service issue: {indigenomes_error}) -- "
                "not evidence of an absent record.",
                styles["BulletText"],
            )
        )
    elif indigenomes_skipped_reason:
        flow.append(Paragraph(f"• IndiGenomes: not queried ({indigenomes_skipped_reason}).", styles["BulletText"]))
    elif gnomad_sas_af is not None:
        # gnomAD had a figure but IndiGenomes was genuinely queried
        # with no matching record -- stated explicitly rather than
        # left silent, so its absence here is never mistaken for
        # "not checked".
        flow.append(Paragraph("• IndiGenomes: variant not found.", styles["BulletText"]))
    if ipf.get("fallback_shown"):
        flow.extend(_build_1000_genomes_sas_fallback_flowables(ipf, styles))
    if ipf.get("common_in_indian_population"):
        threshold_pct = f"{ipf.get('common_af_threshold', 0.01):.0%}"
        flow.append(
            Paragraph(
                f"! Common in Indian populations (at or above the {threshold_pct} threshold).", styles["StatusWarn"]
            )
        )
    return flow


def _build_variant_section(idx: int, variant_result: Dict[str, Any], styles: Dict[str, ParagraphStyle]) -> List[Any]:
    """Renders one variant's already-computed `clinical_report` dict (see report/clinical_report_builder.py) -- no evidence is re-derived here."""
    variant = variant_result.get("variant", {})
    locus = f"{variant.get('chrom')}:{variant.get('pos')} {variant.get('ref')}>{variant.get('alt')}"
    clinical = variant_result.get("clinical_report")

    flow: List[Any] = [
        _Bookmark(f"bm_finding_{idx}", f"Finding {idx}: {locus}"),
        Spacer(1, 4 * mm),
        Paragraph(f"Finding {idx}: {locus}", styles["SectionHeading"]),
    ]

    if not clinical:
        flow.append(
            Paragraph(
                "No clinical interpretation is available for this variant (the interpretation engine "
                "did not produce a result for it). This is reported as a data gap, not a benign finding.",
                styles["BodyText"],
            )
        )
        return flow

    flow.append(Paragraph(clinical["executive_summary"], styles["BodyText"]))

    acmg = clinical.get("acmg_classification") or {}
    if acmg.get("classification"):
        flow.append(Paragraph(f"<b>ACMG/AMP Classification:</b> {acmg['classification']}", styles["BodyText"]))

    # Clinician override (geper/review/signoff.py's "override" command) --
    # layered on top of, never substituting for, GEPER's own
    # classification above: both are shown, explicitly labelled. Absent
    # for every variant no clinician has overridden (the common case).
    override = acmg.get("clinician_override")
    if override:
        flow.append(
            Paragraph(
                f"<b>Clinician Override:</b> GEPER classification: {override.get('original_classification') or 'Not classified'}; "
                f"Clinician override: <b>{override.get('new_classification')}</b> -- {override.get('reason')} "
                f"(by {override.get('clinician_id')}, {override.get('timestamp')})",
                styles["StatusWarn"],
            )
        )

    confidence = clinical.get("confidence") or {}
    if not confidence.get("pending") and confidence.get("label"):
        flow.append(Paragraph(f"<b>Confidence:</b> {confidence['label']}", styles["BodyText"]))
        flow.append(
            Paragraph(
                "Confidence reflects how complete the available evidence is, not how certain this classification is.",
                styles["Footnote"],
            )
        )

    triggered = acmg.get("triggered_criteria") or []
    if triggered:
        rows = [
            [
                Paragraph("Criterion", styles["TableHeader"]),
                Paragraph("Strength", styles["TableHeader"]),
                Paragraph("Rationale", styles["TableHeader"]),
            ]
        ]
        for crit in triggered:
            rows.append(
                [
                    Paragraph(str(crit.get("code") or ""), styles["TableValue"]),
                    Paragraph(str(crit.get("strength") or ""), styles["TableValue"]),
                    Paragraph(str(crit.get("rationale") or ""), styles["TableValueSmall"]),
                ]
            )
        # `repeatRows=1` -- same as `_build_clinician_summary_table`'s table
        # -- so that if a wide rationale forces ReportLab to split this
        # table between two of its rows, the "Criterion / Strength /
        # Rationale" header repeats on the continuation page instead of
        # being strandable alone on the page before it (reproduced without
        # this: with enough preceding content, the header could be the
        # last thing on a page while every data row started fresh on the
        # next one, with no header to explain them). ReportLab's default
        # table split is already row-atomic -- a single row's cells always
        # move to the next page together, never mid-sentence -- so this is
        # about the header's company, not about a row itself splitting.
        table = Table(rows, colWidths=[22 * mm, 25 * mm, 113 * mm], hAlign="LEFT", repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.3, _TABLE_GRID_COLOR),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3c5e")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        flow.append(Spacer(1, 2 * mm))
        flow.append(table)

    supporting = clinical.get("supporting_evidence") or []
    if supporting:
        flow.append(Spacer(1, 2 * mm))
        flow.append(Paragraph("<b>Supporting Evidence:</b>", styles["BodyText"]))
        flow.extend(Paragraph(f"• {item}", styles["BulletText"]) for item in supporting)

    flow.extend(_build_indian_population_frequency_flowables(clinical, styles))

    limitations = clinical.get("limitations") or []
    if limitations:
        flow.append(Spacer(1, 2 * mm))
        flow.append(Paragraph("<b>Limitations:</b>", styles["BodyText"]))
        flow.extend(Paragraph(f"• {item}", styles["BulletText"]) for item in limitations)

    return flow


def _build_signoff_block(styles: Dict[str, ParagraphStyle]) -> List[Any]:
    """
    Signature + date lines, followed immediately by the standard legal
    disclaimer.

    Only the heading + signature table are wrapped in `KeepTogether` (a
    signature line split from its own "Signature:"/"Date:" labels would
    be unreadable). The disclaimer paragraph that follows is
    deliberately left outside that atomic unit: it's ordinary body text
    with no layout requirement to stay glued to the table, and folding
    it into the same `KeepTogether` was inflating the group's "must all
    fit together" height by the disclaimer's own extra lines for no
    visual benefit -- on a run where the sign-off would otherwise have
    landed on a page with genuine but tight remaining room, that was
    enough to push the whole block, disclaimer included, onto an
    otherwise near-empty next page. Splitting it out lets the heading
    and table land wherever they naturally fit, with the disclaimer
    flowing right after -- same as any other paragraph.
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
    sig_table.setStyle(
        TableStyle(
            [
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    return [
        KeepTogether(
            [
                _Bookmark("bm_signoff", "Sign-off & Disclaimer"),
                Spacer(1, 10 * mm),
                Paragraph("Chief Pathologist / Medical Director", styles["SignoffTitle"]),
                Spacer(1, 4 * mm),
                sig_table,
            ]
        ),
        Spacer(1, 8 * mm),
        Paragraph(_DISCLAIMER_TEXT, styles["Disclaimer"]),
    ]


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
        # Bumped from 22mm: room for the mandatory ICMR AI-disclosure
        # footer (see `_NumberedCanvas`/`_icmr_ai_disclosure_footer_text`)
        # above the existing rule/page-number band, without body
        # flowables ever reaching down into it.
        bottomMargin=30 * mm,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        title="GEPER Clinical Genomic Analysis Report",
    )

    story: List[Any] = list(_build_report_header(logo_path, styles))
    story.append(Spacer(1, 4 * mm))
    story.extend(_build_clinician_summary_flowables(document, variants, sample_id, resolved_run_id, assembly, styles))
    story.extend(
        [
            _build_patient_header_table(patient, sample_id, resolved_run_id, assembly, styles),
            Spacer(1, 6 * mm),
            _Bookmark("bm_qc", "Sequencing Quality Control Metrics"),
            Paragraph("Sequencing Quality Control Metrics", styles["SectionHeading"]),
        ]
    )
    story.extend(_build_qc_flowables(qc_metrics, styles))
    story.append(Spacer(1, 4 * mm))

    for idx, variant_result in enumerate(variants, start=1):
        story.extend(_build_variant_section(idx, variant_result, styles))

    story.extend(_build_signoff_block(styles))

    doc.build(
        story,
        onFirstPage=_make_first_page_decoration(),
        onLaterPages=_make_later_page_decoration(header_label),
        # Mandatory ICMR AI-disclosure footer (see `_NumberedCanvas`'s
        # docstring) -- bound in via `functools.partial` since
        # `canvasmaker` is called as `canvasmaker(filename, **kwargs)`
        # by ReportLab itself, not pre-instantiated by this function.
        canvasmaker=functools.partial(_NumberedCanvas, footer_text=_icmr_ai_disclosure_footer_text(patient)),
    )
    logger.info(f"Wrote clinical PDF report to '{output_path}' ({len(variants)} variant finding(s)).")
    return output_path
