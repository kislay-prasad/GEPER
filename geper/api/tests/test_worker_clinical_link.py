"""
api/tests/test_worker_clinical_link.py
──────────────────────────────────────
D0 ("build the link", 2026-09-12): the worker turns a completed pipeline
run into clinical records, against a REAL PostgreSQL (api/tests/conftest.py
::clinical_conn -- disposable databases only).

The engine itself is not run: spawn_tracked is patched to "exit 0" and a
fixture geper_results.json is written, exactly as test_submission_worker.py
does. What is real is everything this commit adds: the organisation's
system principal, the Phase 5 preconditions, the idempotency lookup, the
atomic record, reconciliation of interrupted rows, clinical_link.json, and
the clinical_record_write_failed exception. Clinical state is built through
the real intake methods; raw SQL only reads results back.

Scope note (human ruling D1, 2026-09-12): nothing in production creates the
patient/order/sample records these tests build by hand -- order entry is
its own card. Until it exists this path is exercised here and nowhere else.
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
from clinical.data_access import DataAccess, SystemClock

VCF_TEXT = (
    "##fileformat=VCFv4.2\n"
    "##assembly=GRCh38\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample1\n"
    "chr1\t1000\t.\tA\tT\t30\tPASS\t.\tGT\t0/1\n"
)
RUN_DOCUMENT = {"variants": [{"chrom": "1", "pos": 1000, "ref": "A", "alt": "T"}], "run_complete": True}


class _PlainHasher:
    """The clinical layer's hasher protocol without bcrypt, which the geper
    environment does not install. Only these fixtures' own logins use it."""

    def hash(self, plaintext: str) -> str:
        return "plain$" + plaintext

    def verify(self, plaintext: str, hashed: str) -> bool:
        return hashed == "plain$" + plaintext


@pytest.fixture
def dao(clinical_conn):
    return DataAccess(clinical_conn, clock=SystemClock(), password_hasher=_PlainHasher())


def _organisation(dao, conn, name, qc="passed"):
    org_id = dao.create_organisation(name)
    email = f"admin@{name.lower().replace(' ', '-')}.test"
    user_id = dao.create_user(org_id, email, "password")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Administrator', %s, %s)",
            (user_id, org_id, user_id, datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)),
        )
        test_id = uuid.uuid4()
        cur.execute(
            "INSERT INTO tests (test_id, org_id, name, assembly, status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (test_id, org_id, "Panel", "GRCh38", "active", datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)),
        )
    conn.commit()
    admin = dao.login(email, org_id, "password")
    patient_id = dao.create_patient(admin, "Test Patient", datetime.date(1990, 1, 1), "F")
    consent_id = dao.record_consent(admin, patient_id, "testing")
    order_id = dao.create_order(
        admin,
        patient_id=patient_id,
        test_id=test_id,
        required_scope="testing",
        consent_id=consent_id,
        priority="routine",
        clinical_indication="Test indication",
    )
    dao.place_order(admin, order_id)
    sample_id = dao.receive_sample(admin, order_id, "blood")
    if qc != "pending":
        dao.record_qc(admin, sample_id, qc)
    return {"org_id": org_id, "order_id": order_id, "sample_id": sample_id}


@pytest.fixture
def org_a(dao, clinical_conn):
    return _organisation(dao, clinical_conn, "Org A")


@pytest.fixture
def org_b(dao, clinical_conn):
    return _organisation(dao, clinical_conn, "Org B")


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "submissions.db"


@pytest.fixture
def store(store_path):
    return SubmissionStore(store_path)


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
def vcf_path(tmp_path):
    path = tmp_path / "input.vcf"
    path.write_text(VCF_TEXT, encoding="utf-8")
    return str(path)


def _submit(store, org, vcf_path, *, org_id=None, key="key-1"):
    return store.create_submission(
        org_id=str(org_id or org["org_id"]),
        order_id=str(org["order_id"]),
        sample_id=str(org["sample_id"]),
        submission_key=key,
        vcf_path=vcf_path,
        assembly="GRCh38",
        sample_ref="s",
        consent_ref="c",
    )


class _Engine:
    """spawn_tracked stand-in: 'runs' geper/main.py by writing the results
    file into the --output-dir it was given, and counts its runs."""

    def __init__(self, on_run=None):
        self.runs = 0
        self.on_run = on_run

    def __call__(self, cmd, **kwargs):
        self.runs += 1
        out_dir = Path(cmd[cmd.index("--output-dir") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "geper_results.json").write_text(json.dumps(RUN_DOCUMENT), encoding="utf-8")
        if self.on_run:
            self.on_run()
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        return proc


def _rows(conn, table, org_id=None):
    with conn.cursor() as cur:
        if org_id is None:
            cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 -- fixed names in this file
        else:
            cur.execute(f"SELECT count(*) FROM {table} WHERE org_id = %s", (org_id,))  # noqa: S608
        n = cur.fetchone()[0]
    conn.commit()
    return n


def _exception_codes(conn, order_id):
    with conn.cursor() as cur:
        cur.execute("SELECT reason_code FROM exceptions WHERE order_id = %s ORDER BY created_at", (order_id,))
        codes = [r[0] for r in cur.fetchall()]
    conn.commit()
    return codes


def test_completed_run_becomes_a_clinical_interpretation_and_draft_report(
    worker, store, clinical_conn, org_a, vcf_path, output_root
):
    sub = _submit(store, org_a, vcf_path)
    engine = _Engine()
    with patch("api.submission_worker.spawn_tracked", engine):
        assert worker.process_queued_submission(sub) is True

    updated = store.get_submission(sub.id)
    assert updated.status == "complete", updated.error_message
    with clinical_conn.cursor() as cur:
        cur.execute(
            "SELECT i.id, i.run_document, r.id, r.state FROM interpretations i "
            "JOIN reports r ON r.org_id = i.org_id AND r.interpretation_id = i.id WHERE i.org_id = %s",
            (org_a["org_id"],),
        )
        rows = cur.fetchall()
    clinical_conn.commit()
    assert len(rows) == 1
    interpretation_id, run_document, report_id, state = rows[0]
    run_document = json.loads(run_document) if isinstance(run_document, str) else run_document
    assert updated.interpretation_id == str(interpretation_id)
    assert run_document == RUN_DOCUMENT
    assert state == "draft"

    link = json.loads((output_root / sub.id / "clinical_link.json").read_text(encoding="utf-8"))
    assert link["interpretation_id"] == str(interpretation_id)
    assert link["report_id"] == str(report_id)
    assert link["org_id"] == str(org_a["org_id"])


def test_requeue_after_success_does_not_rerun_or_duplicate(worker, store, clinical_conn, org_a, vcf_path):
    sub = _submit(store, org_a, vcf_path)
    engine = _Engine()
    with patch("api.submission_worker.spawn_tracked", engine):
        worker.process_queued_submission(sub)
        first = store.get_submission(sub.id).interpretation_id
        store.update_status(sub.id, "queued")
        worker.process_queued_submission(store.get_submission(sub.id))

    assert engine.runs == 1
    assert _rows(clinical_conn, "interpretations") == 1
    again = store.get_submission(sub.id)
    assert again.status == "complete"
    assert again.interpretation_id == first


def test_concurrent_duplicate_is_reconciled_not_failed(worker, store, dao, clinical_conn, org_a, vcf_path, monkeypatch):
    """Another worker records the same run between this one's lookup and its
    insert: uk_interp_submission refuses the second insert, and this worker
    adopts the first record instead of failing."""
    sub = _submit(store, org_a, vcf_path)
    real_find = dao.find_interpretation_by_submission_key
    calls = {"n": 0}

    def find_misses_once(session, key):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_find(session, key)

    def other_worker_records_first():
        from clinical.data_access import _sha256_file

        other = dao._create_system_session(org_a["org_id"])
        dao.record_pipeline_result(other, org_a["sample_id"], vcf_path, _sha256_file(vcf_path), {"other": True})

    monkeypatch.setattr(dao, "find_interpretation_by_submission_key", find_misses_once)
    with patch("api.submission_worker.spawn_tracked", _Engine(on_run=other_worker_records_first)):
        worker.process_queued_submission(sub)

    updated = store.get_submission(sub.id)
    assert updated.status == "complete", updated.error_message
    assert _rows(clinical_conn, "interpretations") == 1


def test_record_failure_leaves_no_clinical_rows_and_raises_its_own_exception(
    worker, store, clinical_conn, org_a, vcf_path
):
    """The VCF is rewritten while the engine runs: the record is refused
    (the run analysed other bytes), the submission fails, nothing is in the
    clinical record, and the lab gets a clinical_record_write_failed
    exception on the order."""

    def vcf_changes_during_the_run():
        Path(vcf_path).write_text(VCF_TEXT + "chr1\t2000\t.\tG\tC\t30\tPASS\t.\tGT\t0/1\n", encoding="utf-8")

    sub = _submit(store, org_a, vcf_path)
    with patch("api.submission_worker.spawn_tracked", _Engine(on_run=vcf_changes_during_the_run)):
        worker.process_queued_submission(sub)

    updated = store.get_submission(sub.id)
    assert updated.status == "failed"
    assert "could not be recorded" in updated.error_message
    for table in ("sequencing_runs", "vcfs", "interpretations", "reports"):
        assert _rows(clinical_conn, table) == 0, table
    assert _exception_codes(clinical_conn, org_a["order_id"]) == ["clinical_record_write_failed"]


def test_worker_death_after_the_record_is_reconciled_at_restart(
    store_path, store, dao, clinical_conn, org_a, vcf_path, output_root
):
    """The clinical write commits, then the worker dies before SQLite says
    'complete'. The restart marks the row interrupted (as it always has);
    reconciliation finds the clinical record and completes the row with it
    -- no second run, no second interpretation."""
    worker = InterpretationWorker(store, dao)
    sub = _submit(store, org_a, vcf_path)
    real_update = store.update_status

    def die_on_complete(submission_id, status, **kwargs):
        if status == "complete":
            raise SystemExit("worker killed")
        return real_update(submission_id, status, **kwargs)

    engine = _Engine()
    with patch.object(store, "update_status", die_on_complete), patch("api.submission_worker.spawn_tracked", engine):
        with pytest.raises(SystemExit):
            worker.process_queued_submission(sub)
    assert _rows(clinical_conn, "interpretations") == 1

    restarted_store = SubmissionStore(store_path)  # startup: running -> interrupted
    assert restarted_store.get_submission(sub.id).status == "interrupted"
    restarted = InterpretationWorker(restarted_store, dao)
    with patch("api.submission_worker.spawn_tracked", engine):
        assert restarted.reconcile_interrupted_submissions() == 1

    done = restarted_store.get_submission(sub.id)
    assert done.status == "complete"
    assert engine.runs == 1
    with clinical_conn.cursor() as cur:
        cur.execute("SELECT id FROM interpretations")
        (interpretation_id,) = cur.fetchone()
    clinical_conn.commit()
    assert done.interpretation_id == str(interpretation_id)
    assert (output_root / sub.id / "clinical_link.json").is_file()


def test_interrupted_row_with_no_record_stays_interrupted(store_path, store, dao, clinical_conn, org_a, vcf_path):
    sub = _submit(store, org_a, vcf_path)
    store.update_status(sub.id, "running")
    restarted_store = SubmissionStore(store_path)
    restarted = InterpretationWorker(restarted_store, dao)
    assert restarted.reconcile_interrupted_submissions() == 0
    assert restarted_store.get_submission(sub.id).status == "interrupted"


def test_failed_precondition_blocks_the_run(worker, store, dao, clinical_conn, vcf_path):
    """QC not yet passed: validate_order_for_submission refuses and records
    its own exception; the engine never runs; nothing is recorded."""
    org = _organisation(dao, clinical_conn, "Org Pending", qc="pending")
    sub = _submit(store, org, vcf_path)
    engine = _Engine()
    with patch("api.submission_worker.spawn_tracked", engine):
        worker.process_queued_submission(sub)

    assert engine.runs == 0
    updated = store.get_submission(sub.id)
    assert updated.status == "failed"
    assert "qc_pending" in _exception_codes(clinical_conn, org["order_id"])
    assert _rows(clinical_conn, "interpretations") == 0


def test_another_organisations_order_is_refused_and_nothing_is_written(
    worker, store, clinical_conn, org_a, org_b, vcf_path
):
    """A submission owned by Org B that names Org A's order and sample:
    Org B's system principal cannot see them, so the run is refused before
    the engine starts, and neither organisation gains a row."""
    sub = _submit(store, org_a, vcf_path, org_id=org_b["org_id"])
    engine = _Engine()
    with patch("api.submission_worker.spawn_tracked", engine):
        worker.process_queued_submission(sub)

    assert engine.runs == 0
    updated = store.get_submission(sub.id)
    assert updated.status == "failed"
    assert "not found" in updated.error_message.lower()
    assert _rows(clinical_conn, "interpretations") == 0
    assert _rows(clinical_conn, "exceptions", org_a["org_id"]) == 0
    assert _rows(clinical_conn, "exceptions", org_b["org_id"]) == 0


def test_unknown_sample_on_a_real_order_is_refused(worker, store, clinical_conn, org_a, vcf_path):
    """The order exists, the sample does not (in this organisation): the run
    is refused and the lab gets a sample_unresolved exception on the order."""
    sub = store.create_submission(
        org_id=str(org_a["org_id"]),
        order_id=str(org_a["order_id"]),
        sample_id=str(uuid.uuid4()),
        submission_key="key-mismatch",
        vcf_path=vcf_path,
        assembly="GRCh38",
        sample_ref="s",
        consent_ref="c",
    )
    engine = _Engine()
    with patch("api.submission_worker.spawn_tracked", engine):
        worker.process_queued_submission(sub)
    assert engine.runs == 0
    assert store.get_submission(sub.id).status == "failed"
    assert _exception_codes(clinical_conn, org_a["order_id"]) == ["sample_unresolved"]
