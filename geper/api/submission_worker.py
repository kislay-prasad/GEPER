"""
geper/api/submission_worker.py
──────────────────────────────
Background worker for Bij AI interpretation submissions.

Polls submissions with status='queued', invokes Bij AI CLI via spawn_tracked
(so interpretations are killable), handles timeouts, and updates submission status.
On failure, creates exceptions in the clinical database to notify lab operators.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path

from shared.process_control import spawn_tracked, kill_process_tree_now

from .submission_store import SubmissionStore

logger = logging.getLogger("geper.api.submission_worker")

# Timeout for a single interpretation run (30 minutes)
INTERPRETATION_TIMEOUT_SECONDS = 1800


class InterpretationWorker:
    """Poll and process Bij AI interpretation submissions."""

    def __init__(self, store: SubmissionStore, data_access=None):
        self.store = store
        self.data_access = data_access
        self.bij_ai_cli = os.getenv("BIJ_AI_CLI_PATH", "bij-interpret")

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

        # Build Bij AI CLI command
        cmd = [
            self.bij_ai_cli,
            "--vcf",
            submission.vcf_path,
            "--assembly",
            submission.assembly,
            "--sample-ref",
            submission.sample_ref,
            "--consent-ref",
            submission.consent_ref,
        ]

        # Add optional parameters
        if submission.hpo_terms:
            cmd.extend(["--hpo-terms", json.dumps(submission.hpo_terms)])
        if submission.qc_metrics:
            cmd.extend(["--qc-metrics", json.dumps(submission.qc_metrics)])

        logger.info(f"Executing: {' '.join(cmd)}")

        try:
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

            # Check return code
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

            # Process succeeded — parse output for interpretation details
            # Bij AI CLI outputs a JSON document with run_complete flag
            try:
                result = json.loads(stdout)
            except json.JSONDecodeError as e:
                error_msg = f"Failed to parse Bij AI output: {e}"
                logger.error(f"[{submission.id}] {error_msg}")
                self.store.update_status(
                    submission.id,
                    "failed",
                    error_message=error_msg,
                )
                # Create exception for parse error
                self._create_submission_failure_exception(
                    submission,
                    "bij_ai_error_other",
                    error_msg,
                )
                return True

            # Check run_complete flag — not file presence
            if not result.get("run_complete"):
                error_msg = "Interpretation did not complete (run_complete=false)"
                logger.warning(f"[{submission.id}] {error_msg}")
                self.store.update_status(
                    submission.id,
                    "failed",
                    error_message=error_msg,
                )
                # Create exception for incomplete interpretation
                self._create_submission_failure_exception(
                    submission,
                    "bij_ai_error_other",
                    error_msg,
                )
                return True

            # Extract interpretation ID and run document reference
            interpretation_id = result.get("interpretation_id")
            run_document_ref = json.dumps(result.get("run_document", {}))

            logger.info(f"[{submission.id}] Interpretation complete: {interpretation_id}")
            self.store.update_status(
                submission.id,
                "complete",
                interpretation_id=interpretation_id,
                run_document_ref=run_document_ref,
            )
            return True

        except FileNotFoundError as e:
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
