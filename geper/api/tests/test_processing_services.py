"""
api/tests/test_processing_services.py
─────────────────────────────────────
Smoke tests for running the processing hand-off as services (docs/PROCESSING_DEPLOYMENT.md): each service has a command
that starts, bad configuration is refused before any work is taken, a stand-in job goes through a running worker loop
into a correctly linked draft report, a restart does not duplicate it, and nothing failed or partial is published.

Synthetic fixtures and the disposable database only; no engine, no network. The compose file is checked as a file (its
`docker compose config` validation is a separate step); no container is started.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from api import exception_retry_worker, service_health, submission_worker
from api.main import app
from api.submission_worker import InterpretationWorker
from api.tests import test_processing_handoff as _h
from api.tests.conftest import place_vcf

api = _h.api
dao = _h.dao
store = _h.store
store_path = _h.store_path
output_root = _h.output_root
engine = _h.engine
worker = _h.worker
org_a = _h.org_a
org_b = _h.org_b
vcf_path = _h.vcf_path
_cleanup_overrides = _h._cleanup_overrides
KEY_A = _h.KEY_A
NOTHING_RECORDED = _h.NOTHING_RECORDED
clinical_counts = _h.clinical_counts
make_org = _h.make_org
admin_session = _h.admin_session
process = _h.process
submit = _h.submit
q = _h.q
_link = _h._link
VCF_TEXT = _link.VCF_TEXT

REPO = Path(__file__).resolve().parents[3]
GEPER_DIR = REPO / "geper"
COMPOSE = REPO / "docker-compose.processing.yml"
ONE = {k: 1 for k in NOTHING_RECORDED}


# ═══ the deployment file ═══


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_the_compose_file_defines_the_three_services_with_a_command_that_exists(compose):
    import importlib.util

    services = compose["services"]
    assert set(services) == {"processing-api", "processing-worker", "processing-retry-worker"}
    modules = {
        "processing-api": ("api.main", "app"),
        "processing-worker": ("api.submission_worker", "main"),
        "processing-retry-worker": ("api.exception_retry_worker", "main"),
    }
    for name, (module, attribute) in modules.items():
        command = services[name]["command"]
        assert command[:2] == ["python", "-m"]
        started = command[2]
        if name == "processing-api":
            assert started == "uvicorn" and command[3] == f"{module}:{attribute}"
        else:
            assert started == module
        assert importlib.util.find_spec(module) is not None
    assert services["processing-api"]["working_dir"] == "/app/geper"


def test_the_compose_file_holds_no_secret_and_requires_the_ones_it_needs(compose):
    text = COMPOSE.read_text(encoding="utf-8")
    assert not re.search(r"://[^\s:@$]+:[^\s@$]+@", text), "a DSN with a literal password"
    for variable in ("CLINICAL_DSN", "GEPER_API_KEYS", "GEPER_CORS_ORIGINS", "GEPER_VCF_HOST_DIR"):
        assert re.search(rf"\$\{{{variable}:\?", text), f"{variable} is not required"


def test_only_the_two_workers_receive_the_clinical_database_credential(compose):
    """The API is the network-facing service and neither reads nor writes the clinical database (it uses the SQLite
    store and the VCF root), so it must not be handed the DSN."""
    services = compose["services"]
    assert "CLINICAL_DSN" not in services["processing-api"]["environment"]
    for name in ("processing-worker", "processing-retry-worker"):
        assert services[name]["environment"]["CLINICAL_DSN"].startswith("${CLINICAL_DSN:?"), name
    text = COMPOSE.read_text(encoding="utf-8")
    assert len(re.findall(r"^\s+CLINICAL_DSN:", text, re.MULTILINE)) == 2  # exactly the two workers set it as a key


def test_the_api_starts_and_is_ready_without_the_clinical_database_setting(good_config, monkeypatch):
    """What makes removing it safe: nothing the API runs reads CLINICAL_DSN."""
    set_dev_insecure(monkeypatch, False)
    monkeypatch.delenv("CLINICAL_DSN")
    assert service_health.validate("api") == []
    with TestClient(app) as client:
        assert client.get("/readyz").json() == {"status": "ready"}
    # and the workers still refuse without it
    assert "clinical_dsn_missing" in codes("submission_worker") and "clinical_dsn_missing" in codes("retry_worker")


def test_the_blast_policy_reaches_only_the_submission_worker_defaults_on_and_stays_configurable(compose):
    """Clinical workflow = AI-only (no BLAST). The engine's own default is untouched; the setting can be turned off."""
    services = compose["services"]
    assert services["processing-worker"]["environment"]["GEPER_AI_ONLY"] == "${GEPER_AI_ONLY:-true}"
    for name in ("processing-api", "processing-retry-worker"):
        assert "GEPER_AI_ONLY" not in services[name]["environment"], f"{name} does not run the engine"
    assert "GEPER_AI_ONLY" in (REPO / ".env.processing.example").read_text(encoding="utf-8")
    deployment_doc = (REPO / "docs" / "PROCESSING_DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "GEPER_AI_ONLY" in deployment_doc and "api.genome.ucsc.edu" in deployment_doc


@pytest.mark.parametrize(
    "value,expected",
    [(None, False), ("true", True), ("1", True), ("yes", True), ("on", True)]
    + [("false", False), ("0", False), ("no", False), ("off", False), ("FALSE", False), ("bogus", True)],
)
def test_the_engines_ai_only_switch_defaults_off_and_only_the_documented_values_turn_it_off(value, expected):
    """The deployment docs say: only 0/false/no/off mean off, anything else (even a typo) means AI-only; unset = off."""
    env = {k: v for k, v in os.environ.items() if k != "GEPER_AI_ONLY"}
    if value is not None:
        env["GEPER_AI_ONLY"] = value
    env["PYTHONPATH"] = os.pathsep.join([str(REPO), str(GEPER_DIR)])
    result = subprocess.run(
        [sys.executable, "-c", "from config import CONFIG; print(CONFIG.AI_ONLY_MODE)"],
        cwd=GEPER_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-400:]
    assert result.stdout.strip().splitlines()[-1] == str(expected)


def test_the_compose_file_keeps_the_storage_and_exposure_policy(compose):
    services = compose["services"]
    for name, service in services.items():
        assert "ALL" in service["cap_drop"] and "no-new-privileges:true" in service["security_opt"]
        assert service["healthcheck"]["test"], name
        assert service["restart"] == "unless-stopped"
        mounts = [v for v in service["volumes"] if isinstance(v, str) and v.endswith(":/data/vcf:ro")]
        assert len(mounts) == 1, f"{name}: VCFs must be mounted read-only"
        assert service["environment"]["GEPER_VCF_ROOT"] == "/data/vcf"
        assert service["environment"]["GEPER_SUBMISSION_STORE_PATH"].startswith("/data/state/")
    assert all(
        p.startswith("127.0.0.1:") for p in services["processing-api"]["ports"]
    )  # loopback unless a proxy fronts it
    assert services["processing-worker"]["stop_grace_period"] == "120s"


# ═══ configuration is checked before any work is taken ═══


@pytest.fixture
def good_config(tmp_path, monkeypatch):
    root = tmp_path / "vcf-root"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("GEPER_VCF_ROOT", str(root))
    monkeypatch.setenv("GEPER_SUBMISSION_STORE_PATH", str(tmp_path / "state" / "s.db"))
    (tmp_path / "state").mkdir(exist_ok=True)
    monkeypatch.setenv("GEPER_SUBMISSION_OUTPUT_ROOT", str(tmp_path / "out"))
    monkeypatch.setenv("CLINICAL_DSN", "postgresql://example.invalid/none")
    monkeypatch.delenv("BIJ_AI_CLI_PATH", raising=False)
    return root


@pytest.mark.parametrize("role", service_health.ROLES)
def test_a_complete_configuration_has_no_problems(good_config, role):
    assert service_health.validate(role) == []


def codes(role):
    return {code for code, _message in service_health.validate(role)}


def test_each_missing_or_invalid_setting_is_named(good_config, tmp_path, monkeypatch):
    monkeypatch.delenv("GEPER_VCF_ROOT")
    assert "vcf_root_not_configured" in codes("api") and "vcf_root_not_configured" in codes("submission_worker")
    assert "vcf_root_not_configured" not in codes("retry_worker")  # it never reads a VCF

    monkeypatch.setenv("GEPER_VCF_ROOT", str(tmp_path / "does-not-exist"))
    assert "vcf_root_not_configured" in codes("api")
    (tmp_path / "a-file").write_text("x", encoding="utf-8")
    monkeypatch.setenv("GEPER_VCF_ROOT", str(tmp_path / "a-file"))
    assert "vcf_root_not_configured" in codes("submission_worker")

    monkeypatch.setenv("GEPER_VCF_ROOT", str(tmp_path))
    monkeypatch.delenv("CLINICAL_DSN")
    assert "clinical_dsn_missing" in codes("submission_worker") and "clinical_dsn_missing" in codes("retry_worker")
    assert "clinical_dsn_missing" not in codes("api")

    monkeypatch.setenv("GEPER_SUBMISSION_STORE_PATH", str(tmp_path / "no-such-dir" / "s.db"))
    assert all("store_dir_not_writable" in codes(r) for r in service_health.ROLES)

    monkeypatch.setenv("BIJ_AI_CLI_PATH", str(tmp_path / "no-engine.py"))
    assert "engine_not_found" in codes("submission_worker")


@pytest.mark.skipif(os.name != "posix", reason="world-writable is a POSIX permission")
def test_a_world_writable_vcf_root_is_refused(good_config):
    good_config.chmod(0o777)
    assert "vcf_root_world_writable" in codes("api") and "vcf_root_world_writable" in codes("submission_worker")
    good_config.chmod(0o755)
    assert service_health.validate("api") == []


def clean_environment(tmp_path, **extra):
    """A process environment with none of the service settings, so what is missing is exactly what the test says."""
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE")}
    keep["PYTHONPATH"] = os.pathsep.join([str(REPO), str(GEPER_DIR)])
    keep["GEPER_SUBMISSION_STORE_PATH"] = str(tmp_path / "s.db")
    keep.update(extra)
    return keep


@pytest.mark.parametrize(
    "module,expected",
    [("api.submission_worker", "vcf_root_not_configured"), ("api.exception_retry_worker", "clinical_dsn_missing")],
)
def test_a_worker_process_with_missing_configuration_exits_1_naming_the_problem_and_no_secret(
    tmp_path, module, expected
):
    secret = "s3cret-value-must-not-appear"
    env = clean_environment(tmp_path, GEPER_API_KEYS=f"{secret}:00000000-0000-0000-0000-000000000001")
    result = subprocess.run(
        [sys.executable, "-m", module], cwd=GEPER_DIR, env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 1
    assert expected in result.stderr and "refusing to start" in result.stderr
    assert secret not in result.stdout + result.stderr


def set_dev_insecure(monkeypatch, value):
    """
    Set the API's dev-mode flag on the module that THE `app` OBJECT came from. Patching the name `api.main._DEV_INSECURE`
    patches whatever `sys.modules` holds now, and in a long session (CI runs the whole geper suite in one process, with
    GEPER_DEV_INSECURE=1 in its environment) another test may have imported the module afresh, leaving this `app`
    reading its own, older, flag.
    """
    import inspect

    lifespan = inspect.unwrap(app.router.lifespan_context)
    monkeypatch.setitem(lifespan.__globals__, "_DEV_INSECURE", value)


# ═══ the API's startup and probes ═══


def test_the_api_refuses_to_start_without_a_vcf_root_and_serves_probes_with_one(good_config, monkeypatch):
    set_dev_insecure(monkeypatch, False)
    monkeypatch.delenv("GEPER_VCF_ROOT")
    with pytest.raises(RuntimeError, match="vcf_root_not_configured"):
        with TestClient(app):
            pass

    monkeypatch.setenv("GEPER_VCF_ROOT", str(good_config))
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").json() == {"status": "ready"}
        monkeypatch.delenv("GEPER_VCF_ROOT")  # it goes away while running
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready", "problems": ["vcf_root_not_configured"]}
        assert str(good_config) not in response.text  # codes only: the route is unauthenticated


def test_readiness_notices_a_store_that_exists_but_cannot_be_opened_and_accepts_one_not_yet_created(
    good_config, monkeypatch
):
    set_dev_insecure(monkeypatch, False)
    store_file = Path(os.environ["GEPER_SUBMISSION_STORE_PATH"])
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200  # no store yet: the first use creates it
        store_file.write_bytes(b"this is not a sqlite database" * 50)
        response = client.get("/readyz")
        assert response.status_code == 503 and response.json()["problems"] == ["store_unreadable"]
        assert str(store_file) not in response.text
        store_file.unlink()
        from api.submission_store import SubmissionStore

        SubmissionStore(store_file, reconcile_on_open=False)
        assert client.get("/readyz").status_code == 200  # a real store is fine


# ═══ heartbeat and stop ═══


def test_the_heartbeat_check_says_missing_stale_or_fresh(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GEPER_HEARTBEAT_FILE", str(tmp_path / "beat"))
    assert service_health.main(["heartbeat", "retry_worker"]) == 1  # never beat
    service_health.make_heartbeat("retry_worker")()
    assert service_health.main(["heartbeat", "retry_worker"]) == 0
    (tmp_path / "beat").write_text(repr(time.time() - 4000), encoding="utf-8")
    assert service_health.main(["heartbeat", "retry_worker"]) == 1
    assert service_health.main(["heartbeat", "retry_worker", "--max-age", "5000"]) == 0
    # the submission worker is allowed one full engine timeout between beats
    assert service_health.default_max_age("submission_worker") > submission_worker.INTERPRETATION_TIMEOUT_SECONDS


def test_a_stop_signal_asks_the_worker_to_stop_and_nothing_else(monkeypatch):
    class Stub:
        stopped = False

        def request_stop(self):
            self.stopped = True

    stub = Stub()
    previous = {n: signal.getsignal(getattr(signal, n)) for n in ("SIGTERM", "SIGINT")}
    try:
        service_health.install_stop_handlers(stub)
        signal.raise_signal(signal.SIGTERM)
        assert stub.stopped
    finally:
        for name, handler in previous.items():
            signal.signal(getattr(signal, name), handler)


def run_in_thread(target):
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def test_the_retry_worker_loop_beats_and_stops_when_asked():
    class NoOrganisations:
        passes = 0

        def ping(self):
            return True

        def list_active_organisation_ids(self):
            NoOrganisations.passes += 1
            return []

    beats = []
    retry = exception_retry_worker.ExceptionRetryWorker(NoOrganisations(), None)
    retry.heartbeat = lambda: beats.append(1)
    thread = run_in_thread(lambda: retry.run_loop(poll_interval=0.05))
    deadline = time.time() + 20
    while len(beats) < 3 and time.time() < deadline:
        time.sleep(0.05)
    retry.request_stop()
    thread.join(timeout=20)
    assert not thread.is_alive() and len(beats) >= 3 and NoOrganisations.passes >= 3


# ═══ a stand-in job through a running worker loop ═══


def wait_for(predicate, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("timed out waiting for the worker loop")


def run_worker_loop(worker):
    thread = run_in_thread(lambda: worker.run_loop(poll_interval=0.1))
    return thread


def stop(worker, thread):
    worker.request_stop()
    thread.join(timeout=60)
    assert not thread.is_alive()


def draft_report_chain(conn, org_id):
    return q(
        conn,
        "SELECT r.state, s.sample_id, o.order_id, o.patient_id, o.org_id FROM reports r "
        "JOIN interpretations i ON i.org_id = r.org_id AND i.id = r.interpretation_id "
        "JOIN vcfs v ON v.org_id = i.org_id AND v.id = i.vcf_id "
        "JOIN sequencing_runs sr ON sr.org_id = v.org_id AND sr.id = v.sequencing_run_id "
        "JOIN samples s ON s.org_id = sr.org_id AND s.sample_id = sr.sample_id "
        "JOIN orders o ON o.org_id = s.org_id AND o.order_id = s.order_id WHERE r.org_id = %s",
        (org_id,),
    )


def test_a_submitted_job_is_processed_by_the_running_loop_into_a_correctly_linked_draft_and_a_restart_adds_nothing(
    api, store, dao, clinical_conn, org_a, vcf_path, engine, output_root, tmp_path, monkeypatch
):
    monkeypatch.setenv("GEPER_HEARTBEAT_FILE", str(tmp_path / "worker.beat"))
    sub_id = submit(api(org_a["org_id"], KEY_A), org_a, vcf_path)

    first = InterpretationWorker(store, dao)
    first.heartbeat = service_health.make_heartbeat("submission_worker")
    thread = run_worker_loop(first)
    wait_for(lambda: store.get_submission(sub_id).status == "complete")
    stop(first, thread)
    assert service_health.heartbeat_age("submission_worker") is not None

    ((state, sample_id, order_id, patient_id, org_id),) = draft_report_chain(clinical_conn, org_a["org_id"])
    assert state == "draft"
    assert (str(sample_id), str(order_id), str(org_id)) == (
        str(org_a["sample_id"]),
        str(org_a["order_id"]),
        str(org_a["org_id"]),
    )
    ((expected_patient,),) = q(clinical_conn, "SELECT patient_id FROM orders WHERE order_id = %s", (org_a["order_id"],))
    assert patient_id == expected_patient
    assert clinical_counts(clinical_conn, org_a["org_id"]) == ONE and len(engine.runs()) == 1

    # a restarted worker finds nothing to do and cannot create a second report or run the engine again
    second = InterpretationWorker(store, dao)
    thread = run_worker_loop(second)
    time.sleep(1.0)
    stop(second, thread)
    assert clinical_counts(clinical_conn, org_a["org_id"]) == ONE and len(engine.runs()) == 1
    assert store.get_submission(sub_id).status == "complete"


def test_a_stop_request_lets_the_job_in_progress_finish_and_claims_no_other(
    api, store, dao, clinical_conn, org_a, vcf_path, engine, output_root
):
    engine.mode("slow")  # about a second per run
    client = api(org_a["org_id"], KEY_A)
    first = submit(client, org_a, vcf_path, key="first")
    second = submit(client, org_a, vcf_path, key="second")
    running = InterpretationWorker(store, dao)
    thread = run_worker_loop(running)
    wait_for(lambda: store.get_submission(first).status == "running")
    running.request_stop()  # while the first is mid-run
    thread.join(timeout=90)
    assert not thread.is_alive()
    assert store.get_submission(first).status == "complete"  # finished, not abandoned
    assert store.get_submission(second).status == "queued"  # not started
    assert len(engine.runs()) == 1
    # the first made exactly one draft; the second job (same VCF and sample) was never started
    assert clinical_counts(clinical_conn, org_a["org_id"]) == ONE


def test_failed_or_partial_output_is_never_published_by_the_running_loop(
    api, store, dao, clinical_conn, org_a, vcf_path, engine, output_root
):
    engine.mode("partial_fail")  # writes a checkpoint, then exits 1
    sub_id = submit(api(org_a["org_id"], KEY_A), org_a, vcf_path)
    running = InterpretationWorker(store, dao)
    thread = run_worker_loop(running)
    wait_for(lambda: store.get_submission(sub_id).status == "failed")
    stop(running, thread)
    assert clinical_counts(clinical_conn, org_a["org_id"]) == NOTHING_RECORDED
    assert not (output_root / sub_id / submission_worker.CLINICAL_LINK_FILENAME).exists()


def test_an_invalid_vcf_path_is_rejected_at_the_api_and_never_reaches_a_worker(api, store, org_a):
    response = api(org_a["org_id"], KEY_A).post(
        "/interpretations",
        json={
            "submission_key": "bad",
            "vcf_path": "/etc/passwd.vcf",
            "assembly": "GRCh38",
            "sample_ref": "s",
            "consent_ref": "c",
            "order_id": str(org_a["order_id"]),
            "sample_id": str(org_a["sample_id"]),
        },
    )
    assert response.status_code == 400 and store.get_queued_submissions(limit=10) == []


def test_the_retry_worker_loop_requeues_a_resolved_failure_and_the_worker_completes_it_once(
    api, store, dao, clinical_conn, vcf_root, engine, output_root
):
    org = make_org(dao, clinical_conn, "Org Recovery", qc="pending")
    sub_id = submit(api(org["org_id"], KEY_A), org, place_vcf(vcf_root, org["org_id"], VCF_TEXT))
    assert process(InterpretationWorker(store, dao), store, sub_id).status == "failed"  # QC still pending
    ((exc_id,),) = q(clinical_conn, "SELECT id FROM exceptions WHERE order_id = %s", (org["order_id"],))
    session = admin_session(dao, org)
    dao.record_qc(session, org["sample_id"], "passed")
    dao.resolve_exception(session, exc_id, "retry_check", "QC re-run", str(session.user_id))
    clinical_conn.commit()

    retry = exception_retry_worker.ExceptionRetryWorker(dao, store)
    thread = run_in_thread(lambda: retry.run_loop(poll_interval=0.1))
    wait_for(lambda: store.get_submission(sub_id).status == "queued")
    retry.request_stop()
    thread.join(timeout=60)
    assert not thread.is_alive()

    running = InterpretationWorker(store, dao)
    thread = run_worker_loop(running)
    wait_for(lambda: store.get_submission(sub_id).status == "complete")
    stop(running, thread)
    assert clinical_counts(clinical_conn, org["org_id"]) == ONE


# ═══ the real command, as a process ═══


def test_the_submission_worker_command_runs_a_job_and_stops_cleanly_on_request(
    api, store, store_path, dao, clinical_conn, clinical_dsn, org_a, vcf_path, engine, output_root, vcf_root, tmp_path
):
    """`python -m api.submission_worker`, started the way the compose file starts it, against the disposable database."""
    sub_id = submit(api(org_a["org_id"], KEY_A), org_a, vcf_path)
    clinical_conn.commit()  # the organisation, order and sample must be visible to the other process
    env = dict(os.environ)
    env.update(
        CLINICAL_DSN=clinical_dsn,
        GEPER_VCF_ROOT=str(vcf_root),
        GEPER_SUBMISSION_STORE_PATH=str(store_path),
        GEPER_SUBMISSION_OUTPUT_ROOT=str(output_root),
        GEPER_HEARTBEAT_FILE=str(tmp_path / "proc.beat"),
        GEPER_WORKER_POLL_INTERVAL="1",
        PYTHONPATH=os.pathsep.join([str(REPO), str(GEPER_DIR)]),
    )
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        [sys.executable, "-m", "api.submission_worker"],
        cwd=GEPER_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=flags,
    )
    try:
        wait_for(lambda: store.get_submission(sub_id).status in ("complete", "failed") or proc.poll() is not None, 120)
        assert proc.poll() is None, proc.stdout.read()
        assert store.get_submission(sub_id).status == "complete", store.get_submission(sub_id).error_message
        assert (tmp_path / "proc.beat").exists()
        proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
        output, _ = proc.communicate(timeout=60)
        assert proc.returncode == 0, output
        assert "worker stopped" in output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert len(draft_report_chain(clinical_conn, org_a["org_id"])) == 1
    assert uuid.UUID(str(org_a["org_id"]))


# ═══ the clinical database going away ═══


class FakeDao:
    def __init__(self, alive):
        self.alive = alive
        self.pings = 0

    def ping(self):
        self.pings += 1
        return self.alive

    def list_active_organisation_ids(self):
        return []


class FakeStore:
    claimed = 0

    def get_queued_submissions(self, limit=10):
        FakeStore.claimed += 1
        return []

    def get_submissions_with_status(self, status, limit=100):
        return []


def test_the_submission_worker_gives_up_without_claiming_or_beating_when_the_database_is_gone():
    FakeStore.claimed = 0
    beats = []
    lost = InterpretationWorker(FakeStore(), FakeDao(alive=False))
    lost.heartbeat = lambda: beats.append(1)
    lost.run_loop(poll_interval=0.01)
    assert lost.exit_code == 1 and beats == [1]  # only the start-up beat: none once the database was unreachable
    assert FakeStore.claimed == 0  # nothing was claimed while it was gone


def test_the_submission_worker_keeps_going_while_the_database_answers():
    well = InterpretationWorker(FakeStore(), FakeDao(alive=True))
    beats = []
    well.heartbeat = lambda: beats.append(1)
    thread = run_in_thread(lambda: well.run_loop(poll_interval=0.02))
    deadline = time.time() + 20
    while len(beats) < 4 and time.time() < deadline:
        time.sleep(0.02)
    well.request_stop()
    thread.join(timeout=20)
    assert well.exit_code == 0 and len(beats) >= 4


def test_the_retry_worker_gives_up_without_beating_when_the_database_is_gone():
    beats = []
    lost = exception_retry_worker.ExceptionRetryWorker(FakeDao(alive=False), None)
    lost.heartbeat = lambda: beats.append(1)
    lost.run_loop(poll_interval=0.01)
    assert lost.exit_code == 1 and beats == []


def test_ping_is_true_on_a_working_connection_true_after_a_failed_statement_and_false_when_the_connection_is_gone(
    dao, clinical_conn
):
    assert dao.ping() is True
    with pytest.raises(Exception):  # noqa: B017 -- any database error: it leaves the transaction failed
        with clinical_conn.cursor() as cur:
            cur.execute("SELECT * FROM a_table_that_does_not_exist")
    assert dao.ping() is True  # a failed transaction is recoverable, not a lost connection
    clinical_conn.close()
    assert dao.ping() is False


@pytest.mark.parametrize("module", [submission_worker, exception_retry_worker])
def test_a_worker_connects_to_the_database_with_a_timeout(module, tmp_path, monkeypatch, good_config):
    """Without one, a database that does not answer hangs the start-up forever instead of failing it."""
    import types

    seen = {}

    def connect(dsn, **kwargs):
        seen.update(kwargs)
        raise OSError("unreachable")

    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=connect))
    monkeypatch.setenv("BIJ_AI_CLI_PATH", str(Path(submission_worker.__file__).resolve().parent.parent / "main.py"))
    with pytest.raises(SystemExit) as exit_info:
        module.main()
    assert exit_info.value.code == 1 and seen.get("connect_timeout")
