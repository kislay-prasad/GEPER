"""
geper/api/submission_store.py
────────────────────────────
SQLite-backed store for GEPER interpretation submissions.

Enforces idempotency via UNIQUE(org_id, submission_key) constraint.
Marks submissions interrupted at startup if worker crashed mid-execution.
Persists on all paths (queued, running, complete, failed).
No in-memory cache — load from store at startup or don't cache.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("geper.api.submission_store")


class Submission:
    """Represents one interpretation submission."""

    def __init__(
        self,
        id: str,
        org_id: str,
        submission_key: str,
        vcf_path: str,
        assembly: str,
        sample_ref: str,
        consent_ref: str,
        status: str,
        order_id: Optional[str] = None,
        interpretation_id: Optional[str] = None,
        run_document_ref: Optional[str] = None,
        error_message: Optional[str] = None,
        hpo_terms: Optional[dict] = None,
        qc_metrics: Optional[dict] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ):
        self.id = id
        self.org_id = org_id
        self.submission_key = submission_key
        self.vcf_path = vcf_path
        self.assembly = assembly
        self.sample_ref = sample_ref
        self.consent_ref = consent_ref
        self.status = status
        self.order_id = order_id
        self.interpretation_id = interpretation_id
        self.run_document_ref = run_document_ref
        self.error_message = error_message
        self.hpo_terms = hpo_terms or {}
        self.qc_metrics = qc_metrics or {}
        self.created_at = created_at or datetime.utcnow().isoformat()
        self.updated_at = updated_at or datetime.utcnow().isoformat()


class SubmissionStore:
    """SQLite-backed store for GEPER interpretation submissions.

    Design properties:
    - Idempotency: UNIQUE(org_id, submission_key) enforced at database level
    - Persistence: All paths persist (queued, running, complete, failed)
    - Startup reconciliation: rows with status='running' marked 'interrupted'
      with reason; this is only safe at startup before workers can exist
    - No in-memory cache divergence: load from store at startup, don't cache
    """

    def __init__(self, db_path: Path | str):
        """Initialize the submission store.

        Args:
            db_path: Path to SQLite database file (created if not exists)
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._reconcile_interrupted()

    def _init_schema(self) -> None:
        """Create the submissions table if it doesn't exist."""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS submissions (
                    id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    order_id TEXT,
                    submission_key TEXT NOT NULL,
                    vcf_path TEXT NOT NULL,
                    assembly TEXT NOT NULL,
                    sample_ref TEXT NOT NULL,
                    consent_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    interpretation_id TEXT,
                    run_document_ref TEXT,
                    error_message TEXT,
                    hpo_terms TEXT,
                    qc_metrics TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(org_id, submission_key)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_org_submission ON submissions(org_id, submission_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON submissions(status)")
            conn.commit()

    def _reconcile_interrupted(self) -> None:
        """Mark any rows with status='running' as 'interrupted' at startup.

        This is only safe at startup before any worker processes can exist.
        A process that died underneath an interpretation didn't fail — nobody
        knows what it reached or whether output is valid. Mark as interrupted
        with reason naming the restart, not as failed.
        """
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            now = datetime.utcnow().isoformat()
            conn.execute(
                """
                UPDATE submissions
                SET status = 'interrupted',
                    error_message = 'Worker died mid-execution at startup reconciliation',
                    updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
            affected = conn.total_changes
            conn.commit()

        if affected > 0:
            logger.warning(f"Startup reconciliation: marked {affected} submission(s) as interrupted")

    def create_submission(
        self,
        org_id: str,
        submission_key: str,
        vcf_path: str,
        assembly: str,
        sample_ref: str,
        consent_ref: str,
        order_id: Optional[str] = None,
        hpo_terms: Optional[dict] = None,
        qc_metrics: Optional[dict] = None,
    ) -> Submission:
        """Create a new submission or return existing if submission_key is known.

        Idempotency enforced by UNIQUE(org_id, submission_key) constraint.

        Returns:
            Submission object (new with status='queued' or existing)

        Raises:
            sqlite3.IntegrityError: Should not happen (caught by constraint)
        """
        submission_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            # Check if submission_key already exists
            cursor = conn.execute(
                """
                SELECT id, order_id, status, interpretation_id, error_message
                FROM submissions
                WHERE org_id = ? AND submission_key = ?
                """,
                (org_id, submission_key),
            )
            existing = cursor.fetchone()
            if existing:
                existing_id, existing_order_id, status, interp_id, error_msg = existing
                return Submission(
                    id=existing_id,
                    org_id=org_id,
                    submission_key=submission_key,
                    vcf_path=vcf_path,
                    assembly=assembly,
                    sample_ref=sample_ref,
                    consent_ref=consent_ref,
                    status=status,
                    order_id=existing_order_id,
                    interpretation_id=interp_id,
                    error_message=error_msg,
                    hpo_terms=hpo_terms,
                    qc_metrics=qc_metrics,
                )

            # Insert new submission
            conn.execute(
                """
                INSERT INTO submissions (
                    id, org_id, order_id, submission_key, vcf_path, assembly, sample_ref,
                    consent_ref, status, hpo_terms, qc_metrics, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    org_id,
                    order_id,
                    submission_key,
                    vcf_path,
                    assembly,
                    sample_ref,
                    consent_ref,
                    "queued",
                    json.dumps(hpo_terms or {}),
                    json.dumps(qc_metrics or {}),
                    now,
                    now,
                ),
            )
            conn.commit()

        return Submission(
            id=submission_id,
            org_id=org_id,
            submission_key=submission_key,
            vcf_path=vcf_path,
            assembly=assembly,
            sample_ref=sample_ref,
            consent_ref=consent_ref,
            status="queued",
            order_id=order_id,
            hpo_terms=hpo_terms,
            qc_metrics=qc_metrics,
            created_at=now,
            updated_at=now,
        )

    def get_submission(self, submission_id: str) -> Optional[Submission]:
        """Get a submission by ID."""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            cursor = conn.execute(
                """
                SELECT id, org_id, order_id, submission_key, vcf_path, assembly, sample_ref,
                       consent_ref, status, interpretation_id, run_document_ref,
                       error_message, hpo_terms, qc_metrics, created_at, updated_at
                FROM submissions
                WHERE id = ?
                """,
                (submission_id,),
            )
            row = cursor.fetchone()

        if not row:
            return None

        return Submission(
            id=row[0],
            org_id=row[1],
            order_id=row[2],
            submission_key=row[3],
            vcf_path=row[4],
            assembly=row[5],
            sample_ref=row[6],
            consent_ref=row[7],
            status=row[8],
            interpretation_id=row[9],
            run_document_ref=row[10],
            error_message=row[11],
            hpo_terms=json.loads(row[12]) if row[12] else {},
            qc_metrics=json.loads(row[13]) if row[13] else {},
            created_at=row[14],
            updated_at=row[15],
        )

    def get_queued_submissions(self, limit: int = 10) -> list[Submission]:
        """Get submissions with status='queued' for worker to process."""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            cursor = conn.execute(
                """
                SELECT id, org_id, order_id, submission_key, vcf_path, assembly, sample_ref,
                       consent_ref, status, interpretation_id, run_document_ref,
                       error_message, hpo_terms, qc_metrics, created_at, updated_at
                FROM submissions
                WHERE status = 'queued'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cursor.fetchall()

        submissions = []
        for row in rows:
            submissions.append(
                Submission(
                    id=row[0],
                    org_id=row[1],
                    order_id=row[2],
                    submission_key=row[3],
                    vcf_path=row[4],
                    assembly=row[5],
                    sample_ref=row[6],
                    consent_ref=row[7],
                    status=row[8],
                    interpretation_id=row[9],
                    run_document_ref=row[10],
                    error_message=row[11],
                    hpo_terms=json.loads(row[12]) if row[12] else {},
                    qc_metrics=json.loads(row[13]) if row[13] else {},
                    created_at=row[14],
                    updated_at=row[15],
                )
            )
        return submissions

    def update_status(
        self,
        submission_id: str,
        status: str,
        interpretation_id: Optional[str] = None,
        run_document_ref: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Update submission status and related fields."""
        now = datetime.utcnow().isoformat()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE submissions
                SET status = ?,
                    interpretation_id = ?,
                    run_document_ref = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, interpretation_id, run_document_ref, error_message, now, submission_id),
            )
            conn.commit()
