"""
api/tests/test_processing_handoff.py
────────────────────────────────────
PR A of the processing hand-off: the EXISTING path from a VCF submission to a persisted draft report, driven end to end
in one test module and against a real PostgreSQL (api/tests/conftest.py::clinical_conn, disposable databases only).

    POST /interpretations (real FastAPI app, API key bound to an organisation)
      -> SubmissionStore (real SQLite file)
      -> InterpretationWorker (real code)
      -> preflight: identity, consent, QC, VCF checks (real DataAccess, real PostgreSQL)
      -> the engine: a REAL SUBPROCESS through the real `spawn_tracked`, running `standin_engine.py`
      -> record_pipeline_result -> draft report
      -> the clinical service's own HTTP routes, read as a reviewer

WHAT THE ENGINE IS. `geper/main.py` is not run. The stand-in obeys its command-line and output contract (see
`standin_engine.py` and `test_processing_handoff_contract.py`) and produces a synthetic document. Everything here proves
the hand-off: identities, relationships, failure handling, idempotency. It proves NOTHING about the pipeline's
biological output, its accuracy, or clinical validity. All fixtures are synthetic.

DEFECTS PINNED AS STRICT xfail (PR A changes no production code). Each test states the behaviour that SHOULD hold and
fails today for the stated reason; `strict=True` makes the fix PR remove the marker (an unexpected pass fails the run).
  F1  the QC gate reads the order's LATEST sample, not the sample that was submitted
  F2  an engine that exits 0 and writes `{}` is recorded as a completed interpretation and a draft report
  F3  a stale `geper_results.json` from an earlier attempt satisfies the "engine wrote output" check
  F6  a retry re-queues EVERY failed submission of the order and clears the submission's own failure message
  F7  nothing stops two workers from running the engine for the same queued submission
  F9  a failure that should be retried never gets a `next_retry_at`, so the retry worker never selects it
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

import psutil
import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api import submission_worker
from api.main import app, get_submission_store
from api.submission_worker import InterpretationWorker
from api.tests import standin_engine
from api.tests import test_clinical_http_adapter as _http
from api.tests import test_worker_clinical_link as _link
from clinical.data_access import DataAccess, SystemClock
from review.clinical_http import HttpClinicalDataAccess

# fixtures this module reuses from the existing worker and HTTP-adapter tests
dao = _link.dao
store = _link.store
store_path = _link.store_path
output_root = _link.output_root
vcf_path = _link.vcf_path
service = _http.service
URL = _http.URL

STANDIN = Path(standin_engine.__file__)
KEY_A, KEY_B = "handoff-key-a", "handoff-key-b"


def xfail_defect(code: str, why: str):
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"{code}: {why}")


# ═══ rig ═══


class Engine:
    """The stand-in engine as the tests drive it: a mode, and a log of every invocation."""

    def __init__(self, monkeypatch, tmp_path):
        self._mp = monkeypatch
        self.log = tmp_path / "standin.log"
        self.rendezvous = tmp_path / "rendezvous"
        monkeypatch.setenv("BIJ_AI_CLI_PATH", str(STANDIN))
        monkeypatch.setenv("STANDIN_LOG", str(self.log))
        monkeypatch.setenv("STANDIN_RENDEZVOUS_DIR", str(self.rendezvous))
        self.mode("success")

    def mode(self, name: str) -> None:
        self._mp.setenv("STANDIN_MODE", name)

    def runs(self) -> List[Dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def engine(monkeypatch, tmp_path):
    return Engine(monkeypatch, tmp_path)


@pytest.fixture
def worker(engine, store, dao, output_root):
    # built AFTER the engine fixture: the worker reads BIJ_AI_CLI_PATH when it is constructed
    return InterpretationWorker(store, dao)


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def api(store, monkeypatch):
    """`api(org_id, key)` -> a TestClient authenticated as that organisation. Keys accumulate across calls."""
    keys: Dict[str, uuid.UUID] = {}
    monkeypatch.setattr(api_main, "_API_KEYS", keys)
    app.dependency_overrides[get_submission_store] = lambda: store

    def make(org_id, key):
        keys[key] = uuid.UUID(str(org_id))
        return TestClient(app, headers={"X-Api-Key": key})

    return make


def make_org(dao, conn, name, qc="passed"):
    world = _link._organisation(dao, conn, name, qc=qc)
    world["name"] = name
    return world


def admin_session(dao, org):
    return dao.login(f"admin@{org['name'].lower().replace(' ', '-')}.test", org["org_id"], "password")


def add_sample(dao, org, qc):
    """A further sample on the same order (a re-collection), received AFTER the existing ones."""
    admin = admin_session(dao, org)
    time.sleep(0.02)  # created_at must order the samples; the clock has microsecond resolution
    sample_id = dao.receive_sample(admin, org["order_id"], "blood")
    if qc:
        dao.record_qc(admin, sample_id, qc)
    return sample_id


@pytest.fixture
def org_a(dao, clinical_conn):
    return make_org(dao, clinical_conn, "Org A")


@pytest.fixture
def org_b(dao, clinical_conn):
    return make_org(dao, clinical_conn, "Org B")


def submit(client, org, vcf, *, key="k-1", sample_id=None, order_id=None, **extra) -> str:
    body = {
        "submission_key": key,
        "vcf_path": str(vcf),
        "assembly": "GRCh38",
        "sample_ref": "sample-ref",
        "consent_ref": "consent-ref",
        "order_id": str(order_id or org["order_id"]),
        "sample_id": str(sample_id or org["sample_id"]),
    }
    body.update(extra)
    response = client.post("/interpretations", json=body)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def process(worker, store, submission_id):
    worker.process_queued_submission(store.get_submission(submission_id))
    return store.get_submission(submission_id)


def q(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def count(conn, table, org_id):
    return q(conn, f"SELECT count(*) FROM {table} WHERE org_id = %s", (org_id,))[0][0]  # noqa: S608 -- fixed names


def clinical_counts(conn, org_id):
    return {t: count(conn, t, org_id) for t in ("sequencing_runs", "vcfs", "interpretations", "reports")}


NOTHING_RECORDED = {"sequencing_runs": 0, "vcfs": 0, "interpretations": 0, "reports": 0}


def exception_rows(conn, order_id):
    return q(
        conn,
        "SELECT reason_code, status, error_message, attempt_count, next_retry_at FROM exceptions "
        "WHERE order_id = %s ORDER BY created_at",
        (order_id,),
    )


def exception_codes(conn, order_id):
    return [row[0] for row in exception_rows(conn, order_id)]


def sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ═══ the happy path ═══


def test_a_qc_passed_sample_becomes_a_draft_report_with_every_identity_preserved(
    api, worker, store, dao, clinical_conn, org_a, vcf_path, engine, output_root
):
    client = api(org_a["org_id"], KEY_A)
    sub_id = submit(
        client, org_a, vcf_path, hpo_terms={"terms": ["HP:0001250"]}, qc_metrics={"mean_coverage_depth": 45.2}
    )
    assert client.get(f"/interpretations/{sub_id}").json()["status"] == "queued"

    done = process(worker, store, sub_id)
    assert done.status == "complete", done.error_message
    assert client.get(f"/interpretations/{sub_id}").json()["status"] == "complete"

    # the engine was run once, through a real subprocess, with the arguments the submission carried
    (run,) = engine.runs()
    argv = run["argv"]
    assert argv[argv.index("--vcf") + 1] == str(vcf_path)
    assert argv[argv.index("--assembly") + 1] == "GRCh38"
    assert argv[argv.index("--output-dir") + 1] == str(output_root / sub_id)
    assert argv[argv.index("--hpo-terms") + 1] == "HP:0001250"
    assert "--qc-metrics-json" in argv

    # patient -> order -> sample -> sequencing run -> VCF -> interpretation -> report, one chain, one organisation
    (chain,) = q(
        clinical_conn,
        "SELECT p.patient_id, o.order_id, s.sample_id, sr.id, v.id, v.vcf_path, v.content_hash, "
        "       i.id, i.run_document, r.id, r.state, p.org_id, o.org_id, s.org_id, sr.org_id, v.org_id, i.org_id, r.org_id "
        "FROM reports r "
        "JOIN interpretations i ON i.org_id = r.org_id AND i.id = r.interpretation_id "
        "JOIN vcfs v ON v.org_id = i.org_id AND v.id = i.vcf_id "
        "JOIN sequencing_runs sr ON sr.org_id = v.org_id AND sr.id = v.sequencing_run_id "
        "JOIN samples s ON s.org_id = sr.org_id AND s.sample_id = sr.sample_id "
        "JOIN orders o ON o.org_id = s.org_id AND o.order_id = s.order_id "
        "JOIN patients p ON p.org_id = o.org_id AND p.patient_id = o.patient_id "
        "WHERE r.org_id = %s",
        (org_a["org_id"],),
    )
    patient_id, order_id, sample_id, _run, _vcf, recorded_path, recorded_hash, interpretation_id, document = chain[:9]
    report_id, state = chain[9], chain[10]
    assert (order_id, sample_id) == (org_a["order_id"], org_a["sample_id"])
    assert (
        patient_id == q(clinical_conn, "SELECT patient_id FROM orders WHERE order_id = %s", (org_a["order_id"],))[0][0]
    )
    assert recorded_path == str(vcf_path) and recorded_hash == sha256(vcf_path)
    assert set(chain[11:]) == {org_a["org_id"]}
    assert state == "draft"
    document = json.loads(document) if isinstance(document, str) else document
    assert document["input_vcf"] == str(vcf_path) and document["variant_count"] == 1
    assert document["standin"]["hpo_terms"] == "HP:0001250"
    assert document["standin"]["qc_metrics"]  # the worker converted the platform's flat metrics for the engine
    assert clinical_counts(clinical_conn, org_a["org_id"]) == {k: 1 for k in NOTHING_RECORDED}
    assert exception_codes(clinical_conn, org_a["order_id"]) == []

    # the submission, the output directory and the clinical record all name the same things
    assert done.interpretation_id == str(interpretation_id)
    link = json.loads((output_root / sub_id / "clinical_link.json").read_text(encoding="utf-8"))
    assert link == {
        "submission_id": sub_id,
        "org_id": str(org_a["org_id"]),
        "order_id": str(org_a["order_id"]),
        "sample_id": str(org_a["sample_id"]),
        "interpretation_id": str(interpretation_id),
        "report_id": str(report_id),
        "vcf_id": link["vcf_id"],
        "submission_key": link["submission_key"],
    }
    # recorded by the organisation's system principal, in the organisation's own audit trail
    audited = q(
        clinical_conn,
        "SELECT action, org_id FROM audit_log WHERE action = ANY(%s) AND outcome = 'success'",
        (["sequencing_run_created", "vcf_created", "interpretation_created"],),
    )
    assert {a for a, _ in audited} == {"sequencing_run_created", "vcf_created", "interpretation_created"}
    assert {o for _, o in audited} == {org_a["org_id"]}


def test_the_draft_report_is_visible_to_a_reviewer_through_the_clinical_service(
    api, worker, store, dao, clinical_conn, service, org_a, vcf_path, engine
):
    sub_id = submit(api(org_a["org_id"], KEY_A), org_a, vcf_path)
    assert process(worker, store, sub_id).status == "complete"
    (report_id, interpretation_id) = q(
        clinical_conn, "SELECT id, interpretation_id FROM reports WHERE org_id = %s", (org_a["org_id"],)
    )[0]

    admin = admin_session(dao, org_a)
    with clinical_conn.cursor() as cur:  # the account that works the bench also reads for review: Interpreter
        cur.execute(
            "INSERT INTO role_assignments (user_id, org_id, role, assigned_by, assigned_at) "
            "VALUES (%s, %s, 'Interpreter', %s, now())",
            (admin.user_id, org_a["org_id"], admin.user_id),
        )
    clinical_conn.commit()

    adapter = HttpClinicalDataAccess(client=service)  # the human tools' own HTTPS client, over the real service
    session = adapter.login(f"admin@{org_a['name'].lower().replace(' ', '-')}.test", org_a["org_id"], "password")
    report = adapter.get_report(session, report_id)
    assert report["state"] == "draft" and str(report["interpretation_id"]) == str(interpretation_id)
    state = adapter.get_signoff_state(session, report_id)
    assert state["state"] == "draft" and not state["is_retracted"] and state["approver_id"] is None
    assert state["claims"] == []  # nothing has been signed: it is waiting for a human


def test_a_second_organisation_cannot_see_the_first_ones_submission(api, worker, store, org_a, org_b, vcf_path, engine):
    sub_id = submit(api(org_a["org_id"], KEY_A), org_a, vcf_path)
    other = api(org_b["org_id"], KEY_B)
    assert other.get(f"/interpretations/{sub_id}").status_code == 404
    assert api(org_a["org_id"], KEY_A).get(f"/interpretations/{sub_id}").status_code == 200


# ═══ QC and identity gates: nothing runs, nothing is recorded ═══


def _assert_refused_before_the_engine(done, engine, clinical_conn, org):
    assert done.status == "failed" and done.interpretation_id is None
    assert engine.runs() == []  # the engine process was never started
    assert clinical_counts(clinical_conn, org["org_id"]) == NOTHING_RECORDED


@pytest.mark.parametrize("qc", ["pending", "failed"])
def test_a_sample_that_has_not_passed_qc_never_reaches_the_engine(
    api, worker, store, dao, clinical_conn, vcf_path, engine, qc
):
    org = make_org(dao, clinical_conn, f"Org QC {qc}", qc=qc)
    done = process(worker, store, submit(api(org["org_id"], KEY_A), org, vcf_path))
    _assert_refused_before_the_engine(done, engine, clinical_conn, org)
    assert "see clinical exception" in done.error_message
    assert exception_codes(clinical_conn, org["order_id"]) == [f"qc_{qc}"]


def test_an_unknown_sample_is_refused_and_recorded_against_the_order(
    api, worker, store, clinical_conn, org_a, vcf_path, engine
):
    done = process(worker, store, submit(api(org_a["org_id"], KEY_A), org_a, vcf_path, sample_id=uuid.uuid4()))
    _assert_refused_before_the_engine(done, engine, clinical_conn, org_a)
    assert "is not a sample of order" in done.error_message
    assert exception_codes(clinical_conn, org_a["order_id"]) == ["sample_unresolved"]


def test_an_unknown_order_is_refused(api, worker, store, clinical_conn, org_a, vcf_path, engine):
    done = process(worker, store, submit(api(org_a["org_id"], KEY_A), org_a, vcf_path, order_id=uuid.uuid4()))
    _assert_refused_before_the_engine(done, engine, clinical_conn, org_a)
    assert "not found in this submission's organisation" in done.error_message


def test_another_organisations_order_and_sample_are_not_reachable_with_a_valid_key(
    api, worker, store, clinical_conn, org_a, org_b, vcf_path, engine
):
    """Organisation B holds a REAL key and names organisation A's real order and sample."""
    before = {org["name"]: clinical_counts(clinical_conn, org["org_id"]) for org in (org_a, org_b)}
    done = process(worker, store, submit(api(org_b["org_id"], KEY_B), org_a, vcf_path))
    assert done.status == "failed" and engine.runs() == []
    assert "not found in this submission's organisation" in done.error_message
    after = {org["name"]: clinical_counts(clinical_conn, org["org_id"]) for org in (org_a, org_b)}
    assert after == before
    assert exception_codes(clinical_conn, org_a["order_id"]) == []  # nothing is written onto the other tenant's order


def test_a_vcf_that_is_missing_is_refused_and_recorded(api, worker, store, clinical_conn, org_a, tmp_path, engine):
    done = process(worker, store, submit(api(org_a["org_id"], KEY_A), org_a, tmp_path / "no-such.vcf"))
    _assert_refused_before_the_engine(done, engine, clinical_conn, org_a)
    assert exception_codes(clinical_conn, org_a["order_id"]) == ["vcf_missing"]


# ═══ engine failures: a failed job never looks successful ═══

FAILURES = {
    "exit_1": ("exit1", "exit code 1", "bij_ai_error_other"),
    "interrupted_130": ("exit130", "exit code 130", "bij_ai_error_other"),
    "no_output_file": ("no_output", "did not write", "bij_ai_error_other"),
    "malformed_json": ("malformed_json", "Unexpected error", "bij_ai_error_other"),
}


@pytest.mark.parametrize("case", sorted(FAILURES))
def test_an_engine_failure_ends_failed_with_an_exception_and_records_nothing(
    api, worker, store, clinical_conn, org_a, vcf_path, engine, output_root, case
):
    mode, message, reason = FAILURES[case]
    engine.mode(mode)
    sub_id = submit(api(org_a["org_id"], KEY_A), org_a, vcf_path)
    done = process(worker, store, sub_id)
    assert done.status == "failed" and done.interpretation_id is None and message in done.error_message
    assert len(engine.runs()) == 1
    assert clinical_counts(clinical_conn, org_a["org_id"]) == NOTHING_RECORDED
    assert exception_codes(clinical_conn, org_a["order_id"]) == [reason]
    assert not (output_root / sub_id / "clinical_link.json").exists()  # nothing claims a clinical record exists
    assert api(org_a["org_id"], KEY_A).get(f"/interpretations/{sub_id}").json()["status"] == "failed"


def test_an_engine_that_hangs_is_killed_and_the_submission_fails_as_a_timeout(
    api, worker, store, clinical_conn, org_a, vcf_path, engine, monkeypatch
):
    monkeypatch.setattr(submission_worker, "INTERPRETATION_TIMEOUT_SECONDS", 2)
    engine.mode("hang")
    done = process(worker, store, submit(api(org_a["org_id"], KEY_A), org_a, vcf_path))
    assert done.status == "failed" and "timed out" in done.error_message
    assert exception_codes(clinical_conn, org_a["order_id"]) == ["bij_ai_timeout"]
    assert clinical_counts(clinical_conn, org_a["org_id"]) == NOTHING_RECORDED
    pid = engine.runs()[0]["pid"]
    deadline = time.monotonic() + 10
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not psutil.pid_exists(pid)  # the engine process was really terminated, not just abandoned


@xfail_defect("F2", "exit 0 plus `{}` is recorded as a completed interpretation and a draft report")
def test_an_engine_that_writes_an_empty_document_is_not_a_completed_run(
    api, worker, store, clinical_conn, org_a, vcf_path, engine
):
    engine.mode("empty_object")
    done = process(worker, store, submit(api(org_a["org_id"], KEY_A), org_a, vcf_path))
    assert done.status == "failed"
    assert clinical_counts(clinical_conn, org_a["org_id"]) == NOTHING_RECORDED


@xfail_defect("F3", "a stale geper_results.json from an earlier attempt satisfies the 'engine wrote output' check")
def test_an_engine_that_exits_zero_without_writing_cannot_borrow_an_earlier_attempts_results(
    api, worker, store, clinical_conn, org_a, vcf_path, engine, output_root
):
    sub_id = submit(api(org_a["org_id"], KEY_A), org_a, vcf_path)
    stale = output_root / sub_id
    stale.mkdir(parents=True)
    (stale / "geper_results.json").write_text(json.dumps({"variants": [], "stale": True}), encoding="utf-8")
    engine.mode("no_output")  # exits 0 and writes nothing this time
    done = process(worker, store, sub_id)
    assert done.status == "failed"
    assert clinical_counts(clinical_conn, org_a["org_id"]) == NOTHING_RECORDED


# ═══ idempotency and duplicates ═══


def test_a_requeued_completed_submission_is_adopted_not_rerun_and_not_duplicated(
    api, worker, store, clinical_conn, org_a, vcf_path, engine
):
    sub_id = submit(api(org_a["org_id"], KEY_A), org_a, vcf_path)
    first = process(worker, store, sub_id)
    store.update_status(sub_id, "queued")
    again = process(worker, store, sub_id)
    assert again.status == "complete" and again.interpretation_id == first.interpretation_id
    assert len(engine.runs()) == 1
    assert clinical_counts(clinical_conn, org_a["org_id"]) == {k: 1 for k in NOTHING_RECORDED}


def test_the_same_vcf_and_sample_under_a_new_submission_key_adopts_the_existing_record(
    api, worker, store, clinical_conn, org_a, vcf_path, engine
):
    client = api(org_a["org_id"], KEY_A)
    first = process(worker, store, submit(client, org_a, vcf_path, key="first"))
    second = process(worker, store, submit(client, org_a, vcf_path, key="second"))
    assert (first.status, second.status) == ("complete", "complete")
    assert second.interpretation_id == first.interpretation_id
    assert len(engine.runs()) == 1  # the engine did not run twice for the same bytes and sample
    assert clinical_counts(clinical_conn, org_a["org_id"]) == {k: 1 for k in NOTHING_RECORDED}


def test_replaying_a_submission_key_over_the_api_returns_the_same_submission(api, store, org_a, vcf_path):
    client = api(org_a["org_id"], KEY_A)
    first = submit(client, org_a, vcf_path, key="same")
    replay = client.post(
        "/interpretations",
        json={
            "submission_key": "same",
            "vcf_path": str(vcf_path),
            "assembly": "GRCh38",
            "sample_ref": "s",
            "consent_ref": "c",
            "order_id": str(org_a["order_id"]),
            "sample_id": str(org_a["sample_id"]),
        },
    )
    assert replay.status_code == 200 and replay.json()["id"] == first


def test_a_submission_left_running_by_a_dead_worker_is_reconciled_from_the_clinical_record_without_a_rerun(
    api, worker, store, clinical_conn, org_a, vcf_path, engine
):
    sub_id = submit(api(org_a["org_id"], KEY_A), org_a, vcf_path)
    done = process(worker, store, sub_id)  # the record is committed...
    store.update_status(sub_id, "interrupted")  # ...but the worker died before it could say so
    assert worker.reconcile_interrupted_submissions() == 1
    healed = store.get_submission(sub_id)
    assert healed.status == "complete" and healed.interpretation_id == done.interpretation_id
    assert len(engine.runs()) == 1


# ═══ retries ═══


def _make_transient_failure(api, worker, store, clinical_conn, org, vcf_path, engine, monkeypatch, key="k-1"):
    # `bij_ai_timeout` is one of the two reason codes classed as a TRANSIENT submission failure (the other is
    # clinical_record_write_failed); `bij_ai_error_other` (exit 1, no output, bad JSON) is an INTERPRETATION failure that
    # waits for an operator and is not retried automatically.
    monkeypatch.setattr(submission_worker, "INTERPRETATION_TIMEOUT_SECONDS", 2)
    engine.mode("hang")
    sub_id = submit(api(org["org_id"], KEY_A), org, vcf_path, key=key)
    done = process(worker, store, sub_id)
    assert done.status == "failed"
    return sub_id


def _make_retry_due(clinical_conn, order_id):
    """What the retry worker waits for: next_retry_at in the past (the existing retry tests set it the same way)."""
    with clinical_conn.cursor() as cur:
        cur.execute(
            "UPDATE exceptions SET next_retry_at = now() - interval '1 minute' "
            "WHERE order_id = %s AND reason_code = 'bij_ai_timeout'",
            (order_id,),
        )
    clinical_conn.commit()


def _run_retry_scheduler(dao, store, org):
    dao.retry_scheduler(dao._create_system_session(org["org_id"]), store)


def test_a_failed_then_retried_submission_ends_with_exactly_one_draft_report_and_a_kept_failure_record(
    api, worker, store, dao, clinical_conn, org_a, vcf_path, engine, monkeypatch
):
    sub_id = _make_transient_failure(api, worker, store, clinical_conn, org_a, vcf_path, engine, monkeypatch)
    _make_retry_due(clinical_conn, org_a["order_id"])
    _run_retry_scheduler(dao, store, org_a)
    assert store.get_submission(sub_id).status == "queued"  # re-queued by the existing retry scheduler

    engine.mode("success")
    done = process(worker, store, sub_id)
    assert done.status == "complete"
    assert len(engine.runs()) == 2
    assert clinical_counts(clinical_conn, org_a["org_id"]) == {k: 1 for k in NOTHING_RECORDED}
    ((reason, _status, message, attempts, _next),) = exception_rows(clinical_conn, org_a["order_id"])
    assert reason == "bij_ai_timeout" and "timed out" in message  # the first failure is still on record
    assert attempts == 1  # and the scheduler counted the attempt


def test_a_retry_after_success_does_not_make_a_second_report(
    api, worker, store, dao, clinical_conn, org_a, vcf_path, engine, monkeypatch
):
    sub_id = _make_transient_failure(api, worker, store, clinical_conn, org_a, vcf_path, engine, monkeypatch)
    engine.mode("success")
    store.update_status(sub_id, "queued")
    assert process(worker, store, sub_id).status == "complete"
    store.update_status(sub_id, "queued")  # a late duplicate retry
    assert process(worker, store, sub_id).status == "complete"
    assert clinical_counts(clinical_conn, org_a["org_id"]) == {k: 1 for k in NOTHING_RECORDED}
    assert len(engine.runs()) == 2  # the failure and the one success; the late retry adopted the record


@xfail_defect("F9", "next_retry_at is never initialised, so the retry worker never selects a new failure")
def test_a_transient_engine_failure_is_scheduled_for_retry(
    api, worker, store, clinical_conn, org_a, vcf_path, engine, monkeypatch
):
    _make_transient_failure(api, worker, store, clinical_conn, org_a, vcf_path, engine, monkeypatch)
    ((_reason, _status, _message, _attempts, next_retry_at),) = exception_rows(clinical_conn, org_a["order_id"])
    assert next_retry_at is not None


@xfail_defect(
    "F6", "the scheduler re-queues every failed submission of the order, including ones that failed for another reason"
)
def test_a_retry_requeues_only_the_submission_the_exception_belongs_to(
    api, worker, store, dao, clinical_conn, org_a, vcf_path, engine, monkeypatch, tmp_path
):
    transient = _make_transient_failure(
        api, worker, store, clinical_conn, org_a, vcf_path, engine, monkeypatch, key="transient"
    )
    unrelated = process(
        worker, store, submit(api(org_a["org_id"], KEY_A), org_a, tmp_path / "no-such.vcf", key="unrelated")
    )
    assert unrelated.status == "failed"  # failed for a different, non-transient reason (the VCF is missing)
    _make_retry_due(clinical_conn, org_a["order_id"])
    _run_retry_scheduler(dao, store, org_a)
    assert store.get_submission(transient).status == "queued"
    assert store.get_submission(unrelated.id).status == "failed"


@xfail_defect("F6", "re-queueing clears the submission's own error_message; only the exception row keeps the reason")
def test_a_requeued_submission_keeps_the_reason_it_failed(
    api, worker, store, dao, clinical_conn, org_a, vcf_path, engine, monkeypatch
):
    sub_id = _make_transient_failure(api, worker, store, clinical_conn, org_a, vcf_path, engine, monkeypatch)
    reason = store.get_submission(sub_id).error_message
    assert "timed out" in reason
    _make_retry_due(clinical_conn, org_a["order_id"])
    _run_retry_scheduler(dao, store, org_a)
    assert store.get_submission(sub_id).error_message == reason


# ═══ the QC gate follows the submitted sample (F1) ═══


@xfail_defect("F1", "the gate reads the order's latest sample: an older, failed sample rides on a newer sample's pass")
def test_a_failed_sample_is_not_processed_because_a_newer_sample_of_its_order_passed(
    api, worker, store, dao, clinical_conn, vcf_path, engine
):
    org = make_org(dao, clinical_conn, "Org Recollect", qc="failed")  # the original sample failed QC
    add_sample(dao, org, "passed")  # a re-collection, received later, passed
    done = process(worker, store, submit(api(org["org_id"], KEY_A), org, vcf_path))  # a VCF for the FAILED sample
    assert done.status == "failed"
    assert engine.runs() == []
    assert clinical_counts(clinical_conn, org["org_id"]) == NOTHING_RECORDED


@xfail_defect(
    "F1", "the gate reads the order's latest sample: a passed sample is blocked by a newer sample that has not"
)
def test_a_passed_sample_is_processed_even_if_a_newer_sample_of_its_order_has_not_passed(
    api, worker, store, dao, clinical_conn, vcf_path, engine
):
    org = make_org(dao, clinical_conn, "Org Second", qc="passed")
    add_sample(dao, org, "failed")  # a later specimen on the same order failed
    done = process(worker, store, submit(api(org["org_id"], KEY_A), org, vcf_path))  # a VCF for the PASSED sample
    assert done.status == "complete", done.error_message
    assert clinical_counts(clinical_conn, org["org_id"]) == {k: 1 for k in NOTHING_RECORDED}


# ═══ two workers (F7) ═══


def _race(dao, clinical_conn, store, org, vcf_path, api, engine, monkeypatch):
    """Two workers, each on its own database connection, both holding the same QUEUED row: what two worker processes
    that polled at the same moment see. The engine waits (rendezvous) so that, if both start it, both are running."""
    import psycopg
    import threading

    monkeypatch.setattr(submission_worker, "INTERPRETATION_TIMEOUT_SECONDS", 60)
    monkeypatch.setenv("STANDIN_RENDEZVOUS_COUNT", "2")
    monkeypatch.setenv("STANDIN_RENDEZVOUS_TIMEOUT", "6")
    engine.mode("rendezvous")
    sub_id = submit(api(org["org_id"], KEY_A), org, vcf_path)
    second_connection = psycopg.connect(clinical_conn.info.dsn, autocommit=False)
    try:
        second_dao = DataAccess(second_connection, clock=SystemClock(), password_hasher=_link._PlainHasher())
        workers = [InterpretationWorker(store, dao), InterpretationWorker(store, second_dao)]
        row = store.get_submission(sub_id)  # both read the queue before either has claimed the row
        threads = [threading.Thread(target=w.process_queued_submission, args=(row,)) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=90)
        assert not any(t.is_alive() for t in threads)
    finally:
        second_connection.close()
    return sub_id


def test_two_workers_racing_on_one_submission_never_create_two_reports(
    api, store, dao, clinical_conn, org_a, vcf_path, engine, output_root, monkeypatch
):
    sub_id = _race(dao, clinical_conn, store, org_a, vcf_path, api, engine, monkeypatch)
    assert store.get_submission(sub_id).status == "complete"
    assert clinical_counts(clinical_conn, org_a["org_id"]) == {k: 1 for k in NOTHING_RECORDED}  # the unique key holds


@xfail_defect("F7", "no claim: both workers run the engine for the same queued submission")
def test_two_workers_racing_on_one_submission_run_the_engine_once(
    api, store, dao, clinical_conn, org_a, vcf_path, engine, output_root, monkeypatch
):
    _race(dao, clinical_conn, store, org_a, vcf_path, api, engine, monkeypatch)
    assert len(engine.runs()) == 1
