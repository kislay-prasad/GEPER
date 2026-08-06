"""
Clinician review workflow.

Minimal, filesystem-only mechanism to move a completed GEPER run from
"draft" to "reviewed", and to layer a clinician's classification
override on top of GEPER's own ACMG result -- WITHOUT inventing a
second draft/final file mechanism alongside the ICMR footer's own
draft/reviewed decision (`report/summary.py::_icmr_ai_disclosure_footer_text`).

Confirmed design constraint, established by investigating that
deliverable before writing any of this: "draft" vs "reviewed" is NOT a
file-location choice anywhere in GEPER -- it is decided at PDF-
generation time, purely from whether the `patient_meta` passed to
`generate_pdf`/`generate_short_pdf` has a non-empty `physician` field
(see `report/summary.py::_parse_patient_meta`). `physician` is
deliberately independent of `patient_name` (see that function's
docstring) precisely so this workflow can sign off a de-identified/
research run -- GEPER's stated default -- without ever attaching a
patient name to do it.

`approve()` below therefore works by (1) writing/updating a
`patient_meta` JSON file with `physician` populated, then (2) RE-
INVOKING `generate_pdf`/`generate_short_pdf` against the existing
`geper_results.json` -- exactly the same functions the orchestrator
itself already calls (`pipeline/orchestrator.py::run`), with no new
rendering path, no PDF-editing library, and no watermark-removal code.
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
    tell REVIEWED apart from DRAFT.
  - `geper_signoff_audit.log` -- JSON-Lines append log of every
    `approve`/`override` action across this `--output-dir`'s lifetime.

No database, no SQLite, no IP-address/hash audit trail beyond the
SHA-256 checksums the manifest itself records, no erasure/withdrawal
workflow -- filesystem-only, matching the same "deliberately small,
metadata/workflow capture only" discipline
`report/summary.py::_parse_consent`'s docstring already established
for this codebase's other DPDP-adjacent feature.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, cast

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
            f"No '{RESULTS_FILENAME}' found in '{output_dir}'. This directory must be a GEPER "
            f"--output-dir from a completed run (python main.py --vcf ... --output-dir '{output_dir}') "
            f"before it can be reviewed."
        )
    return results_path


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
    """
    clinical = variant_result.get("clinical_report") or {}
    acmg = clinical.get("acmg_classification") or {}
    override = acmg.get("clinician_override")
    if override and override.get("new_classification"):
        return cast(str, override["new_classification"])
    return cast(Optional[str], acmg.get("classification"))


def _confidence_label(variant_result: Dict[str, Any]) -> str:
    confidence = ((variant_result.get("clinical_report") or {}).get("confidence")) or {}
    if confidence.get("pending", True):
        return "Pending"
    return confidence.get("label") or "n/a"


def has_conflicting_evidence(variant_result: Dict[str, Any]) -> bool:
    """Same field, same three severities `report/summary.py::_variant_reviewer_flags` already checks -- reused, not re-derived."""
    severity = ((variant_result.get("clinical_report") or {}).get("conflict_resolution") or {}).get("severity")
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
    Moves the run in `output_dir` from DRAFT to REVIEWED: writes/updates
    `geper_patient_meta.json` with `physician` populated (folding
    name/registration-number/hospital into that one existing field,
    the same convention `report/summary.py
    ::_icmr_ai_disclosure_footer_text`'s own docstring already
    documents, e.g. "Dr. A. Sharma, MD, Reg. No. 12345, ABC
    Diagnostics"), then re-invokes `generate_pdf`/`generate_short_pdf`
    against the existing `geper_results.json` -- which, per the
    already-confirmed footer mechanism, naturally renders "reviewed by
    {physician}" on every page instead of "DRAFT" (see
    `report/summary.py::_parse_patient_meta`'s docstring for why
    `physician` alone -- no `patient_name` required -- is sufficient).

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

    pdf_path = os.path.join(output_dir, FULL_PDF_FILENAME)
    short_pdf_path = os.path.join(output_dir, SHORT_PDF_FILENAME)
    generate_pdf(document, pdf_path, patient_meta=patient_meta_path)
    generate_short_pdf(document, short_pdf_path, patient_meta=patient_meta_path, companion_filename=FULL_PDF_FILENAME)

    approved_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "pdf_sha256": _sha256_file(pdf_path),
        "short_pdf_sha256": _sha256_file(short_pdf_path),
        "results_json_sha256": _sha256_file(results_path),
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

    Deliberately does NOT write or modify `geper_patient_meta.json`:
    whatever draft/reviewed state that file already implies (present
    with `physician` set = reviewed; absent or `physician`-less =
    draft) is read as-is and carries through unchanged into the
    regenerated reports -- an override on a not-yet-approved run stays
    DRAFT until a separate `approve()` call, exactly matching the
    footer mechanism's own single source of truth.

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

    clinical_report = variant_result.get("clinical_report")
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

    _write_document(results_path, document)

    ReportGenerator().write(document, os.path.join(output_dir, MARKDOWN_REPORT_FILENAME))

    patient_meta_path = _patient_meta_path(output_dir)
    patient_meta_arg = patient_meta_path if os.path.exists(patient_meta_path) else None
    pdf_path = os.path.join(output_dir, FULL_PDF_FILENAME)
    short_pdf_path = os.path.join(output_dir, SHORT_PDF_FILENAME)
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
