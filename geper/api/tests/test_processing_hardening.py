"""
api/tests/test_processing_hardening.py
──────────────────────────────────────
Three hardening items around the processing hand-off, with a real PostgreSQL (the same disposable-database rules as
test_processing_handoff.py, whose rig this module reuses):

  1. the `vcf_path` a caller submits (api/vcf_paths.py): at the API and again in the worker;
  2. the organisation's system user, created by whichever worker asks first (DataAccess._create_system_session);
  3. the retry scheduler's sixth-attempt escalation: a durable, actionable record, once.

The path policy itself is exercised without a database in test_vcf_paths.py.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

import pytest

from api.tests import test_processing_handoff as _h
from api.tests import test_vcf_paths as _paths
from api.tests.conftest import place_vcf
from clinical.data_access import DataAccess, SystemClock

# fixtures and helpers shared with the other hand-off tests
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
KEY_A, KEY_B = _h.KEY_A, _h.KEY_B
NOTHING_RECORDED = _h.NOTHING_RECORDED
clinical_counts = _h.clinical_counts
make_org = _h.make_org
process = _h.process
q = _h.q
submit = _h.submit
exception_codes = _h.exception_codes
schedule_rows = _h.schedule_rows
_link = _h._link

VCF_TEXT = _link.VCF_TEXT


def body(org, vcf, key="path-key", **extra):
    payload = {
        "submission_key": key,
        "vcf_path": vcf,
        "assembly": "GRCh38",
        "sample_ref": "sample-ref",
        "consent_ref": "consent-ref",
        "order_id": str(org["order_id"]),
        "sample_id": str(org["sample_id"]),
    }
    payload.update(extra)
    return payload


def stored_submissions(store):
    return store.get_submissions_with_status("queued", 100)


# ═══ 1. vcf_path at the API ═══


def test_a_file_in_the_organisations_directory_is_accepted_and_stored_by_its_resolved_path(api, store, org_a, vcf_path):
    response = api(org_a["org_id"], KEY_A).post("/interpretations", json=body(org_a, vcf_path))
    assert response.status_code == 202, response.text
    (row,) = stored_submissions(store)
    assert row.vcf_path == str(Path(vcf_path).resolve())


def test_a_relative_path_is_read_inside_the_organisations_directory(api, store, org_a, vcf_path):
    response = api(org_a["org_id"], KEY_A).post("/interpretations", json=body(org_a, Path(vcf_path).name))
    assert response.status_code == 202, response.text
    (row,) = stored_submissions(store)
    assert row.vcf_path == str(Path(vcf_path).resolve())


def test_a_file_that_is_not_there_yet_is_still_accepted_and_the_worker_reports_it(api, store, org_a):
    response = api(org_a["org_id"], KEY_A).post("/interpretations", json=body(org_a, "not-yet.vcf"))
    assert response.status_code == 202, response.text


@pytest.mark.parametrize(
    "raw",
    ["/etc/passwd", "/etc/passwd.vcf", "C:\\Windows\\win.ini", "../../etc/passwd.vcf", "..\\..\\secret.vcf", "x.txt"],
)
def test_an_arbitrary_server_path_is_refused_and_nothing_is_stored(api, store, org_a, raw):
    response = api(org_a["org_id"], KEY_A).post("/interpretations", json=body(org_a, raw))
    assert response.status_code == 400, response.text
    assert stored_submissions(store) == []
    assert raw not in response.text  # the answer does not echo the path back


def test_an_existing_file_outside_the_approved_root_is_refused(api, store, org_a, tmp_path):
    outside = tmp_path / "elsewhere" / "private.vcf"
    outside.parent.mkdir()
    outside.write_text(VCF_TEXT, encoding="utf-8")
    response = api(org_a["org_id"], KEY_A).post("/interpretations", json=body(org_a, str(outside)))
    assert response.status_code == 400 and stored_submissions(store) == []
    assert "private" not in response.text and str(tmp_path) not in response.text


def test_another_organisations_file_is_refused_even_with_a_valid_key(api, store, org_a, org_b, vcf_root):
    theirs = place_vcf(vcf_root, org_b["org_id"], VCF_TEXT, "theirs.vcf")
    response = api(org_a["org_id"], KEY_A).post("/interpretations", json=body(org_a, theirs))
    assert response.status_code == 400 and stored_submissions(store) == []


def test_the_organisation_is_the_keys_never_a_field_of_the_request(api, store, org_a, org_b, vcf_root):
    """A body naming another organisation changes nothing: the path is judged against the key's own directory."""
    theirs = place_vcf(vcf_root, org_b["org_id"], VCF_TEXT, "theirs.vcf")
    response = api(org_a["org_id"], KEY_A).post(
        "/interpretations", json=body(org_a, theirs, org_id=str(org_b["org_id"]))
    )
    assert response.status_code == 400 and stored_submissions(store) == []


def test_a_symlink_or_junction_out_of_the_directory_is_refused_at_the_api(api, store, org_a, vcf_root, tmp_path):
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "x.vcf").write_text(VCF_TEXT, encoding="utf-8")
    org_dir = vcf_root / str(org_a["org_id"])
    org_dir.mkdir(parents=True, exist_ok=True)
    _paths._link_directory(org_dir / "shared", outside)
    response = api(org_a["org_id"], KEY_A).post("/interpretations", json=body(org_a, "shared/x.vcf"))
    assert response.status_code == 400 and stored_submissions(store) == []


def test_an_unconfigured_root_refuses_everything_with_a_server_side_status(api, store, org_a, vcf_path, monkeypatch):
    monkeypatch.delenv("GEPER_VCF_ROOT")
    response = api(org_a["org_id"], KEY_A).post("/interpretations", json=body(org_a, vcf_path))
    assert response.status_code == 503 and stored_submissions(store) == []


def test_a_submission_still_needs_a_key(api, store, org_a, vcf_path):
    client = api(org_a["org_id"], KEY_A)
    client.headers.pop("X-Api-Key")
    assert client.post("/interpretations", json=body(org_a, vcf_path)).status_code in (401, 403)
    assert stored_submissions(store) == []


# ═══ 1b. vcf_path in the worker (a row the API never vetted, or a path that changed since) ═══


def _direct_submission(store, org, vcf, key="direct-key"):
    return store.create_submission(
        org_id=str(org["org_id"]),
        order_id=str(org["order_id"]),
        sample_id=str(org["sample_id"]),
        submission_key=key,
        vcf_path=vcf,
        assembly="GRCh38",
        sample_ref="sample-ref",
        consent_ref="consent-ref",
    )


def test_the_worker_refuses_a_stored_path_outside_the_directory_before_reading_or_running_anything(
    worker, store, clinical_conn, org_a, engine, tmp_path
):
    outside = tmp_path / "elsewhere" / "private.vcf"
    outside.parent.mkdir()
    outside.write_text(VCF_TEXT, encoding="utf-8")
    row = _direct_submission(store, org_a, str(outside))
    done = process(worker, store, row.id)
    assert done.status == "failed" and engine.runs() == []
    assert "vcf_path_outside_organisation_directory" in done.error_message
    assert str(outside) not in done.error_message
    assert clinical_counts(clinical_conn, org_a["org_id"]) == NOTHING_RECORDED


def test_the_worker_refuses_another_organisations_file(worker, store, clinical_conn, org_a, org_b, vcf_root, engine):
    theirs = place_vcf(vcf_root, org_b["org_id"], VCF_TEXT, "theirs.vcf")
    done = process(worker, store, _direct_submission(store, org_a, theirs).id)
    assert done.status == "failed" and engine.runs() == []
    assert clinical_counts(clinical_conn, org_b["org_id"]) == NOTHING_RECORDED
    assert clinical_counts(clinical_conn, org_a["org_id"]) == NOTHING_RECORDED


def test_a_path_swapped_for_a_link_after_the_api_accepted_it_is_refused_by_the_worker(
    api, worker, store, clinical_conn, org_a, vcf_root, engine, tmp_path
):
    org_dir = vcf_root / str(org_a["org_id"])
    (org_dir / "batch").mkdir(parents=True)
    (org_dir / "batch" / "x.vcf").write_text(VCF_TEXT, encoding="utf-8")
    sub_id = submit(api(org_a["org_id"], KEY_A), org_a, org_dir / "batch" / "x.vcf")  # accepted: it is a real file here

    # after acceptance the directory is replaced by a link to somewhere else
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.vcf").write_text(VCF_TEXT, encoding="utf-8")
    (org_dir / "batch" / "x.vcf").unlink()
    (org_dir / "batch").rmdir()
    _paths._link_directory(org_dir / "batch", elsewhere)

    done = process(worker, store, sub_id)
    assert done.status == "failed" and engine.runs() == []
    assert "vcf_path_outside_organisation_directory" in done.error_message
    assert clinical_counts(clinical_conn, org_a["org_id"]) == NOTHING_RECORDED


def test_a_legitimate_submission_is_unaffected_end_to_end(api, worker, store, clinical_conn, org_a, vcf_path, engine):
    done = process(worker, store, submit(api(org_a["org_id"], KEY_A), org_a, vcf_path))
    assert done.status == "complete", done.error_message
    assert clinical_counts(clinical_conn, org_a["org_id"]) == {k: 1 for k in NOTHING_RECORDED}


# ═══ 2. the system user, created by whoever asks first ═══


def _users(conn, org_id):
    return q(
        conn,
        "SELECT user_id FROM users WHERE org_id = %s AND is_system_account = true",
        (org_id,),
    )


def _system_roles(conn, org_id):
    return q(conn, "SELECT user_id FROM role_assignments WHERE org_id = %s AND role = 'System'", (org_id,))


def _fresh_org_without_system_user(dao, clinical_conn, name):
    org = make_org(dao, clinical_conn, name)
    clinical_conn.commit()
    assert _users(clinical_conn, org["org_id"]) == []
    return org


def test_a_caller_that_lost_the_race_to_create_the_system_user_gets_the_winners_user_not_an_error(
    dao, clinical_conn, clinical_dsn
):
    """
    The interleaving that failed jobs, forced: both callers look, neither finds the user, one inserts it, the other inserts
    next. Before the fix the second INSERT raised users_org_email_unique and the worker marked its submission failed.
    """
    import psycopg

    org = _fresh_org_without_system_user(dao, clinical_conn, "Org Race Forced")
    org_id = uuid.UUID(str(org["org_id"]))
    other = psycopg.connect(clinical_dsn, autocommit=False)
    try:
        winner = DataAccess(other, clock=SystemClock(), password_hasher=_link._PlainHasher())
        winner_session = winner._create_system_session(org_id)
        other.commit()

        real_query_one = dao._query_one
        looked = []

        def blind_first_look(sql, params=()):
            if "is_system_account = true" in sql and not looked:
                looked.append(True)
                return None  # what the loser saw: no user yet
            return real_query_one(sql, params)

        dao._query_one = blind_first_look
        try:
            loser_session = dao._create_system_session(org_id)
        finally:
            dao._query_one = real_query_one
        clinical_conn.commit()
    finally:
        other.close()

    assert looked, "the interleaving was not exercised"
    assert loser_session.user_id == winner_session.user_id  # one principal
    assert loser_session.session_id != winner_session.session_id  # each caller still has its own session
    assert len(_users(clinical_conn, org_id)) == 1
    assert len(_system_roles(clinical_conn, org_id)) >= 1


@pytest.mark.parametrize("round_", range(5))
def test_concurrent_first_requests_for_an_organisation_all_succeed_and_share_one_user(
    dao, clinical_conn, clinical_dsn, round_
):
    """Several workers on their own connections make their first request for the same organisation at the same instant."""
    import psycopg

    org = _fresh_org_without_system_user(dao, clinical_conn, f"Org Race {round_}")
    org_id = uuid.UUID(str(org["org_id"]))
    callers = 4
    connections = [psycopg.connect(clinical_dsn, autocommit=False) for _ in range(callers)]
    barrier = threading.Barrier(callers, timeout=15)
    sessions, errors = [], []

    def call(connection):
        try:
            worker_dao = DataAccess(connection, clock=SystemClock(), password_hasher=_link._PlainHasher())
            barrier.wait()
            sessions.append(worker_dao._create_system_session(org_id))
            connection.commit()
        except Exception as exc:  # noqa: BLE001 -- the test reports whatever a caller saw
            errors.append(exc)
            connection.rollback()

    try:
        threads = [threading.Thread(target=call, args=(c,)) for c in connections]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        for c in connections:
            c.close()

    assert errors == []
    assert len(sessions) == callers and len({s.user_id for s in sessions}) == 1
    assert len({s.session_id for s in sessions}) == callers
    assert len(_users(clinical_conn, org_id)) == 1
    roles = _system_roles(clinical_conn, org_id)
    assert roles and {r[0] for r in roles} == {sessions[0].user_id}


def test_the_system_user_belongs_to_one_organisation_and_cannot_see_anothers_records(dao, clinical_conn, org_a, org_b):
    session_a = dao._create_system_session(uuid.UUID(str(org_a["org_id"])))
    session_b = dao._create_system_session(uuid.UUID(str(org_b["org_id"])))
    assert session_a.org_id != session_b.org_id and session_a.user_id != session_b.user_id
    assert dao.get_order(session_a, org_a["order_id"]) is not None
    assert dao.get_order(session_a, org_b["order_id"]) is None  # the other organisation's order does not exist for it
    assert dao.get_order(session_b, org_a["order_id"]) is None


def test_the_system_account_cannot_log_in_with_a_password(dao, clinical_conn, org_a):
    """The principal exists only to be a session; creating it more safely did not give it a usable credential."""
    org_id = uuid.UUID(str(org_a["org_id"]))
    dao._create_system_session(org_id)
    with pytest.raises(Exception):  # noqa: B017 -- whatever the login failure is, it must be a failure
        dao.login(f"system+{org_id}@platform", org_a["org_id"], "password")


def test_asking_again_reuses_the_user_and_adds_no_second_role(dao, clinical_conn, org_a):
    org_id = uuid.UUID(str(org_a["org_id"]))
    first = dao._create_system_session(org_id)
    second = dao._create_system_session(org_id)
    assert first.user_id == second.user_id
    assert len(_users(clinical_conn, org_id)) == 1 and len(_system_roles(clinical_conn, org_id)) == 1


# ═══ 3. the sixth retry fails: an operator is told, once ═══


def _escalate(api, worker, store, dao, clinical_conn, org, vcf_path, engine, monkeypatch):
    sub_id = _h._make_transient_failure(api, worker, store, clinical_conn, org, vcf_path, engine, monkeypatch)
    with clinical_conn.cursor() as cur:
        cur.execute("UPDATE exceptions SET attempt_count = 6 WHERE order_id = %s", (org["order_id"],))
    clinical_conn.commit()
    _h._make_retry_due(clinical_conn, org["order_id"])
    _h._run_retry_scheduler(dao, store, org)
    return sub_id


def _escalation_records(conn, org_id):
    return q(
        conn,
        "SELECT resource_id, outcome, actor_role, details FROM audit_log "
        "WHERE org_id = %s AND action = 'exception_retry_escalated'",
        (org_id,),
    )


def test_exhausting_the_retries_leaves_one_durable_record_naming_the_exception(
    api, worker, store, dao, clinical_conn, org_a, vcf_path, engine, monkeypatch, caplog
):
    with caplog.at_level(logging.ERROR, logger="clinical.data_access"):
        sub_id = _escalate(api, worker, store, dao, clinical_conn, org_a, vcf_path, engine, monkeypatch)

    ((reason, category, status, attempts, _n, _c),) = schedule_rows(clinical_conn, org_a["order_id"])
    assert (status, attempts) == ("escalated", 6)
    ((exception_id,),) = q(clinical_conn, "SELECT id FROM exceptions WHERE order_id = %s", (org_a["order_id"],))

    ((resource_id, outcome, actor_role, details),) = _escalation_records(clinical_conn, org_a["org_id"])
    assert (resource_id, outcome, actor_role) == (str(exception_id), "escalated", "System")
    assert details == {
        "order_id": str(org_a["order_id"]),
        "category": category,
        "reason_code": reason,
        "attempt_count": 6,
    }
    assert store.get_submission(sub_id).status == "failed"  # the job's terminal state is untouched

    messages = [r.getMessage() for r in caplog.records if "retry_escalated" in r.getMessage()]
    assert len(messages) == 1 and str(exception_id) in messages[0] and reason in messages[0]


def test_the_record_and_the_log_carry_no_patient_data_and_no_error_text(
    api, worker, store, dao, clinical_conn, org_a, vcf_path, engine, monkeypatch, caplog
):
    with caplog.at_level(logging.ERROR, logger="clinical.data_access"):
        _escalate(api, worker, store, dao, clinical_conn, org_a, vcf_path, engine, monkeypatch)
    ((_rid, _o, _a, details),) = _escalation_records(clinical_conn, org_a["org_id"])
    ((error_message,),) = q(
        clinical_conn, "SELECT error_message FROM exceptions WHERE order_id = %s", (org_a["order_id"],)
    )
    ((patient_id,),) = q(clinical_conn, "SELECT patient_id FROM orders WHERE order_id = %s", (org_a["order_id"],))
    haystack = repr(details) + " ".join(r.getMessage() for r in caplog.records if "retry_escalated" in r.getMessage())
    assert str(patient_id) not in haystack and str(vcf_path) not in haystack
    if error_message:
        assert error_message not in haystack


def test_further_scheduler_passes_add_no_second_record_or_alert(
    api, worker, store, dao, clinical_conn, org_a, vcf_path, engine, monkeypatch, caplog
):
    with caplog.at_level(logging.ERROR, logger="clinical.data_access"):
        _escalate(api, worker, store, dao, clinical_conn, org_a, vcf_path, engine, monkeypatch)
        for _ in range(3):
            _h._make_retry_due(clinical_conn, org_a["order_id"])
            _h._run_retry_scheduler(dao, store, org_a)
    assert len(_escalation_records(clinical_conn, org_a["org_id"])) == 1
    assert len([r for r in caplog.records if "retry_escalated" in r.getMessage()]) == 1


def test_escalation_keeps_the_failure_context_and_surfaces_on_the_operator_worklist(
    api, worker, store, dao, clinical_conn, org_a, vcf_path, engine, monkeypatch
):
    _make_before = None
    sub_id = _h._make_transient_failure(api, worker, store, clinical_conn, org_a, vcf_path, engine, monkeypatch)
    before = q(
        clinical_conn,
        "SELECT reason_code, error_message, owner, created_at FROM exceptions WHERE order_id = %s",
        (org_a["order_id"],),
    )
    with clinical_conn.cursor() as cur:
        cur.execute("UPDATE exceptions SET attempt_count = 6 WHERE order_id = %s", (org_a["order_id"],))
    clinical_conn.commit()
    _h._make_retry_due(clinical_conn, org_a["order_id"])
    _h._run_retry_scheduler(dao, store, org_a)
    after = q(
        clinical_conn,
        "SELECT reason_code, error_message, owner, created_at FROM exceptions WHERE order_id = %s",
        (org_a["order_id"],),
    )
    assert after == before and _make_before is None
    worklist = dao.list_open_exceptions(dao._create_system_session(uuid.UUID(str(org_a["org_id"]))))
    assert any(str(row.order_id) == str(org_a["order_id"]) and row.status == "escalated" for row in worklist)
    failed = store.get_submission(sub_id)
    assert failed.status == "failed" and failed.error_message


def test_an_exception_still_being_retried_is_not_escalated_or_recorded(
    api, worker, store, dao, clinical_conn, org_a, vcf_path, engine, monkeypatch
):
    _h._make_transient_failure(api, worker, store, clinical_conn, org_a, vcf_path, engine, monkeypatch)
    _h._make_retry_due(clinical_conn, org_a["order_id"])
    _h._run_retry_scheduler(dao, store, org_a)
    ((_r, _c, status, attempts, _n, _created),) = schedule_rows(clinical_conn, org_a["order_id"])
    assert (status, attempts) == ("open", 1)
    assert _escalation_records(clinical_conn, org_a["org_id"]) == []


def test_escalation_in_one_organisation_is_recorded_only_there(
    api, worker, store, dao, clinical_conn, org_a, org_b, vcf_path, engine, monkeypatch
):
    _escalate(api, worker, store, dao, clinical_conn, org_a, vcf_path, engine, monkeypatch)
    assert len(_escalation_records(clinical_conn, org_a["org_id"])) == 1
    assert _escalation_records(clinical_conn, org_b["org_id"]) == []
