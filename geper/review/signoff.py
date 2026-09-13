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

LINKED RUNS (2026-09-13, w117) -- the one place this module is no longer
filesystem-only. A run that `api/submission_worker.py` recorded as a
clinical interpretation carries a `clinical_link.json` in its
`--output-dir`. `approve` and `override` now read it, and on such a run
ALSO write to the clinical record: a `sole_signatory` claim, submission
and approval by the one authenticated person who signed (`approve`), or
a `disagree` claim (`override` before approval; an override AFTER
approval is refused and pointed at the amendment path). A run with no
`clinical_link.json` is untouched by all of it -- no credentials, no
database, no import of the clinical package. See the "clinical link"
section below for the ruling, the claim-kind reasoning and why the
clinical writes come before the file writes.

`withdraw` joined that set in w119 (2026-09-13) and is the one member of
it whose clinical side is a REFUSAL rather than a write: the clinical
layer has no operation that retracts an approval, and its schema forbids
walking an approved report's state back, so a withdrawal on a run whose
clinical report is already `approved`/`released` is refused before any
file changes instead of leaving the record saying `approved` behind a
retracted sign-off. See `withdraw`'s docstring for the evidence and for
the linked-but-not-yet-approved case, which withdraws normally.

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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple, cast

from component_identity import SHORT_NAME
from pipeline.provenance import HashVerification, VersionStatus
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

# Written by api/submission_worker.py once a run has been recorded as a
# clinical interpretation: which organisation, order, sample, interpretation,
# report and VCF this output directory's files became. Its PRESENCE is what
# makes a run "linked" for everything below. Named here as a literal rather
# than imported from api/submission_worker.py deliberately: this module is
# filesystem-only and must not acquire an import edge onto the API worker
# (which pulls in the submission store, psycopg and the whole clinical
# package) just to learn one filename. The two constants are asserted equal
# by api/tests/test_signoff_clinical_link.py, so a rename cannot drift.
CLINICAL_LINK_FILENAME = "clinical_link.json"

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
            f"No '{RESULTS_FILENAME}' found in '{output_dir}'. This directory must be a {SHORT_NAME} "
            f"--output-dir from a completed run (python main.py --vcf ... --output-dir '{output_dir}') "
            f"before it can be reviewed."
        )
    return results_path


def _check_no_model_hash_mismatch(document: Dict[str, Any]) -> None:
    """
    Refuses `approve()` before it writes anything if this run recorded a
    model-weight hash MISMATCH for any model -- human-ruled 2026-09-10:
    "a signer approving a report without visibility into a mismatch is
    signing something they cannot vouch for; the signature becomes
    decorative." `hash_verification: MISMATCH` is written into
    `geper_results.json` at run time
    (`pipeline/orchestrator.py::run`, opt-in via
    `CONFIG.VERIFY_MODEL_ARTIFACT_HASHES`/`GEPER_VERIFY_MODEL_HASHES`) but
    nothing downstream reads it -- not `approve()` before this change, not
    the rendered Markdown (`report/report_generator.py::_render_provenance`)
    or PDF (`report/summary.py`), both of which show only
    identifier/status/reason from each `model_checkpoints` entry. That is
    not missing information, it is suppression, however unintentional: the
    fact is already on disk and the signer is the one party not told. This
    function is the fix -- called from `approve()` immediately after the
    document loads and before any write (patient_meta, the JSON rewrite,
    PDF regeneration, the manifest), so a refusal here leaves nothing on
    disk changed.

    NO BYPASS OF ANY KIND EXISTS FOR THIS CHECK, DELIBERATELY: no flag, no
    env var, no "force". A mismatch means the cached weights this run used
    do not match what the cache claims to hold -- corrupted or substituted
    -- and the only correct response is fixing the cache and re-running,
    never signing off on unknown weights. Consistent with
    `require_reviewed()`'s own content-hash check just below, which has
    never had an override either.

    WHAT THE MESSAGE NAMES: the model, the EXPECTED sha256 the cache
    declared (`cache_declared_sha256`), and -- as of this change -- the
    ACTUAL observed hash (`observed_sha256`), now persisted by
    `pipeline/provenance.py::verify_model_artifact` via
    `WeightCache.verify_checksum_detailed`, which returns the bytes-on-disk
    hash it computed rather than discarding it after only logging it. A
    record written before this change (or one where `hash_verification`
    reached MISMATCH some other way this function does not anticipate) may
    still lack `observed_sha256`; that case is named explicitly below
    rather than silently printing `None`.
    """
    checkpoints = document.get("model_checkpoints") or {}
    mismatches: List[Dict[str, Any]] = []
    for name, value in checkpoints.items():
        if not isinstance(value, dict):
            continue
        artifact = value.get("loaded_artifact")
        if not isinstance(artifact, dict):
            continue
        if artifact.get("hash_verification") == HashVerification.MISMATCH.value:
            mismatches.append(
                {
                    "name": name,
                    "expected_sha256": artifact.get("cache_declared_sha256"),
                    "observed_sha256": artifact.get("observed_sha256"),
                    "resolved_path": artifact.get("resolved_path"),
                }
            )
    if not mismatches:
        return

    lines = [
        "Approval refused: model-weight hash MISMATCH recorded for this run -- signing off would "
        "attest to findings produced by weights that do not match what the model cache declares.",
    ]
    for m in mismatches:
        if m["observed_sha256"]:
            observed_clause = f"actual observed sha256 = {m['observed_sha256']!r}"
        else:
            # Named, not silently printed as None -- a record written before
            # `observed_sha256` existed, or one that reached MISMATCH by a
            # path this function does not anticipate.
            observed_clause = "actual observed hash is not present in this record"
        lines.append(
            f"  - {m['name']}: expected sha256 (declared by the cache) = {m['expected_sha256']!r}; "
            f"{observed_clause} (cache file: {m['resolved_path']!r})."
        )
    lines.append(
        "This means the cached weights are corrupted or were substituted after this run recorded "
        "them -- re-verify or re-download the model cache for the model(s) named above and re-run "
        "this sample, then approve the fresh run. There is no override for this check."
    )
    raise SignoffError("\n".join(lines))


def require_reviewed(document: Dict[str, Any], consumer: str = "This system", output_dir: Optional[str] = None) -> None:
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

    When `output_dir` is provided, audit entries distinguish verification paths:
    - 'content_verified': hash present and matches (new Phase 6 reports)
    - 'content_not_verified_no_hash_predates_phase_6': hash absent (pre-Phase 6 reports)
    Both paths allow export (backward compatibility), but audit trail is clear.

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
    `output_dir`: optional directory for audit log. If provided, verification
    path is recorded in audit trail for compliance review.
    """
    status = document.get("review_status")
    if status != "reviewed":
        raise SignoffError(
            f"{consumer} refused: review_status is {status!r}, not 'reviewed'. Automated "
            "consumption requires completed review and sign-off (review/signoff.py::approve()) "
            "before this run's data may reach a downstream system."
        )

    # Phase 6: Verify clinical content hash (ISO 15189 7.5 nonconformance detection)
    # Distinguish between verified (hash present + matches) and not-verified (hash absent, predates Phase 6)
    stored_hash = document.get("content_hash")
    if stored_hash:
        # Hash present: verify it matches
        if not verify_content_hash(document, stored_hash):
            raise SignoffError(
                f"{consumer} refused: content hash verification failed. Report's clinical content "
                f"has changed since approval (ISO 15189 7.5 nonconformance). "
                f"Stored hash: {stored_hash!r}. "
                f"This indicates the approved report was modified after sign-off. "
                f"A fresh approval (review/signoff.py::approve()) is required before export."
            )
        # Hash verified - write audit entry if output_dir provided
        if output_dir:
            _append_audit_log(
                output_dir,
                {
                    "action": "content_verified",
                    "consumer": consumer,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
    else:
        # Hash absent (pre-Phase 6 report) - allow export but audit distinction for compliance
        if output_dir:
            _append_audit_log(
                output_dir,
                {
                    "action": "content_not_verified_no_hash_predates_phase_6",
                    "consumer": consumer,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
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
# The signing clinician's identity (R10)
# ---------------------------------------------------------------------------

_IDENTITY_FIELDS = ("full_name", "registration_number", "hospital")


@dataclass(frozen=True)
class ClinicianIdentity:
    """
    The three values a signed report shows -- name, registration number,
    hospital -- AS HELD BY THE USER RECORD OF THE ACCOUNT THAT SIGNED, rather
    than as typed at sign-off time.

    R10, human ruling 2026-09-13. Until this existed, `approve()` took all
    three as free text and printed whatever it was given: the report could
    name a clinician who never signed it, or a registration number belonging
    to nobody, and no record anywhere could contradict the printed document.
    That is the gap this class closes, on the rendering side; the storage side
    is `clinical/schema.sql`'s users.full_name/registration_number/hospital
    and `clinical/data_access.py::get_signing_identity`.

    DELIBERATELY NOT AN IMPORT FROM `clinical`. This package is the
    filesystem-only pipeline and renders reports in deployments that have no
    clinical database at all (see this module's own "filesystem-only"
    docstring); importing the data-access layer here would make the renderer
    depend on Postgres to print a PDF. `from_user_record` takes the plain dict
    `get_signing_identity` returns instead, so the two halves meet at a data
    shape rather than at an import.

    `user_id` is carried purely so the manifest can record WHICH account the
    identity came from -- the audit question "who signed" wants an account,
    not just a printed name. It is optional because the typed-in path (below)
    has no account behind it, and saying so is more honest than inventing one.
    """

    full_name: str
    registration_number: str
    hospital: str
    user_id: Optional[str] = None

    @classmethod
    def from_user_record(cls, record: Mapping[str, Any]) -> "ClinicianIdentity":
        """
        Build an identity from `clinical/data_access.py::get_signing_identity`'s
        dict, REFUSING anything incomplete and naming every field that is
        missing.

        The check is repeated here rather than trusted from the database side
        on purpose: this is the last point before the values reach a rendered
        PDF, and it is reachable from callers that never went through
        `get_signing_identity` at all (a test, a future second source of user
        records, a deployment that reads its own directory service). A
        guarantee that only holds when the caller remembered to use one
        specific function is not a guarantee.

        Whitespace-only counts as missing. `"  "` is not falsy, satisfies any
        NOT NULL constraint, and renders as nothing -- a signature block with
        a blank credential, which is precisely the document this ruling
        exists to prevent.
        """
        missing = [field for field in _IDENTITY_FIELDS if not str(record.get(field) or "").strip()]
        if missing:
            raise SignoffError(
                "Cannot sign a report with this user record: it is missing "
                f"{', '.join(missing)}. A clinical report shows the signing clinician's name, "
                "registration number and hospital (R10), so no report is produced with any of "
                "them absent. Record them on the user account first."
            )
        user_id = record.get("user_id")
        return cls(
            full_name=str(record["full_name"]).strip(),
            registration_number=str(record["registration_number"]).strip(),
            hospital=str(record["hospital"]).strip(),
            user_id=str(user_id) if user_id is not None else None,
        )

    def physician_string(self) -> str:
        """
        The one rendered form, e.g. "Dr. A. Sharma, Reg. No. 12345, ABC
        Diagnostics" -- byte-identical to what `approve()` has always folded
        into `patient_meta["physician"]`, so nothing downstream
        (`report/summary.py`'s footer, the identity block, the LIMS export)
        changes shape because the values now come from a record.
        """
        return f"{self.full_name}, Reg. No. {self.registration_number}, {self.hospital}"


def _resolve_signing_identity(
    clinician_name: Optional[str],
    reg_number: Optional[str],
    hospital: Optional[str],
    identity: Optional[ClinicianIdentity],
) -> Tuple[ClinicianIdentity, str]:
    """
    Decide which identity a sign-off prints, and return it with the source it
    came from ("user_record" or "typed_in").

    THE RULE, and it is the whole point of R10: a report must not be able to
    show an identity that CONTRADICTS the user record. So when an `identity`
    is supplied, any typed-in value that disagrees with it is refused -- not
    silently overridden, and not silently preferred. Overriding would print
    the right thing while leaving the caller believing it printed theirs;
    preferring the typed value would reopen the exact gap this closes. A
    refusal names the field that disagreed, because the disagreement is
    either a caller bug or an attempt to sign as somebody else, and both need
    to be seen.

    Typed-in values that AGREE are accepted rather than rejected as
    redundant: `review/cli.py` and every existing caller pass all three
    positionally, and a caller that also resolves the account should not have
    to choose between the two.

    With no `identity`, all three typed values are required -- and, as of
    R10, actually CHECKED. They were declared required by the CLI's argparse
    and by nothing else, so a programmatic caller passing None for one of
    them signed a report reading "Dr. X, Reg. No. None, AIIMS Delhi". A
    missing registration number is refused here for the same reason
    `from_user_record` refuses one: the report shows it, so there is no
    report to produce without it.
    """
    if identity is not None:
        supplied = {
            "clinician_name": (clinician_name, identity.full_name),
            "reg_number": (reg_number, identity.registration_number),
            "hospital": (hospital, identity.hospital),
        }
        conflicts = [
            f"{field} (given {given!r}, user record holds {held!r})"
            for field, (given, held) in supplied.items()
            if given is not None and given.strip() != held
        ]
        if conflicts:
            raise SignoffError(
                "Refusing to sign: the values supplied disagree with the signing user's own record -- "
                + "; ".join(conflicts)
                + ". A report must not show an identity that contradicts the user record (R10). "
                "Correct the user record if it is wrong, or drop the supplied value to use the record's."
            )
        return identity, "user_record"

    missing = [
        field
        for field, value in (
            ("clinician_name", clinician_name),
            ("reg_number", reg_number),
            ("hospital", hospital),
        )
        if not (value or "").strip()
    ]
    if missing:
        raise SignoffError(
            f"Cannot sign a report: {', '.join(missing)} not supplied. A clinical report shows the "
            "signing clinician's name, registration number and hospital (R10). Pass all three, or "
            "pass identity=ClinicianIdentity.from_user_record(...) to take them from the signing "
            "user's own record."
        )
    return (
        ClinicianIdentity(
            full_name=cast(str, clinician_name).strip(),
            registration_number=cast(str, reg_number).strip(),
            hospital=cast(str, hospital).strip(),
        ),
        "typed_in",
    )


# ---------------------------------------------------------------------------
# The clinical link (w117, 2026-09-13)
#
# A run that api/submission_worker.py recorded as a clinical interpretation
# leaves a `clinical_link.json` in its output directory. Until this change
# nothing read it: a clinician could `approve()` such a run, get a signed PDF
# with their name on every page, and leave the CLINICAL RECORD -- the thing an
# inspector, a LIMS and the amendment machinery all read -- still saying
# `draft`, with no reviewer claim, no approver_id, no approved_at and no
# content_hash. Two records of one act, disagreeing, with the authoritative
# one wrong.
#
# THIS IS ONE-PERSON SIGN-OFF AND STAYS ONE-PERSON SIGN-OFF (human ruling,
# 2026-09-13). Nothing here starts requiring a second clinician. What it does
# is make the clinical record SAY that one person signed, using the claim kind
# that means exactly that -- `sole_signatory` (commit 5) -- rather than
# `accept`, which the clinical schema defines as an independent concurrence by
# a SECOND clinician and which a same-person sign-off would therefore make a
# false statement. Whether this product should move to two-person sign-off is
# a separate, carded decision; this change deliberately does not make it, and
# deliberately makes the historical record able to answer it.
#
# WRITE ORDER IS LOAD-BEARING AND IS CLINICAL-FIRST. The clinical writes
# happen BEFORE the JSON rewrite, the PDF regeneration and the manifest. If
# the clinical side refuses (wrong role, a report already approved, a bad
# password, the database down), nothing on disk has changed and there is no
# signed PDF anywhere. The opposite order would allow the one state this
# module must never produce: A SIGNED PDF EXISTING WHILE THE CLINICAL RECORD
# STILL SAYS DRAFT. The reverse residue -- a clinical record that says
# approved while the PDF regeneration then fails -- is the survivable one: it
# blocks delivery (the report is approved but the artefacts are stale and the
# manifest absent, so list_pending still reports DRAFT) rather than releasing
# something unapproved.
#
# AN UNLINKED RUN IS UNTOUCHED. No `clinical_link.json` means no clinical
# record exists to keep in step, and `approve`/`override` behave exactly as
# they did before this change -- no credentials required, no database
# consulted, no import of the clinical package attempted.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClinicalCredentials:
    """
    The authenticated person performing a sign-off on a LINKED run.

    A separate object rather than three more parameters on `approve`, because
    these three travel together and never apply to an unlinked run.

    `--clinician-name`/`--reg-number`/`--hospital` remain what they always
    were: UNVALIDATED FREE TEXT for attribution on the rendered artefacts (see
    this module's docstring on human trust). These are different in kind --
    they are checked against the clinical platform's own identity store, and
    the user_id they resolve to is what gets written as the claim's actor and
    the report's approver. The two are deliberately not merged: one is a name
    printed on a page, the other is an authenticated principal.
    """

    email: str
    password: str
    totp_code: Optional[str] = None


def _clinical_link_path(output_dir: str) -> str:
    return os.path.join(output_dir, CLINICAL_LINK_FILENAME)


def read_clinical_link(output_dir: str) -> Optional[Dict[str, Any]]:
    """
    The run's `clinical_link.json` as a dict, or None when the run is not
    linked to a clinical record.

    RAISES on a link file that exists but cannot be read or parsed, rather
    than falling back to None. The fallback would be the worse bug by far: a
    corrupt link file would silently demote a LINKED run to the unlinked path,
    and the unlinked path signs off without touching the clinical record at
    all. "I could not read it" and "there is nothing to read" are different
    answers and only one of them is safe to proceed on.
    """
    path = _clinical_link_path(output_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            link = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SignoffError(
            f"Could not read/parse '{path}': {exc}. This run IS linked to a clinical record "
            "(the file exists), so it must not be signed off through the filesystem-only path "
            "-- that would leave the clinical record saying 'draft' behind a signed PDF. Fix or "
            "restore the file, then retry."
        ) from exc
    if not isinstance(link, dict):
        raise SignoffError(f"'{path}' does not contain a JSON object: {link!r}.")
    return cast(Dict[str, Any], link)


def _link_ids(link: Dict[str, Any]) -> Tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """
    (org_id, interpretation_id, report_id) as UUIDs.

    `report_id` is nullable in the link file -- the worker writes None when the
    record it created carried no report -- and a missing one is refused here
    rather than worked around. Every clinical write below is scoped to a
    report: the claim names one, submission moves one, approval signs one.
    There is no partial version of this to fall back to.
    """
    missing = [k for k in ("org_id", "interpretation_id", "report_id") if not link.get(k)]
    if missing:
        raise SignoffError(
            f"This run's '{CLINICAL_LINK_FILENAME}' does not name {', '.join(missing)}, so its "
            "sign-off cannot be recorded against the clinical report it belongs to. A run whose "
            "clinical record has no report cannot be signed off through this path."
        )
    try:
        return (
            uuid.UUID(str(link["org_id"])),
            uuid.UUID(str(link["interpretation_id"])),
            uuid.UUID(str(link["report_id"])),
        )
    except (TypeError, ValueError) as exc:
        raise SignoffError(f"'{CLINICAL_LINK_FILENAME}' contains a malformed identifier: {exc}") from exc


def _connect_clinical_data_access() -> Any:
    """
    A DataAccess over CLINICAL_DSN -- the same environment variable
    api/submission_worker.py and api/exception_retry_worker.py already refuse
    to start without, not a second name for the same thing.

    Imported here rather than at module scope on purpose: `review/signoff.py`
    is used on runs that have no clinical record at all, and importing
    `clinical.data_access` (and through it psycopg) at module load would make
    a database driver a hard requirement of every unlinked sign-off.
    """
    dsn = os.getenv("CLINICAL_DSN")
    if not dsn:
        raise SignoffError(
            "This run is linked to a clinical record but CLINICAL_DSN is not set, so the "
            "sign-off cannot be recorded there. Refusing rather than signing the files alone: "
            "that would leave the clinical record saying 'draft' behind a signed PDF. Set "
            "CLINICAL_DSN to the clinical database DSN (the same one the submission worker uses)."
        )
    try:
        import psycopg

        from clinical.data_access import DataAccess
    except ImportError as exc:  # pragma: no cover -- a broken install, not a code path
        raise SignoffError(f"This run is linked to a clinical record but the clinical layer is unavailable: {exc}")
    return DataAccess(psycopg.connect(dsn, autocommit=False))


def _clinical_login(
    link: Dict[str, Any], credentials: Optional[ClinicalCredentials], data_access: Any
) -> Tuple[Any, Any]:
    """
    (data_access, session) for a linked run's sign-off.

    REFUSES WITHOUT CREDENTIALS. A linked run signed off with no authenticated
    person is precisely the divergence this whole section exists to stop, so
    the absence is an error and not a quiet fall-through to the file-only path.
    """
    if credentials is None:
        raise SignoffError(
            f"This run is linked to a clinical record ('{CLINICAL_LINK_FILENAME}' is present), so its "
            "sign-off must be recorded there by an authenticated person -- the name/registration "
            "number on the report are attribution text, not an identity. Supply clinical "
            "credentials (CLI: --clinical-email, and the password in the environment variable named "
            "by --clinical-password-env). Refusing rather than signing the files alone, which would "
            "leave the clinical record saying 'draft' behind a signed PDF."
        )
    org_id, _interpretation_id, _report_id = _link_ids(link)
    dao = data_access if data_access is not None else _connect_clinical_data_access()
    session = dao.login(credentials.email, org_id, credentials.password, credentials.totp_code)
    return dao, session


def _linked_signing_identity(
    dao: Any,
    session: Any,
    identity: Optional[ClinicianIdentity],
) -> ClinicianIdentity:
    """
    THE IDENTITY A LINKED RUN PRINTS COMES FROM THE USER RECORD OF THE ACCOUNT
    THAT IS SIGNING. Never from the typed-in flags. (The join between w117's
    linked sign-off and R10's record-backed identity, 2026-09-13.)

    Three reasons, and the third is the one that makes this not merely
    preferable but required:

    1. R10 kept the typed-in path for exactly one deployment -- the
       filesystem-only one, which has no user record to read. A LINKED RUN IS
       BY DEFINITION NOT THAT DEPLOYMENT: `approve()` has just authenticated
       against the clinical platform and is holding the signing account's own
       session. The one justification for typed values is absent here.

    2. R10's rule is that a report must not show an identity that CONTRADICTS
       the user record. Where the record is reachable, reading it is the only
       way to keep that true without trusting the caller to have typed
       correctly.

    3. THE LINKED SIGN-OFF ALREADY CREATES A SECOND ANSWER TO "WHO SIGNED".
       `approve_report` writes `approver_id = session.user_id` on the clinical
       report, and R10's `signing_identity_for_report(session, report_id)`
       answers "whose name, registration number and hospital does this report
       show?" from that very column. So after a linked sign-off there are two
       renderings of one signature: the PDF's printed string and the clinical
       layer's answer. Typed-in values would let those two disagree -- the
       same shape of defect (a signed artefact and the clinical record saying
       different things about one act) that the linked path exists to close,
       reintroduced one field over. Reading the record makes them identical by
       construction rather than by discipline.

    CONTRADICTIONS ARE REFUSED BY R10'S OWN RULE, NOT BY NEW CODE. This
    function only decides WHERE the identity comes from; the caller hands the
    result to `_resolve_signing_identity` as `identity=`, which already
    refuses any typed value that disagrees and accepts ones that agree. There
    is therefore ONE resolution path and ONE refusal message for both
    deployments, rather than a second identity path that could drift from it.

    A caller-supplied `identity` object is checked against the record here for
    the same reason and with the same verdict: on a linked run the record is
    authoritative, so an identity object that disagrees with it is a caller
    bug or an attempt to sign as somebody else, and both need to be seen.

    Raises `SignoffError` (from `ClinicianIdentity.from_user_record`) when the
    signing account carries no complete identity, and `clinical`'s own
    `IncompleteClinicianIdentityError` from `get_signing_identity` for the
    same condition detected one layer down. Called BEFORE any clinical write
    and before any file write, so an account without a recorded identity
    leaves both records untouched.
    """
    record = ClinicianIdentity.from_user_record(dao.get_signing_identity(session, session.user_id))
    if identity is not None:
        disagreements = [
            f"{field} (identity object holds {given!r}, user record holds {held!r})"
            for field, given, held in (
                ("full_name", identity.full_name, record.full_name),
                ("registration_number", identity.registration_number, record.registration_number),
                ("hospital", identity.hospital, record.hospital),
            )
            if given != held
        ]
        if disagreements:
            raise SignoffError(
                "Refusing to sign: the identity supplied disagrees with the signing account's own "
                "record -- " + "; ".join(disagreements) + ". This run is linked to a clinical record, "
                "so the identity it prints is read from the account that signs (R10). Correct the user "
                "record if it is wrong, or drop the supplied identity to use the record's."
            )
    return record


def _record_clinical_signoff(
    dao: Any,
    session: Any,
    link: Dict[str, Any],
    reason: str,
) -> Dict[str, Any]:
    """
    The whole clinical half of `approve()` on a linked run, as ONE
    authenticated person (human ruling, 2026-09-13):

        login -> sole_signatory claim (with an explicit reason)
              -> submit_for_review -> approve_report (approver = that person)

    Takes an ALREADY-OPEN `(dao, session)` rather than logging in itself. One
    login per sign-off, and the session that reads the signing identity is the
    same one that records the claim and the approval -- so the identity the
    PDF prints and the `approver_id` the clinical report stores cannot come
    from two different accounts.

    All four through PUBLIC DataAccess methods. Nothing here reaches into a
    private one, which is why commit 5 added the wrappers: a delivery path
    calling `_approve_report` directly is one refactor away from bypassing an
    enforcement it cannot see.

    THE SIGNATORY MUST HOLD BOTH THE Interpreter AND THE Approver ROLE, and
    that is a consequence of one person doing both halves rather than a
    relaxation of anything: `submit_for_review` has always required
    Interpreter and `_approve_report` has always required Approver, on the
    session and on the recorded approver both. Those checks are untouched;
    with one person they simply land on one identity. A signatory holding only
    one of the two is refused, by the same code that refused them before.

    `reason` is required and is the signatory's stated grounds, recorded on
    the claim. A concurrence with no grounds is not evidence -- the clinical
    layer requires it of every claim kind and this path does not invent an
    exception.

    Returns the identifiers actually written, for the manifest and the audit
    log, so the filesystem record can be matched to the clinical one later.
    """
    org_id, interpretation_id, report_id = _link_ids(link)

    claim_id = dao.record_sole_signatory_claim(
        session,
        interpretation_id=interpretation_id,
        report_id=report_id,
        reason=reason,
    )
    dao.submit_for_review(session, report_id)
    dao.approve_report(session, report_id)

    return {
        "org_id": str(org_id),
        "interpretation_id": str(interpretation_id),
        "report_id": str(report_id),
        "claim_id": str(claim_id),
        "claim_type": "sole_signatory",
        "approver_user_id": str(session.user_id),
    }


def _record_clinical_disagreement(
    link: Dict[str, Any],
    credentials: Optional[ClinicalCredentials],
    variant_key: str,
    new_classification: str,
    reason: str,
    data_access: Any = None,
) -> Dict[str, Any]:
    """
    The clinical half of `override()` on a linked run.

    AN OVERRIDE BEFORE APPROVAL is a DISAGREEMENT CLAIM by the same person,
    plus this module's existing file change. It is not an edit of the
    interpretation and the clinical layer does not treat it as one: the
    pipeline's own classification stays exactly where it was and the claim is
    appended beside it, which is the whole reason spec 13.2 made disagreement
    a claim.

    AN OVERRIDE AFTER APPROVAL IS REFUSED, and the refusal points at the
    amendment CONCEPT while saying plainly that the path is NOT YET AVAILABLE
    from this tool. It used to name `clinical.data_access::create_amendment`,
    which no command reaches: a refusal naming something a user cannot run is a
    false claim in user-facing text, and it arrives exactly when somebody is
    trying to act (human ruling, w119). An approved report has a content_hash over exactly what was
    approved, and `verify_report_integrity` exists to detect content that
    changed underneath it; quietly appending a claim to an approved report
    would trip that as a nonconformance, and quietly rewriting the run
    document behind it would be the nonconformance. An approved report is
    changed by AMENDING it (a second report on the same interpretation, which
    the clinical layer already models), never by editing the signed one.
    Refused BEFORE any write, so a refusal leaves the directory untouched.

    VARIANT KEY FORM: the clinical layer identifies a variant by the engine's
    own `chrom-pos-ref-alt` key (reviewer_claims.variant_key's schema comment
    says so). This module's CLI speaks `chrom:pos:ref>alt`. The conversion
    happens here, once, from the parsed components -- not by string-munging
    the CLI value -- so the two vocabularies meet in exactly one place.
    """
    org_id, interpretation_id, report_id = _link_ids(link)
    dao, session = _clinical_login(link, credentials, data_access)

    report = dao.get_report(session, report_id)
    if report is None:
        raise SignoffError(
            f"This run's clinical report {report_id} was not found in organisation {org_id}. "
            "Refusing to change the files of a run whose clinical record cannot be reached."
        )
    state = report.get("state")
    if state in ("approved", "released"):
        raise SignoffError(
            f"Refusing to override: this run's clinical report {report_id} is already '{state}'. "
            "An approved report is not edited -- its content_hash attests to exactly what was "
            "approved, and changing the content behind it is the nonconformance ISO 15189 7.5 "
            "asks to be detected, not a workflow. The remedy is an AMENDMENT -- a second report on "
            "the same interpretation, recording the change as a new document rather than silently "
            "altering a signed one -- and THAT PATH IS NOT YET AVAILABLE from this tool: there is "
            "no command here that issues one. Raise it with the clinical platform's owners. "
            "Nothing on disk has been changed."
        )

    chrom, pos, ref, alt = parse_variant_key(variant_key)
    clinical_variant_key = f"{chrom}-{pos}-{ref}-{alt}"
    claim_id = dao.record_disagreement_claim(
        session,
        interpretation_id=interpretation_id,
        report_id=report_id,
        variant_key=clinical_variant_key,
        new_classification=new_classification,
        reason=reason,
    )
    return {
        "org_id": str(org_id),
        "interpretation_id": str(interpretation_id),
        "report_id": str(report_id),
        "claim_id": str(claim_id),
        "claim_type": "disagree",
        "variant_key": clinical_variant_key,
        "actor_user_id": str(session.user_id),
    }


# ---------------------------------------------------------------------------
# Command 1: approve
# ---------------------------------------------------------------------------


def approve(
    output_dir: str,
    clinician_name: Optional[str] = None,
    reg_number: Optional[str] = None,
    hospital: Optional[str] = None,
    identity: Optional[ClinicianIdentity] = None,
    clinical_credentials: Optional[ClinicalCredentials] = None,
    clinical_reason: Optional[str] = None,
    clinical_data_access: Any = None,
) -> Dict[str, Any]:
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

    WHERE THE THREE IDENTITY VALUES COME FROM (R10, human-ruled
    2026-09-13). Preferred: `identity=ClinicianIdentity.from_user_record(...)`
    -- the name, registration number and hospital held by the account that
    signed, so the printed identity cannot contradict the record. Still
    supported: the three as free text, for the filesystem-only deployment
    that has no user record to read (`review/cli.py`). Supplying BOTH is
    allowed only while they agree; a disagreement is refused by
    `_resolve_signing_identity` before anything is written. The typed-in path
    now actually enforces that all three are present -- previously only
    argparse required them, so a programmatic caller could sign a report
    reading "Reg. No. None".

    Returns the manifest dict that was also written to
    `geper_signoff_manifest.json`. Raises `SignoffError` if the identity is
    incomplete or contradicts the user record (checked FIRST, before the
    directory is even read, so nothing is written), if `output_dir`
    has no `geper_results.json` (nothing to approve), OR if this run
    recorded a model-weight hash MISMATCH for any model (see
    `_check_no_model_hash_mismatch`, human-ruled 2026-09-10) -- checked
    immediately below, before any write, so a refusal here leaves the
    directory exactly as it was.

    LINKED RUNS (w117, 2026-09-13). If the output directory carries a
    `clinical_link.json`, this run is already a clinical interpretation with a
    clinical report, and signing only the files would leave that report saying
    `draft` behind a signed PDF. So the sign-off is ALSO recorded through the
    clinical layer, as ONE authenticated person: login, a `sole_signatory`
    claim carrying `clinical_reason`, `submit_for_review`, then `approve_report`
    with that same person as approver. See the "clinical link" section above
    for why the claim kind is `sole_signatory` and not `accept`, and why the
    clinical writes come FIRST.

    `clinical_credentials` and `clinical_reason` are REQUIRED for a linked run
    and IGNORED for an unlinked one, which is the only shape that leaves
    today's behaviour untouched where there is no clinical record to keep in
    step with. `clinical_data_access` is an injection point for tests and for
    a caller that already holds a connection; when it is None a linked run
    connects using CLINICAL_DSN.

    ON A LINKED RUN THE THREE IDENTITY VALUES COME FROM THE SIGNING ACCOUNT'S
    USER RECORD, NOT FROM `clinician_name`/`reg_number`/`hospital` (the join
    between this path and R10). `identity_source` is therefore always
    "user_record" for a linked sign-off. Typed-in values are still accepted
    while they AGREE with the record and refused when they contradict it --
    by `_resolve_signing_identity`, R10's own rule, rather than by a second
    identity path that could drift from it. See `_linked_signing_identity` for
    why: chiefly that `approve_report` writes `approver_id` on the clinical
    report and R10's `signing_identity_for_report` reads the printed identity
    back out of that same column, so a typed identity here would let the PDF
    and the clinical record name different people. An account carrying no
    recorded identity refuses before the claim, the submission, the approval
    and every file write.

    Raises `SignoffError`, before any write, when a linked run is missing
    credentials or a reason, and propagates the clinical layer's own refusals
    (a wrong password, a signatory without the Interpreter or Approver role, a
    report not in `draft`) unchanged -- all of them leave the directory exactly
    as it was.
    """
    link = read_clinical_link(output_dir)

    # UNLINKED: R10's ordering exactly as it was -- the identity is resolved
    # before the directory is read, so an incomplete or contradicting identity
    # refuses without anything on disk having been touched.
    if link is None:
        signer, identity_source = _resolve_signing_identity(clinician_name, reg_number, hospital, identity)

    results_path = _require_results(output_dir)
    document = _load_document(results_path)
    _check_no_model_hash_mismatch(document)

    # LINKED: identity from the signing account's own record, then the clinical
    # writes -- both before a single byte on disk changes. See the "clinical
    # link" section's note on write order (the state this forbids is a signed
    # PDF existing while the clinical record still says draft) and
    # _linked_signing_identity's docstring for why the identity is read rather
    # than typed.
    clinical_record: Optional[Dict[str, Any]] = None
    if link is not None:
        if clinical_reason is None or not str(clinical_reason).strip():
            raise SignoffError(
                "This run is linked to a clinical record, so its sign-off is recorded there as a "
                "'sole_signatory' claim -- and a claim requires the signatory's stated grounds. "
                "Supply a reason (CLI: --reason). Nothing has been changed."
            )
        dao, session = _clinical_login(link, clinical_credentials, clinical_data_access)
        # Read the identity BEFORE the claim, the submission and the approval:
        # an account with no recorded identity must leave the clinical record
        # untouched too, not approve a report it then cannot print.
        signer, identity_source = _resolve_signing_identity(
            clinician_name,
            reg_number,
            hospital,
            _linked_signing_identity(dao, session, identity),
        )
        clinical_record = _record_clinical_signoff(dao, session, link, str(clinical_reason))

    physician = signer.physician_string()
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
        "clinician_name": signer.full_name,
        "reg_number": signer.registration_number,
        "hospital": signer.hospital,
        # R10: WHERE the printed identity came from, recorded rather than
        # inferred. "user_record" means it was read from the signing account
        # and could not contradict it; "typed_in" means a human typed it at
        # sign-off with no account behind it (the filesystem-only CLI path).
        # An auditor reading an old manifest must be able to tell the two
        # apart -- without this key every manifest looks equally attested,
        # including the ones that are not.
        "identity_source": identity_source,
        "clinician_user_id": signer.user_id,
        "approved_at": approved_at,
        "variants": [_variant_manifest_entry(vr) for vr in document.get("variants", [])],
        # Present only on a linked run, and the key is omitted rather than set
        # to None on an unlinked one: "there is no clinical record" and "there
        # is one and we did not record it" must not read the same in a manifest
        # a forensic audit is holding.
        **({"clinical": clinical_record} if clinical_record else {}),
    }
    with open(_manifest_path(output_dir), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    _append_audit_log(
        output_dir,
        {
            "action": "approved",
            "output_dir": os.path.abspath(output_dir),
            "clinician": signer.full_name,
            "clinician_user_id": signer.user_id,
            "identity_source": identity_source,
            "timestamp": approved_at,
            **({"clinical": clinical_record} if clinical_record else {}),
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
    clinical_credentials: Optional[ClinicalCredentials] = None,
    clinical_data_access: Any = None,
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

    LINKED RUNS (w117, 2026-09-13). On a run carrying `clinical_link.json`:

      - BEFORE the clinical report is approved, an override is ALSO a
        DISAGREEMENT CLAIM by the same authenticated person, recorded first,
        followed by the file change exactly as before. The claim is appended
        beside the pipeline's own classification, never over it.
      - AFTER the clinical report is approved (or released), an override is
        REFUSED and the refusal names the amendment path. A signed report's
        content_hash attests to exactly what was approved; changing what sits
        underneath it is the ISO 15189 7.5 nonconformance the platform exists
        to detect, not a workflow. The refusal happens before any write, so
        nothing on disk changes.

    `clinical_credentials` is required for a linked run and ignored for an
    unlinked one. `reason` is already required by this command and is reused as
    the claim's stated grounds -- deliberately the same sentence, so the two
    records cannot disagree about why.
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

    # Clinical side first, for the same reason approve() does it first: an
    # approved clinical report must refuse BEFORE the run document, the PDFs
    # and the Markdown have been rewritten underneath it.
    link = read_clinical_link(output_dir)
    clinical_claim: Optional[Dict[str, Any]] = None
    if link is not None:
        clinical_claim = _record_clinical_disagreement(
            link,
            clinical_credentials,
            variant_key,
            new_classification,
            reason,
            data_access=clinical_data_access,
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
        # Omitted entirely on an unlinked run -- see approve()'s manifest note.
        **({"clinical": clinical_claim} if clinical_claim else {}),
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
            **({"clinical": clinical_claim} if clinical_claim else {}),
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
    has_conflicting_evidence, status, model_checkpoints,
    model_provenance_status}`.

    `model_provenance_status` is `VersionStatus.VERSION_KNOWN` when the run
    recorded which model checkpoints it used and `VersionStatus.NOT_CONSULTED`
    when it did not (an absent key, or an explicit null from a
    carried-forward document). *** THE SECOND CASE IS NOT "NO MODELS" AND MUST
    NEVER BE READ AS ONE. *** A caller filtering these entries to answer
    "which runs used model X" is deciding who gets recalled; a run that never
    recorded its identifiers can only answer "we cannot tell", and that has to
    be visible rather than showing up as an absence of hits.

    `has_conflicting_evidence` is
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
        checkpoints = document.get("model_checkpoints")
        # "NEVER RECORDED" AND "RECORDED NOTHING" ARE DIFFERENT ANSWERS, AND
        # THIS IS THE LAST PLACE THEY CAN STILL BE TOLD APART.
        #
        # This projection is what an impact query reads to decide WHO GETS
        # RECALLED. A run whose document predates model-checkpoint capture has
        # no identifiers to match, so it would silently produce no hits -- and
        # the reader would see "no affected reports" where the truth is "we
        # cannot tell for this run". A PARTIAL ANSWER READS AS A COMPLETE ONE,
        # and it fails in the direction of not recalling someone.
        #
        # `report/json_builder.py` already makes exactly this distinction when
        # carrying a prior document forward ("model_checkpoints" in
        # prior_document and checkpoints is not None); an explicit null there
        # means the same gap as an absent key, so both collapse to
        # NOT_CONSULTED here and neither is treated as an empty result set.
        #
        # VersionStatus is provenance.py's own vocabulary rather than a
        # parallel one invented here: NOT_CONSULTED already means "never
        # queried this run", and is deliberately a different member from
        # UNKNOWN ("queried, nothing obtainable").
        model_provenance_status = VersionStatus.VERSION_KNOWN if checkpoints else VersionStatus.NOT_CONSULTED
        results.append(
            {
                "output_dir": dirpath,
                "report_date": document.get("generated_at"),
                "num_variants": document.get("variant_count", len(variants)),
                "has_conflicting_evidence": any(has_conflicting_evidence(vr) for vr in variants),
                "status": status,
                # Carried verbatim, never normalised into {} -- an empty dict
                # would read as "recorded, and there were none".
                "model_checkpoints": checkpoints if checkpoints else None,
                "model_provenance_status": model_provenance_status,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Command 4: withdraw
# ---------------------------------------------------------------------------


def withdraw(
    output_dir: str,
    reason: str,
    actor: str,
    clinical_credentials: Optional[ClinicalCredentials] = None,
    clinical_data_access: Any = None,
) -> Dict[str, Any]:
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

    LINKED RUNS (w119, 2026-09-13). `approve()` and `override()` already
    read `clinical_link.json` and keep the clinical record in step; this
    function did not, and it has no path to the clinical record in its
    signature at all. On a LINKED run that meant a withdrawal removed the
    manifest and reset `review_status` to `draft` WHILE THE CLINICAL REPORT
    -- what an inspector, a LIMS and the amendment machinery read -- still
    said `approved`. The human's ruling on why that is worse than the
    routine divergence w117 closed: a withdrawal is a DELIBERATE SAFETY
    ACT, so a record that still says `approved` afterwards contradicts a
    clinician's explicit decision rather than merely lagging a change.

    THE CLINICAL LAYER HAS NO OPERATION THAT RETRACTS AN APPROVAL, AND THIS
    FUNCTION DOES NOT INVENT ONE. Established by reading the layer, not
    assumed:

      - `clinical/data_access.py` has no unapprove/retract/rescind method.
        Its only public verbs past approval are `_release_report` (forward)
        and `create_amendment` (a NEW report beside the signed one).
      - `reports.approver_id`/`approved_at`/`content_hash` are write-once:
        `enforce_content_immutability()` (clinical/schema.sql) raises on any
        attempt to change them once set.
      - The same trigger refuses to WALK STATE BACK: `IF OLD.state IN
        ('approved','released') AND NEW.state IS DISTINCT FROM OLD.state`
        is an exception unless it is exactly `approved -> released`. So
        `approved -> draft` is not merely unimplemented, it is FORBIDDEN AT
        THE DATABASE, deliberately (that comment names walking an approved
        report back as "effectively withdrawn from the record" and refuses
        it).
      - `reviewer_claims.claim_type` is a closed CHECK -- 'accept',
        'disagree', 'variant_added', 'variant_not_relevant',
        'sole_signatory'. None of them means "the sign-off is retracted",
        and writing one that does not mean that is how w117's own
        `sole_signatory` ruling says a record becomes false.

    SO THE CLINICAL SIDE OF A WITHDRAWAL ON AN APPROVED REPORT IS A
    REFUSAL, which is exactly the shape `override()` already uses for the
    same collision (see `_record_clinical_disagreement`): refuse rather
    than half-do it, before a single byte on disk changes. The divergence
    is closed by never creating it -- the files keep saying `reviewed`, the
    clinical record keeps saying `approved`, and the two still agree --
    rather than by a state transition the schema does not support. Giving
    the clinical layer a real retraction is a schema and governance
    question and is NOT decided here.

    A LINKED RUN WHOSE CLINICAL REPORT IS NOT YET APPROVED (`draft`,
    `under_review`, `returned`) IS WITHDRAWN NORMALLY: there is no approval
    on the clinical side to contradict, so the file-side retraction leaves
    the two records agreeing. That case is reachable -- `approve()` stops
    at `under_review` when the signatory lacks the Approver role, and a
    link file can be added to a directory that was signed off before it
    existed.

    Credentials are REQUIRED on a linked run and IGNORED on an unlinked
    one, same rule and same reason as `approve()`: reading the report's
    state is an authenticated act, and "I could not look" must not read the
    same as "there was nothing to look at". `clinical_data_access` is the
    same test/caller injection point `approve()` takes; when it is None a
    linked run connects using CLINICAL_DSN.
    """
    results_path = _require_results(output_dir)
    manifest_path = _manifest_path(output_dir)
    if not os.path.exists(manifest_path):
        raise SignoffError(
            f"No '{MANIFEST_FILENAME}' found in '{output_dir}' -- nothing to withdraw (this run has "
            f"never been through approve(), or a prior withdrawal already removed it)."
        )

    # LINKED: before any file change, exactly like approve(). See the
    # docstring for why the clinical side of a withdrawal is a refusal and
    # not a write.
    link = read_clinical_link(output_dir)
    clinical_state: Optional[str] = None
    if link is not None:
        _org_id, _interpretation_id, report_id = _link_ids(link)
        dao, session = _clinical_login(link, clinical_credentials, clinical_data_access)
        report = dao.get_report(session, report_id)
        if report is None:
            raise SignoffError(
                f"This run's clinical report {report_id} was not found in organisation {_org_id}. "
                "Refusing to withdraw the sign-off on the files of a run whose clinical record "
                "cannot be reached -- that would leave the clinical record standing behind a "
                "withdrawal nobody can see. Nothing on disk has been changed."
            )
        clinical_state = report.get("state")
        if clinical_state in ("approved", "released"):
            raise SignoffError(
                f"Refusing to withdraw: this run's clinical report {report_id} is already "
                f"'{clinical_state}', and the clinical record has no way to retract an approval. "
                "An approved report's approver, approval time and content_hash are write-once and "
                "its state cannot be walked back -- the database itself refuses it, because a "
                "report walked back from 'approved' would be withdrawn from the record, which ISO "
                "15189 does not permit. Withdrawing only the files would therefore leave the "
                "clinical record saying 'approved' behind a retracted sign-off, which is the "
                "contradiction this refusal exists to prevent. An approved report is corrected by "
                "AMENDMENT -- a second report on the same interpretation, recording the change as a "
                "new document -- and that path is NOT YET AVAILABLE from this tool; raise it with "
                "the clinical platform's owners. Nothing on disk has been changed."
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
            # Only on a linked run, and omitted rather than None on an
            # unlinked one -- same rule as approve()'s manifest: "there is no
            # clinical record" and "there is one and we did not look" must not
            # read the same in an audit log.
            **({"clinical_report_state": clinical_state} if link is not None else {}),
        },
    )
    logger.info(f"Withdrew sign-off for '{output_dir}' (was {previous_status!r}).")
    return {
        "output_dir": os.path.abspath(output_dir),
        "previous_review_status": previous_status,
        "reason": reason,
        "actor": actor,
        "timestamp": timestamp,
        **({"clinical_report_state": clinical_state} if link is not None else {}),
    }
