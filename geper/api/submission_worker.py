"""
geper/api/submission_worker.py
──────────────────────────────
Background worker for Bij AI interpretation submissions.

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
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from pipeline.hpo.utils import is_well_formed_hpo_id
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


class InterpretationWorker:
    """Poll and process Bij AI interpretation submissions."""

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
                qc_metrics_path = os.path.join(output_dir, "qc_metrics.json")
                with open(qc_metrics_path, "w", encoding="utf-8") as fh:
                    json.dump(submission.qc_metrics, fh)
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
                error_msg = f"Bij AI returned exit code {proc.returncode}"
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
                error_msg = f"Bij AI exited 0 but did not write {results_path}"
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

            # interpretation_id: THE PLATFORM MINTS IT (2026-09-08 ruling,
            # correcting this file's own prior "left None" note -- that
            # was right while unresolved and is now resolved the other
            # way). geper/main.py's real output (geper_results.json) has
            # no run-id/interpretation-id concept anywhere -- confirmed
            # again: grepped geper/pipeline/orchestrator.py and
            # geper/report/json_builder.py, zero hits -- so this was
            # never the engine's to supply, and
            # the ruling is explicit that it also isn't submission.id
            # reused ("generate one rather than leaving the column empty
            # or borrowing the response id" -- that would make this
            # column redundant with the API's own response `id` field,
            # the objection this file raised and which the ruling
            # answered by minting a genuinely distinct id, not by
            # dropping the column). Minted here, at completion, the same
            # way geper/api/submission_store.py:165 mints submission.id
            # itself (uuid.uuid4()) -- this worker is "the platform" at
            # the one layer it actually touches; wiring this to
            # clinical/data_access.py::create_interpretation() (the
            # OTHER, Postgres-side "platform creates the interpretations
            # row" -- clinical/schema.sql:438-440,
            # clinical/data_access.py:1703-1773) would need a vcf_id and
            # an authenticated Session
            # this SQLite-backed submission has neither of; that's a real
            # architecture decision, out of this diff's scope, and not
            # what was ruled on.
            interpretation_id = str(uuid.uuid4())
            # run_document_ref: the PATH to the file main.py wrote, not a
            # re-serialization of its content -- "the run document IS
            # ALREADY A FILE the platform stores" (human's ruling).
            run_document_ref = results_path

            logger.info(f"[{submission.id}] Interpretation complete: {results_path}")
            self.store.update_status(
                submission.id,
                "complete",
                interpretation_id=interpretation_id,
                run_document_ref=run_document_ref,
            )
            return True

        except FileNotFoundError as e:
            # NOTE (2026-09-08): with cmd = [sys.executable, main_path,
            # ...], this branch no longer fires for "main.py doesn't
            # exist at that path" -- sys.executable itself always exists,
            # so Popen succeeds; Python then exits nonzero when it can't
            # open the script argument, which surfaces via the exit-code
            # branch above instead, with a less specific message. This
            # branch still covers spawn_tracked/subprocess machinery
            # itself being unavailable.
            error_msg = f"Bij AI CLI not found: {e}"
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

    # Initialize DataAccess for exception creation (optional; worker works without it)
    data_access = None
    try:
        dsn = os.getenv("CLINICAL_DSN")
        if dsn:
            import psycopg

            connection = psycopg.connect(dsn, autocommit=False)
            from clinical.data_access import DataAccess

            data_access = DataAccess(connection)
            logger.info("DataAccess initialized for exception creation")
        else:
            logger.warning("CLINICAL_DSN not set; exceptions will not be created")
    except Exception as e:
        logger.warning(f"Failed to initialize DataAccess: {e}; continuing without exception creation")

    worker = InterpretationWorker(store, data_access)
    worker.run_loop()


if __name__ == "__main__":
    main()
