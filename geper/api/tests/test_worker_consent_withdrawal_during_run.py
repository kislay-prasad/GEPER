"""
api/tests/test_worker_consent_withdrawal_during_run.py
──────────────────────────────────────────────────────
CONSENT CHARACTERIZATION (Backend Phase 1), WORKER LEVEL. The submission-to-result window, driven through the REAL
`InterpretationWorker` against a REAL disposable PostgreSQL (api/tests/conftest.py::clinical_conn), with only the engine
process stubbed -- the same stand-in `test_worker_clinical_link.py` uses.

THE WINDOW. `InterpretationWorker._clinical_preflight` calls `DataAccess.validate_order_for_submission` (which calls
`check_consent`) BEFORE the engine starts. `DataAccess.record_pipeline_result` takes no consent argument and reads no
consent. A consent withdrawn while the engine runs is therefore invisible to the recording. The stub engine below
withdraws the consent WHILE IT "RUNS", which is exactly that moment.

CURRENT IMPLEMENTATION ONLY. What these rows pin is what the worker and the data layer do at this commit. It is NOT
approved clinical or legal policy, it is NOT a ruling that a result should be recorded after withdrawal, and a future
decision to enforce consent at record time will change the first row here, in the same commit as the enforcement and
docs/CONSENT_ENFORCEMENT_MATRIX.md. Nothing here claims DPDP or NABL compliance, clinical validation, or production
readiness. The candidate enforcement points are listed in the matrix document and NONE is selected by these tests.

The first row is the characterization; the second shows the existing, DIFFERENT behaviour when the withdrawal comes
before the attempt starts (a retry), so the pair brackets the window.
"""

from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.submission_store import SubmissionStore
from api.submission_worker import InterpretationWorker
from api.tests.conftest import place_vcf
from api.tests.standin_engine import valid_engine_document
from clinical.data_access import DataAccess, GenomeConsentWithheldError, SystemClock

VCF_TEXT = (
    "##fileformat=VCFv4.2\n"
    "##assembly=GRCh38\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample1\n"
    "chr1\t1000\t.\tA\tT\t30\tPASS\t.\tGT\t0/1\n"
)
T0 = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)


class _PlainHasher:
    """The clinical layer's hasher protocol without bcrypt (not installed in the geper environment)."""

    def hash(self, plaintext: str) -> str:
        return "plain$" + plaintext

    def verify(self, plaintext: str, hashed: str) -> bool:
        return hashed == "plain$" + plaintext


@pytest.fixture
def dao(clinical_conn):
    return DataAccess(clinical_conn, clock=SystemClock(), password_hasher=_PlainHasher())


@pytest.fixture
def org(dao, clinical_conn):
    """One organisation whose single staff account orders, receives and records QC, with an order at QC-passed."""
    org_id = dao.create_organisation("Consent Org")
    email = "admin@consent-org.test"
    user_id = dao.create_user(org_id, email, "password")
    test_id = uuid.uuid4()
    with clinical_conn.cursor() as cur:
        for role in ("Administrator", "Orderer", "Lab technician", "Interpreter"):
            cur.execute(
                "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (user_id, org_id, role, user_id, T0),
            )
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, org_id, "Panel", "GRCh38", "active", T0),
        )
    clinical_conn.commit()
    staff = dao.login(email, org_id, "password")
    patient_id = dao.create_patient(staff, "Test Patient", datetime.date(1990, 1, 1), "F")
    consent_id = dao.record_consent(staff, patient_id, "testing")
    order_id = dao.create_order(
        staff,
        patient_id=patient_id,
        test_id=test_id,
        required_scope="testing",
        consent_id=consent_id,
        priority="routine",
        clinical_indication="consent characterization",
    )
    dao.place_order(staff, order_id)
    sample_id = dao.receive_sample(staff, order_id, "blood")
    dao.record_qc(staff, sample_id, "passed")
    return {
        "org_id": org_id,
        "staff": staff,
        "patient_id": patient_id,
        "consent_id": consent_id,
        "order_id": order_id,
        "sample_id": sample_id,
    }


@pytest.fixture
def store(tmp_path):
    return SubmissionStore(tmp_path / "submissions.db")


@pytest.fixture
def output_root(tmp_path, monkeypatch):
    root = tmp_path / "outputs"
    root.mkdir()
    monkeypatch.setenv("GEPER_SUBMISSION_OUTPUT_ROOT", str(root))
    return root


@pytest.fixture
def worker(store, dao, output_root):
    return InterpretationWorker(store, dao)


@pytest.fixture
def vcf_path(vcf_root, org):
    return place_vcf(vcf_root, org["org_id"], VCF_TEXT)


def _submit(store, org, vcf_path, key):
    return store.create_submission(
        org_id=str(org["org_id"]),
        order_id=str(org["order_id"]),
        sample_id=str(org["sample_id"]),
        submission_key=key,
        vcf_path=vcf_path,
        assembly="GRCh38",
        sample_ref="s",
        consent_ref="c",
    )


class _Engine:
    """spawn_tracked stand-in: 'runs' the engine by writing a valid results file, counts runs, and calls `on_run`
    while it is 'running' -- the moment a consent can be withdrawn without the worker knowing."""

    def __init__(self, on_run=None):
        self.runs = 0
        self.on_run = on_run

    def __call__(self, cmd, **kwargs):
        self.runs += 1
        out_dir = Path(cmd[cmd.index("--output-dir") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        document = valid_engine_document(cmd[cmd.index("--vcf") + 1])
        (out_dir / "geper_results.json").write_text(json.dumps(document), encoding="utf-8")
        if self.on_run:
            self.on_run()
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        return proc


def _count(conn, table, org_id):
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE org_id = %s", (org_id,))  # noqa: S608 -- fixed names here
        n = cur.fetchone()[0]
    conn.commit()
    return n


def _exception_codes(conn, order_id):
    with conn.cursor() as cur:
        cur.execute("SELECT reason_code FROM exceptions WHERE order_id = %s ORDER BY created_at", (order_id,))
        codes = [r[0] for r in cur.fetchall()]
    conn.commit()
    return codes


def test_a_withdrawal_while_the_engine_runs_is_currently_recorded_as_a_completed_result(
    worker, store, dao, clinical_conn, org, vcf_path
):
    """
    CHARACTERIZATION OF THE WINDOW (current implementation only; not policy). Consent was valid at the start of the
    attempt, the engine "runs", the consent is withdrawn during the run, and the worker then records the result and
    completes the submission: no exception is raised on the order, and the clinical rows exist.
    """
    sub = _submit(store, org, vcf_path, "key-race")
    engine = _Engine(on_run=lambda: dao.withdraw_consent(org["staff"], org["consent_id"]))

    with patch("api.submission_worker.spawn_tracked", engine):
        assert worker.process_queued_submission(sub) is True

    # The submission completed and the engine ran once.
    updated = store.get_submission(sub.id)
    assert updated.status == "complete", updated.error_message
    assert engine.runs == 1
    # The clinical record was written although the consent was withdrawn before it was.
    assert _count(clinical_conn, "interpretations", org["org_id"]) == 1
    assert _count(clinical_conn, "reports", org["org_id"]) == 1
    assert _exception_codes(clinical_conn, org["order_id"]) == []
    # What the system says afterwards: the consent is withdrawn, so the preflight rule and Policy C now disagree
    # with the recording that just happened.
    system = dao._create_system_session(org["org_id"])
    assert dao.check_consent(system, org["patient_id"], "testing") == "consent_withdrawn"
    with pytest.raises(GenomeConsentWithheldError):
        dao.list_patient_genome_analyses(org["staff"], org["patient_id"])
    # The withdrawal is in the audit log before the recording.
    with clinical_conn.cursor() as cur:
        cur.execute(
            "SELECT action FROM audit_log WHERE org_id = %s AND outcome = 'success' ORDER BY log_id", (org["org_id"],)
        )
        actions = [r[0] for r in cur.fetchall()]
    clinical_conn.commit()
    assert actions.index("consent_withdrawn") < actions.index("pipeline_result_recorded")


def test_a_withdrawal_before_the_attempt_starts_is_refused_at_preflight_and_the_engine_never_runs(
    worker, store, dao, clinical_conn, org, vcf_path
):
    """
    The existing behaviour on the OTHER side of the window (established elsewhere at the data layer by
    test_phase_5d_orchestration; shown here through the real worker): withdrawal before the attempt starts fails the
    submission at preflight with a `consent_withdrawn` exception, runs no engine and records nothing.
    """
    dao.withdraw_consent(org["staff"], org["consent_id"])
    sub = _submit(store, org, vcf_path, "key-before")
    engine = _Engine()

    with patch("api.submission_worker.spawn_tracked", engine):
        assert worker.process_queued_submission(sub) is True

    updated = store.get_submission(sub.id)
    assert updated.status == "failed"
    assert engine.runs == 0
    assert _count(clinical_conn, "interpretations", org["org_id"]) == 0
    assert _exception_codes(clinical_conn, org["order_id"]) == ["consent_withdrawn"]


def test_a_requeue_after_a_completed_run_and_a_later_withdrawal_is_refused_at_preflight_not_adopted(
    worker, store, dao, clinical_conn, org, vcf_path
):
    """
    Characterization of a second path: a completed run's submission is re-queued after the consent is withdrawn.
    Adoption of an already-recorded result happens only AFTER the preflight, and the preflight now refuses, so the
    re-queued submission FAILS at preflight with a `consent_withdrawn` exception rather than adopting the record.
    Current implementation only; not policy.
    """
    sub = _submit(store, org, vcf_path, "key-requeue")
    engine = _Engine()
    with patch("api.submission_worker.spawn_tracked", engine):
        worker.process_queued_submission(sub)
        assert store.get_submission(sub.id).status == "complete"
        dao.withdraw_consent(org["staff"], org["consent_id"])
        store.update_status(sub.id, "queued")
        worker.process_queued_submission(store.get_submission(sub.id))

    again = store.get_submission(sub.id)
    assert again.status == "failed"
    assert _exception_codes(clinical_conn, org["order_id"]) == ["consent_withdrawn"]
    assert engine.runs == 1
    # The record from the first attempt is still there, unchanged.
    assert _count(clinical_conn, "interpretations", org["org_id"]) == 1
