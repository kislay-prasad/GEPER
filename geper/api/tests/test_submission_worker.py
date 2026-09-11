"""
tests/test_submission_worker.py
───────────────────────────────
Unit tests for InterpretationWorker.

2026-09-08: rewritten to match the real geper/main.py contract (exit
code + files on disk under --output-dir) instead of the fictional stdout
JSON envelope (run_complete/interpretation_id/run_document) the old
version of this file mocked. These mocks still don't prove main.py
itself behaves this way -- they prove the WORKER'S OWN handling of a
given exit code / file layout is correct. Per the human's explicit
instruction: "The current tests mock spawn_tracked and would pass if the
engine didn't exist" -- a green run of this file is not the acceptance
test for the 2026-09-08 change; one real submission through the real
engine is.
"""

import json
import os
import subprocess
import tempfile
import uuid
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
def output_root():
    """Isolated --output-dir root so tests never touch the real
    .submission_outputs/ directory or each other's files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def worker(store, output_root, monkeypatch):
    """Create a test InterpretationWorker with an isolated output root."""
    monkeypatch.setenv("GEPER_SUBMISSION_OUTPUT_ROOT", output_root)
    return InterpretationWorker(store)


def _write_results_file(output_root: str, submission_id: str) -> str:
    """Simulates geper/main.py's real behavior (orchestrator.py:712):
    writes geper_results.json under --output-dir, which this worker
    passes as <output_root>/<submission.id>. Returns that path."""
    out_dir = Path(output_root) / submission_id
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "geper_results.json"
    results_path.write_text(json.dumps({"variants": []}))
    return str(results_path)


class TestInterpretationWorker:
    """Tests for InterpretationWorker."""

    def test_process_queued_submission_success(self, worker, store, output_root):
        """Worker processes queued submission and marks complete when
        geper/main.py exits 0 and writes geper_results.json."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-1",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = ("", "")
            mock_proc.returncode = 0
            mock_spawn.return_value = mock_proc

            # geper/main.py has no stdout contract to mock -- the
            # worker's success path reads the exit code (above) and the
            # file main.py writes (simulated here), not stdout.
            results_path = _write_results_file(output_root, sub.id)

            result = worker.process_queued_submission(sub)

        assert result is True

        # Verify submission was marked complete
        updated = store.get_submission(sub.id)
        assert updated.status == "complete"
        # interpretation_id: THE PLATFORM MINTS IT (2026-09-08 ruling,
        # correcting the prior "left None" version of this test -- that
        # was right while unresolved, and is now resolved the other way:
        # a genuinely distinct id, not the response's own `id`/submission
        # id, and not None). Asserted as a real, parseable UUID, and
        # explicitly NOT equal to sub.id -- reusing sub.id was the
        # ruling's own named alternative it rejected ("generate one
        # rather than... borrowing the response id").
        assert updated.interpretation_id is not None
        assert uuid.UUID(updated.interpretation_id) is not None
        assert updated.interpretation_id != sub.id
        assert updated.run_document_ref == results_path

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

    def test_process_queued_submission_exit_zero_but_no_results_file(self, worker, store):
        """Worker marks failed if main.py exits 0 but geper_results.json
        was never written under --output-dir -- replaces the old
        run_complete=false test, which asserted a stdout flag
        geper/main.py has never produced (2026-09-08 finding)."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-4",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = ("", "")
            mock_proc.returncode = 0
            mock_spawn.return_value = mock_proc

            # Deliberately do NOT write geper_results.json.
            result = worker.process_queued_submission(sub)

        assert result is True

        updated = store.get_submission(sub.id)
        assert updated.status == "failed"
        assert "did not write" in updated.error_message
        assert "geper_results.json" in updated.error_message

    def test_process_queued_submission_cli_not_found(self, worker, store):
        """Worker handles the subprocess machinery itself being
        unavailable (spawn_tracked raising FileNotFoundError). Note
        (2026-09-08): this no longer models 'main.py is missing at that
        path' -- sys.executable always exists, so Popen succeeds and a
        missing script surfaces as a nonzero exit instead (see the
        nonzero-exit test above); this test now covers spawn_tracked/
        subprocess itself failing to launch."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-6",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
        )

        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            mock_spawn.side_effect = FileNotFoundError("python interpreter not found")

            result = worker.process_queued_submission(sub)

        assert result is True

        # Verify submission was marked failed
        updated = store.get_submission(sub.id)
        assert updated.status == "failed"
        assert "not found" in updated.error_message.lower()

    def test_process_queued_submission_with_hpo_terms_and_qc(self, worker, store, output_root):
        """Worker includes optional metadata in CLI command, under the
        real flag names (2026-09-08: --qc-metrics-json, not
        --qc-metrics), writes qc_metrics to a sidecar file rather than
        passing it inline (geper/main.py:157-173 wants a file path), and
        CONVERTS hpo_terms to the real comma-separated "HP:#######"
        format geper/main.py:175-189 wants (2026-09-08 ruling: convert,
        don't degrade) rather than passing the JSON blob through
        unconverted.

        2026-09-11 CORRECTION (PHASE8-bij-interpret, option A): this
        test used to submit qc_metrics={"depth": 50} and assert that
        exact raw dict landed in the sidecar unconverted. "depth" is not
        one of _QC_METRIC_ORDER -- writing it through unconverted is
        the precise defect that made `_parse_qc_metrics` render a false
        "not reported" about a value that WAS reported (see
        test_process_queued_submission_qc_metrics_unrecognized_key_fails_loudly
        below). Fixed to use a RECOGNIZED key and assert on the
        CONVERTED {"status": "found", "value": ..., "reason": None}
        shape _qc_metrics_validated now produces -- the corrected
        contract, not the old, silently-wrong one."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-7",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
            hpo_terms={"terms": ["HP:0001234", "HP:0002011"]},
            qc_metrics={"mean_coverage_depth": 45.2},
        )

        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = ("", "")
            mock_proc.returncode = 0
            mock_spawn.return_value = mock_proc

            _write_results_file(output_root, sub.id)

            result = worker.process_queued_submission(sub)

        assert result is True

        # Verify spawn_tracked was called with the real flag names
        call_args = mock_spawn.call_args[0][0]
        assert "--hpo-terms" in call_args
        assert "--qc-metrics-json" in call_args
        assert "--qc-metrics" not in call_args
        # --sample-ref / --consent-ref must NOT be on the command line
        # (2026-09-08 ruling) even though they're still on the Submission
        # object (sample_ref="sample-1", consent_ref="consent-1" above).
        assert "--sample-ref" not in call_args
        assert "--consent-ref" not in call_args

        # --hpo-terms value must be the real comma-separated format, not
        # the JSON dict this submission actually stores.
        hpo_idx = call_args.index("--hpo-terms")
        assert call_args[hpo_idx + 1] == "HP:0001234,HP:0002011"

        # The qc-metrics-json argument must be a real file containing the
        # CONVERTED shape _parse_qc_metrics requires, not the submitted
        # dict serialized inline.
        qc_idx = call_args.index("--qc-metrics-json")
        qc_path = call_args[qc_idx + 1]
        assert os.path.isfile(qc_path)
        assert json.loads(Path(qc_path).read_text()) == {
            "mean_coverage_depth": {"status": "found", "value": 45.2, "reason": None}
        }

    def test_process_queued_submission_qc_metrics_unrecognized_key_fails_loudly(self, worker, store, output_root):
        """CONVERT, DO NOT DEGRADE (2026-09-11 ruling, PHASE8-bij-interpret
        option A -- same class of fix as hpo_terms, 2026-09-08): the
        platform's own tests (test_interpretations_api.py:78, and this
        file's own test above until this correction) use {"depth": 50}
        as "a normal qc_metrics submission". Written through
        unconverted, that shape reached `_parse_qc_metrics` as a silent
        `.get()` miss and rendered a FALSE "not reported by the
        upstream pipeline" sentence about a value that WAS reported. A
        submission with QC data GEPER can't recognize is a precondition
        failure, not a degraded run -- must fail loudly (status=failed,
        a clear error naming the bad key) exactly like a malformed
        hpo_terms submission does. No subprocess is ever spawned."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-10",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
            qc_metrics={"depth": 50},
        )

        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            result = worker.process_queued_submission(sub)

        assert result is True
        mock_spawn.assert_not_called()

        updated = store.get_submission(sub.id)
        assert updated.status == "failed"
        assert "qc_metrics" in updated.error_message
        assert "depth" in updated.error_message

    def test_process_queued_submission_hpo_terms_conversion_failure_raises(self, worker, store, output_root):
        """CONVERT, DO NOT DEGRADE (2026-09-08 ruling): if hpo_terms can't
        be converted to main.py's comma-separated format, the submission
        must fail loudly (status=failed, a clear error, an exception via
        the same path every other failure uses) rather than silently
        passing malformed data through or dropping the flag -- either of
        those would reproduce the exact "wrong clinical answer delivered
        quietly" defect this fix exists to close. No subprocess is ever
        spawned: this is a precondition failure, not a degraded run."""
        sub = store.create_submission(
            org_id="default",
            submission_key="key-9",
            vcf_path="/path/to/vcf",
            assembly="hg38",
            sample_ref="sample-1",
            consent_ref="consent-1",
            hpo_terms={"terms": ["HP:0001234", "not-an-hpo-id"]},
        )

        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            result = worker.process_queued_submission(sub)

        assert result is True
        mock_spawn.assert_not_called()

        updated = store.get_submission(sub.id)
        assert updated.status == "failed"
        assert "hpo_terms" in updated.error_message
        assert "not-an-hpo-id" in updated.error_message

    def test_submission_status_transitions_on_process_queued_submission(self, worker, store, output_root):
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

        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = ("", "")
            mock_proc.returncode = 0
            mock_spawn.return_value = mock_proc

            _write_results_file(output_root, sub.id)

            worker.process_queued_submission(sub)

        # Verify transition queued → running → complete
        updated = store.get_submission(sub.id)
        assert updated.status == "complete"
