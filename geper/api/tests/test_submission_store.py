"""
tests/test_submission_store.py
──────────────────────────────
Unit and integration tests for SubmissionStore.
"""

import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

from api.submission_store import SubmissionStore


class TestSubmissionStore:
    """Tests for SQLite submission store."""

    @pytest.fixture
    def db_path(self):
        """Create a temporary database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "test.db"
            yield db

    def test_init_creates_schema(self, db_path):
        """Verify schema is created on init."""
        SubmissionStore(db_path)
        assert db_path.exists()

        # Verify table exists
        with closing(sqlite3.connect(db_path)) as conn:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='submissions'")
            assert cursor.fetchone() is not None

    def test_unique_constraint_enforced(self, db_path):
        """UNIQUE(org_id, submission_key) prevents duplicate keys."""
        store = SubmissionStore(db_path)

        # Create first submission
        sub1 = store.create_submission(
            org_id="org-1",
            submission_key="key-1",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )
        assert sub1.id is not None
        assert sub1.status == "queued"

        # Create second submission with same org+key — should return existing
        sub2 = store.create_submission(
            org_id="org-1",
            submission_key="key-1",
            vcf_path="/path/to/vcf2",
            assembly="hg38",
            sample_ref="sample-2",
            consent_ref="consent-2",
        )
        assert sub2.id == sub1.id
        assert sub2.status == "queued"

    def test_create_submission_with_metadata(self, db_path):
        """Create submission with optional hpo_terms and qc_metrics."""
        store = SubmissionStore(db_path)

        hpo = {"terms": ["HP:0001234", "HP:0005678"]}
        qc = {"depth": 50, "quality": 85}

        sub = store.create_submission(
            org_id="org-1",
            submission_key="key-1",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
            hpo_terms=hpo,
            qc_metrics=qc,
        )

        assert sub.hpo_terms == hpo
        assert sub.qc_metrics == qc

    def test_get_submission(self, db_path):
        """Retrieve submission by ID."""
        store = SubmissionStore(db_path)

        sub1 = store.create_submission(
            org_id="org-1",
            submission_key="key-1",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        sub2 = store.get_submission(sub1.id)
        assert sub2 is not None
        assert sub2.id == sub1.id
        assert sub2.status == "queued"
        assert sub2.org_id == "org-1"

    def test_get_submission_nonexistent(self, db_path):
        """Get nonexistent submission returns None."""
        store = SubmissionStore(db_path)
        assert store.get_submission("nonexistent-id") is None

    def test_update_status_queued_to_running(self, db_path):
        """Update status from queued to running."""
        store = SubmissionStore(db_path)

        sub = store.create_submission(
            org_id="org-1",
            submission_key="key-1",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )
        assert sub.status == "queued"

        store.update_status(sub.id, "running")

        updated = store.get_submission(sub.id)
        assert updated.status == "running"

    def test_update_status_running_to_complete(self, db_path):
        """Update status from running to complete with interpretation_id."""
        store = SubmissionStore(db_path)

        sub = store.create_submission(
            org_id="org-1",
            submission_key="key-1",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        interp_id = "interp-uuid-123"
        run_doc_ref = '{"document_id": "doc-456"}'

        store.update_status(
            sub.id,
            "complete",
            interpretation_id=interp_id,
            run_document_ref=run_doc_ref,
        )

        updated = store.get_submission(sub.id)
        assert updated.status == "complete"
        assert updated.interpretation_id == interp_id
        assert updated.run_document_ref == run_doc_ref

    def test_update_status_failed_with_error(self, db_path):
        """Update status to failed with error message."""
        store = SubmissionStore(db_path)

        sub = store.create_submission(
            org_id="org-1",
            submission_key="key-1",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        error = "Interpretation timed out after 1800s"
        store.update_status(sub.id, "failed", error_message=error)

        updated = store.get_submission(sub.id)
        assert updated.status == "failed"
        assert updated.error_message == error

    def test_get_queued_submissions(self, db_path):
        """Retrieve queued submissions in creation order."""
        store = SubmissionStore(db_path)

        # Create 3 submissions
        sub1 = store.create_submission(
            org_id="org-1",
            submission_key="key-1",
            vcf_path="/path/1",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )
        sub2 = store.create_submission(
            org_id="org-1",
            submission_key="key-2",
            vcf_path="/path/2",
            assembly="hg38",
            sample_ref="sample-2",
            consent_ref="consent-2",
        )
        sub3 = store.create_submission(
            org_id="org-1",
            submission_key="key-3",
            vcf_path="/path/3",
            assembly="hg38",
            sample_ref="sample-3",
            consent_ref="consent-3",
        )

        # Mark second as running (should not appear in queued)
        store.update_status(sub2.id, "running")

        queued = store.get_queued_submissions()
        assert len(queued) == 2
        assert queued[0].id == sub1.id
        assert queued[1].id == sub3.id

    def test_startup_reconciliation_marks_running_as_interrupted(self, db_path):
        """Startup reconciliation marks status='running' as 'interrupted'."""
        # Create store and insert a submission with status='running'
        store1 = SubmissionStore(db_path)
        sub = store1.create_submission(
            org_id="org-1",
            submission_key="key-1",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        # Manually update to running (bypass normal flow)
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "UPDATE submissions SET status = 'running' WHERE id = ?",
                (sub.id,),
            )
            conn.commit()

        # Create new store instance — should trigger reconciliation
        store2 = SubmissionStore(db_path)

        updated = store2.get_submission(sub.id)
        assert updated.status == "interrupted"
        assert updated.error_message == "Worker died mid-execution at startup reconciliation"

    def test_submission_timestamps_recorded(self, db_path):
        """Submission timestamps are created and updated."""
        store = SubmissionStore(db_path)

        sub = store.create_submission(
            org_id="org-1",
            submission_key="key-1",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        assert sub.created_at is not None
        assert sub.updated_at is not None
        assert isinstance(sub.created_at, str)
        assert isinstance(sub.updated_at, str)

        # Updated should change on status update
        original_updated = sub.updated_at
        store.update_status(sub.id, "running")

        updated = store.get_submission(sub.id)
        assert updated.updated_at >= original_updated
