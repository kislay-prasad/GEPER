"""
tests/test_submission_worker.py
───────────────────────────────
Unit tests for InterpretationWorker.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.submission_store import SubmissionStore
from api.submission_worker import InterpretationWorker


@pytest.fixture
def db_path():
    """Create a temporary database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        yield db


@pytest.fixture
def store(db_path):
    """Create a test SubmissionStore."""
    return SubmissionStore(db_path)


@pytest.fixture
def worker(store):
    """Create a test InterpretationWorker."""
    return InterpretationWorker(store)


class TestInterpretationWorker:
    """Tests for InterpretationWorker."""

    def test_process_queued_submission_success(self, worker, store):
        """Worker processes queued submission and marks complete."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-1",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        # Mock Bij AI CLI output
        bij_output = json.dumps(
            {
                "run_complete": True,
                "interpretation_id": "interp-uuid-1",
                "run_document": {"id": "doc-1", "status": "complete"},
            }
        )

        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = (bij_output, "")
            mock_proc.returncode = 0
            mock_spawn.return_value = mock_proc

            result = worker.process_queued_submission(sub)

        assert result is True

        # Verify submission was marked complete
        updated = store.get_submission(sub.id)
        assert updated.status == "complete"
        assert updated.interpretation_id == "interp-uuid-1"

    def test_process_queued_submission_timeout(self, worker, store):
        """Worker handles Bij AI CLI timeout."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-2",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            with patch("api.submission_worker.kill_process_tree_now") as mock_kill:
                mock_proc = MagicMock()
                mock_proc.communicate.side_effect = subprocess.TimeoutExpired("cmd", 1800)
                mock_spawn.return_value = mock_proc

                result = worker.process_queued_submission(sub)

        assert result is True
        mock_kill.assert_called_once()

        # Verify submission was marked failed with timeout reason
        updated = store.get_submission(sub.id)
        assert updated.status == "failed"
        assert "timed out" in updated.error_message.lower()

    def test_process_queued_submission_nonzero_exit(self, worker, store):
        """Worker handles nonzero exit code from Bij AI CLI."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-3",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = ("", "Error message from CLI")
            mock_proc.returncode = 1
            mock_spawn.return_value = mock_proc

            result = worker.process_queued_submission(sub)

        assert result is True

        # Verify submission was marked failed
        updated = store.get_submission(sub.id)
        assert updated.status == "failed"
        assert "exit code 1" in updated.error_message

    def test_process_queued_submission_run_not_complete(self, worker, store):
        """Worker marks failed if run_complete is false."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-4",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        bij_output = json.dumps(
            {
                "run_complete": False,
                "interpretation_id": None,
            }
        )

        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = (bij_output, "")
            mock_proc.returncode = 0
            mock_spawn.return_value = mock_proc

            result = worker.process_queued_submission(sub)

        assert result is True

        # Verify submission was marked failed (not file presence-based)
        updated = store.get_submission(sub.id)
        assert updated.status == "failed"
        assert "run_complete=false" in updated.error_message

    def test_process_queued_submission_invalid_json(self, worker, store):
        """Worker handles invalid JSON output from Bij AI CLI."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-5",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = ("not valid json", "")
            mock_proc.returncode = 0
            mock_spawn.return_value = mock_proc

            result = worker.process_queued_submission(sub)

        assert result is True

        # Verify submission was marked failed
        updated = store.get_submission(sub.id)
        assert updated.status == "failed"
        assert "parse" in updated.error_message.lower()

    def test_process_queued_submission_cli_not_found(self, worker, store):
        """Worker handles Bij AI CLI not being installed."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-6",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            mock_spawn.side_effect = FileNotFoundError("bij-interpret not found")

            result = worker.process_queued_submission(sub)

        assert result is True

        # Verify submission was marked failed
        updated = store.get_submission(sub.id)
        assert updated.status == "failed"
        assert "not found" in updated.error_message.lower()

    def test_process_queued_submission_with_hpo_terms_and_qc(self, worker, store):
        """Worker includes optional metadata in CLI command."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-7",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
            hpo_terms={"terms": ["HP:0001234"]},
            qc_metrics={"depth": 50},
        )

        bij_output = json.dumps(
            {
                "run_complete": True,
                "interpretation_id": "interp-uuid-7",
                "run_document": {"id": "doc-7"},
            }
        )

        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = (bij_output, "")
            mock_proc.returncode = 0
            mock_spawn.return_value = mock_proc

            result = worker.process_queued_submission(sub)

        assert result is True

        # Verify spawn_tracked was called with hpo and qc arguments
        call_args = mock_spawn.call_args[0][0]
        assert "--hpo-terms" in call_args
        assert "--qc-metrics" in call_args

    def test_submission_status_transitions_on_process_queued_submission(self, worker, store):
        """Submission transitions queued → running → complete."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-8",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        assert sub.status == "queued"

        bij_output = json.dumps(
            {
                "run_complete": True,
                "interpretation_id": "interp-uuid-8",
                "run_document": {"id": "doc-8"},
            }
        )

        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = (bij_output, "")
            mock_proc.returncode = 0
            mock_spawn.return_value = mock_proc

            worker.process_queued_submission(sub)

        # Verify transition queued → running → complete
        updated = store.get_submission(sub.id)
        assert updated.status == "complete"
