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
run_id=None, logo_path=None)`. Run-level QC is read from `document`
rather than passed in -- see `_document_qc_metrics`.

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
from typing import Any, Dict, List, Optional, Tuple, Union

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
from pipeline.acmg_rules import mtdna_interpretation_disclaimer
from pipeline.hgvs_utils import is_mitochondrial_chrom
from pipeline.models.status import DISABLED as _STATUS_DISABLED
from pipeline.models.status import FAILED as _STATUS_FAILED
from pipeline.models.status import SKIPPED as _STATUS_SKIPPED
from pipeline.models.status import USED as _STATUS_USED
from pipeline.provenance import EVIDENCE_SOURCE_TO_PROVENANCE_PREFIX, RETRIEVAL_MODE_LABELS
from pipeline.stage_schemas import StageStatus as _StageStatus
from report.clinical_report_builder import (
    candidate_interpretation_of,
    ACMG_METHODOLOGY_STATEMENT,
    EVIDENCE_COMPLETENESS_CAPTION,
    DOCUMENT_POSITIONING_STATEMENT,
    ISO_RESEARCH_ELEMENT,
    RESEARCH_USE_DISCLAIMER,
    # Moved out of this module on 2026-08-21 so the Markdown renderer and
    # the JSON run-level block could reach them without importing from a
    # PDF renderer -- see that module's "Shared run-level caveat helpers"
    # comment. Re-imported here (rather than call sites being rewritten)
    # so `report.summary._variant_reviewer_flags` and friends still
    # resolve for existing callers and tests.
    _consent_value_label,
    _offline_sources_caveat_text,
    _variant_hgvs_or_locus,
    _variant_reviewer_flags,
    # Relocated 2026-08-21 (A7): the QC vocabulary, thresholds and
    # `{status, value, reason}` parsing are renderer-neutral, so the
    # Markdown report can render the same table this module does instead
    # of rendering nothing. Re-imported here so existing callers and
    # tests that reach for `report.summary._parse_qc_metrics` still work.
    _parse_qc_metrics,
    _qc_status,
    _qc_threshold_pass_min,
    _QC_METRIC_LABELS,
    _QC_METRIC_ORDER,
    _QC_METRIC_UNITS,
    # Re-exported for callers/tests that read it from `report.summary`,
    # where it lived before the A7 move. This module only names it in
    # comments, so ruff's F401 autofix stripped it when the relocation
    # landed and broke those readers -- the noqa is what keeps a
    # deliberate re-export from looking like a dead import.
    _QC_METRICS_NOT_APPLICABLE_REASON,  # noqa: F401
    # The single shared rendering of a per-sample allele fraction, called
    # by this module and by the Markdown renderer, so the two full reports
    # degrade identically -- the `_variant_hgvs_or_locus` pattern above,
    # applied to the tri-state this field carries.
    variant_allele_fraction_text,
)
from report.pdf_escape import esc
from utils.logger import get_logger
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

# Single source of truth for both PDF formats' sign-off roles -- the
# diagnostic-genetics two-signature convention (an authoring scientist
# plus an authorising consultant). `report/summary.py::_build_signoff_
# block` and `report/summary_short.py::_build_signoff_block` both read
# this same tuple rather than hardcoding their own role titles, so the
# two documents can never again show a different signatory for the
# same run.
_SIGNOFF_ROLES = ("Clinical Scientist", "Consultant Clinical Scientist")

# The closing disclaimer block. `RESEARCH_USE_DISCLAIMER` is imported
# rather than restated: this constant previously held US CLIA/LDT
# template wording asserting the report was "intended for clinical
# use", which contradicted both the research-use limitation this same
# PDF prints under every finding and this module's own docstring above
# (which describes hospital use as pending a compliance sign-off not
# yet obtained). Retired 2026-08-21. The label is applied here, not
# carried in the shared constant, so each renderer can present it its
# own way -- see that constant's own comment.
_DISCLAIMER_LABEL = "Limitations and Disclaimer: "

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

# The `review_status == "overridden"` counterpart. Deliberately NOT
# folded into `_ICMR_DRAFT_FOOTER_TEXT`: an override is a real,
# recorded clinical action, and rendering it as "DRAFT" would discard
# the fact that a human deliberately changed a classification --
# information a reporting pathologist needs, and which no other
# per-page surface carries. It still leads with NOT FOR PATIENT USE,
# because an overridden run is not export-eligible either
# (`report/export_lims.py::_require_reviewed` gates on "reviewed"
# alone), so all three surfaces agree on readiness while differing on
# how they got there.
#
# Phrased so it is true whether or not the run was ever approved --
# `review/signoff.py::override()` can be called on a never-approved run
# -- hence "has overridden ... in this run" rather than anything
# implying a prior sign-off.
#
# Deliberately does NOT name `reviewed_by`, even though `override()`
# preserves it: a clinician's name sitting next to review wording in a
# footer repeated on every page is exactly the at-a-glance misread this
# whole change set exists to remove. Identity is not lost -- the
# override's author, reason and timestamp are already rendered per
# finding in the report body (see the "Clinician Override" line built
# below), and `reviewed_by`/`reviewed_at` remain in `geper_results.json`
# and the sign-off audit log. This footer states the run's STATE; the
# body carries the IDENTITY.
_ICMR_OVERRIDDEN_FOOTER_TEXT = (
    "OVERRIDDEN -- NOT FOR PATIENT USE. A clinician has overridden at least one "
    "classification in this run; a fresh sign-off is required before use."
)


def _icmr_ai_disclosure_footer_text(
    patient: Dict[str, Any],
    document: Optional[Dict[str, Any]] = None,
) -> str:
    """
    The mandatory per-page AI-disclosure footer text for this report.

    REVIEW STATE COMES FROM `document["review_status"]`, and from
    nothing else. That field is written only by
    `review/signoff.py::approve()` (`"reviewed"`) and `override()`
    (`"overridden"`); every other path leaves the `"draft"` that
    `report/json_builder.py` writes. Three outcomes, matching the three
    states the Markdown banner already distinguishes
    (`report/report_generator.py::_render_review_status_banner`):
    `"reviewed"` names the reviewer, `"overridden"` renders
    `_ICMR_OVERRIDDEN_FOOTER_TEXT`, and everything else -- `"draft"`, an
    unrecognised value, a missing key, or a missing `document` entirely
    -- renders `_ICMR_DRAFT_FOOTER_TEXT`. That last catch-all is the
    fail-safe direction: a report nobody has attested to reviewing must
    never look, at a glance, indistinguishable from one that is
    actually ready for clinical use.

    This deliberately reverses an earlier design in which
    `patient["physician"]` -- a free-text field supplied by the caller
    via `--patient-meta` -- was the sole signal for "has a clinician
    actually reviewed this run". That made the review claim assertable
    by an input file: supplying any physician string suppressed the
    DRAFT warning on every page and printed "has been reviewed by X",
    with no sign-off ever performed and `review_status` still `"draft"`.
    An input field must not be able to make the clinician-facing
    artefact assert something the sign-off record does not.

    `physician` therefore now means ATTRIBUTION ONLY -- the referring
    clinician, already rendered as "Referring Physician" in the patient
    identity block of both PDFs -- and no longer carries any review
    state. It survives here only as a fallback for the reviewer's NAME,
    and only once `review_status` has already established that a
    sign-off exists; it can no longer create that state on its own.

    The reviewer named is `document["reviewed_by"]`, which
    `approve()` writes alongside `review_status` from the same
    clinician/registration/hospital string it folds into `physician`
    (e.g. "Dr. A. Sharma, MD, Reg. No. 12345, ABC Diagnostics"), so the
    rendered sentence is unchanged for the normal sign-off path -- it
    is now simply sourced from the sign-off record rather than from an
    input file.
    """
    status = (document or {}).get("review_status")
    if status == "overridden":
        return _ICMR_OVERRIDDEN_FOOTER_TEXT
    if status != "reviewed":
        return _ICMR_DRAFT_FOOTER_TEXT
    reviewer = (document or {}).get("reviewed_by") or (patient or {}).get("physician")
    if not reviewer:
        # Signed off, but no reviewer identity was recorded. Saying DRAFT
        # here would be the opposite error -- denying a sign-off that did
        # happen -- so this states the attested fact and omits the name
        # rather than inventing one.
        return (
            "This report was generated using AI-assisted genomic interpretation and has been "
            "reviewed and signed off. This is not a standalone diagnosis."
        )
    return (
        f"This report was generated using AI-assisted genomic interpretation and has been "
        f"reviewed by {reviewer}. This is not a standalone diagnosis."
    )


# Run-level QC parsing, thresholds and vocabulary now live in
# `report/clinical_report_builder.py` -- they are renderer-neutral (no
# reportlab), and the Markdown report needs the same labels, thresholds
# and PASS/WARNING rule this module does. Re-exported via the import
# above because `pipeline/orchestrator.py` and the test suite import
# several of them from `report.summary`, where they lived until the A7 fix.


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
        NAMES this field as the reviewer once `document["review_status"]`
        has already established that the report is reviewed).
        CORRECTED 2026-09-11 -- this sentence previously read: "which
        reads exactly this field to decide \"DRAFT\" vs \"reviewed by
        {physician}\"". That describes the design this codebase
        DELIBERATELY REVERSED on 2026-08-21, and it described it as
        current: when `physician` decided the DRAFT banner, an input
        file could assert a review that no sign-off had performed.
        `_icmr_ai_disclosure_footer_text`'s own docstring, ~170 lines
        above in this same file, has said since then that review state
        comes from `document["review_status"]` "and from nothing else"
        and that `physician` is "ATTRIBUTION ONLY". A caveat corrected
        in one place and left standing in another is worse than an
        uncorrected one, because the corrected copy is a reader's
        evidence that someone checked. Coupling `physician` to `patient_name` would
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
    return f"BIJ-RUN-{compact[:14]}"


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
        canvas.drawString(_MARGIN, _PAGE_H - 12 * mm, f"Bij AI Clinical Genomic Report -- {header_label}")
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
        # Page-1 positioning banner. Sized and boxed to be read, not
        # skimmed past: the claim it carries was previously in 6.5pt
        # footer text, which is present-but-unread. Larger than body
        # text, bold, tinted, and boxed -- the most prominent thing on
        # the page before the findings table.
        "PositioningBanner": ParagraphStyle(
            "GeperPositioningBanner",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=13.5,
            textColor=navy,
            backColor=colors.HexColor("#eef3f8"),
            borderColor=navy,
            borderWidth=0.9,
            borderPadding=6,
            spaceBefore=2,
            spaceAfter=4,
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
        "StatusError": ParagraphStyle(
            "GeperStatusError",
            parent=base["BodyText"],
            fontSize=9,
            textColor=colors.HexColor("#a11d1d"),
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


SCOPE_LINE = (
    "Variant interpretation from a supplied VCF — produced by the GEPER engine. "
    "Sequencing, alignment and variant calling are performed upstream; any run-level "
    "QC shown here is supplied by that run and is not computed by this report."
)


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
    title = Paragraph("Bij AI Clinical Genomic Analysis Report", styles["ReportTitle"])
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


def _document_consent(document: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    DPDP Act 2023 consent metadata for this run, read from the document
    and from nowhere else.

    Both PDF renderers previously took consent from whatever
    `--patient-meta` they were handed, while the Markdown report read
    `document["patient_consent"]`. Those are two independent inputs
    answering one question, and a render-and-diff of a single run caught
    them disagreeing: a document carrying consent, rendered without a
    patient_meta argument, produced a Markdown report stating consent
    and PDFs stating none. They had only ever agreed by convention --
    `pipeline/orchestrator.py` happens to populate the document field by
    calling `_parse_patient_meta` on the same file it later passes to
    these renderers -- never by construction.

    The document wins because it is the unified shape every other field
    already flows through, and because it is what survives to
    `geper_results.json` for `report/export_lims.py` and any other
    machine consumer. Consequence, stated rather than buried: a
    `--patient-meta` consent object no longer reaches a PDF on its own.
    For a real pipeline run that changes nothing (the orchestrator
    populates the document from that same file), but a caller invoking
    `generate_pdf` directly against a document with no
    `patient_consent` key will no longer see consent rows.

    Same "absent, not fabricated" contract as before: `None` when the
    run recorded no consent object at all, which `_consent_rows` renders
    as no rows rather than three "Not stated" ones.
    """
    return (document or {}).get("patient_consent")


def _document_qc_metrics(document: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Run-level sequencing/alignment QC for this run, read from the document
    and from nowhere else -- the same consolidation `_document_consent`
    performs one function above, for the same reason.

    QC used to arrive only as a `generate_pdf(qc_metrics=...)` argument,
    which made it a PDF-only fact that nothing else could see. Two
    consequences, both real:

      - `report/report_generator.py` rendered no QC at all and had no way
        to -- the data never reached it. That is the A7 gap: a
        full-fidelity human-readable renderer silently omitting a section
        the PDF shows, which in this codebase reads identically to a run
        that genuinely had none.
      - `review/signoff.py` re-rendered from the stored document days
        later with no argument to pass, so a signed report claimed no
        upstream QC existed for runs that had supplied it (fixed in
        ae0d9f7 by threading the document's copy through; this commit
        removes the need to thread anything).

    The document wins for the same reason it does for consent: it is the
    unified shape every other field already flows through, and it is what
    survives to `geper_results.json` for machine consumers.

    `None` -- no `--qc-metrics-json` was supplied, or a document written
    before QC was stored -- keeps its exact meaning. `_parse_qc_metrics`
    turns it into the all-NOT_RUN default whose per-metric reasons say
    plainly that GEPER never observed an upstream sequencing step, which
    is a true statement rather than a gap. That contract is the whole
    point: a run that DID supply QC must never render as one that did
    not, and a run that genuinely had none must keep saying so honestly.
    """
    return (document or {}).get("qc_metrics")


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
        rows.append([Paragraph("Consent Recorded", lbl), Paragraph(esc(consent["timestamp"]), val)])
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
        rows.append([Paragraph("Patient Name", lbl), Paragraph(esc(patient["patient_name"]), val)])
        rows.append([Paragraph("Date of Birth", lbl), Paragraph(esc(patient["dob"]) or "Not provided", val)])
        rows.append([Paragraph("Gender", lbl), Paragraph(esc(patient["gender"]) or "Not provided", val)])
        rows.append(
            [Paragraph("Referring Physician", lbl), Paragraph(esc(patient["physician"]) or "Not provided", val)]
        )

    rows.extend(_consent_rows(patient, lbl, val))

    rows.append([Paragraph("Sample ID", lbl), Paragraph(esc(sample_id), val)])
    rows.append([Paragraph("Run ID", lbl), Paragraph(esc(run_id), val)])
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


_PROVENANCE_STATUS_LABELS = {
    "not_consulted": "Not consulted this run",
    "unknown": "Consulted -- no version/hash could be determined",
    "timestamp_only": "Consulted -- no release version published; query time recorded",
    "hash_only": "Consulted -- content hash recorded, no release version published",
    "version_known": "Version known",
}

# Mirrors `report/report_generator.py`'s `_MODEL_STATUS_LABELS` exactly
# (same `pipeline/models/status.py` vocabulary) so the PDF and Markdown
# "AI model checkpoints" sections never drift on wording (F1a, report
# review round 4).
_MODEL_STATUS_LABELS = {
    _STATUS_USED: "ran this run",
    _STATUS_FAILED: "attempted, failed to load/run this run",
    _STATUS_DISABLED: "not available in this environment",
    _STATUS_SKIPPED: "not applicable to any variant this run",
}


def _build_provenance_flowables(document: Dict[str, Any], styles: Dict[str, ParagraphStyle]) -> List[Any]:
    """
    Data Source Provenance section -- the PDF's own rendering of what
    `report/report_generator.py::MarkdownReportGenerator._render_provenance`
    already builds for the Markdown output (this document's
    `code_version`/`model_checkpoints`/`provenance`, from
    `pipeline/provenance.py`). Previously that section existed only in
    the Markdown path -- the PDF, GEPER's primary clinical deliverable,
    had no reproducibility record at all despite the run-level data
    already being computed and present on `document`, which is unwired,
    not a design choice; this renders the identical content, in the
    same source-of-truth status vocabulary (`_PROVENANCE_STATUS_LABELS`
    mirrors `report_generator.py`'s `status_labels` exactly so the two
    formats never drift on wording).
    """
    flow: List[Any] = [
        Paragraph(
            "Recorded for reproducibility: if this report needs to be reproduced later, the exact "
            "data-source versions and code version below are what to match.",
            styles["Footnote"],
        ),
        Spacer(1, 2 * mm),
        Paragraph(f"<b>Bij AI code version:</b> {document.get('code_version') or 'unknown'}", styles["BodyText"]),
        Spacer(1, 2 * mm),
    ]

    checkpoints = document.get("model_checkpoints") or {}
    flow.append(Paragraph("<b>AI model checkpoints:</b>", styles["BodyText"]))
    # Round 17: see `report/report_generator.py::_render_provenance`'s
    # identical branch for the full reasoning (ROUND_CANDIDATES.md,
    # round 12) -- a run that died mid-loop leaves `run_complete` at its
    # honest `False` default (also what a pre-round-17 file with no
    # such key reads as, via `bool(...)`), and every checkpoint below is
    # then still a bare config identifier with no real run status.
    if not bool(document.get("run_complete")):
        flow.append(
            Paragraph(
                "This run did not complete (no post-loop status enrichment was recorded) -- the "
                "identifiers below are configuration only. Whether each model actually ran, failed to "
                "load, or was skipped this run is not yet known.",
                styles["StatusWarn"],
            )
        )
    if checkpoints:
        for name, value in sorted(checkpoints.items()):
            # See `report/report_generator.py`'s identical branch for
            # why both shapes are handled (F1a, report review round 4).
            if isinstance(value, dict):
                status_label = _MODEL_STATUS_LABELS.get(value.get("status"), value.get("status") or "unknown")
                text = f"• {esc(name)}: {esc(value.get('identifier'))} -- {esc(status_label)}"
                if value.get("reason"):
                    text += f" ({esc(value['reason'])})"
            else:
                text = f"• {esc(name)}: {esc(value)}"
            flow.append(Paragraph(text, styles["BulletText"]))
    else:
        flow.append(Paragraph("No AI model checkpoint identifiers recorded for this run.", styles["Footnote"]))
    flow.append(Spacer(1, 2 * mm))

    provenance = document.get("provenance") or []
    flow.append(Paragraph("<b>External data sources:</b>", styles["BodyText"]))
    if not provenance:
        flow.append(Paragraph("No data-source provenance was recorded for this run.", styles["Footnote"]))
        return flow

    for record in provenance:
        status = record.get("status", "unknown")
        label = _PROVENANCE_STATUS_LABELS.get(status, status)
        detail_bits = [f"{esc(record.get('source'))}: {esc(label)}"]
        if record.get("version"):
            detail_bits.append(f"version {esc(record['version'])}")
        if record.get("release_date"):
            detail_bits.append(f"released {esc(record['release_date'])}")
        if record.get("content_hash"):
            detail_bits.append(
                f"hash ({esc(record.get('hash_algorithm') or 'unknown algorithm')}): {esc(record['content_hash'])}"
            )
        # Second axis -- see `report_generator.py::_render_provenance`
        # and `pipeline/provenance.py::RetrievalMode`. Same shared
        # label map as the Markdown, so the two formats cannot drift
        # on this the way the status labels above are only kept in step
        # by a comment.
        retrieval = record.get("retrieval")
        if retrieval:
            detail_bits.append(f"retrieval: {esc(RETRIEVAL_MODE_LABELS.get(retrieval, retrieval))}")
        flow.append(Paragraph("• " + "; ".join(detail_bits), styles["BulletText"]))
    return flow


def _build_qc_flowables(qc_metrics: Dict[str, Dict[str, Any]], styles: Dict[str, ParagraphStyle]) -> List[Any]:
    """
    Sequencing QC status table -- PASS/WARNING against
    `CONFIG.qc_report`'s configurable thresholds, computed only from a
    real, validated `status: "found"` entry (see `_parse_qc_metrics`,
    which the caller -- `generate_pdf` -- has already run this dict
    through). This function itself never invents numbers, never
    coerces one, and never reads a `value` from anything but a `found`
    entry: a clinical PDF asserting "PASS" against a fake coverage/Q30
    value is exactly the kind of fabricated-evidence bug this pipeline
    works to eliminate elsewhere (see e.g. PP3/BP4's conflicting-
    evidence discipline; this is the report-review-round-7 fix for the
    same bug class in this specific table, after a B-series round had
    to fix an earlier instance of it here).

    Three distinct row renderings, not two:
      - FOUND   -> the existing PASS/WARNING logic, unchanged.
      - NOT_RUN -> "Not applicable", grey -- covers both "this
        invocation never had an upstream sequencing pipeline to
        measure from" (VCF-only mode) and "no tool to compute this
        metric has been wired yet" (bases_at_20x, this round) --
        distinguished from each other only by each row's own `reason`,
        surfaced in the footnote below.
      - ERROR   -> "Measurement failed", red -- an upstream step was
        genuinely attempted and did not succeed. Must never render
        identically to NOT_RUN: a reviewer needs to know "nobody
        tried" is a different fact from "somebody tried and it broke".
    """
    header = [
        Paragraph("Metric", styles["TableHeader"]),
        Paragraph("Result", styles["TableHeader"]),
        Paragraph("Threshold (PASS ≥)", styles["TableHeader"]),
        Paragraph("Status", styles["TableHeader"]),
    ]
    rows = [header]
    row_statuses: List[str] = []  # "PASS" | "WARNING" | "NOT_RUN" | "ERROR", one per data row
    not_run_entries: List[Tuple[str, Dict[str, Any]]] = []
    error_entries: List[Tuple[str, Dict[str, Any]]] = []

    for key in _QC_METRIC_ORDER:
        unit = _QC_METRIC_UNITS[key]
        threshold = _qc_threshold_pass_min(key)
        label = _QC_METRIC_LABELS[key]
        entry = qc_metrics.get(key) or {"status": _StageStatus.NOT_RUN.value, "value": None, "reason": None}
        status = entry.get("status")
        value = entry.get("value")

        if status == _StageStatus.FOUND.value and isinstance(value, (int, float)) and not isinstance(value, bool):
            pass_status = _qc_status(key, value)
            row_statuses.append(pass_status)
            rows.append(
                [
                    Paragraph(label, styles["TableLabel"]),
                    Paragraph(f"{value:g}{unit}", styles["TableValue"]),
                    Paragraph(f"{threshold:g}{unit}", styles["TableValue"]),
                    Paragraph(pass_status, styles["StatusPass"] if pass_status == "PASS" else styles["StatusWarn"]),
                ]
            )
        elif status == _StageStatus.ERROR.value:
            row_statuses.append("ERROR")
            error_entries.append((label, entry))
            rows.append(
                [
                    Paragraph(label, styles["TableLabel"]),
                    Paragraph("Measurement failed", styles["TableValue"]),
                    Paragraph(f"{threshold:g}{unit}", styles["TableValue"]),
                    Paragraph("ERROR", styles["StatusError"]),
                ]
            )
        else:
            # NOT_RUN, or anything that failed `_parse_qc_metrics`'s own
            # validation and fell back here -- deliberately the same
            # rendering as an explicit NOT_RUN rather than a fourth
            # visual state, since by the time this function runs, an
            # unrecognized/malformed input has already been turned into
            # a validated status by `_parse_qc_metrics`.
            row_statuses.append("NOT_RUN")
            not_run_entries.append((label, entry))
            rows.append(
                [
                    Paragraph(label, styles["TableLabel"]),
                    Paragraph("Not applicable", styles["TableValue"]),
                    Paragraph(f"{threshold:g}{unit}", styles["TableValue"]),
                    Paragraph("N/A", styles["TableValue"]),
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
    _QC_ROW_BG = {
        "PASS": colors.HexColor("#e3f6e8"),
        "WARNING": colors.HexColor("#fdf1d6"),
        "ERROR": colors.HexColor("#fbe0e0"),
        "NOT_RUN": colors.HexColor("#eeeeee"),
    }
    for i, status in enumerate(row_statuses, start=1):
        style_cmds.append(("BACKGROUND", (0, i), (-1, i), _QC_ROW_BG[status]))
    table.setStyle(TableStyle(style_cmds))

    flowables: List[Any] = [table]

    if not_run_entries:
        flowables.append(Spacer(1, 2 * mm))
        # Grouped by the EXACT reason string, not a fixed enum of the
        # three reasons known at the time this was written
        # (VCF-only's `_QC_METRICS_NOT_APPLICABLE_REASON`, the per-key
        # "not reported" fallback in `_parse_one_qc_metric`, and
        # `bridge/combined_pipeline.py`'s per-metric tool-absence
        # reason for bases_at_20x). `_parse_one_qc_metric` passes any
        # caller-supplied `reason` through verbatim for a NOT_RUN
        # entry, so a future or third-party --qc-metrics-json producer
        # can introduce a reason this code has never seen -- keying on
        # the string itself, not a name-based special case, is what
        # keeps grouping correct for that reason too, not just the
        # three known today. When two or more metrics share the exact
        # same reason (round 13: this was previously the SAME ~40-word
        # VCF-only sentence repeated once per metric, verbatim, in one
        # paragraph), it is now stated once, naming every metric label
        # that shares it -- content and wording untouched, see
        # `_QC_METRICS_NOT_APPLICABLE_REASON`'s and
        # `_BASES_AT_20X_NOT_RUN_REASON`'s own docstrings/comments for
        # why neither may be shortened. Distinct reasons (e.g. Run 2's
        # real shape: two metrics found, bases_at_20x NOT_RUN for a
        # tool-absence reason wholly unrelated to VCF-only) still each
        # get their own clause -- grouping only ever merges entries
        # whose reason text is identical, never entries whose reasons
        # merely both happen to be NOT_RUN.
        reason_groups: Dict[str, List[str]] = {}
        for label, entry in not_run_entries:
            reason = entry.get("reason") or "not applicable this run."
            reason_groups.setdefault(reason, []).append(label)
        clauses = "; ".join(f"{', '.join(labels)} -- {esc(reason)}" for reason, labels in reason_groups.items())
        flowables.append(
            Paragraph(
                f"Not applicable this run: {clauses} To supply real values from a kim_pipeline-combined "
                "run, see bridge/combined_pipeline.py's --qc-metrics-json handoff, which the "
                "orchestrator records on the report document itself.",
                styles["Footnote"],
            )
        )

    if error_entries:
        flowables.append(Spacer(1, 2 * mm))
        reasons = "; ".join(
            f"{label} -- {esc(entry.get('reason') or 'measurement failed.')}" for label, entry in error_entries
        )
        flowables.append(
            Paragraph(
                f"Measurement failed this run (an upstream step was attempted and did not succeed, not "
                f"merely unreported): {reasons}",
                styles["StatusError"],
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
        clinical = candidate_interpretation_of(variant_result) or {}
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

    # "Completeness", not "Confidence" (C2, report review round 2): this
    # column is `ConfidenceEngine`'s evidence-completeness score, not a
    # measure of how certain the classification is -- see the
    # explanatory footnote appended below the table, and
    # `pipeline/confidence_engine.py`'s own module docstring. Renamed
    # site-wide (table header, per-finding label, Markdown heading) so
    # "Pathogenic / Low (25%)" can no longer be misread as doubt about
    # the call itself.
    header_labels = ["#", "Variant / Gene", "Classification", "Completeness"]
    if has_case_ranking:
        header_labels.append("Phenotype Match")
    header_labels += ["Top Evidence", "Attention"]
    header = [Paragraph(h, styles["TableHeader"]) for h in header_labels]
    rows: List[List[Paragraph]] = [header]
    flag_rows: List[int] = []  # 1-based row indices (into `rows`) carrying at least one reviewer flag

    for row_num, (idx, variant_result) in enumerate(indexed, start=1):
        variant = variant_result.get("variant", {})
        locus = esc(f"{variant.get('chrom')}:{variant.get('pos')} {variant.get('ref')}>{variant.get('alt')}")

        if variant_result.get("out_of_scope"):
            # Report review round 8: must never render as "Not
            # classified" / "Pending" -- those are today's rendering
            # for a variant GEPER genuinely tried to classify and
            # couldn't, which is a different claim from "never
            # evaluated, by design". No reviewer-attention flag either
            # (that column signals something needs review; this
            # variant needs no ACMG review at all).
            row = [
                Paragraph(str(idx), val),
                Paragraph(locus, small),
                Paragraph("Out of scope (mitochondrial)", small),
                Paragraph("N/A", small),
            ]
            if has_case_ranking:
                row.append(Paragraph("N/A", small))
            row += [Paragraph("—", small), Paragraph("—", small)]
            rows.append(row)
            continue

        clinical = candidate_interpretation_of(variant_result)
        gene = (variant_result.get("interpretation_result") or {}).get("gene_symbol")
        gene_line = f"{locus}<br/><b>{esc(gene)}</b>" if gene else locus

        acmg = (clinical or {}).get("acmg_classification") or {}
        classification = acmg.get("classification") or "Not classified"
        # Report review round 10: net Tavtigian points inline, right on
        # the front-page dashboard row -- a reviewer skimming this table
        # can see why without opening the detailed finding section.
        # Absent (no second line) only for BA1's stand-alone-benign
        # short-circuit or an unclassified variant.
        net_points = acmg.get("net_points")
        if net_points is not None:
            classification = f"{classification}<br/>net {net_points:g}"

        # No percentage in this front-page table (C2, report review
        # round 2): "Pathogenic / Low (25%)" reads as doubt about the
        # classification, when this score in fact measures evidence
        # completeness (how many of 7 unrelated categories returned
        # data), not certainty -- a PVS1-very_strong frameshift with a
        # Definitive ClinGen gene-disease case can legitimately score
        # "Low" here purely because AlphaMissense/MMSplice don't apply
        # to a null variant. The label alone is still useful triage
        # signal; the percentage invited a misreading the label alone
        # doesn't. The full percentage remains in each finding's own
        # detail section, next to the explanatory footnote that
        # contextualizes it.
        confidence = (clinical or {}).get("confidence") or {}
        if confidence.get("pending", True):
            confidence_text = "Pending"
        else:
            confidence_text = confidence.get("label") or "n/a"

        top_evidence = [
            _normalize_evidence_text(item) for item in ((clinical or {}).get("supporting_evidence") or [])[:3]
        ]
        top_evidence = [t for t in top_evidence if t]
        evidence_text = "<br/>".join(f"• {esc(t)}" for t in top_evidence) if top_evidence else "—"

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
                        f"{esc(m.get('matched_gene_term_name') or m.get('matched_gene_term_id'))} "
                        f"({esc(m['match_type'])})"
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

    # This column's header is now "Completeness" (C2, report review
    # round 2), which is longer than the "Confidence" text the D5-fixed
    # width below was originally sized for -- it re-wrapped mid-word
    # ("Completene / ss") at the old 21/24mm (D5, report review round
    # 3). Widened again to fit "Completeness" specifically; the extra
    # width is taken from "Top Evidence", the widest column, so the
    # table's total width is unchanged.
    if has_case_ranking:
        col_widths = [8 * mm, 30 * mm, 25 * mm, 26 * mm, 30 * mm, 31 * mm, 20 * mm]
    else:
        col_widths = [8 * mm, 35 * mm, 30 * mm, 29 * mm, 44 * mm, 24 * mm]
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


def _gene_clusters(variants: List[Dict[str, Any]]) -> Dict[str, List[int]]:
    """
    Groups this run's 1-based finding numbers by gene symbol; returns
    only genes with more than one finding (C7, report review round 2).
    Works from `interpretation_result.gene_symbol`, already resolved
    for every finding -- no new evidence source, no phenotype input
    required, unlike `pipeline/case_prioritization.py`'s HPO-driven
    ranking (which only activates with --hpo-terms/--phenotype-file
    and leaves every other run with no cross-finding view at all).
    """
    by_gene: Dict[str, List[int]] = {}
    for idx, vr in enumerate(variants, start=1):
        gene = (vr.get("interpretation_result") or {}).get("gene_symbol")
        if not gene:
            continue
        by_gene.setdefault(gene, []).append(idx)
    return {gene: idxs for gene, idxs in by_gene.items() if len(idxs) > 1}


def _genomic_window_clusters(variants: List[Dict[str, Any]], window_bp: int) -> List[List[int]]:
    """
    Groups this run's 1-based finding numbers whose genomic positions
    sit within `window_bp` of their nearest same-chromosome neighbor
    (C7, report review round 2) -- a chained/transitive grouping (A-B
    within window and B-C within window group A, B, C together even if
    A-C alone would exceed it), matching how a reviewer would eyeball a
    cluster on a coordinate track. Deliberately reports proximity only:
    this is NOT phase, NOT compound-heterozygosity, NOT a cis/trans
    call -- GEPER has no phase data, and the report text built from
    this must never imply one. Findings missing chrom/pos are skipped,
    never guessed into a cluster.
    """
    by_chrom: Dict[str, List[Tuple[int, int]]] = {}
    for idx, vr in enumerate(variants, start=1):
        variant = vr.get("variant") or {}
        chrom, pos = variant.get("chrom"), variant.get("pos")
        if chrom is None or pos is None:
            continue
        try:
            by_chrom.setdefault(str(chrom), []).append((int(pos), idx))
        except (TypeError, ValueError):
            continue

    clusters: List[List[int]] = []
    for entries in by_chrom.values():
        entries.sort()
        current: List[Tuple[int, int]] = []
        for pos, idx in entries:
            if current and pos - current[-1][0] > window_bp:
                if len(current) > 1:
                    clusters.append([i for _, i in current])
                current = []
            current.append((pos, idx))
        if len(current) > 1:
            clusters.append([i for _, i in current])
    return clusters


def _multi_finding_observation_lines(document: Dict[str, Any], variants: List[Dict[str, Any]]) -> List[str]:
    """
    Plain-language lines for the gene- and genomic-window clustering
    observations above -- both computed unconditionally (no phenotype
    input needed), unlike `case_prioritization`'s HPO-gated ranking.
    Returns [] when this run has nothing to group (a single variant, or
    every variant in a different gene and far apart).
    """
    lines: List[str] = []
    gene_groups = _gene_clusters(variants)
    for gene, idxs in sorted(gene_groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        finding_list = ", ".join(f"#{i}" for i in idxs)
        lines.append(f"{len(idxs)} findings in {gene}: {finding_list}.")

    window_bp = CONFIG.variant_clustering.WINDOW_BP
    for cluster in _genomic_window_clusters(variants, window_bp):
        chrom = (variants[cluster[0] - 1].get("variant") or {}).get("chrom")
        positions = [(variants[i - 1].get("variant") or {}).get("pos") for i in cluster]
        finding_list = ", ".join(f"#{i}" for i in cluster)
        lines.append(
            f"Findings {finding_list} sit within {window_bp:,} bp of each other on chromosome {chrom} "
            f"(positions {min(positions)}-{max(positions)}). Bij AI has no phase data and does not infer "
            "compound heterozygosity or a cis/trans relationship from this -- proximity is noted for "
            "reviewer awareness only."
        )
    return lines


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
        f"<b>Sample:</b> {esc(sample_id)} &nbsp;&nbsp; <b>Run:</b> {esc(run_id)} &nbsp;&nbsp; "
        f"<b>Reference build:</b> {esc(assembly) or 'not specified'} &nbsp;&nbsp; "
        f"<b>Variants analyzed:</b> {len(variants)}"
    )
    flow.append(Paragraph(identity, styles["BodyText"]))
    flow.append(Spacer(1, 3 * mm))

    if variants:
        flow.append(_build_clinician_summary_table(variants, styles))
        flow.append(
            Paragraph(
                f"{EVIDENCE_COMPLETENESS_CAPTION} A variant with the strongest possible single line of "
                "evidence can still score Low here if other, unrelated evidence categories do not apply "
                "to it (see each finding's own detail section).",
                styles["Footnote"],
            )
        )
    else:
        flow.append(Paragraph("No variants were analyzed in this run.", styles["BodyText"]))
    flow.append(Spacer(1, 3 * mm))

    # Gene- and genomic-window clustering observation (C7, report
    # review round 2): works without any phenotype input, unlike
    # `case_prioritization`'s HPO-gated ranking, so it's present on
    # every run with more than one finding worth grouping.
    multi_finding_lines = _multi_finding_observation_lines(document, variants)
    if multi_finding_lines:
        flow.append(Paragraph("Multi-Finding Observations", styles["SectionHeading"]))
        flow.extend(Paragraph(f"• {esc(line)}", styles["BulletText"]) for line in multi_finding_lines)
        flow.append(Spacer(1, 3 * mm))

    attention_lines: List[str] = []
    for idx, variant_result in enumerate(variants, start=1):
        clinical = candidate_interpretation_of(variant_result)
        flags = _variant_reviewer_flags(variant_result, clinical)
        if flags:
            variant = variant_result.get("variant", {})
            locus = esc(f"{variant.get('chrom')}:{variant.get('pos')} {variant.get('ref')}>{variant.get('alt')}")
            attention_lines.append(f"Finding {idx} ({locus}): {'; '.join(flags)}")
    gap_sources = _provenance_gap_sources(document, variants)
    if gap_sources:
        attention_lines.append(
            "Data-source version not determinable for cited evidence from: " + ", ".join(sorted(gap_sources)) + "."
        )
    offline_caveat = _offline_sources_caveat_text(document)
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


def _build_1000_genomes_sas_flowables(ipf: Dict[str, Any], styles: Dict[str, ParagraphStyle]) -> List[Any]:
    """
    Renders the 1000 Genomes SAS section -- the SOLE Indian/South-Asian
    cohort source as of 2026-08-08 (IndiGenomes was retired from
    GEPER's active query path -- see `DATA_SOURCE_LICENSE_AUDIT.md` and
    `report/clinical_report_builder.py::_indian_population_frequency`).
    Called whenever `ipf["sas_shown"]` is True -- in real pipeline
    operation, always. Both mandatory disclosures
    (`SAMPLE_SIZE_DISCLOSURE`/`DIASPORA_DISCLOSURE` -- the same shared
    text `report/report_generator.py`'s Markdown path renders, so the
    two output formats never drift on wording) are always shown
    whenever this section has anything to report at all -- found, not
    found, or errored -- not only on the "found" path.
    """
    flow: List[Any] = [Paragraph("• 1000 Genomes (South Asian, SAS):", styles["BulletText"])]
    if ipf.get("sas_available"):
        pooled = ipf.get("sas_pooled") or {}
        if pooled.get("af") is not None:
            flow.append(
                Paragraph(
                    f"&nbsp;&nbsp;&nbsp;&nbsp;- Pooled SAS: AF = {pooled.get('af'):.2e} "
                    f"(AC={pooled.get('ac')}, AN={pooled.get('an')})",
                    styles["BulletText"],
                )
            )
        labels = ipf.get("sas_population_labels") or {}
        sizes = ipf.get("sas_sample_sizes") or {}
        for code, sub in (ipf.get("sas_sub_populations") or {}).items():
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
    elif ipf.get("sas_error"):
        flow.append(
            Paragraph(
                f"&nbsp;&nbsp;&nbsp;&nbsp;- Lookup failed (external service issue: {esc(ipf['sas_error'])}).",
                styles["BulletText"],
            )
        )
    else:
        flow.append(Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;- Variant not found in this source.", styles["BulletText"]))
    flow.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;! {SAMPLE_SIZE_DISCLOSURE}", styles["StatusWarn"]))
    flow.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;! {DIASPORA_DISCLOSURE}", styles["StatusWarn"]))
    return flow


def _build_indian_population_frequency_flowables(
    clinical: Dict[str, Any], styles: Dict[str, ParagraphStyle]
) -> List[Any]:
    """
    India-deployment feature: gnomAD South Asian (SAS) AF alongside the
    1000 Genomes Project's SAS sub-population AF -- the sole
    Indian/South-Asian cohort source as of 2026-08-08 (see
    `report/clinical_report_builder.py::_indian_population_frequency`
    for where this dict comes from; IndiGenomes was retired from
    GEPER's active query path, see `DATA_SOURCE_LICENSE_AUDIT.md`),
    plus a "Common in Indian populations" flag when gnomAD SAS alone
    exceeds the configured threshold. Rendered for every finding, never
    omitted -- a missing section used to be indistinguishable from a
    queried-and-empty one (both showed nothing at all), which is
    exactly the "absence of the section is indistinguishable from
    absence of the query" failure mode this report works to eliminate
    elsewhere. In real pipeline operation the 1000 Genomes SAS stage
    always runs, so the explicit not-queried branches below are the
    honest fallback for the cases where a caller genuinely didn't wire
    that stage's result through, not a state a real end-to-end run
    should ever hit.
    """
    ipf = clinical.get("indian_population_frequency") or {}
    gnomad_sas_af = ipf.get("gnomad_af_sas")

    flow: List[Any] = [Spacer(1, 2 * mm), Paragraph("<b>Indian Population Frequency:</b>", styles["BodyText"])]
    if ipf.get("gnomad_sas_error") is not None:
        # Same fix and same wording as `_build_1000_genomes_sas_flowables`'s
        # `sas_error` branch four lines below, and as
        # `report_generator.py`'s Section 11 Markdown equivalent -- a
        # genuine gnomAD lookup failure must not render identically to a
        # variant that was never queried (2026-09-11 fix; this bullet was
        # the one sub-block of this function that still collapsed the two).
        flow.append(
            Paragraph(
                f"• gnomAD (South Asian, SAS): Lookup failed (external service issue: "
                f"{esc(ipf['gnomad_sas_error'])}) -- not evidence of no South Asian subpopulation "
                "data, see Annotation Detail below.",
                styles["BulletText"],
            )
        )
    elif gnomad_sas_af is not None:
        flow.append(Paragraph(f"• gnomAD (South Asian, SAS): AF = {gnomad_sas_af:.2e}", styles["BulletText"]))
    elif ipf.get("gnomad_sas_queried"):
        flow.append(Paragraph("• gnomAD (South Asian, SAS): variant not found in this source.", styles["BulletText"]))
    else:
        flow.append(Paragraph("• gnomAD (South Asian, SAS): not queried for this variant.", styles["BulletText"]))
    if ipf.get("sas_shown"):
        flow.extend(_build_1000_genomes_sas_flowables(ipf, styles))
    else:
        flow.append(
            Paragraph("• 1000 Genomes Project (South Asian, SAS): not queried for this variant.", styles["BulletText"])
        )
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
    # Card T3-F4: HGVS added alongside the coordinate string (not a
    # replacement -- the coordinate stays for precision, HGVS is the
    # identifier clinicians actually use), via the same shared fallback
    # helper the full Markdown heading and the short PDF heading both
    # call, so all three degrade identically when neither hgvs_c nor
    # hgvs_g is available. In that fallback-to-locus case the
    # parenthetical below repeats `locus` verbatim -- an accepted
    # consequence of one shared rule, not a fourth special case.
    locus_with_hgvs = f"{locus} ({_variant_hgvs_or_locus(variant_result)})"
    clinical = candidate_interpretation_of(variant_result)

    flow: List[Any] = [
        # `_Bookmark`'s title is a raw PDF outline string (`Canvas.
        # addOutlineEntry`), never parsed as XML the way `Paragraph`
        # text is -- deliberately built from the unescaped
        # `locus_with_hgvs` here (escaping it would show a literal
        # "&gt;" in the PDF's sidebar/outline panel instead of decoding
        # it). Only the `Paragraph` heading right below needs `esc()`.
        # The bookmark carries the same HGVS-augmented text as the
        # heading (Card T3-F4) -- a PDF outline entry is itself a
        # navigation identifier, and giving it a different string from
        # the heading it points to would be the same correlation
        # failure one level down.
        _Bookmark(f"bm_finding_{idx}", f"Finding {idx}: {locus_with_hgvs}"),
        Spacer(1, 4 * mm),
        Paragraph(f"Finding {idx}: {esc(locus_with_hgvs)}", styles["SectionHeading"]),
    ]

    # Within-sample read support (NABL 112A s.7.8.5(b)(iii)). Read from
    # `variant_result["variant"]` directly -- the same way this function
    # already reads the locus above and the mitochondrial chromosome
    # below, and the same way the Markdown renderer reads it -- through
    # the ONE shared helper both renderers call, so the two full reports
    # cannot drift on how an absent or unparseable fraction reads. It is
    # deliberately not a `candidate_interpretation_of()` section: that
    # dict is `None` whenever interpretation failed, which would make a
    # failed interpretation and a VCF with no per-sample data render
    # identically. Explicitly labelled "within-sample" because the
    # Population Evidence section further down carries gnomAD's
    # population allele frequency -- a different quantity with the same
    # name, and the confusion this label exists to prevent.
    flow.append(
        Paragraph(
            f"<b>Allele fraction (within this sample):</b> {esc(variant_allele_fraction_text(variant_result))}",
            styles["BodyText"],
        )
    )

    # Round 14, B2: per-finding, not report-level -- see
    # report/report_generator.py's identical placement/reasoning.
    if is_mitochondrial_chrom(variant.get("chrom")):
        # Round 16: see report/report_generator.py's identical change for why
        # `not_evaluated_rules` is threaded through here.
        not_evaluated_rules = (variant_result.get("interpretation_result") or {}).get("not_evaluated_rules", [])
        flow.append(
            Paragraph(
                esc(mtdna_interpretation_disclaimer(variant_result.get("transcript"), not_evaluated_rules)),
                styles["StatusWarn"],
            )
        )
        flow.append(Spacer(1, 2 * mm))

    out_of_scope = variant_result.get("out_of_scope")
    if out_of_scope:
        # Distinct from the "no clinical interpretation available"
        # fallback just below -- that one means GEPER tried and the
        # interpretation engine produced nothing; this means GEPER never
        # attempted anything for this variant at all, by design. No
        # current producer as of round 14, B2 (which replaced the
        # mitochondrial compartment's whole-variant rejection with
        # per-criterion gating -- see `pipeline/acmg_rules.py`) -- kept
        # as reusable infrastructure for a future genuinely-unassessable
        # variant class.
        flow.append(
            Paragraph(
                f"<b>Status:</b> Out of scope ({esc(out_of_scope.get('scope', 'unspecified'))})", styles["StatusWarn"]
            )
        )
        flow.append(
            Paragraph(
                esc(out_of_scope.get("reason")) or "This variant is out of scope for this Bij AI build.",
                styles["BodyText"],
            )
        )
        flow.append(
            Paragraph(
                "No ACMG/AMP criteria were evaluated for this variant. This is not a Variant of "
                "Uncertain Significance -- it was never assessed.",
                styles["Footnote"],
            )
        )
        return flow

    if not clinical:
        flow.append(
            Paragraph(
                "No clinical interpretation is available for this variant (the interpretation engine "
                "did not produce a result for it). This is reported as a data gap, not a benign finding.",
                styles["BodyText"],
            )
        )
        return flow

    flow.append(Paragraph(esc(clinical["executive_summary"]), styles["BodyText"]))

    acmg = clinical.get("acmg_classification") or {}
    if acmg.get("classification"):
        flow.append(
            Paragraph(
                f"<b>ACMG/AMP Classification (Tavtigian 2018 point system):</b> {acmg['classification']}",
                styles["BodyText"],
            )
        )
        # Report review round 10: the real Tavtigian point total this
        # classification was actually decided from, plus the threshold
        # band it landed in -- see `pipeline/acmg_rules.py::
        # CombineResult`'s docstring. `None` only for BA1's stand-alone-
        # benign short-circuit (no point tally ran), never fabricated.
        net_points = acmg.get("net_points")
        if net_points is not None:
            band = acmg.get("net_points_band")
            band_text = f" -- threshold band: {band}" if band else ""
            flow.append(
                Paragraph(
                    f"<b>Net points:</b> {net_points:g} "
                    f"(pathogenic {acmg.get('pathogenic_points', 0):g} − "
                    f"benign {acmg.get('benign_points', 0):g}){band_text}",
                    styles["BodyText"],
                )
            )

    # Clinician override (geper/review/signoff.py's "override" command) --
    # layered on top of, never substituting for, GEPER's own
    # classification above: both are shown, explicitly labelled. Absent
    # for every variant no clinician has overridden (the common case).
    override = acmg.get("clinician_override")
    if override:
        flow.append(
            Paragraph(
                f"<b>Clinician Override:</b> GEPER classification: "
                f"{esc(override.get('original_classification')) or 'Not classified'}; "
                f"Clinician override: <b>{esc(override.get('new_classification'))}</b> -- {esc(override.get('reason'))} "
                f"(by {esc(override.get('clinician_id'))}, {esc(override.get('timestamp'))})",
                styles["StatusWarn"],
            )
        )

    confidence = clinical.get("confidence") or {}
    if not confidence.get("pending") and confidence.get("label"):
        score = confidence.get("score")
        score_text = f" ({score:.0f}%)" if isinstance(score, (int, float)) else ""
        flow.append(Paragraph(f"<b>Evidence Completeness:</b> {confidence['label']}{score_text}", styles["BodyText"]))
        flow.append(
            Paragraph(
                f"{EVIDENCE_COMPLETENESS_CAPTION} It is computed across seven independent categories "
                "(clinical, population, AI, protein, structural, sequence-context, additional). A variant "
                "with the strongest possible single line of evidence (e.g. PVS1 very_strong on a clear "
                "loss-of-function call) can still score Low here if unrelated categories genuinely don't "
                "apply to it -- see the breakdown below for exactly which categories contributed and why.",
                styles["Footnote"],
            )
        )
        category_breakdown = (confidence.get("breakdown") or {}).get("category_breakdown") or []
        if category_breakdown:
            cb_rows = [
                [
                    Paragraph("Category", styles["TableHeader"]),
                    Paragraph("Present", styles["TableHeader"]),
                    Paragraph("Quality", styles["TableHeader"]),
                    Paragraph("Why", styles["TableHeader"]),
                ]
            ]
            for cat in category_breakdown:
                cb_rows.append(
                    [
                        Paragraph(esc(cat.get("category")), styles["TableValue"]),
                        Paragraph(f"{cat.get('presence', 0):.0%}", styles["TableValue"]),
                        Paragraph(f"{cat.get('quality', 0):.0%}", styles["TableValue"]),
                        Paragraph(esc(cat.get("rationale")), styles["TableValueSmall"]),
                    ]
                )
            cb_table = Table(cb_rows, colWidths=[28 * mm, 18 * mm, 18 * mm, 96 * mm], hAlign="LEFT", repeatRows=1)
            cb_table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.3, _TABLE_GRID_COLOR),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#5a5a5a")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            flow.append(Spacer(1, 1 * mm))
            flow.append(cb_table)

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
                    Paragraph(esc(crit.get("code")), styles["TableValue"]),
                    Paragraph(esc(crit.get("strength")), styles["TableValue"]),
                    Paragraph(esc(crit.get("rationale")), styles["TableValueSmall"]),
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

    # Criteria that were actually checked against this variant's
    # evidence and came back negative -- shown so every evaluated
    # criterion is accounted for somewhere in the report, not just the
    # ones that triggered (not-evaluated criteria already get their own
    # footnote in `limitations`; without this block, a not-triggered
    # criterion was indistinguishable in the PDF from one that was
    # never checked at all).
    not_triggered = acmg.get("not_triggered_criteria") or []
    if not_triggered:
        nt_rows = [
            [
                Paragraph("Criterion", styles["TableHeader"]),
                Paragraph("Rationale", styles["TableHeader"]),
            ]
        ]
        for crit in not_triggered:
            nt_rows.append(
                [
                    Paragraph(esc(crit.get("code")), styles["TableValue"]),
                    Paragraph(esc(crit.get("rationale")), styles["TableValueSmall"]),
                ]
            )
        nt_table = Table(nt_rows, colWidths=[22 * mm, 138 * mm], hAlign="LEFT", repeatRows=1)
        nt_table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.3, _TABLE_GRID_COLOR),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#5a5a5a")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        flow.append(Spacer(1, 2 * mm))
        flow.append(Paragraph(f"<b>Criteria Checked, Not Triggered ({len(not_triggered)}):</b>", styles["BodyText"]))
        flow.append(Spacer(1, 1 * mm))
        flow.append(nt_table)

    # PVS1 decision-tree path (C5, report review round 2): PVS1's own
    # rationale text (see `pipeline/pvs1/decision_tree.py::
    # PVS1DecisionTree._apply_mechanism_gate`) refers to "the decision
    # tree" reaching its strength -- this is that tree's actual
    # question-by-question path, computed by `PVS1DecisionTree.evaluate`
    # for every PVS1 result (triggered, not_triggered, or not_evaluated
    # alike) but previously dropped before reaching any report section.
    # Referencing an audit trail that isn't shown is worse than not
    # referencing it at all, so this is now rendered whenever PVS1
    # carries one, not just cited.
    pvs1_entry = next((c for c in (triggered + not_triggered) if c.get("code") == "PVS1" and c.get("details")), None)
    if pvs1_entry:
        decision_path = pvs1_entry["details"].get("decision_path") or []
        if decision_path:
            flow.append(Spacer(1, 2 * mm))
            flow.append(Paragraph("<b>PVS1 Decision-Tree Path:</b>", styles["BodyText"]))
            flow.extend(Paragraph(f"{i}. {esc(step)}", styles["BulletText"]) for i, step in enumerate(decision_path, 1))
        unchecked_caveats = pvs1_entry["details"].get("unchecked_caveats") or []
        if unchecked_caveats:
            flow.append(Spacer(1, 1 * mm))
            flow.append(Paragraph("<i>PVS1 caveats not checked this run:</i>", styles["Footnote"]))
            flow.extend(Paragraph(f"• {esc(c)}", styles["Footnote"]) for c in unchecked_caveats)

    # Combining-rule trace (C3, report review round 2): already
    # computed by `ACMGRuleEngine._combine` and already rendered in the
    # Markdown report (`report/report_generator.py`), but previously
    # never rendered in this PDF -- the exact "cited but not shown"
    # problem this round's C5 item names for `decision_path`. Shown
    # here because it's what actually explains BP6-triggered-but-
    # excluded-from-scoring (and every other point contribution) in
    # this specific finding's own arithmetic, not just in the abstract.
    combining_trace = acmg.get("combining_rule_trace") or []
    if combining_trace:
        flow.append(Spacer(1, 2 * mm))
        flow.append(Paragraph("<b>Classification Combining-Rule Trace:</b>", styles["BodyText"]))
        flow.extend(Paragraph(f"• {esc(line)}", styles["BulletText"]) for line in combining_trace)

    supporting = clinical.get("supporting_evidence") or []
    if supporting:
        flow.append(Spacer(1, 2 * mm))
        flow.append(Paragraph("<b>Supporting Evidence:</b>", styles["BodyText"]))
        flow.extend(Paragraph(f"• {esc(item)}", styles["BulletText"]) for item in supporting)

    # Conflicting evidence: computed by the same
    # `report/clinical_report_builder.py` pass that fills
    # `supporting_evidence` above, and rendered by the Markdown report
    # ("### 6. Conflicting Evidence" in `report/report_generator.py`),
    # but never rendered in this PDF -- the same "cited but not shown"
    # asymmetry already noted for `decision_path` and the combining-rule
    # trace above, and the more dangerous direction of it: this is where
    # evidence AGAINST the benign-direction reading is stated. BP7's
    # uncalibrated-splice-predictor disclosure lands here (see
    # `pipeline/acmg_rules.py::ACMGRuleEngine._bp7`), so dropping the
    # section silently dropped that caveat from the PDF while the
    # Markdown report showed it. Placed before Limitations deliberately:
    # a per-variant evidence caveat belongs next to that variant's
    # evidence, not pooled into the report-wide boilerplate at the end.
    conflicting = clinical.get("conflicting_evidence") or []
    if conflicting:
        flow.append(Spacer(1, 2 * mm))
        flow.append(Paragraph("<b>Conflicting Evidence:</b>", styles["BodyText"]))
        flow.extend(Paragraph(f"• {esc(item)}", styles["BulletText"]) for item in conflicting)

    flow.extend(_build_indian_population_frequency_flowables(clinical, styles))

    # Recommendations: computed per finding by
    # `report/clinical_report_builder.py` and rendered by the Markdown
    # report as its section 14, but never by this one -- so a clinician
    # reading the PDF saw no follow-up actions at all, while the same
    # run's Markdown listed them. Per-finding, not run-level: each
    # recommendation is attached to the variant that prompted it, and
    # aggregating them to the document would strip which finding a
    # "refer for genetic counselling" actually came from.
    #
    # Rendered only when non-empty, unlike Markdown's explicit "no
    # recommendations generated" line: this section sits inside a
    # finding's detail block rather than a fixed numbered outline, and
    # the surrounding blocks here follow the same
    # omit-when-absent convention (Supporting Evidence, Conflicting
    # Evidence, Limitations above and below all do).
    recommendations = clinical.get("recommendations") or []
    if recommendations:
        flow.append(Spacer(1, 2 * mm))
        flow.append(Paragraph("<b>Recommendations:</b>", styles["BodyText"]))
        flow.extend(Paragraph(f"• {esc(item)}", styles["BulletText"]) for item in recommendations)

    limitations = clinical.get("limitations") or []
    if limitations:
        flow.append(Spacer(1, 2 * mm))
        flow.append(Paragraph("<b>Limitations:</b>", styles["BodyText"]))
        flow.extend(Paragraph(f"• {esc(item)}", styles["BulletText"]) for item in limitations)

    # `clinical["references"]` (see `report/clinical_report_builder.py
    # ::_references`) was already computed for the Markdown report's own
    # "References" section, but this PDF path never rendered it -- found
    # while adding HPO/Orphanet citations (2026-08-08). `_references()`
    # now always returns at least the unconditional Orphanet entry, so
    # this list is never empty in practice, but the `if` guard is kept
    # for defensive symmetry with every other optional section here.
    references = clinical.get("references") or []
    if references:
        flow.append(Spacer(1, 2 * mm))
        flow.append(Paragraph("<b>References:</b>", styles["BodyText"]))
        flow.extend(Paragraph(f"• {item}", styles["BulletText"]) for item in references)

    return flow


def _build_signoff_block(styles: Dict[str, ParagraphStyle]) -> List[Any]:
    """
    Signature + date lines for both required signatories, followed
    immediately by the standard legal disclaimer.

    Dual sign-off -- Clinical Scientist and Consultant Clinical
    Scientist -- matching `report/summary_short.py::_build_signoff_block`'s
    convention exactly (the diagnostic-genetics two-signature standard:
    an authoring scientist plus an authorising consultant). Previously
    this report used a different, single-signer title ("Chief
    Pathologist / Medical Director") from the short report's dual
    signers for the same run -- a reviewer moving between the two PDFs
    for one variant would see two different answers to "who signs
    this". `_SIGNOFF_ROLES` is the shared, single source of truth for
    both formats now; update it there, not here, to change either.

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
    role_blocks: List[Any] = []
    for i, role in enumerate(_SIGNOFF_ROLES):
        if i:
            role_blocks.append(Spacer(1, 6 * mm))
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
        role_blocks.extend([Paragraph(role, styles["SignoffTitle"]), Spacer(1, 4 * mm), sig_table])

    return [
        KeepTogether(
            [
                _Bookmark("bm_signoff", "Sign-off & Disclaimer"),
                Spacer(1, 10 * mm),
                *role_blocks,
            ]
        ),
        Spacer(1, 8 * mm),
        Paragraph(
            _DISCLAIMER_LABEL + RESEARCH_USE_DISCLAIMER,
            styles["Disclaimer"],
        ),
        Spacer(1, 3 * mm),
        Paragraph(
            ISO_RESEARCH_ELEMENT,
            styles["Disclaimer"],
        ),
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def generate_pdf(
    document: Dict[str, Any],
    output_path: str,
    patient_meta: Optional[Union[Dict[str, Any], str]] = None,
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
    Run-level QC is NOT a parameter: it is read from
    `document["qc_metrics"]` by `_document_qc_metrics`. See
    `_parse_qc_metrics`'s docstring for the three-state (found/not
    applicable/failed) shape each of "mean_coverage_depth",
    "bases_at_20x", "q30_score" is validated into, and
    `config.py::QCReportConfig`'s docstring for why GEPER cannot
    compute these itself from a VCF-only pipeline. `bridge/
    combined_pipeline.py` is the reference producer of this file when
    kim_pipeline supplied the upstream FASTQ->BAM run.
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
    patient["consent"] = _document_consent(document)
    parsed_qc_metrics = _parse_qc_metrics(_document_qc_metrics(document))
    sample_id = _derive_sample_id(document)
    resolved_run_id = _derive_run_id(document, run_id)
    # Display only: `assembly_note` (card #15) says why an all-mitochondrial
    # run has no build, and fills both build slots in its place.
    assembly = document.get("assembly") or document.get("assembly_note")
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
        title="Bij AI Clinical Genomic Analysis Report — variant interpretation from a supplied VCF (GEPER engine)",
    )

    story: List[Any] = list(_build_report_header(logo_path, styles))
    # WHAT THIS REPORT IS, said on the report itself -- see SCOPE_LINE.
    # Added to the STORY rather than inside _build_report_header on purpose:
    # that function's letterhead sizes the logo to the TITLE's height
    # (`logo_h = title_height`), so putting this in the title would double
    # the logo, and adding it to the header's return value would change a
    # contract its own tests pin.
    story.append(Paragraph(SCOPE_LINE, styles["Footnote"]))
    story.append(Spacer(1, 4 * mm))
    # What this document IS, before it says what it found. Placement,
    # not emphasis: this claim was already in the page footer at 6.5pt,
    # so making it bigger down there would have changed how it looks
    # without changing when it is read. The Markdown report puts its
    # banner on line 3 and this is the PDF's equivalent position --
    # above the findings table whose first column is "Classification".
    # See `DOCUMENT_POSITIONING_STATEMENT` in clinical_report_builder.py
    # for why it is that constant and not a copy of it.
    story.append(Paragraph(DOCUMENT_POSITIONING_STATEMENT, styles["PositioningBanner"]))
    story.append(Spacer(1, 4 * mm))
    # Report-header-level methodology disclosure (C0, report review
    # round 2): stated once, up front, before any classification is
    # shown, so a reviewer never has to infer which combining system
    # produced the classifications below -- see `ACMG_METHODOLOGY_
    # STATEMENT`'s own docstring comment in clinical_report_builder.py
    # for the full citation and the Tavtigian-vs-Richards divergence
    # this exists to prevent silently misreading.
    story.append(Paragraph(ACMG_METHODOLOGY_STATEMENT, styles["Footnote"]))
    story.append(Spacer(1, 3 * mm))
    story.extend(_build_clinician_summary_flowables(document, variants, sample_id, resolved_run_id, assembly, styles))
    story.extend(
        [
            _build_patient_header_table(patient, sample_id, resolved_run_id, assembly, styles),
            Spacer(1, 6 * mm),
            _Bookmark("bm_qc", "Sequencing Quality Control Metrics"),
            Paragraph("Sequencing Quality Control Metrics", styles["SectionHeading"]),
        ]
    )
    story.extend(_build_qc_flowables(parsed_qc_metrics, styles))
    story.append(Spacer(1, 6 * mm))

    story.extend(
        [
            _Bookmark("bm_provenance", "Data Source Provenance"),
            Paragraph("Data Source Provenance", styles["SectionHeading"]),
        ]
    )
    story.extend(_build_provenance_flowables(document, styles))
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
        canvasmaker=functools.partial(_NumberedCanvas, footer_text=_icmr_ai_disclosure_footer_text(patient, document)),
    )
    logger.info(f"Wrote clinical PDF report to '{output_path}' ({len(variants)} variant finding(s)).")
    return output_path
