"""
Clinician review workflow.

Minimal, filesystem-only mechanism to move a completed GEPER run from
"draft" to "reviewed", and to layer a clinician's classification
override on top of GEPER's own ACMG result -- WITHOUT inventing a
second draft/final file mechanism alongside the ICMR footer's own
draft/reviewed decision (`report/summary.py::_icmr_ai_disclosure_footer_text`).

Design constraint, still true: "draft" vs "reviewed" is NOT a
file-location choice anywhere in GEPER -- it is decided at PDF-
generation time, from the document being rendered.

What decides it CHANGED on 2026-08-21 (see `override()`'s SUPERSEDED
note for the full reasoning). It is now `geper_results.json`'s
`review_status` field -- the same source `report/export_lims.py` and
the Markdown banner already used. It was previously whether the
`patient_meta` passed to `generate_pdf`/`generate_short_pdf` had a
non-empty `physician` field, which meant an input file could assert
review that no sign-off had performed. `physician` is now attribution
only. It remains deliberately independent of `patient_name` (see
`report/summary.py::_parse_patient_meta`) so this workflow can still
sign off a de-identified/research run -- GEPER's stated default --
without ever attaching a patient name to do it.

`approve()` below therefore works by (1) writing `review_status`,
`reviewed_by` and `reviewed_at` into `geper_results.json` (and
writing/updating a `patient_meta` JSON file with `physician` populated,
for attribution), then (2) RE-INVOKING
`generate_pdf`/`generate_short_pdf` against that updated document --
exactly the same functions the orchestrator itself already calls
(`pipeline/orchestrator.py::run`), with no new rendering path, no
PDF-editing library, and no watermark-removal code.
`override()` similarly re-invokes `generate_pdf`/`generate_short_pdf`
plus `report/report_generator.py::ReportGenerator` -- the same three
render functions any GEPER run already produces its reports with.

Canonical file names, all inside the run's existing `--output-dir`
(the same directory `pipeline/orchestrator.py::run` already writes
`geper_results.json`/`geper_report.md`/`geper_report_full.pdf`/
`geper_report_short.pdf` into):

  - `geper_patient_meta.json` -- the patient_meta file `approve` writes/
    updates and `override` reads (read-only, never modified by
    `override` -- see that function's docstring). A NEW filename this
    module introduces: the orchestrator itself never persists a
    patient_meta file into `--output-dir` on its own, it only ever
    reads a caller-supplied `--patient-meta` path each run.
  - `geper_signoff_manifest.json` -- written only by a successful
    `approve`. Its presence is exactly what `list_pending` checks to
    tell REVIEWED apart from DRAFT. Removed by `withdraw` (see below)
    or automatically by `override` (a changed classification
    invalidates the prior sign-off the same way a fresh `approve` does).
  - `geper_signoff_audit.log` -- JSON-Lines append log of every
    `approve`/`override`/`withdraw` action across this `--output-dir`'s
    lifetime.

SCOPE EXTENSION (2026-08-22), recorded here rather than silently
enlarged: this module previously stated "no erasure/withdrawal
workflow" as deliberate scope -- correct when written, against the
requirements that existed then. The ratified intended-use statement
subsequently made "mandatory human review and final sign-off" the
product's central clinical control, which this module was never asked
to fully back. Two additions close part of that gap, scoped
deliberately narrow rather than solving it all at once (see
`withdraw`'s and `require_reviewed`'s own docstrings for what each does
and does NOT cover):

  - `withdraw` -- an explicit erasure of a standing sign-off (the
    module's own manifest, specifically), with its own audit-log entry.
    `override` also now removes a stale manifest automatically, closing
    the gap where `list_pending` disagreed with `review_status` after
    an override (`review-status-two-sources-of-truth`'s known B2 case).
  - `require_reviewed` -- the LIMS-export gate
    (`report/export_lims.py::_require_reviewed`) generalized into this
    module, so any FUTURE automated consumer of a signed report has one
    canonical, reusable gate to call rather than reinventing the check
    or shipping without one. Extracted now specifically because it is
    cheap now -- there is exactly one caller today; that window closes
    the moment a second one exists.

THE GATING RULE (spelled out, not left implicit, since the whole point
of extracting `require_reviewed` was to give future callers one place
to find it): any code path that hands a run's data to a CONSUMER --
something that acts on the data without a human first reading the
DRAFT/OVERRIDDEN banner in front of them -- must call
`require_reviewed()` before producing that consumer-facing artifact.
`report/export_lims.py::export_lims_json()` and
`::export_lims_csv()` are today's two examples (both delegate to
`require_reviewed` via their own `_require_reviewed` wrapper, which
re-raises as `LIMSExportBlockedError` to preserve that module's
existing public contract). A future EHR push, a second LIMS
integration, a webhook, an API endpoint that serves `geper_results.json`
directly -- anything of that shape -- is in scope for this same call,
not a new bespoke check.

This is the PRODUCTION vs DELIVERY boundary this module draws: writing
`geper_results.json`/the PDFs/the Markdown report into a run's
`--output-dir` is production -- unconditional, happens for every run,
gated on nothing (a clinician still has to open the file and read the
DRAFT banner before acting on it; see `report/summary.py::
_icmr_ai_disclosure_footer_text` and `report/report_generator.py::
_render_review_status_banner` for that human-facing control). Handing
that data to an automated CONSUMER -- a system that will act on it
without a human reading the banner first -- is delivery, and delivery
is what `require_reviewed()` gates. Writing the artifact is not the
same event as delivering it to something that will act on it
unsupervised; only the latter needs this gate.

Truthiness trap, since it has bitten adjacent code before (see
`pipeline/models/status.py`'s `calibration_status` work for the same
shape of bug): `document["review_status"]` is ALWAYS a non-empty
string -- `"draft"`, `"reviewed"`, `"overridden"`, or a reviewer's own
name in some older/malformed record -- so `if review_status:` is true
in every one of those cases, including the unreviewed ones. Every
read site, this module's own `require_reviewed()` included, must
compare by equality (`== "reviewed"`), never by truthiness.

Neither addition closes the harder, unfalsifiable half of this
module's central problem (a manifest's SHA-256 digests are still
write-once and never re-verified; `--clinician-name`/`--reg-number`
are still unvalidated free text) -- HUMAN TRUST REMAINS THE DESIGN for
everything below, CHOSEN rather than inherited: only lab personnel with
legitimate access to a run's `--output-dir` are expected to invoke this
module's commands, and the SHA-256 manifest is a tamper-EVIDENCE record
for manual/forensic audit, not an automated integrity gate. That is a
real, named limit, not an oversight: alternatives (hash re-verification
at read time, credential/identity verification behind the clinician
fields) were costed and deliberately not chosen this round -- human
trust was chosen instead, for a research-positioned product, with its
limits stated here rather than left implied. Still no
database, no SQLite, no IP-address/hash audit trail beyond the SHA-256
checksums the manifest itself records -- filesystem-only, matching the
same "deliberately small, metadata/workflow capture only" discipline
`report/summary.py::_parse_consent`'s docstring already established
for this codebase's other DPDP-adjacent feature.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, cast

from report.clinical_report_builder import candidate_interpretation_of
from report.models import compute_content_hash, verify_content_hash
from report.report_generator import ReportGenerator
from report.summary import generate_pdf
from report.summary_short import generate_short_pdf
from utils.exceptions import SignoffError
from utils.logger import get_logger

logger = get_logger(__name__)

RESULTS_FILENAME = "geper_results.json"
FULL_PDF_FILENAME = "geper_report_full.pdf"
SHORT_PDF_FILENAME = "geper_report_short.pdf"
MARKDOWN_REPORT_FILENAME = "geper_report.md"
PATIENT_META_FILENAME = "geper_patient_meta.json"
MANIFEST_FILENAME = "geper_signoff_manifest.json"
AUDIT_LOG_FILENAME = "geper_signoff_audit.log"

# Same three severities `pipeline/acmg_rules.py`'s Conflict Resolution
# Engine (Phase 6) already reports via `clinical_report["conflict_resolution"]
# ["severity"]` -- reused here verbatim, not re-derived, matching
# `report/summary.py::_variant_reviewer_flags`'s identical check.
_CONFLICT_SEVERITIES = ("Minor", "Moderate", "Major")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _results_path(output_dir: str) -> str:
    return os.path.join(output_dir, RESULTS_FILENAME)


def _patient_meta_path(output_dir: str) -> str:
    return os.path.join(output_dir, PATIENT_META_FILENAME)


def _manifest_path(output_dir: str) -> str:
    return os.path.join(output_dir, MANIFEST_FILENAME)


def _audit_log_path(output_dir: str) -> str:
    return os.path.join(output_dir, AUDIT_LOG_FILENAME)


def _require_results(output_dir: str) -> str:
    """Returns the results path, or raises `SignoffError` with a clear message -- every command needs this same precondition."""
    results_path = _results_path(output_dir)
    if not os.path.exists(results_path):
        raise SignoffError(
            f"No '{RESULTS_FILENAME}' found in '{output_dir}'. This directory must be a Bij AI "
            f"--output-dir from a completed run (python main.py --vcf ... --output-dir '{output_dir}') "
            f"before it can be reviewed."
        )
    return results_path


def require_reviewed(document: Dict[str, Any], consumer: str = "This system") -> None:
    """
    The general gate: raises `SignoffError` unless
    `document["review_status"] == "reviewed"`. Extracted (2026-08-22)
    from `report/export_lims.py::_require_reviewed`, which enforced this
    exact check for LIMS export specifically -- generalized here, into
    the module that owns the review/sign-off concept, so any FUTURE
    automated consumer of a signed report (a second LIMS integration, an
    EHR push, anything that acts on `geper_results.json` without a human
    reading the DRAFT/OVERRIDDEN banner first) has one canonical gate to
    call rather than reinventing `export_lims.py`'s check or -- worse --
    shipping without one. Extracted now specifically because it is cheap
    now: there is exactly one caller today (`export_lims.py`, which
    delegates to this function and re-raises its own
    `LIMSExportBlockedError` to preserve its existing public contract);
    that window closes the moment a second automated consumer exists.

    Phase 6 (2026-09-03): Now also performs content hash verification.
    The hash captures clinical content at approval time and must match
    at export time. Mismatch indicates ISO 15189 7.5 nonconformance:
    released report whose clinical content no longer matches approved
    state. Export refuses and audit entry records mismatch for
    investigation.

    `"overridden"` is deliberately NOT treated as good enough -- same
    reasoning as `export_lims.py`'s original check: a run a clinician has
    changed since it was last (or ever) approved must stay blocked from
    ANY automated consumption until a fresh `approve()` (or, after an
    override, a fresh `approve()` following a `withdraw()` if the stale
    manifest wasn't already auto-removed -- see `override()`'s
    docstring).

    Deliberately does NOT cover every way an unreviewed report can reach
    a person: this is a gate for AUTOMATED consumption specifically. The
    PDF/Markdown/JSON a human clinician opens directly are governed by
    the DRAFT/OVERRIDDEN banner instead -- a design choice this module's
    own docstring records as chosen (human trust), not an oversight this
    function is meant to patch over.

    `consumer`: an optional name for the calling system, folded into the
    error message so a blocked caller's error is self-explanatory (e.g.
    "LIMS export refused" rather than a bare "refused").
    """
    status = document.get("review_status")
    if status != "reviewed":
        raise SignoffError(
            f"{consumer} refused: review_status is {status!r}, not 'reviewed'. Automated "
            "consumption requires completed review and sign-off (review/signoff.py::approve()) "
            "before this run's data may reach a downstream system."
        )

    # Phase 6: Verify clinical content hash (ISO 15189 7.5 nonconformance detection)
    # Only verify if hash is present (new reports approved with Phase 6 will have it)
    stored_hash = document.get("content_hash")
    if stored_hash and not verify_content_hash(document, stored_hash):
        raise SignoffError(
            f"{consumer} refused: content hash verification failed. Report's clinical content "
            f"has changed since approval (ISO 15189 7.5 nonconformance). "
            f"Stored hash: {stored_hash!r}. "
            f"This indicates the approved report was modified after sign-off. "
            f"A fresh approval (review/signoff.py::approve()) is required before export."
        )


def _withdraw_manifest(output_dir: str, reason: str, actor: str) -> bool:
    """
    Removes `output_dir`'s `geper_signoff_manifest.json` if present, and
    appends a `"manifest_withdrawn"` audit-log entry. Returns whether a
    manifest was actually present to remove -- idempotent: calling this
    on a run with no manifest is a no-op, not an error, since `override`
    (see below) calls it unconditionally whenever a manifest exists, and
    a never-approved run simply has nothing to withdraw.

    Shared by `override` (an automatic, incidental withdrawal -- the
    classification changed, so the prior sign-off no longer covers the
    current content) and the public `withdraw` command (an explicit,
    standalone retraction with no accompanying classification change).
    `withdraw` layers its own additional `"withdrawn"` audit entry and
    `review_status` handling on top of this shared removal -- see its
    docstring.
    """
    manifest_path = _manifest_path(output_dir)
    if not os.path.exists(manifest_path):
        return False
    os.remove(manifest_path)
    _append_audit_log(
        output_dir,
        {
            "action": "manifest_withdrawn",
            "reason": reason,
            "actor": actor,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    logger.info(f"Withdrew signoff manifest for '{output_dir}': {reason}")
    return True


def _load_document(results_path: str) -> Dict[str, Any]:
    try:
        with open(results_path, "r", encoding="utf-8") as fh:
            return cast(Dict[str, Any], json.load(fh))
    except (OSError, ValueError) as exc:
        raise SignoffError(f"Could not read/parse '{results_path}': {exc}") from exc


def _write_document(results_path: str, document: Dict[str, Any]) -> None:
    with open(results_path, "w", encoding="utf-8") as fh:
        json.dump(document, fh, indent=2)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _append_audit_log(output_dir: str, entry: Dict[str, Any]) -> None:
    """Appends one JSON-Lines record. Never truncates/rewrites the existing log -- an append-only audit trail is the entire point."""
    with open(_audit_log_path(output_dir), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Variant identification
# ---------------------------------------------------------------------------


def _bare_chrom(chrom: str) -> str:
    """Strips an optional 'chr' prefix -- same normalization `pipeline/gnomad/utils.py::variant_key` already applies, reused here so a `--variant` key matches regardless of which convention the run's own VCF used."""
    return chrom[3:] if chrom.lower().startswith("chr") else chrom


def variant_id(variant_dict: Dict[str, Any]) -> str:
    """The `chrom:pos:ref>alt` key this module's CLI (`--variant`) and manifest both use -- matches the exact format shown in the CLI's own `--help` example."""
    return f"{variant_dict.get('chrom')}:{variant_dict.get('pos')}:{variant_dict.get('ref')}>{variant_dict.get('alt')}"


def parse_variant_key(key: str) -> Tuple[str, int, str, str]:
    """Parses a `--variant` value like `17:43106534:C>A` into `(chrom, pos, ref, alt)`. Raises `SignoffError` (not a bare ValueError) on a malformed key -- this always means the CLI caller's input is wrong, matching every other user-facing validation error in this module."""
    parts = key.split(":")
    if len(parts) != 3 or ">" not in parts[2]:
        raise SignoffError(f"Malformed --variant '{key}' -- expected 'chrom:pos:ref>alt', e.g. '17:43106534:C>A'.")
    chrom, pos_str, ref_alt = parts
    ref, _, alt = ref_alt.partition(">")
    try:
        pos = int(pos_str)
    except ValueError as exc:
        raise SignoffError(f"Malformed --variant '{key}' -- '{pos_str}' is not a valid position.") from exc
    if not chrom or not ref or not alt:
        raise SignoffError(f"Malformed --variant '{key}' -- expected 'chrom:pos:ref>alt', e.g. '17:43106534:C>A'.")
    return chrom, pos, ref, alt


def find_variant(document: Dict[str, Any], chrom: str, pos: int, ref: str, alt: str) -> Optional[Dict[str, Any]]:
    """Returns the matching `variant_result` dict (a live reference into `document["variants"]`, so mutating it mutates `document`), or `None` if no exact chrom:pos:ref:alt match exists. Position AND allele, never position alone -- same matching discipline `database/clinvar_client.py`/`annotation/indigenomes.py` already use."""
    target_chrom = _bare_chrom(chrom).upper()
    target_ref = ref.upper()
    target_alt = alt.upper()
    for variant_result in document.get("variants", []):
        variant = variant_result.get("variant") or {}
        if (
            _bare_chrom(str(variant.get("chrom") or "")).upper() == target_chrom
            and variant.get("pos") == pos
            and str(variant.get("ref") or "").upper() == target_ref
            and str(variant.get("alt") or "").upper() == target_alt
        ):
            return cast(Dict[str, Any], variant_result)
    return None


# ---------------------------------------------------------------------------
# Effective classification / conflict summary (shared by approve's
# manifest and list_pending's table)
# ---------------------------------------------------------------------------


def effective_classification(variant_result: Dict[str, Any]) -> Optional[str]:
    """
    GEPER's own ACMG classification, UNLESS a clinician override (see
    `override()` below) exists for this variant, in which case the
    override's `new_classification` is "effective" -- what a clinician
    actually signed off on. The original GEPER classification is never
    lost either way: it remains in `clinical_report["acmg_classification"]
    ["classification"]` (and, when overridden, also in the override
    record's own `original_classification`) for anyone reading the raw
    JSON or the rendered reports, which show both explicitly (see
    `report/report_generator.py`/`report/summary.py`/
    `report/summary_short.py`'s "Clinician override" additions).

    Reads via `candidate_interpretation_of` (2026-08-22, A-5 rename),
    not `.get("clinical_report")` directly -- see that function's own
    docstring for why: this module reloads `geper_results.json` from
    disk, so the `candidate_interpretation`/`clinical_report` alias pair
    are independent copies here, not the same object `json_builder.py`
    wrote. Preferring the new key with a fallback to the old is what
    keeps this correct across the deprecation window regardless of
    which key a given writer last touched.
    """
    clinical = candidate_interpretation_of(variant_result) or {}
    acmg = clinical.get("acmg_classification") or {}
    override = acmg.get("clinician_override")
    if override and override.get("new_classification"):
        return cast(str, override["new_classification"])
    return cast(Optional[str], acmg.get("classification"))


def _confidence_label(variant_result: Dict[str, Any]) -> str:
    confidence = (candidate_interpretation_of(variant_result) or {}).get("confidence") or {}
    if confidence.get("pending", True):
        return "Pending"
    return confidence.get("label") or "n/a"


def has_conflicting_evidence(variant_result: Dict[str, Any]) -> bool:
    """Same field, same three severities `report/summary.py::_variant_reviewer_flags` already checks -- reused, not re-derived."""
    severity = ((candidate_interpretation_of(variant_result) or {}).get("conflict_resolution") or {}).get("severity")
    return severity in _CONFLICT_SEVERITIES


def _variant_manifest_entry(variant_result: Dict[str, Any]) -> Dict[str, Any]:
    variant = variant_result.get("variant") or {}
    gene = (variant_result.get("interpretation_result") or {}).get("gene_symbol")
    return {
        "variant_id": variant_id(variant),
        "gene": gene,
        "final_classification": effective_classification(variant_result),
        "confidence": _confidence_label(variant_result),
    }


# ---------------------------------------------------------------------------
# patient_meta merge helper (approve reads/preserves whatever is
# already there -- dob/gender/consent/etc -- and only ever sets/updates
# "physician")
# ---------------------------------------------------------------------------


def _load_existing_patient_meta_raw(path: str) -> Dict[str, Any]:
    """
    Raw (un-normalized) load of an existing `geper_patient_meta.json`,
    for `approve` to merge into rather than clobber. Deliberately NOT
    `report/summary.py::_parse_patient_meta` -- that function normalizes
    away unknown keys and fills in defaults, which would silently drop
    anything a lab already stored there (e.g. a real `patient_name`
    they attached separately, or extra documentation comments like
    `patient_metadata.example.json`'s own `_comment` keys). Never
    raises: a missing or corrupt file just means "start from an empty
    object", matching every other patient_meta failure mode in this
    codebase's "never crash over optional metadata" convention.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning(f"Could not read existing '{path}' ({exc}); starting from an empty patient_meta file instead.")
        return {}


# ---------------------------------------------------------------------------
# Command 1: approve
# ---------------------------------------------------------------------------


def approve(output_dir: str, clinician_name: str, reg_number: str, hospital: str) -> Dict[str, Any]:
    """
    Moves the run in `output_dir` from DRAFT to REVIEWED: sets
    `review_status`/`reviewed_by`/`reviewed_at` on `geper_results.json`
    (see the round 30 part 2 note below), writes/updates
    `geper_patient_meta.json` with `physician` populated for attribution
    (folding name/registration-number/hospital into that one existing
    field, e.g. "Dr. A. Sharma, MD, Reg. No. 12345, ABC Diagnostics"),
    then re-invokes `generate_pdf`/`generate_short_pdf` against the
    UPDATED document -- which, reading `review_status`, renders
    "reviewed by {reviewed_by}" on every page instead of "DRAFT".
    Note the ordering matters and is load-bearing: the document is
    mutated in memory before it is handed to the PDF renderers, so they
    see `"reviewed"` rather than the `"draft"` still on disk when this
    function started. `reviewed_by` is written from the same string
    folded into `physician`, so the rendered sentence is unchanged from
    when the footer read `physician` directly -- it is simply sourced
    from the sign-off record now rather than from an input file.

    Round 30 part 2 (root-cause fix): also rewrites `geper_results.json`
    itself with `review_status: "reviewed"` plus `reviewed_by`/
    `reviewed_at`. Before this, `approve()` only ever touched the
    sidecar `geper_patient_meta.json` and regenerated the two PDFs --
    `geper_results.json` (GEPER's primary MACHINE-readable output,
    consumed directly by `report/export_lims.py` and anything else that
    reads it without ever opening a PDF) stayed byte-identical to its
    pre-review state forever. `review_status` is what
    `report/export_lims.py::_require_reviewed` gates on and what
    `report/report_generator.py`'s Markdown banner renders -- neither
    existed before this JSON write, so neither could have reflected
    review status either.

    Always sets `review_status` to `"reviewed"` regardless of the
    document's prior value (including `"overridden"` -- see
    `override()`'s docstring for why an override needs its own,
    subsequent `approve()` to become export-eligible again). This is
    the intended "sign off on whatever the record currently says"
    re-review workflow, not a bug: a clinician calling `approve()`
    after an `override()` is attesting to the CURRENT (possibly
    overridden) content, exactly like re-approving after any other
    change to `geper_results.json`.

    Returns the manifest dict that was also written to
    `geper_signoff_manifest.json`. Raises `SignoffError` if `output_dir`
    has no `geper_results.json` (nothing to approve).
    """
    results_path = _require_results(output_dir)
    document = _load_document(results_path)

    physician = f"{clinician_name}, Reg. No. {reg_number}, {hospital}"
    patient_meta_path = _patient_meta_path(output_dir)
    patient_meta_raw = _load_existing_patient_meta_raw(patient_meta_path)
    patient_meta_raw["physician"] = physician
    with open(patient_meta_path, "w", encoding="utf-8") as fh:
        json.dump(patient_meta_raw, fh, indent=2)

    approved_at = datetime.now(timezone.utc).isoformat()
    document["review_status"] = "reviewed"
    document["reviewed_by"] = physician
    document["reviewed_at"] = approved_at
    # Phase 6: Compute and store content hash for later verification
    document["content_hash"] = compute_content_hash(document)
    _write_document(results_path, document)

    pdf_path = os.path.join(output_dir, FULL_PDF_FILENAME)
    short_pdf_path = os.path.join(output_dir, SHORT_PDF_FILENAME)
    # QC needs no threading here any more: generate_pdf reads it from the
    # document itself (report/summary.py::_document_qc_metrics). ae0d9f7
    # fixed the signed-PDF-claims-no-QC defect by passing document QC
    # through explicitly; that hand-off is now the renderer's own job, so
    # no future re-render path can forget it.
    generate_pdf(document, pdf_path, patient_meta=patient_meta_path)
    generate_short_pdf(document, short_pdf_path, patient_meta=patient_meta_path, companion_filename=FULL_PDF_FILENAME)

    manifest = {
        "pdf_sha256": _sha256_file(pdf_path),
        "short_pdf_sha256": _sha256_file(short_pdf_path),
        "results_json_sha256": _sha256_file(results_path),
        "content_hash": document.get("content_hash"),  # Phase 6: Clinical content hash
        "clinician_name": clinician_name,
        "reg_number": reg_number,
        "hospital": hospital,
        "approved_at": approved_at,
        "variants": [_variant_manifest_entry(vr) for vr in document.get("variants", [])],
    }
    with open(_manifest_path(output_dir), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    _append_audit_log(
        output_dir,
        {
            "action": "approved",
            "output_dir": os.path.abspath(output_dir),
            "clinician": clinician_name,
            "timestamp": approved_at,
        },
    )

    logger.info(
        f"Approved run in '{output_dir}': regenerated '{pdf_path}' and '{short_pdf_path}', "
        f"wrote '{_manifest_path(output_dir)}'."
    )
    return manifest


# ---------------------------------------------------------------------------
# Command 2: override
# ---------------------------------------------------------------------------


def override(
    output_dir: str,
    variant_key: str,
    new_classification: str,
    reason: str,
    clinician_id: str,
) -> Dict[str, Any]:
    """
    Layers a clinician's classification override on top of the matching
    variant's existing `clinical_report`, in `geper_results.json` --
    never replacing GEPER's own `acmg_classification.classification`
    (see `effective_classification`'s docstring for how the two co-
    exist). Appends the override record to that variant's own
    `"overrides"` list (full history, most recent last) AND sets
    `acmg_classification["clinician_override"]` to that same record (a
    single-object convenience pointer the three report renderers read
    to show "GEPER classification: X; Clinician override: Y" without
    each having to walk the list themselves).

    Regenerates all three existing report formats
    (`geper_report_full.pdf`, `geper_report_short.pdf`,
    `geper_report.md`) from the updated document, via the same
    `generate_pdf`/`generate_short_pdf`/`ReportGenerator` functions
    every GEPER run already uses -- no new rendering path.

    Deliberately does NOT write or modify `geper_patient_meta.json`.
    That file no longer carries any review state (see the SUPERSEDED
    note below), so there is nothing for an override to say in it --
    `review_status`, which this function does set, is the whole answer.

    SUPERSEDED (2026-08-21) -- recorded rather than deleted, because
    the decision below was correct when it was made and the next reader
    needs to know it expired rather than that someone ignored it.
    Round 30 part 2 originally left the PDF footer alone, reasoning:
    "the footer text function
    (`report/summary.py::_icmr_ai_disclosure_footer_text`) was
    deliberately left untouched, since 'sign-off legal weight stays
    PDF-only' was this round's explicit decision -- extending it to
    read `review_status` would make it a second, PDF-side place that
    decides what counts as reviewed."
    That reasoning held only while `patient_meta`'s `physician` field
    was the single source of truth for review state. Round 30 part 2
    itself ended that, by introducing `review_status` as the gate for
    `report/export_lims.py::_require_reviewed` and for the Markdown
    banner: from that commit onward there were already TWO sources that
    could disagree, and the PDF's was the weaker one -- a free-text
    input field supplied via `--patient-meta`, which meant any caller
    could suppress the DRAFT warning and make the clinician-facing
    artefact assert "has been reviewed by X" with no sign-off ever
    performed. Reading `review_status` in the PDFs therefore collapses
    two sources into one rather than adding a third, which is the
    opposite of what the original concern feared.
    Both PDF renderers now derive review state from `review_status`
    alone. `physician` remains purely attribution ("Referring
    Physician" in the identity block) and can no longer create review
    state -- it is still the right thing to name, just not the right
    thing to trust.

    Round 30 part 2: ALWAYS sets `geper_results.json`'s `review_status`
    to `"overridden"`, regardless of the document's prior value --
    including when the run was already `"reviewed"` (a prior
    `approve()` call). This is what makes `report/export_lims.py`'s
    hard gate correctly re-block export after an override: a clinician
    changed the classification a LIMS would otherwise ship, so the
    prior sign-off no longer covers the CURRENT content and a fresh
    `approve()` is required before this run is export-eligible again.
    `reviewed_by`/`reviewed_at` are deliberately left untouched (not
    cleared) -- they still answer "who approved a PREVIOUS state of
    this document, and when", which remains true and audit-relevant
    even though it no longer implies "this document is currently
    approved" (`review_status` alone is what gates that).

    RESOLVED (2026-08-21). This docstring previously recorded a "KNOWN,
    DELIBERATELY UNRESOLVED TENSION": calling `override()` on an
    already-`approve()`d run produced PDFs still reading "reviewed by
    {physician}" while `review_status` and the Markdown banner said
    `"overridden"`, so a reader comparing all three surfaces saw
    PDF="reviewed", JSON/Markdown="overridden". That is fixed. Making
    the PDF footer override-aware -- named there as what closing it
    would require -- is exactly what happened: both PDFs now render a
    distinct overridden footer
    (`report/summary.py::_ICMR_OVERRIDDEN_FOOTER_TEXT`), so all three
    surfaces agree that an overridden run is not ready for use and that
    a fresh `approve()` is required.
    An overridden run's footer deliberately does NOT collapse into
    DRAFT: an override is a real, recorded clinical action, and saying
    only "DRAFT" would discard the fact that a human changed a
    classification. It also does not name `reviewed_by` -- preserved
    though that field is (see above) -- because a clinician's name
    beside review wording, repeated on every page, is the same
    at-a-glance misread that motivated this change. The override's
    author, reason and timestamp are already rendered per finding in
    the report body.

    Raises `SignoffError` if `output_dir` has no `geper_results.json`,
    `variant_key` doesn't parse, no variant matches it, or the matched
    variant has no `clinical_report` (interpretation failed for it, so
    there is no classification to override).
    """
    results_path = _require_results(output_dir)
    chrom, pos, ref, alt = parse_variant_key(variant_key)
    document = _load_document(results_path)

    variant_result = find_variant(document, chrom, pos, ref, alt)
    if variant_result is None:
        raise SignoffError(f"No variant matching '{variant_key}' found in '{results_path}'.")

    clinical_report = candidate_interpretation_of(variant_result)
    if not clinical_report:
        raise SignoffError(
            f"Variant '{variant_key}' has no clinical_report (interpretation did not complete for it) -- "
            f"nothing to override."
        )

    acmg = clinical_report.setdefault("acmg_classification", {})
    original_classification = acmg.get("classification") or "Not classified"
    timestamp = datetime.now(timezone.utc).isoformat()
    record = {
        "variant_id": variant_id(variant_result.get("variant") or {}),
        "original_classification": original_classification,
        "new_classification": new_classification,
        "reason": reason,
        "clinician_id": clinician_id,
        "timestamp": timestamp,
    }
    variant_result.setdefault("overrides", []).append(record)
    acmg["clinician_override"] = record
    document["review_status"] = "overridden"
    # Re-point BOTH alias keys at the same mutated object before writing
    # (candidate_interpretation_of's own docstring calls this out by
    # name): after _load_document's JSON round-trip, "candidate_
    # interpretation" and "clinical_report" are two independent dict
    # copies, not the shared object json_builder.py wrote. Mutating only
    # the one candidate_interpretation_of happened to return would leave
    # the other key stale on disk -- silently, since every reader uses
    # `.get(...) or {}`. Writing the same object back under both keys is
    # what keeps them in agreement regardless of which key a downstream
    # reader (including an external LIMS still on the deprecated alias)
    # ends up using.
    variant_result["candidate_interpretation"] = clinical_report
    variant_result["clinical_report"] = clinical_report

    # Override always invalidates any standing sign-off manifest, the
    # same way a changed review_status does -- closes the gap where
    # list_pending() (which checks manifest PRESENCE, not review_status)
    # kept reporting REVIEWED after an override left review_status
    # "overridden" underneath it. See withdraw()'s docstring for the
    # explicit, standalone form of this same action.
    if os.path.exists(_manifest_path(output_dir)):
        _withdraw_manifest(output_dir, reason=f"override of {record['variant_id']}", actor=clinician_id)

    _write_document(results_path, document)

    ReportGenerator().write(document, os.path.join(output_dir, MARKDOWN_REPORT_FILENAME))

    patient_meta_path = _patient_meta_path(output_dir)
    patient_meta_arg = patient_meta_path if os.path.exists(patient_meta_path) else None
    pdf_path = os.path.join(output_dir, FULL_PDF_FILENAME)
    short_pdf_path = os.path.join(output_dir, SHORT_PDF_FILENAME)
    # QC needs no threading here any more -- see approve()'s note.
    generate_pdf(document, pdf_path, patient_meta=patient_meta_arg)
    generate_short_pdf(document, short_pdf_path, patient_meta=patient_meta_arg, companion_filename=FULL_PDF_FILENAME)

    _append_audit_log(
        output_dir,
        {
            "action": "override",
            "variant": record["variant_id"],
            "original": original_classification,
            "new": new_classification,
            "reason": reason,
            "clinician_id": clinician_id,
            "timestamp": timestamp,
        },
    )

    logger.info(f"Applied clinician override to {record['variant_id']} in '{output_dir}'.")
    return record


# ---------------------------------------------------------------------------
# Command 3: list-pending
# ---------------------------------------------------------------------------


def list_pending(search_root: str, show_all: bool = False) -> List[Dict[str, Any]]:
    """
    Recursively finds every `geper_results.json` under `search_root`
    and reports its review status -- REVIEWED if a
    `geper_signoff_manifest.json` sits alongside it (written only by a
    successful `approve()`), DRAFT otherwise. By default returns only
    DRAFT runs (the ones actually pending review, matching the command
    name); `show_all=True` returns every run found, either status.

    Each entry: `{output_dir, report_date, num_variants,
    has_conflicting_evidence, status}`. `has_conflicting_evidence` is
    True when ANY variant in the run has a Minor/Moderate/Major
    conflict severity already computed by the Conflict Resolution
    Engine (Phase 6) -- read from the existing data, never
    recalculated. A `geper_results.json` that fails to parse is
    skipped with a warning, not a fatal error -- one corrupt run must
    never hide every other run under the same search root.
    """
    results: List[Dict[str, Any]] = []
    for dirpath, _dirnames, filenames in os.walk(search_root):
        if RESULTS_FILENAME not in filenames:
            continue
        results_path = os.path.join(dirpath, RESULTS_FILENAME)
        try:
            document = _load_document(results_path)
        except SignoffError as exc:
            logger.warning(f"Skipping unreadable '{results_path}': {exc}")
            continue

        status = "REVIEWED" if os.path.exists(os.path.join(dirpath, MANIFEST_FILENAME)) else "DRAFT"
        if status == "REVIEWED" and not show_all:
            continue

        variants = document.get("variants", [])
        results.append(
            {
                "output_dir": dirpath,
                "report_date": document.get("generated_at"),
                "num_variants": document.get("variant_count", len(variants)),
                "has_conflicting_evidence": any(has_conflicting_evidence(vr) for vr in variants),
                "status": status,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Command 4: withdraw
# ---------------------------------------------------------------------------


def withdraw(output_dir: str, reason: str, actor: str) -> Dict[str, Any]:
    """
    Explicitly retracts a standing sign-off: removes
    `geper_signoff_manifest.json` (see `_withdraw_manifest`) and, if
    `review_status` is currently `"reviewed"`, resets it to `"draft"` --
    the same fail-safe default every other "not currently reviewed"
    state in this codebase already collapses to (see
    `report/summary.py::_icmr_ai_disclosure_footer_text`'s docstring).

    Does NOT reset an `"overridden"` status back to `"draft"`: that
    value already means "not reviewed" and additionally records that a
    clinician changed a classification, which withdrawing the manifest
    alone does not undo -- collapsing it to `"draft"` would discard
    that fact the same way the PDF footer deliberately never collapses
    `"overridden"` into `"draft"`. (This case is largely academic in
    practice: `override()` already withdraws its own stale manifest
    automatically, so by the time a human calls `withdraw()` on an
    `"overridden"` run, there is usually no manifest left to withdraw
    and this function will have already raised for that reason.)

    This is the module's own scope extension (2026-08-22, see the
    module docstring's "SCOPE EXTENSION" note): "no erasure/withdrawal
    workflow" was correct, deliberate scope when this module was
    written; the ratified intended-use statement's mandatory-sign-off
    claim made that insufficient, not wrong.

    Regenerates all three existing report formats from the updated
    document, same as `approve()`/`override()` -- a withdrawn sign-off
    must not leave an on-disk PDF/Markdown still claiming "reviewed by
    X" once the manifest that attested to it is gone; leaving a stale
    render would be exactly the "two sources of truth disagree" defect
    shape this whole change closes.

    Raises `SignoffError` if `output_dir` has no `geper_results.json`,
    or no manifest currently exists (nothing to withdraw -- this run
    was never approved, or a prior withdrawal already removed it).
    """
    results_path = _require_results(output_dir)
    manifest_path = _manifest_path(output_dir)
    if not os.path.exists(manifest_path):
        raise SignoffError(
            f"No '{MANIFEST_FILENAME}' found in '{output_dir}' -- nothing to withdraw (this run has "
            f"never been through approve(), or a prior withdrawal already removed it)."
        )

    document = _load_document(results_path)
    previous_status = document.get("review_status")
    if previous_status == "reviewed":
        document["review_status"] = "draft"
        _write_document(results_path, document)

    _withdraw_manifest(output_dir, reason=reason, actor=actor)

    patient_meta_path = _patient_meta_path(output_dir)
    patient_meta_arg = patient_meta_path if os.path.exists(patient_meta_path) else None
    pdf_path = os.path.join(output_dir, FULL_PDF_FILENAME)
    short_pdf_path = os.path.join(output_dir, SHORT_PDF_FILENAME)
    generate_pdf(document, pdf_path, patient_meta=patient_meta_arg)
    generate_short_pdf(document, short_pdf_path, patient_meta=patient_meta_arg, companion_filename=FULL_PDF_FILENAME)
    ReportGenerator().write(document, os.path.join(output_dir, MARKDOWN_REPORT_FILENAME))

    timestamp = datetime.now(timezone.utc).isoformat()
    _append_audit_log(
        output_dir,
        {
            "action": "withdrawn",
            "previous_review_status": previous_status,
            "reason": reason,
            "actor": actor,
            "timestamp": timestamp,
        },
    )
    logger.info(f"Withdrew sign-off for '{output_dir}' (was {previous_status!r}).")
    return {
        "output_dir": os.path.abspath(output_dir),
        "previous_review_status": previous_status,
        "reason": reason,
        "actor": actor,
        "timestamp": timestamp,
    }
