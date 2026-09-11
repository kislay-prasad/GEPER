"""
geper/api/submission_worker.py
──────────────────────────────
Background worker for GEPER interpretation submissions.

Polls submissions with status='queued', invokes geper/main.py via spawn_tracked
(so interpretations are killable), handles timeouts, and updates submission status.
On failure, creates exceptions in the clinical database to notify lab operators.

2026-09-08: this file used to shell out to a binary named "bij-interpret"
that has never existed anywhere in this repo (hive finding
meredith-establish-what-bij-interpret-is-the-last-missing-link). The
human ruled: geper/main.py is proven (ran end to end on 2026-09-05) and
this worker has never executed against anything real, so THE WORKER
ADAPTS TO geper/main.py'S CONTRACT, not the other way around. This
revision makes three changes, all traceable to that ruling: (1) invokes
geper/main.py directly via the running interpreter instead of a
nonexistent named binary -- geper/ has no [project.scripts] entry
anywhere (confirmed in the same finding), so there is no real console
script to point at without inventing packaging; (2) drops --sample-ref/
--consent-ref from the command line (geper/main.py's build_arg_parser(),
geper/main.py:43-203, has never accepted either) while leaving them on the
Submission object, the DB schema, and the API model untouched -- the
platform already knows which submission it started and does not need
the CLI to correlate it; (3) reads success/failure from the exit code
and the run document from disk instead of parsing a stdout JSON envelope
geper/main.py has never produced (geper/main.py:206-281 never prints to
stdout; its output is files under --output-dir plus an exit code).

2026-09-12 (D0, "build the link"): a completed run is now RECORDED as
clinical records -- sequencing run, VCF, interpretation, draft report, in
one transaction (clinical/data_access.py::record_pipeline_result) -- as the
organisation's system principal, after the Phase 5 preconditions pass, and
the submission's interpretation_id is that clinical interpretation (it was
a uuid4 naming nothing). A retry, a re-queue or a restart adopts an
existing record for the same VCF bytes and sample instead of running the
engine twice. Scope (ruling D1): runs whose patient/order/sample records
already exist; nothing in production creates those yet.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from component_identity import SHORT_NAME
from pipeline.hpo.utils import is_well_formed_hpo_id
from report.clinical_report_builder import _QC_METRIC_ORDER
from shared.process_control import spawn_tracked, kill_process_tree_now

from .submission_store import SubmissionStore

logger = logging.getLogger("geper.api.submission_worker")

# Timeout for a single interpretation run (30 minutes)
INTERPRETATION_TIMEOUT_SECONDS = 1800

# geper/main.py itself -- the proven CLI. No console-script entry exists
# for it anywhere in the repo (geper/ has no pyproject.toml/setup.py of
# its own; only shared/ is packaged at the repo root), so this points at
# the file directly rather than a name on PATH. BIJ_AI_CLI_PATH is kept
# as an override (now a path to main.py, not a bare command name) in
# case a future packaged install wants to point elsewhere.
_DEFAULT_GEPER_MAIN_PATH = str((Path(__file__).resolve().parent.parent / "main.py"))

# Root directory under which each submission gets its own --output-dir
# (submission.id-scoped). geper/main.py's own default (./geper_output,
# geper/config.py:2706) is a single shared relative path -- concurrent
# submissions would silently overwrite each other's geper_results.json.
# Same env-var-with-file-relative-default pattern main() below already
# uses for GEPER_SUBMISSION_STORE_PATH.
_DEFAULT_OUTPUT_ROOT = str(Path(__file__).parent / ".submission_outputs")

# Written into each submission's output directory once its run is recorded:
# which clinical organisation, order, sample, interpretation, report and VCF
# the directory's files became. Anything that later acts on the directory
# can tell a run governed by the clinical record from a standalone one by
# this file alone. (Nothing reads it yet: how review/signoff.py should treat
# a linked run is a separate, pending design.)
CLINICAL_LINK_FILENAME = "clinical_link.json"


@dataclass(frozen=True)
class _ClinicalLink:
    """One submission resolved to its clinical identity (see
    InterpretationWorker._clinical_preflight)."""

    session: Any
    org_id: uuid.UUID
    order_id: uuid.UUID
    sample_id: uuid.UUID
    vcf_hash: str
    submission_key: str


def _sha256_file(path: str) -> str:
    """Streamed: a VCF can be large. Same digest create_vcf records."""
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _write_clinical_link(output_dir: str, link: dict) -> None:
    """Atomic: written to a temporary name and renamed, so a reader never
    sees half a file."""
    os.makedirs(output_dir, exist_ok=True)
    final = os.path.join(output_dir, CLINICAL_LINK_FILENAME)
    tmp = final + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(link, fh, indent=2)
    os.replace(tmp, final)


def _hpo_terms_to_cli_arg(hpo_terms) -> str:
    """
    Converts the platform's `hpo_terms` JSON into the comma-separated
    "HP:#######" string geper/main.py's --hpo-terms wants
    (geper/main.py:175-189's own help text). The platform spec
    (GEPER_CLINICAL_PLATFORM_SPEC.md:457) declares hpo_terms as a plain
    list -- ["HP:0000001", ...] -- but the implemented request model
    (geper/api/main.py:209) is Optional[Dict[str, Any]], so in practice a
    dict is what reaches here; the one shape attested anywhere in this
    repo (geper/api/tests/test_interpretations_api.py:77,
    geper/api/tests/test_submission_worker.py) is {"terms": [...]}.
    Accepts either shape.

    Raises ValueError, naming exactly what's wrong, on anything that
    isn't a non-empty list of well-formed HPO IDs -- ruled 2026-09-08:
    "the silent degradation is the real defect... convert, and IF
    CONVERSION FAILS, RAISE RATHER THAN PROCEED. A submission that can't
    pass its phenotype data is a precondition failure, not a degraded
    run." Deliberately does NOT reuse geper/main.py's own malformed-ID
    handling (geper/pipeline/hpo/utils.py::parse_hpo_terms_arg, which
    logs a warning and silently drops bad IDs, geper/main.py:183-187) --
    that silent-drop behavior is exactly what this conversion exists to
    keep this caller from ever triggering. Reuses is_well_formed_hpo_id
    (geper/pipeline/hpo/utils.py:84-87) for the same "HP:" + 7 digits
    check geper/main.py's own parsing uses, rather than a second regex.
    """
    if isinstance(hpo_terms, dict):
        terms = hpo_terms.get("terms")
    else:
        terms = hpo_terms

    if not isinstance(terms, list) or not terms:
        raise ValueError(
            f"hpo_terms did not contain a non-empty list of HPO IDs (got {hpo_terms!r}); "
            "expected {'terms': ['HP:#######', ...]} or ['HP:#######', ...]."
        )

    malformed = [t for t in terms if not (isinstance(t, str) and is_well_formed_hpo_id(t))]
    if malformed:
        raise ValueError(f"hpo_terms contained malformed HPO ID(s): {malformed!r} (expected 'HP:#######').")

    return ",".join(terms)


def _qc_metrics_validated(qc_metrics) -> dict:
    """
    Converts the platform's `qc_metrics` JSON -- a flat
    {metric_name: number} dict, e.g. {"mean_coverage_depth": 45.2} --
    into the `{"status": "found", "value": float, "reason": None}`-per-
    metric shape `report/clinical_report_builder.py::_parse_qc_metrics`
    requires. Raises ValueError, naming exactly what's wrong, on
    anything that isn't a dict of real numbers keyed by one of
    `_QC_METRIC_ORDER` -- same "convert, and if conversion fails, RAISE
    rather than proceed" pattern `_hpo_terms_to_cli_arg` above already
    applies (2026-09-08 ruling).

    2026-09-11 ruling (PHASE8-bij-interpret, option A): the platform's
    own tests (geper/api/tests/test_interpretations_api.py:78,
    test_submission_worker.py:247) use {"depth": 50} as "a normal
    qc_metrics submission". Written through unconverted, that shape
    reached `_parse_qc_metrics` as a `.get()` miss on every recognized
    key and rendered "Not reported by the upstream sequencing/alignment
    pipeline for this run" about a value that WAS reported -- a FALSE
    SENTENCE in a clinical report. Option C (aliasing an unrecognized
    key like "depth" onto "mean_coverage_depth") is deliberately NOT
    done here: guessing clinical meaning from a key name is how a wrong
    mapping becomes invisible.
    """
    if not isinstance(qc_metrics, dict):
        raise ValueError(f"qc_metrics must be a JSON object keyed by metric name, got {qc_metrics!r}.")

    unrecognized = [k for k in qc_metrics if k not in _QC_METRIC_ORDER]
    if unrecognized:
        raise ValueError(
            f"qc_metrics contained unrecognized key(s) {unrecognized!r}; "
            f"recognized metric names are {list(_QC_METRIC_ORDER)!r}."
        )

    converted = {}
    for key, value in qc_metrics.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"qc_metrics[{key!r}] must be a real number, got {value!r}.")
        converted[key] = {"status": "found", "value": float(value), "reason": None}
    return converted


class InterpretationWorker:
    """Poll and process GEPER interpretation submissions."""

    def __init__(self, store: SubmissionStore, data_access=None):
        self.store = store
        self.data_access = data_access
        self.geper_main_path = os.getenv("BIJ_AI_CLI_PATH", _DEFAULT_GEPER_MAIN_PATH)
        self.output_root = os.getenv("GEPER_SUBMISSION_OUTPUT_ROOT", _DEFAULT_OUTPUT_ROOT)

    def _create_submission_failure_exception(self, submission, reason_code: str, error_message: str) -> None:
        """
        Create an exception for a submission failure.

        Only called if data_access is available and submission has order_id.
        If either is missing, logs a warning but continues (doesn't block).
        """
        if not self.data_access or not submission.order_id:
            logger.warning(
                f"[{submission.id}] Cannot create exception: "
                f"data_access={'available' if self.data_access else 'unavailable'}, "
                f"order_id={submission.order_id}"
            )
            return

        try:
            from clinical.models.exception import (
                REASON_CODE_TO_CATEGORY,
                REASON_CODE_TO_OWNER,
            )

            # Get system session for this org
            system_session = self.data_access._create_system_session(uuid.UUID(submission.org_id))

            # Create exception
            self.data_access.create_or_reopen_exception(
                system_session,
                uuid.UUID(submission.order_id),
                REASON_CODE_TO_CATEGORY[reason_code].value,
                reason_code,
                error_message,
                REASON_CODE_TO_OWNER[reason_code],
                "system",
            )
            logger.info(f"[{submission.id}] Created exception: {reason_code}")
        except Exception as e:
            logger.error(f"[{submission.id}] Failed to create exception: {e}", exc_info=True)

    # ── The clinical link (2026-09-12, D0 "build the link") ──────────────────
    #
    # A submission is a clinical act: the run's result becomes a clinical
    # interpretation of one sample, with a draft report, inside the
    # organisation that owns the submission. The worker acts as that
    # organisation's SYSTEM PRINCIPAL (Phase 5c) -- the same principal it
    # already used for failure exceptions -- because no human session
    # exists for an automatic submission.
    #
    # SCOPE (human ruling D1, 2026-09-12): this links runs whose patient,
    # order and sample ALREADY EXIST as clinical records. Nothing in
    # production creates those records yet (order entry is its own card), so
    # until it does every real submission is refused here as not linked.

    def _fail(self, submission, message: str, reason_code: Optional[str] = None) -> None:
        logger.error(f"[{submission.id}] {message}")
        self.store.update_status(submission.id, "failed", error_message=message)
        if reason_code:
            self._create_submission_failure_exception(submission, reason_code, message)

    def _identity(self, submission) -> Optional[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
        """(org_id, order_id, sample_id) as UUIDs, or None when the submission
        does not carry all three -- every row from before 2026-09-12 (org
        "default", no sample_id) and any row written without them."""
        try:
            return (
                uuid.UUID(str(submission.org_id)),
                uuid.UUID(str(submission.order_id)),
                uuid.UUID(str(submission.sample_id)),
            )
        except (TypeError, ValueError, AttributeError):
            return None

    def _clinical_preflight(self, submission) -> Optional["_ClinicalLink"]:
        """Everything that must hold before the engine runs. Returns None
        after marking the submission failed (with an exception on the order
        wherever the order exists to carry one)."""
        from clinical.data_access import interpretation_submission_key

        if self.data_access is None:
            self._fail(
                submission,
                "No clinical record store is configured: refusing to run an interpretation "
                "whose result cannot be recorded as a clinical record.",
            )
            return None

        identity = self._identity(submission)
        if identity is None:
            self._fail(
                submission,
                "Submission is not linked to a clinical record: it must name a clinical "
                f"organisation, order and sample (org_id={submission.org_id!r}, "
                f"order_id={submission.order_id!r}, sample_id={getattr(submission, 'sample_id', None)!r}).",
            )
            return None
        org_id, order_id, sample_id = identity

        session = self.data_access._create_system_session(org_id)
        order = self.data_access.get_order(session, order_id)
        if order is None:
            # No exception: exceptions hang off an order, and this
            # organisation has none by that id.
            self._fail(submission, f"Order {order_id} not found in this submission's organisation.")
            return None

        sample = self.data_access.get_sample(session, sample_id)
        if sample is None or sample.get("order_id") != order_id:
            self._fail(
                submission,
                f"Sample {sample_id} is not a sample of order {order_id} in this organisation.",
                "sample_unresolved",
            )
            return None

        passed, exception_id = self.data_access.validate_order_for_submission(
            session,
            order_id,
            order["patient_id"],
            sample_id,
            submission.vcf_path,
            submission.assembly,
            order["required_scope"],
            "system",
        )
        if not passed:
            # validate_order_for_submission has already recorded its own
            # exception, with the precise reason code.
            self.store.update_status(
                submission.id,
                "failed",
                error_message=f"Submission preconditions not met; see clinical exception {exception_id}.",
            )
            return None

        vcf_hash = _sha256_file(submission.vcf_path)
        return _ClinicalLink(
            session=session,
            org_id=org_id,
            order_id=order_id,
            sample_id=sample_id,
            vcf_hash=vcf_hash,
            submission_key=interpretation_submission_key(vcf_hash, sample_id),
        )

    def _complete(self, submission, link: "_ClinicalLink", output_dir: str, record: dict, results_path) -> None:
        """Write clinical_link.json, then mark the submission complete with the
        clinical interpretation id. The file comes first: a submission must
        never read 'complete' while its output directory cannot say which
        clinical records it became."""
        _write_clinical_link(
            output_dir,
            {
                "submission_id": submission.id,
                "org_id": str(link.org_id),
                "order_id": str(link.order_id),
                "sample_id": str(link.sample_id),
                "interpretation_id": str(record["interpretation_id"]),
                "report_id": str(record["report_id"]) if record.get("report_id") else None,
                "vcf_id": str(record["vcf_id"]),
                "submission_key": link.submission_key,
            },
        )
        logger.info(f"[{submission.id}] Recorded as clinical interpretation {record['interpretation_id']}")
        self.store.update_status(
            submission.id,
            "complete",
            interpretation_id=str(record["interpretation_id"]),
            run_document_ref=results_path,
        )

    def _adopt_existing_record(self, submission, link: "_ClinicalLink", output_dir: str) -> bool:
        """If this run (same VCF bytes, same sample) is already recorded --
        an earlier attempt committed and then lost its SQLite update, or the
        row was re-queued after success -- complete the submission with that
        record instead of running the engine again. True if adopted."""
        found = self.data_access.find_interpretation_by_submission_key(link.session, link.submission_key)
        if found is None:
            return False
        results_path = os.path.join(output_dir, "geper_results.json")
        self._complete(submission, link, output_dir, found, results_path if os.path.isfile(results_path) else None)
        return True

    def _record_run(self, submission, link: "_ClinicalLink", output_dir: str, results_path: str, run_document) -> bool:
        from clinical.data_access import DuplicateInterpretationError

        try:
            recorded = self.data_access.record_pipeline_result(
                link.session, link.sample_id, submission.vcf_path, link.vcf_hash, run_document
            )
            record = {
                "interpretation_id": recorded.interpretation_id,
                "report_id": recorded.report_id,
                "vcf_id": recorded.vcf_id,
            }
        except DuplicateInterpretationError:
            # A concurrent attempt recorded this run first; adopt it.
            if self._adopt_existing_record(submission, link, output_dir):
                return True
            record = None
            failure = "the clinical record refused a duplicate it then could not find"
        except Exception as e:
            record = None
            failure = f"{type(e).__name__}: {e}"

        if record is None:
            self._fail(
                submission,
                f"The interpretation ran but could not be recorded as a clinical record ({failure}). "
                "Nothing was recorded; a retry will run it again.",
                "clinical_record_write_failed",
            )
            return True

        try:
            self._complete(submission, link, output_dir, record, results_path)
        except OSError as e:
            self._fail(
                submission,
                f"Recorded as clinical interpretation {record['interpretation_id']}, but "
                f"clinical_link.json could not be written ({e}). A retry will adopt the record.",
                "clinical_record_write_failed",
            )
        return True

    def reconcile_interrupted_submissions(self, limit: int = 100) -> int:
        """
        Startup: SubmissionStore marks rows left 'running' by a dead worker
        as 'interrupted' -- nobody knows how far they got. For each one, ask
        the clinical record: if this run was recorded before the worker
        died, complete the row with that record. Otherwise leave it
        interrupted (unchanged semantics: an operator decides). Never runs
        the engine and never creates an exception. Returns how many were
        completed.
        """
        from clinical.data_access import interpretation_submission_key

        if self.data_access is None:
            return 0
        completed = 0
        for submission in self.store.get_submissions_with_status("interrupted", limit):
            identity = self._identity(submission)
            if identity is None or not os.path.isfile(submission.vcf_path):
                continue
            org_id, order_id, sample_id = identity
            try:
                vcf_hash = _sha256_file(submission.vcf_path)
                link = _ClinicalLink(
                    session=self.data_access._create_system_session(org_id),
                    org_id=org_id,
                    order_id=order_id,
                    sample_id=sample_id,
                    vcf_hash=vcf_hash,
                    submission_key=interpretation_submission_key(vcf_hash, sample_id),
                )
                if self._adopt_existing_record(submission, link, os.path.join(self.output_root, submission.id)):
                    completed += 1
            except Exception as e:
                logger.error(f"[{submission.id}] Could not reconcile interrupted submission: {e}", exc_info=True)
        return completed

    def process_queued_submission(self, submission) -> bool:
        """Process a single queued submission.

        Returns True if processed (succeeded or failed), False if skipped.
        """
        logger.info(f"Processing submission {submission.id}")

        # Update status to running
        self.store.update_status(submission.id, "running")

        try:
            # One output directory per submission -- see
            # _DEFAULT_OUTPUT_ROOT. Scoped by submission.id, which the
            # platform already generated and uses to correlate this run
            # (same reasoning behind dropping --sample-ref/--consent-ref
            # below).
            output_dir = os.path.join(self.output_root, submission.id)
            os.makedirs(output_dir, exist_ok=True)

            # THE CLINICAL LINK (2026-09-12, D0). Before the engine runs:
            # resolve the submission to its organisation, order and sample,
            # run the Phase 5 preconditions, and ask whether this exact run
            # (same VCF bytes, same sample) is already recorded. A refusal
            # here marks the submission failed and returns; the engine never
            # starts for a run that could not be recorded.
            link = self._clinical_preflight(submission)
            if link is None:
                return True
            if self._adopt_existing_record(submission, link, output_dir):
                return True

            # Build the geper/main.py invocation. sys.executable, not a
            # bare command name -- see _DEFAULT_GEPER_MAIN_PATH.
            cmd = [
                sys.executable,
                self.geper_main_path,
                "--vcf",
                submission.vcf_path,
                "--assembly",
                submission.assembly,
                "--output-dir",
                output_dir,
            ]
            # --sample-ref / --consent-ref DELIBERATELY DROPPED (2026-09-08
            # ruling): geper/main.py's build_arg_parser() has never accepted
            # either flag. They exist on the Submission/API/DB so the
            # platform can correlate a submission without passing patient
            # identity to the CLI -- the platform already knows which
            # submission it started (submission.id, used for output_dir
            # above), so the CLI does not need them. The columns, the API
            # fields, and submission.sample_ref/consent_ref themselves are
            # UNCHANGED -- only the command-line invocation stops passing them.

            # Add optional parameters. Both of these can raise (ValueError
            # from the hpo conversion below, OSError from the qc sidecar
            # write) -- deliberately built INSIDE this try block (moved
            # here 2026-09-08) so any such failure is caught by this
            # function's own existing generic `except Exception` handler
            # below: "an exception via the same path you already use"
            # (2026-09-08 ruling), rather than a second, bespoke
            # error-handling block for this one precondition.
            if submission.hpo_terms:
                # CONVERT, DO NOT DEGRADE (2026-09-08 ruling, correcting
                # this file's own prior "not fixed in this change" note):
                # geper/main.py:175-189's --hpo-terms wants a
                # comma-separated "HP:#######" string, not JSON. Passing
                # the JSON blob through unconverted wouldn't crash --
                # geper/main.py's own malformed-ID handling
                # (geper/main.py:183-187) would silently drop it and
                # report PP4 as "not_evaluated" -- which is the actual
                # defect: "a submission with clinical phenotype data
                # produces an interpretation that ignored it, and nothing
                # says so... a wrong clinical answer delivered quietly."
                # So: convert for real (_hpo_terms_to_cli_arg), and if
                # conversion fails, this raises -- caught below,
                # submission marked failed with a clear reason, exception
                # created. A submission that can't pass its phenotype
                # data is a precondition failure, not a degraded run.
                #
                # NOT FIXED, FLAGGED INSTEAD (checked per the same
                # ruling): bridge/combined_pipeline.py:394/448-449 passes
                # its own `hpo_terms: Optional[str]` straight through to
                # --hpo-terms with zero conversion or validation --
                # whatever string its own caller
                # (bridge/run_combined.py:76-83, 112) hands it reaches
                # geper/main.py unchecked. That caller reads a text/JSON
                # FILE of HPO IDs (bridge/run_combined.py's own
                # --hpo-terms help text), not this submission's JSON
                # shape, so it isn't exposed to the SAME bug this fix
                # closes -- but geper/main.py:183-187's
                # silent-drop-and-warn behavior itself is still live and
                # unremoved (the CLI does not move, per every prior
                # ruling this phase) and still reachable by ANY caller
                # that hands it a malformed ID,
                # bridge/combined_pipeline.py included if its own caller
                # ever passes one. That underlying engine behavior is out
                # of this diff's scope -- reported here, not touched.
                cmd.extend(["--hpo-terms", _hpo_terms_to_cli_arg(submission.hpo_terms)])
            if submission.qc_metrics:
                # --qc-metrics-json (renamed from --qc-metrics, 2026-09-08
                # ruling) wants a PATH TO A JSON FILE
                # (geper/main.py:157-173's own help text), not inline
                # JSON -- confirmed by _parse_qc_metrics's file-open
                # branch (geper/report/clinical_report_builder.py:
                # 1519-1524, `with open(qc_metrics, "r")` when the value
                # isn't already a dict). bridge/combined_pipeline.py:452,
                # the reference implementation the human named for this
                # flag, never passes qc_metrics inline either -- it
                # writes a sidecar file first (write_qc_metrics_sidecar,
                # bridge/combined_pipeline.py:228-236) and passes that
                # path. Mirrored here (not reused directly --
                # that helper is shaped around a kim_pipeline checkpoint
                # dict, not this submission's already-final qc_metrics dict).
                #
                # CONVERT, DO NOT DEGRADE (2026-09-11 ruling, PHASE8-
                # bij-interpret option A, same pattern as hpo_terms
                # above): _qc_metrics_validated raises ValueError, caught
                # by this function's own existing generic `except
                # Exception` handler below, on any key it doesn't
                # recognize -- rather than writing the platform's raw
                # dict through unconverted, which is what let
                # {"depth": 50} reach `_parse_qc_metrics` as a silent
                # `.get()` miss and render a false "not reported" for a
                # value that was, in fact, reported.
                qc_metrics_path = os.path.join(output_dir, "qc_metrics.json")
                with open(qc_metrics_path, "w", encoding="utf-8") as fh:
                    json.dump(_qc_metrics_validated(submission.qc_metrics), fh)
                cmd.extend(["--qc-metrics-json", qc_metrics_path])

            logger.info(f"Executing: {' '.join(cmd)}")

            # Spawn process — must be tracked for cancellation support
            proc = spawn_tracked(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            try:
                stdout, stderr = proc.communicate(timeout=INTERPRETATION_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                # Process timed out — kill it and mark submission as failed
                kill_process_tree_now(proc)
                error_msg = f"Interpretation timed out after {INTERPRETATION_TIMEOUT_SECONDS}s"
                logger.warning(f"[{submission.id}] {error_msg}")
                self.store.update_status(
                    submission.id,
                    "failed",
                    error_message=error_msg,
                )
                # Create exception for timeout
                self._create_submission_failure_exception(
                    submission,
                    "bij_ai_timeout",
                    error_msg,
                )
                return True

            # Success/failure is the EXIT CODE (2026-09-08 ruling):
            # geper/main.py:206-281 returns 0 on success, 1 on
            # PipelineError, 130 on KeyboardInterrupt -- not a stdout
            # run_complete flag. main.py never prints JSON to stdout
            # (grepped main.py for run_complete/run_document/
            # print(json/json.dumps-as-output -- zero hits); its output
            # is files under --output-dir plus this exit code. stderr is
            # still surfaced in the error message for diagnosis, as before.
            if proc.returncode != 0:
                error_msg = f"{SHORT_NAME} returned exit code {proc.returncode}"
                if stderr:
                    error_msg += f": {stderr[:500]}"
                logger.error(f"[{submission.id}] {error_msg}")
                self.store.update_status(
                    submission.id,
                    "failed",
                    error_message=error_msg,
                )
                # Create exception for non-zero exit
                self._create_submission_failure_exception(
                    submission,
                    "bij_ai_error_other",
                    error_msg,
                )
                return True

            # The run document is a FILE geper/main.py already writes
            # (geper_results.json, geper/pipeline/orchestrator.py:712)
            # under the
            # --output-dir this call passed -- not a second, stdout-based
            # output path to keep consistent with the first (human's
            # reasoning, 2026-09-08 ruling: "a stdout JSON envelope is
            # the wrong thing to add"). Exit 0 without that file existing
            # is a contract violation worth surfacing distinctly rather
            # than crashing on open() below.
            results_path = os.path.join(output_dir, "geper_results.json")
            if not os.path.isfile(results_path):
                error_msg = f"{SHORT_NAME} exited 0 but did not write {results_path}"
                logger.error(f"[{submission.id}] {error_msg}")
                self.store.update_status(
                    submission.id,
                    "failed",
                    error_message=error_msg,
                )
                self._create_submission_failure_exception(
                    submission,
                    "bij_ai_error_other",
                    error_msg,
                )
                return True

            # interpretation_id: THE CLINICAL INTERPRETATION THIS RUN IS
            # RECORDED AS (2026-09-12, D0 "build the link"). This replaces
            # the uuid4 the 2026-09-08 ruling had this worker mint here --
            # that id named nothing in the clinical record, which is the
            # gap D0 closes. The 2026-09-08 ruling's actual point survives:
            # the id is genuinely distinct from submission.id (the API's
            # own response `id`), not a borrowed copy of it.
            #
            # run_document_ref stays the PATH to the file main.py wrote;
            # the clinical record holds the content.
            with open(results_path, "r", encoding="utf-8") as fh:
                run_document = json.load(fh)
            return self._record_run(submission, link, output_dir, results_path, run_document)

        except FileNotFoundError as e:
            # NOTE (2026-09-08): with cmd = [sys.executable, main_path,
            # ...], this branch no longer fires for "main.py doesn't
            # exist at that path" -- sys.executable itself always exists,
            # so Popen succeeds; Python then exits nonzero when it can't
            # open the script argument, which surfaces via the exit-code
            # branch above instead, with a less specific message. This
            # branch still covers spawn_tracked/subprocess machinery
            # itself being unavailable.
            error_msg = f"{SHORT_NAME} CLI not found: {e}"
            logger.error(f"[{submission.id}] {error_msg}")
            self.store.update_status(
                submission.id,
                "failed",
                error_message=error_msg,
            )
            # Create exception for missing CLI
            self._create_submission_failure_exception(
                submission,
                "bij_ai_error_other",
                error_msg,
            )
            return True
        except Exception as e:
            error_msg = f"Unexpected error: {type(e).__name__}: {e}"
            logger.error(f"[{submission.id}] {error_msg}")
            self.store.update_status(
                submission.id,
                "failed",
                error_message=error_msg,
            )
            # Create exception for unexpected error
            self._create_submission_failure_exception(
                submission,
                "bij_ai_error_other",
                error_msg,
            )
            return True

    def run_loop(self, poll_interval: int = 5, batch_size: int = 10) -> None:
        """Run the main worker loop.

        Args:
            poll_interval: Seconds between polls for queued submissions
            batch_size: Max submissions to process per poll
        """
        logger.info("Starting interpretation worker loop")

        # Before taking new work: complete any submission a dead worker left
        # interrupted AFTER its clinical record was already written.
        try:
            reconciled = self.reconcile_interrupted_submissions()
            if reconciled:
                logger.warning(
                    f"Startup reconciliation: {reconciled} interrupted submission(s) completed from the clinical record"
                )
        except Exception as e:
            logger.exception(f"Startup reconciliation failed: {e}")

        while True:
            try:
                queued = self.store.get_queued_submissions(limit=batch_size)
                if not queued:
                    logger.debug(f"No queued submissions (sleeping {poll_interval}s)")
                    time.sleep(poll_interval)
                    continue

                logger.info(f"Processing {len(queued)} queued submission(s)")
                for submission in queued:
                    self.process_queued_submission(submission)

            except KeyboardInterrupt:
                logger.info("Worker interrupted by user")
                break
            except Exception as e:
                logger.exception(f"Worker loop error: {e}")
                time.sleep(poll_interval)


def main():
    """Entry point for submission worker process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Get store path from env var or default
    store_path = os.getenv(
        "GEPER_SUBMISSION_STORE_PATH",
        str(Path(__file__).parent / ".submissions.db"),
    )
    store = SubmissionStore(store_path)

    # Initialize DataAccess: the clinical record every completed run is written
    # into (D0), and the exceptions every failure raises. NOT optional -- see below.
    #
    # 2026-09-08 (F4), fail-closed. This block used to log a warning and carry
    # on when CLINICAL_DSN was unset or the connection failed, leaving
    # data_access=None. The worker then ran normally, processed submissions,
    # and silently created no clinical exception records at all: the clinical
    # trail simply absent, with one startup log line as the only evidence.
    # The adjacent exception_retry_worker.py refused to start in exactly that
    # situation -- one failure, two opposite policies, chosen by nobody. The
    # refusing policy wins, in both files.
    #
    # Raising the log level would not have been a fix: a warning nobody reads
    # and an error nobody reads fail identically. The control has to be that
    # the worker does not proceed.
    #
    # Deliberately NO opt-out env var here (contrast GEPER_DEV_INSECURE in
    # api/main.py). exception_retry_worker.py has never had one, and adding a
    # bypass to only this worker would re-open the very divergence this change
    # closes. A supported no-clinical-database mode should be an explicit
    # decision applied to BOTH workers, not a flag that grows on one of them.
    dsn = os.getenv("CLINICAL_DSN")
    if not dsn:
        sys.stderr.write(
            "ERROR: refusing to start -- CLINICAL_DSN is not set. Without it this "
            "worker would process submissions normally and silently create no "
            "clinical exception records at all. Set CLINICAL_DSN to the clinical "
            "database DSN.\n"
        )
        sys.exit(1)

    try:
        import psycopg

        connection = psycopg.connect(dsn, autocommit=False)
        from clinical.data_access import DataAccess

        data_access = DataAccess(connection)
        logger.info("DataAccess initialized for clinical records and exceptions")
    except Exception as e:
        # Second route to the same silent-nothing state: with CLINICAL_DSN set
        # but the database unreachable (or psycopg missing), this handler used
        # to downgrade a genuine connection failure to a warning and carry on.
        # Fixing only the missing-DSN branch above would have left this route
        # open one step later.
        sys.stderr.write(
            "ERROR: refusing to start -- CLINICAL_DSN is set but the clinical "
            f"DataAccess could not be initialized: {e}\n"
        )
        sys.exit(1)

    worker = InterpretationWorker(store, data_access)
    worker.run_loop()


if __name__ == "__main__":
    main()
