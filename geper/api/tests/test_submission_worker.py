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
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from api.submission_store import SubmissionStore
from api.submission_worker import InterpretationWorker
from clinical.data_access import RecordedPipelineResult, interpretation_submission_key


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


ORG = "aaaaaaaa-0000-4000-8000-000000000001"
ORDER = "0d0d0d0d-0000-4000-8000-000000000003"
SAMPLE = "5a5a5a5a-0000-4000-8000-000000000004"
PATIENT = uuid.UUID("9a9a9a9a-0000-4000-8000-000000000005")


class FakeClinical:
    """
    Stand-in for clinical.data_access.DataAccess in THIS file only, which
    tests the worker's own handling of the engine's exit code and files
    (see the module docstring). Every precondition passes and nothing is
    recorded yet. The clinical link itself -- preconditions, the
    idempotency lookup, the atomic record, reconciliation -- is tested
    against a real PostgreSQL in test_worker_clinical_link.py, not here.
    """

    def __init__(self):
        self.recorded = []
        self.exceptions = []

    def _create_system_session(self, org_id):
        return SimpleNamespace(org_id=org_id, user_id=uuid.uuid4())

    def get_order(self, session, order_id):
        return {"order_id": order_id, "patient_id": PATIENT, "required_scope": "testing"}

    def get_sample(self, session, sample_id):
        return {"sample_id": sample_id, "order_id": uuid.UUID(ORDER)}

    def validate_order_for_submission(self, *args, **kwargs):
        return True, None

    def find_interpretation_by_submission_key(self, session, submission_key):
        for rec in self.recorded:
            if rec.submission_key == submission_key:
                return {"interpretation_id": rec.interpretation_id, "report_id": rec.report_id, "vcf_id": rec.vcf_id}
        return None

    def record_pipeline_result(self, session, sample_id, vcf_path, expected_vcf_hash, run_document):
        rec = RecordedPipelineResult(
            interpretation_id=uuid.uuid4(),
            report_id=uuid.uuid4(),
            vcf_id=uuid.uuid4(),
            sequencing_run_id=uuid.uuid4(),
            submission_key=interpretation_submission_key(expected_vcf_hash, sample_id),
        )
        self.recorded.append(rec)
        return rec

    def create_or_reopen_exception(self, session, order_id, category, reason_code, *rest):
        self.exceptions.append(reason_code)
        return uuid.uuid4()


@pytest.fixture
def vcf_file(tmp_path):
    """A real file: the worker hashes the VCF before running (the clinical
    idempotency key), so a path to nothing is now a precondition failure."""
    path = tmp_path / "input.vcf"
    path.write_text("##fileformat=VCFv4.2\n##assembly=GRCh38\n#CHROM\tPOS\tID\tREF\tALT\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def clinical():
    return FakeClinical()


@pytest.fixture
def worker(store, output_root, monkeypatch, clinical):
    """Create a test InterpretationWorker with an isolated output root."""
    monkeypatch.setenv("GEPER_SUBMISSION_OUTPUT_ROOT", output_root)
    return InterpretationWorker(store, clinical)


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

    def test_process_queued_submission_success(self, worker, store, output_root, vcf_file, clinical):
        """Worker processes queued submission and marks complete when
        geper/main.py exits 0 and writes geper_results.json."""
        sub = store.create_submission(
            org_id=ORG,
            order_id=ORDER,
            sample_id=SAMPLE,
            submission_key="key-1",
            vcf_path=vcf_file,
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
        # interpretation_id: the CLINICAL interpretation the run was
        # recorded as (2026-09-12, D0 "build the link"), superseding the
        # 2026-09-08 "mint a uuid4" ruling this test used to pin. That
        # ruling's point -- a genuinely distinct id, never the response's
        # own `id` -- still holds: the clinical id is not sub.id.
        assert len(clinical.recorded) == 1
        assert updated.interpretation_id == str(clinical.recorded[0].interpretation_id)
        assert updated.interpretation_id != sub.id
        assert updated.run_document_ref == results_path

    def test_process_queued_submission_timeout(self, worker, store, vcf_file):
        """Worker handles Bij AI CLI timeout."""
        sub = store.create_submission(
            org_id=ORG,
            order_id=ORDER,
            sample_id=SAMPLE,
            submission_key="key-2",
            vcf_path=vcf_file,
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

    def test_process_queued_submission_nonzero_exit(self, worker, store, vcf_file):
        """Worker handles nonzero exit code from Bij AI CLI."""
        sub = store.create_submission(
            org_id=ORG,
            order_id=ORDER,
            sample_id=SAMPLE,
            submission_key="key-3",
            vcf_path=vcf_file,
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

    def test_process_queued_submission_exit_zero_but_no_results_file(self, worker, store, vcf_file):
        """Worker marks failed if main.py exits 0 but geper_results.json
        was never written under --output-dir -- replaces the old
        run_complete=false test, which asserted a stdout flag
        geper/main.py has never produced (2026-09-08 finding)."""
        sub = store.create_submission(
            org_id=ORG,
            order_id=ORDER,
            sample_id=SAMPLE,
            submission_key="key-4",
            vcf_path=vcf_file,
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

    def test_process_queued_submission_cli_not_found(self, worker, store, vcf_file):
        """Worker handles the subprocess machinery itself being
        unavailable (spawn_tracked raising FileNotFoundError). Note
        (2026-09-08): this no longer models 'main.py is missing at that
        path' -- sys.executable always exists, so Popen succeeds and a
        missing script surfaces as a nonzero exit instead (see the
        nonzero-exit test above); this test now covers spawn_tracked/
        subprocess itself failing to launch."""
        sub = store.create_submission(
            org_id=ORG,
            order_id=ORDER,
            sample_id=SAMPLE,
            submission_key="key-6",
            vcf_path=vcf_file,
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

    def test_process_queued_submission_with_hpo_terms_and_qc(self, worker, store, output_root, vcf_file):
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
            org_id=ORG,
            order_id=ORDER,
            sample_id=SAMPLE,
            submission_key="key-7",
            vcf_path=vcf_file,
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

    def test_process_queued_submission_qc_metrics_unrecognized_key_fails_loudly(
        self, worker, store, output_root, vcf_file
    ):
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
            org_id=ORG,
            order_id=ORDER,
            sample_id=SAMPLE,
            submission_key="key-10",
            vcf_path=vcf_file,
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

    def test_process_queued_submission_hpo_terms_conversion_failure_raises(self, worker, store, output_root, vcf_file):
        """CONVERT, DO NOT DEGRADE (2026-09-08 ruling): if hpo_terms can't
        be converted to main.py's comma-separated format, the submission
        must fail loudly (status=failed, a clear error, an exception via
        the same path every other failure uses) rather than silently
        passing malformed data through or dropping the flag -- either of
        those would reproduce the exact "wrong clinical answer delivered
        quietly" defect this fix exists to close. No subprocess is ever
        spawned: this is a precondition failure, not a degraded run."""
        sub = store.create_submission(
            org_id=ORG,
            order_id=ORDER,
            sample_id=SAMPLE,
            submission_key="key-9",
            vcf_path=vcf_file,
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

    def test_submission_status_transitions_on_process_queued_submission(self, worker, store, output_root, vcf_file):
        """Submission transitions queued → running → complete."""
        sub = store.create_submission(
            org_id=ORG,
            order_id=ORDER,
            sample_id=SAMPLE,
            submission_key="key-8",
            vcf_path=vcf_file,
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


def _ok_spawn():
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = ("", "")
    mock_proc.returncode = 0
    return mock_proc


class TestWorkerClinicalLinkUnit:
    """The link's refusal paths, against the fake. The real-database
    behaviour is in test_worker_clinical_link.py."""

    def _submit(self, store, vcf_file, **overrides):
        kwargs = dict(
            org_id=ORG,
            order_id=ORDER,
            sample_id=SAMPLE,
            submission_key="key-link",
            vcf_path=vcf_file,
            assembly="GRCh38",
            sample_ref="s",
            consent_ref="c",
        )
        kwargs.update(overrides)
        return store.create_submission(**kwargs)

    def test_no_clinical_store_means_no_run(self, store, output_root, monkeypatch, vcf_file):
        """A run that cannot be recorded is not run (main() already refuses
        to start without CLINICAL_DSN; this is the same rule per submission)."""
        monkeypatch.setenv("GEPER_SUBMISSION_OUTPUT_ROOT", output_root)
        worker = InterpretationWorker(store)
        sub = self._submit(store, vcf_file)
        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            assert worker.process_queued_submission(sub) is True
        mock_spawn.assert_not_called()
        updated = store.get_submission(sub.id)
        assert updated.status == "failed"
        assert "clinical" in updated.error_message.lower()

    @pytest.mark.parametrize(
        "overrides",
        [
            {"org_id": "default"},  # every submission before 2026-09-12
            {"order_id": None},
            {"sample_id": None},
            {"sample_id": "not-a-uuid"},
        ],
    )
    def test_unlinked_submission_is_refused_without_running(self, worker, store, clinical, vcf_file, overrides):
        sub = self._submit(store, vcf_file, **overrides)
        with patch("api.submission_worker.spawn_tracked") as mock_spawn:
            assert worker.process_queued_submission(sub) is True
        mock_spawn.assert_not_called()
        updated = store.get_submission(sub.id)
        assert updated.status == "failed"
        assert "not linked" in updated.error_message
        assert clinical.recorded == []

    def test_already_recorded_run_is_reconciled_not_rerun(self, worker, store, clinical, output_root, vcf_file):
        sub = self._submit(store, vcf_file)
        with patch("api.submission_worker.spawn_tracked", return_value=_ok_spawn()) as mock_spawn:
            _write_results_file(output_root, sub.id)
            worker.process_queued_submission(sub)
            store.update_status(sub.id, "queued")  # e.g. re-queued by the retry scheduler
            worker.process_queued_submission(store.get_submission(sub.id))
        assert mock_spawn.call_count == 1
        assert len(clinical.recorded) == 1
        updated = store.get_submission(sub.id)
        assert updated.status == "complete"
        assert updated.interpretation_id == str(clinical.recorded[0].interpretation_id)

    def test_record_failure_fails_the_submission_with_its_own_reason_code(
        self, worker, store, clinical, output_root, vcf_file, monkeypatch
    ):
        def boom(*args, **kwargs):
            raise RuntimeError("database went away")

        monkeypatch.setattr(clinical, "record_pipeline_result", boom)
        sub = self._submit(store, vcf_file)
        with patch("api.submission_worker.spawn_tracked", return_value=_ok_spawn()):
            _write_results_file(output_root, sub.id)
            worker.process_queued_submission(sub)
        updated = store.get_submission(sub.id)
        assert updated.status == "failed"
        assert "could not be recorded" in updated.error_message
        assert updated.interpretation_id is None
        assert clinical.exceptions == ["clinical_record_write_failed"]

    def test_clinical_link_file_names_the_records(self, worker, store, clinical, output_root, vcf_file):
        sub = self._submit(store, vcf_file)
        with patch("api.submission_worker.spawn_tracked", return_value=_ok_spawn()):
            _write_results_file(output_root, sub.id)
            worker.process_queued_submission(sub)
        link = json.loads((Path(output_root) / sub.id / "clinical_link.json").read_text(encoding="utf-8"))
        rec = clinical.recorded[0]
        assert link == {
            "submission_id": sub.id,
            "org_id": ORG,
            "order_id": ORDER,
            "sample_id": SAMPLE,
            "interpretation_id": str(rec.interpretation_id),
            "report_id": str(rec.report_id),
            "vcf_id": str(rec.vcf_id),
            "submission_key": rec.submission_key,
        }


class TestHpoTermsDocstringCitations:
    """The 2026-09-13 (wave 117) defect this guards: `_hpo_terms_to_cli_arg`'s
    docstring claimed the platform spec "declares hpo_terms as a plain list"
    and cited GEPER_CLINICAL_PLATFORM_SPEC.md:457 for it. The spec moved --
    the POST /interpretations contract block was reconciled against the
    implementation and hpo_terms became an object -- and :457 became an
    unrelated paragraph, with nothing re-checking the citation. Same class of
    defect as the dbSNP docstring: a claim that stopped being true.

    So: re-check it. Each assertion below reads the line the docstring
    actually cites and fails if that line no longer says what is claimed --
    either because the document moved, or because the shape changed back."""

    @staticmethod
    def _docstring():
        from api.submission_worker import _hpo_terms_to_cli_arg

        return _hpo_terms_to_cli_arg.__doc__

    @staticmethod
    def _cited_line(relpath, lineno):
        """The 1-based `lineno` of `relpath`, as the docstring cites it."""
        root = Path(__file__).resolve().parents[3]
        return (root / relpath).read_text(encoding="utf-8").splitlines()[lineno - 1]

    def _first_citation(self, filename):
        """The line number of the FIRST citation of `filename` in the
        docstring -- the live claim. Later ones are the historical note
        recording what the citation used to be, and are meant to be stale."""
        m = re.search(re.escape(filename) + r":(\d+)", self._docstring())
        assert m, f"docstring no longer cites {filename}"
        return int(m.group(1))

    def test_cited_spec_line_still_declares_hpo_terms_an_object(self):
        lineno = self._first_citation("GEPER_CLINICAL_PLATFORM_SPEC.md")
        line = self._cited_line("GEPER_CLINICAL_PLATFORM_SPEC.md", lineno)
        assert "hpo_terms" in line, (
            f"docstring cites GEPER_CLINICAL_PLATFORM_SPEC.md:{lineno} for hpo_terms, "
            f"but that line is now: {line!r}. The spec moved; fix the citation."
        )
        assert "{" in line and "[" not in line, (
            f"docstring claims the spec declares hpo_terms an OBJECT, but "
            f"GEPER_CLINICAL_PLATFORM_SPEC.md:{lineno} now reads: {line!r}."
        )

    def test_cited_request_model_line_is_the_hpo_terms_field(self):
        lineno = self._first_citation("geper/api/main.py")
        line = self._cited_line("geper/api/main.py", lineno)
        assert "hpo_terms" in line and "Dict[str, Any]" in line, (
            f"docstring cites geper/api/main.py:{lineno} as hpo_terms' declared type, but that line is now: {line!r}."
        )

    def test_cited_attesting_test_line_uses_the_terms_object(self):
        lineno = self._first_citation("geper/api/tests/test_interpretations_api.py")
        line = self._cited_line("geper/api/tests/test_interpretations_api.py", lineno)
        assert 'hpo_terms={"terms"' in line, (
            f"docstring cites geper/api/tests/test_interpretations_api.py:{lineno} as "
            f'the attested {{"terms": [...]}} shape, but that line is now: {line!r}.'
        )
