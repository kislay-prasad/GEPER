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

    def test_sample_id_round_trips(self, db_path):
        """The clinical sample a submission interprets (2026-09-12, D0) is
        stored and read back on every read path, including the idempotent
        return of an existing row."""
        store = SubmissionStore(db_path)
        sub = store.create_submission(
            org_id="aaaaaaaa-0000-4000-8000-000000000001",
            submission_key="k",
            vcf_path="/v",
            assembly="hg38",
            sample_ref="s",
            consent_ref="c",
            order_id="0d0d0d0d-0000-4000-8000-000000000003",
            sample_id="5a5a5a5a-0000-4000-8000-000000000004",
        )
        assert sub.sample_id == "5a5a5a5a-0000-4000-8000-000000000004"
        assert store.get_submission(sub.id).sample_id == sub.sample_id
        assert store.get_queued_submissions()[0].sample_id == sub.sample_id
        again = store.create_submission(
            org_id=sub.org_id, submission_key="k", vcf_path="/v", assembly="hg38", sample_ref="s", consent_ref="c"
        )
        assert again.id == sub.id and again.sample_id == sub.sample_id

    def test_store_file_from_before_sample_id_is_migrated_in_place(self, db_path):
        """A store file written before the column existed gains it on open;
        its old rows read back with sample_id None (the worker refuses those
        as not linked to a clinical record) and nothing else changes."""
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                CREATE TABLE submissions (
                    id TEXT PRIMARY KEY, org_id TEXT NOT NULL, order_id TEXT,
                    submission_key TEXT NOT NULL, vcf_path TEXT NOT NULL,
                    assembly TEXT NOT NULL, sample_ref TEXT NOT NULL,
                    consent_ref TEXT NOT NULL, status TEXT NOT NULL,
                    interpretation_id TEXT, run_document_ref TEXT, error_message TEXT,
                    hpo_terms TEXT, qc_metrics TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, UNIQUE(org_id, submission_key)
                )
                """
            )
            conn.execute(
                "INSERT INTO submissions (id, org_id, submission_key, vcf_path, assembly, sample_ref, "
                "consent_ref, status, created_at, updated_at) VALUES "
                "('old-1', 'default', 'k-old', '/v', 'hg38', 's', 'c', 'complete', 't', 't')"
            )
            conn.commit()

        store = SubmissionStore(db_path)
        old = store.get_submission("old-1")
        assert old.status == "complete"
        assert old.sample_id is None
