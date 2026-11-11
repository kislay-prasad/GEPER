"""
geper/api/exception_retry_worker.py
───────────────────────────────────

Background worker for retrying transient submission failures.

Polls clinical database for exceptions with next_retry_at <= now and:
- Re-attempts failed submissions via exponential backoff
- Escalates to manual review after 6 failed attempts (15 minutes total)
- Re-queues associated submissions for reinterpretation by GEPER

This worker runs independently of the submission_worker. Submission failures
trigger exceptions in the clinical database; this worker manages their retry
lifecycle.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

logger = logging.getLogger("geper.api.exception_retry_worker")

# Consecutive passes with the clinical database unreachable before the loop gives up (the process then exits 1).
_DB_LOSS_LIMIT = 3


class ExceptionRetryWorker:
    """Poll and retry transient submission failures."""

    def __init__(self, data_access, submission_store=None):
        """
        Initialize the exception retry worker.

        Args:
            data_access: DataAccess instance for clinical database
            submission_store: Optional SubmissionStore for re-queueing submissions
        """
        self.data_access = data_access
        self.submission_store = submission_store
        self._stop = threading.Event()  # set by a stop signal: the loop ends after the pass it is on
        self.exit_code = 0  # 1 when the loop ended because the clinical database was lost
        self.heartbeat = None  # called on every pass so a supervisor can tell a live loop from a stuck process

    def request_stop(self) -> None:
        self._stop.set()

    def run_loop(self, poll_interval: int = 30) -> None:
        """Run the main worker loop.

        Polls for retryable exceptions every poll_interval seconds.

        Args:
            poll_interval: Seconds between polls (default: 30s)
        """
        logger.info("Starting exception retry worker loop")

        lost = 0
        while not self._stop.is_set():
            # No beat while the clinical database is unreachable, and after _DB_LOSS_LIMIT passes in a row the loop
            # ends non-zero so the supervisor starts a process that reconnects (see submission_worker.run_loop).
            if not self.data_access.ping():
                lost += 1
                logger.error(f"Clinical database unreachable ({lost}/{_DB_LOSS_LIMIT})")
                if lost >= _DB_LOSS_LIMIT:
                    self.exit_code = 1
                    break
                self._stop.wait(poll_interval)
                continue
            lost = 0
            if self.heartbeat is not None:
                self.heartbeat()
            try:
                # Run retry scheduler for all organisations
                # Note: In a multi-tenant deployment, this should iterate over
                # organisations or use a system-level session
                self._process_all_orgs()

                self._stop.wait(poll_interval)

            except KeyboardInterrupt:
                logger.info("Worker interrupted by user")
                break
            except Exception as e:
                logger.exception(f"Worker loop error: {e}")
                self._stop.wait(poll_interval)
        logger.info("Exception retry worker stopped")

    def _process_all_orgs(self) -> None:
        """Process retryable exceptions for all organisations.

        Iterates each organisation, creates a system session, and runs the retry scheduler.
        """

        # Query all organisations
        try:
            # (This used to read `data_access._conn` and a column called `id`; neither exists, so the query failed on
            # every pass, was logged, and the whole retry lifecycle -- retries, escalation, recovery -- never ran.)
            orgs = self.data_access.list_active_organisation_ids()
        except Exception as e:
            logger.error(f"Failed to list organisations: {e}")
            return

        if not orgs:
            logger.debug("No organisations found")
            return

        # Process each organisation
        for org_id in orgs:
            try:
                # Create system session for this organisation
                system_session = self.data_access._create_system_session(org_id)

                # Run retry scheduler for this organisation
                self.data_access.retry_scheduler(system_session, self.submission_store)
                # And act on any failure an operator has resolved with retry_check since the last pass.
                self.data_access.requeue_resolved_exceptions(system_session, self.submission_store)

                logger.debug(f"Processed retry queue for org {org_id}")
            except Exception as e:
                logger.error(f"Error processing org {org_id}: {e}")


def main():
    """Entry point for exception retry worker process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Refuse to start on bad configuration before connecting to anything (api/service_health.py).
    from .service_health import install_stop_handlers, make_heartbeat, require_valid

    require_valid("retry_worker")

    # Initialize DataAccess from environment
    data_access = None
    submission_store = None

    try:
        dsn = os.getenv("CLINICAL_DSN")
        if not dsn:
            # 2026-09-08 (F4): this already refused to run the worker, but
            # returning from main() exits 0 -- which a process supervisor
            # reads as "the job finished successfully", so the retry lifecycle
            # stops dead with nothing restarting it and nothing alerting. The
            # same silent-nothing shape, one layer up. Exit non-zero so the
            # refusal is visible to whatever supervises the process, matching
            # the posture in api/main.py.
            sys.stderr.write(
                "ERROR: refusing to start -- CLINICAL_DSN is not set. Set it to the clinical database DSN.\n"
            )
            sys.exit(1)

        import psycopg

        connection = psycopg.connect(dsn, autocommit=False, connect_timeout=10)
        from clinical.data_access import DataAccess

        data_access = DataAccess(connection)
        logger.info("DataAccess initialized")

        # Initialize submission store if path is provided
        store_path = os.getenv(
            "GEPER_SUBMISSION_STORE_PATH",
            str(Path(__file__).parent / ".submissions.db"),
        )
        from .submission_store import SubmissionStore

        submission_store = SubmissionStore(
            store_path, reconcile_on_open=False
        )  # only the interpretation worker reconciles
        logger.info("SubmissionStore initialized")

    except Exception as e:
        # As above: returning here reported a failed startup as a clean run.
        # (SystemExit raised by the CLINICAL_DSN refusal above is a
        # BaseException and deliberately passes through this handler.)
        logger.error(f"Failed to initialize: {e}; exiting")
        sys.stderr.write(f"ERROR: refusing to start -- initialization failed: {e}\n")
        sys.exit(1)

    # Run the worker
    worker = ExceptionRetryWorker(data_access, submission_store)
    worker.heartbeat = make_heartbeat("retry_worker")
    install_stop_handlers(worker)
    poll_interval = int(os.getenv("EXCEPTION_RETRY_POLL_INTERVAL", "30"))
    worker.run_loop(poll_interval=poll_interval)
    if worker.exit_code:
        sys.exit(worker.exit_code)


if __name__ == "__main__":
    main()
