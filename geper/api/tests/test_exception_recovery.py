"""
api/tests/test_exception_recovery.py
────────────────────────────────────
The recovery half of the exception workflow, with a real PostgreSQL (same disposable-database rules and rig as
test_processing_handoff.py):

  * an operator resolves an exception with `retry_check` -> `DataAccess.requeue_resolved_exceptions` re-queues the failures
    that preceded the resolution -> the worker's FULL preflight runs again, so a submission that is still not fit to run
    fails again with a fresh exception instead of running;
  * `accept_failure` re-queues nothing; repeated or concurrent resolution re-queues once; another organisation's
    resolutions never touch this organisation's rows;
  * an interrupted row with a refused path, and a failure with no order in its organisation, are visible rather than
    silent.
"""

from __future__ import annotations

import threading
import uuid

import pytest

from api import exception_retry_worker
from api.tests import test_processing_handoff as _h
from api.tests.conftest import place_vcf
from clinical.data_access import DataAccess, NotFoundError, SystemClock


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
admin_session = _h.admin_session
process = _h.process
submit = _h.submit
q = _h.q
_link = _h._link
VCF_TEXT = _link.VCF_TEXT


def system(dao, org):
    return dao._create_system_session(uuid.UUID(str(org["org_id"])))


def sweep(dao, store, org):
    return dao.requeue_resolved_exceptions(system(dao, org), store)


def exceptions(conn, order_id, reason=None):
    sql = "SELECT id, reason_code, status FROM exceptions WHERE order_id = %s"
    params = [order_id]
    if reason:
        sql += " AND reason_code = %s"
        params.append(reason)
    return q(conn, sql + " ORDER BY created_at", params)


def resolve(dao, org, exception_id, action="retry_check"):
    session = admin_session(dao, org)
    dao.resolve_exception(session, exception_id, action, "operator reviewed", str(session.user_id))


def audit(conn, org_id, action):
    return q(
        conn,
        "SELECT resource_id, outcome, actor_role, details FROM audit_log WHERE org_id = %s AND action = %s",
        (org_id, action),
    )


def direct_submission(store, org, path, key="direct", order_id=None):
    return store.create_submission(
        org_id=str(org["org_id"]),
        order_id=str(order_id or org["order_id"]),
        sample_id=str(org["sample_id"]),
        submission_key=key,
        vcf_path=path,
        assembly="GRCh38",
        sample_ref="sample-ref",
        consent_ref="consent-ref",
    )


def failed_on_pending_qc(api, worker, store, dao, conn, vcf_root, engine, name="Org Pending"):
    """An organisation whose QC has not passed, one submission that failed on it, and the exception that records it."""
    org = make_org(dao, conn, name, qc="pending")
    src = place_vcf(vcf_root, org["org_id"], VCF_TEXT)
    sub_id = submit(api(org["org_id"], KEY_A), org, src)
    assert process(worker, store, sub_id).status == "failed"
    ((exc_id, reason, status),) = exceptions(conn, org["order_id"])
    assert (reason, status) == ("qc_pending", "open")
    return org, sub_id, exc_id


# ═══ A: resolved and fixed -> safely re-queued and processed ═══


def test_resolving_after_the_cause_is_fixed_requeues_and_the_run_completes_once(
    api, worker, store, dao, clinical_conn, vcf_root, engine
):
    org, sub_id, exc_id = failed_on_pending_qc(api, worker, store, dao, clinical_conn, vcf_root, engine)
    assert sweep(dao, store, org) == 0  # an unresolved exception re-queues nothing
    assert store.get_submission(sub_id).status == "failed"

    admin = admin_session(dao, org)
    dao.record_qc(admin, org["sample_id"], "passed")  # the operator fixes the cause...
    resolve(dao, org, exc_id)  # ...and says so
    assert sweep(dao, store, org) == 1
    assert store.get_submission(sub_id).status == "queued"

    done = process(worker, store, sub_id)
    assert done.status == "complete", done.error_message
    assert _h.clinical_counts(clinical_conn, org["org_id"]) == {k: 1 for k in NOTHING_RECORDED}
    assert len(engine.runs()) == 1

    ((resource, outcome, role, details),) = audit(clinical_conn, org["org_id"], "submission_requeued_after_resolution")
    assert (resource, outcome, role) == (sub_id, "queued", "System")
    assert details["exception_id"] == str(exc_id) and details["reason_code"] == "qc_pending"
    assert sweep(dao, store, org) == 0  # nothing left to do


def test_a_missing_vcf_that_is_then_supplied_is_recovered(api, worker, store, dao, clinical_conn, vcf_root, engine):
    org = make_org(dao, clinical_conn, "Org Later", qc="passed")
    target = vcf_root / str(org["org_id"]) / "later.vcf"
    sub_id = submit(api(org["org_id"], KEY_A), org, target)  # accepted: a path inside the directory
    assert process(worker, store, sub_id).status == "failed"
    ((exc_id, reason, _s),) = exceptions(clinical_conn, org["order_id"])
    assert reason == "vcf_missing"
    place_vcf(vcf_root, org["org_id"], VCF_TEXT, "later.vcf")  # the lab delivers the file
    resolve(dao, org, exc_id)
    assert sweep(dao, store, org) == 1
    assert process(worker, store, sub_id).status == "complete"


# ═══ B and C: resolved but still unfit -> not run, visible again ═══


def test_resolving_without_fixing_the_cause_fails_again_with_a_fresh_exception_and_does_not_loop(
    api, worker, store, dao, clinical_conn, vcf_root, engine
):
    org, sub_id, exc_id = failed_on_pending_qc(api, worker, store, dao, clinical_conn, vcf_root, engine)
    resolve(dao, org, exc_id)  # resolved, QC still pending
    assert sweep(dao, store, org) == 1
    again = process(worker, store, sub_id)
    assert again.status == "failed" and engine.runs() == []
    assert clinical_counts(clinical_conn, org["org_id"]) == NOTHING_RECORDED  # nothing published

    rows = exceptions(clinical_conn, org["order_id"], "qc_pending")
    assert [status for _i, _r, status in rows] == ["resolved", "open"]  # the problem is visible again, as a new item
    worklist = dao.list_open_exceptions(system(dao, org))
    assert [(e.reason_code, e.status) for e in worklist] == [("qc_pending", "open")]

    for _ in range(3):  # no resolution since the new failure: nothing is retried unattended
        assert sweep(dao, store, org) == 0
    assert store.get_submission(sub_id).status == "failed" and engine.runs() == []


def test_accept_failure_requeues_nothing(api, worker, store, dao, clinical_conn, vcf_root, engine):
    org, sub_id, exc_id = failed_on_pending_qc(api, worker, store, dao, clinical_conn, vcf_root, engine)
    resolve(dao, org, exc_id, "accept_failure")
    assert sweep(dao, store, org) == 0
    assert store.get_submission(sub_id).status == "failed"
    assert audit(clinical_conn, org["org_id"], "submission_requeued_after_resolution") == []


@pytest.mark.parametrize("path", ["notes.txt", "../escape.vcf"])
def test_an_invalid_path_stays_visible_after_resolution_and_is_never_run(
    api, worker, store, dao, clinical_conn, org_a, engine, path
):
    sub_id = direct_submission(store, org_a, path).id
    assert process(worker, store, sub_id).status == "failed"
    ((exc_id, reason, _s),) = exceptions(clinical_conn, org_a["order_id"])
    assert reason.startswith("vcf_path_")
    resolve(dao, org_a, exc_id)
    assert sweep(dao, store, org_a) == 1  # the decision is honoured: it is tried again...
    assert (
        process(worker, store, sub_id).status == "failed" and engine.runs() == []
    )  # ...and the policy still refuses it
    assert clinical_counts(clinical_conn, org_a["org_id"]) == NOTHING_RECORDED
    rows = exceptions(clinical_conn, org_a["order_id"], reason)
    assert [s for _i, _r, s in rows] == ["resolved", "open"]
    assert sweep(dao, store, org_a) == 0


def test_a_submission_whose_sample_does_not_belong_to_the_order_is_not_processed_and_stays_visible(
    api, worker, store, dao, clinical_conn, org_a, vcf_path, engine
):
    row = store.create_submission(
        org_id=str(org_a["org_id"]),
        order_id=str(org_a["order_id"]),
        sample_id=str(uuid.uuid4()),
        submission_key="bad-sample",
        vcf_path=vcf_path,
        assembly="GRCh38",
        sample_ref="s",
        consent_ref="c",
    )
    assert process(worker, store, row.id).status == "failed" and engine.runs() == []
    assert [r for _i, r, _s in exceptions(clinical_conn, org_a["order_id"])] == ["sample_unresolved"]
    assert clinical_counts(clinical_conn, org_a["org_id"]) == NOTHING_RECORDED


# ═══ E: no order in the organisation ═══


def test_a_failure_with_no_order_leaves_one_organisation_scoped_audit_record(
    worker, store, dao, clinical_conn, org_a, org_b, vcf_path, engine
):
    ghost_order = uuid.uuid4()
    sub_id = direct_submission(store, org_a, vcf_path, order_id=ghost_order).id
    assert process(worker, store, sub_id).status == "failed" and engine.runs() == []

    ((resource, outcome, role, details),) = audit(clinical_conn, org_a["org_id"], "submission_failed_unlinked")
    assert (resource, outcome, role) == (sub_id, "order_not_found", "System")
    assert details["reason_code"] == "order_not_found" and str(vcf_path) not in repr(details)
    assert q(clinical_conn, "SELECT count(*) FROM exceptions WHERE org_id = %s", (org_a["org_id"],)) == [(0,)]

    assert store.requeue(sub_id)  # processing it again does not duplicate the record
    process(worker, store, sub_id)
    assert len(audit(clinical_conn, org_a["org_id"], "submission_failed_unlinked")) == 1
    assert audit(clinical_conn, org_b["org_id"], "submission_failed_unlinked") == []
    assert clinical_counts(clinical_conn, org_a["org_id"]) == NOTHING_RECORDED


def test_a_refused_path_with_no_order_is_recorded_without_touching_any_order(
    worker, store, dao, clinical_conn, org_a, org_b, engine
):
    sub_id = direct_submission(store, org_a, "notes.txt", order_id=org_b["order_id"]).id  # another organisation's order
    assert process(worker, store, sub_id).status == "failed" and engine.runs() == []
    ((resource, outcome, _r, _d),) = audit(clinical_conn, org_a["org_id"], "submission_failed_unlinked")
    assert (resource, outcome) == (sub_id, "vcf_path_not_a_vcf")
    assert exceptions(clinical_conn, org_b["order_id"]) == []  # nothing written onto the other organisation's order
    assert exceptions(clinical_conn, org_a["order_id"]) == []
    assert audit(clinical_conn, org_b["org_id"], "submission_failed_unlinked") == []


def test_a_submission_with_no_order_at_all_is_recorded_too(worker, store, dao, clinical_conn, org_a, vcf_path, engine):
    row = store.create_submission(
        org_id=str(org_a["org_id"]),
        submission_key="no-order",
        vcf_path="notes.txt",
        assembly="GRCh38",
        sample_ref="s",
        consent_ref="c",
    )
    process(worker, store, row.id)  # not linked to a clinical record: refused before anything else
    assert store.get_submission(row.id).status == "failed" and engine.runs() == []


def committed_unlinked_records(clinical_dsn, org_id, submission_id):
    """
    The `submission_failed_unlinked` rows for one submission as ANOTHER database connection sees them. A row the worker
    wrote but did not commit is invisible here; the tests above read through the worker's own connection, which sees
    its own uncommitted writes, and so could not tell.
    """
    import psycopg

    with psycopg.connect(clinical_dsn, connect_timeout=10) as other:
        return other.execute(
            "SELECT outcome, actor_role FROM audit_log WHERE org_id = %s AND action = 'submission_failed_unlinked' "
            "AND resource_id = %s",
            (org_id, submission_id),
        ).fetchall()


@pytest.mark.parametrize(
    "scenario,expected_outcome",
    [("order_does_not_exist", "order_not_found"), ("path_refused_with_a_foreign_order", "vcf_path_not_a_vcf")],
)
def test_the_unlinked_failure_record_is_committed_not_just_written_by_the_worker(
    worker, store, dao, clinical_dsn, org_a, org_b, engine, scenario, expected_outcome
):
    """
    The worker logs "recorded in the audit log as submission_failed_unlinked". That must be true for anyone else reading
    the database, and it must survive what the worker loop does next (`DataAccess.ping()` rolls back whatever is pending).
    Found on a real stack: the row was written, never committed, and rolled back by the next ping.
    """
    if scenario == "order_does_not_exist":
        sub_id = direct_submission(store, org_a, "synthetic.vcf", order_id=uuid.uuid4()).id
    else:
        sub_id = direct_submission(store, org_a, "notes.txt", order_id=org_b["order_id"]).id
    assert process(worker, store, sub_id).status == "failed" and engine.runs() == []

    assert committed_unlinked_records(clinical_dsn, org_a["org_id"], sub_id) == [(expected_outcome, "System")]

    assert dao.ping() is True  # what the worker loop does before its next pass
    assert committed_unlinked_records(clinical_dsn, org_a["org_id"], sub_id) == [(expected_outcome, "System")]

    assert store.requeue(sub_id)  # processing the same failure again records nothing more
    process(worker, store, sub_id)
    assert dao.ping() is True
    assert committed_unlinked_records(clinical_dsn, org_a["org_id"], sub_id) == [(expected_outcome, "System")]
    assert committed_unlinked_records(clinical_dsn, org_b["org_id"], sub_id) == []  # and nothing leaks to the other org


# ═══ D: interrupted rows ═══


def test_an_interrupted_row_with_a_refused_path_becomes_a_visible_recoverable_failure(
    worker, store, dao, clinical_conn, org_a, engine
):
    sub_id = direct_submission(store, org_a, "notes.txt").id
    store.update_status(sub_id, "interrupted")
    assert worker.reconcile_interrupted_submissions() == 0
    assert store.get_submission(sub_id).status == "failed"
    ((exc_id, reason, status),) = exceptions(clinical_conn, org_a["order_id"])
    assert (reason, status) == ("vcf_path_not_a_vcf", "open")
    assert worker.reconcile_interrupted_submissions() == 0  # a second startup finds nothing and adds nothing
    assert len(exceptions(clinical_conn, org_a["order_id"])) == 1

    resolve(dao, org_a, exc_id)  # an authorised operator can act on it
    assert sweep(dao, store, org_a) == 1
    assert store.get_submission(sub_id).status == "queued"
    assert engine.runs() == []  # and nothing has run: the worker's preflight still has to pass


# ═══ F: repeated and concurrent resolution ═══


def test_resolving_twice_and_sweeping_repeatedly_requeues_once_and_yields_one_report(
    api, worker, store, dao, clinical_conn, vcf_root, engine
):
    org, sub_id, exc_id = failed_on_pending_qc(api, worker, store, dao, clinical_conn, vcf_root, engine)
    dao.record_qc(admin_session(dao, org), org["sample_id"], "passed")
    resolve(dao, org, exc_id)
    resolve(dao, org, exc_id)  # an impatient second click
    assert [sweep(dao, store, org) for _ in range(3)] == [1, 0, 0]
    assert process(worker, store, sub_id).status == "complete"
    assert sweep(dao, store, org) == 0
    assert clinical_counts(clinical_conn, org["org_id"]) == {k: 1 for k in NOTHING_RECORDED}
    assert len(engine.runs()) == 1


def test_two_workers_sweeping_at_the_same_moment_requeue_once(
    api, worker, store, dao, clinical_conn, clinical_dsn, vcf_root, engine
):
    import psycopg

    org, sub_id, exc_id = failed_on_pending_qc(api, worker, store, dao, clinical_conn, vcf_root, engine)
    dao.record_qc(admin_session(dao, org), org["sample_id"], "passed")
    resolve(dao, org, exc_id)
    clinical_conn.commit()

    second = psycopg.connect(clinical_dsn, autocommit=False)
    barrier = threading.Barrier(2, timeout=15)
    results, errors = [], []

    def run(data_access):
        try:
            session = data_access._create_system_session(uuid.UUID(str(org["org_id"])))
            barrier.wait()
            results.append(data_access.requeue_resolved_exceptions(session, store))
        except Exception as exc:  # noqa: BLE001 -- the test reports whatever a caller saw
            errors.append(exc)

    try:
        other = DataAccess(second, clock=SystemClock(), password_hasher=_link._PlainHasher())
        threads = [threading.Thread(target=run, args=(d,)) for d in (dao, other)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
    finally:
        second.close()
    assert errors == [] and sorted(results) == [0, 1]
    assert process(worker, store, sub_id).status == "complete"
    assert clinical_counts(clinical_conn, org["org_id"]) == {k: 1 for k in NOTHING_RECORDED}


# ═══ cross-organisation ═══


def test_another_organisations_resolution_or_sweep_never_touches_this_organisations_failures(
    api, worker, store, dao, clinical_conn, vcf_root, engine
):
    org_1, sub_1, exc_1 = failed_on_pending_qc(api, worker, store, dao, clinical_conn, vcf_root, engine, "Org One")
    org_2 = make_org(dao, clinical_conn, "Org Two", qc="pending")
    sub_2 = submit(api(org_2["org_id"], KEY_B), org_2, place_vcf(vcf_root, org_2["org_id"], VCF_TEXT))
    assert process(worker, store, sub_2).status == "failed"
    ((exc_2, _r, _s),) = exceptions(clinical_conn, org_2["order_id"])

    with pytest.raises(NotFoundError):  # org two's operator cannot even name org one's exception
        resolve(dao, org_2, exc_1)
    clinical_conn.rollback()

    resolve(dao, org_2, exc_2)  # org two resolves its own (same reason code)
    assert sweep(dao, store, org_1) == 0 and store.get_submission(sub_1).status == "failed"
    assert sweep(dao, store, org_2) == 1
    assert store.get_submission(sub_1).status == "failed" and store.get_submission(sub_2).status == "queued"
    assert audit(clinical_conn, org_1["org_id"], "submission_requeued_after_resolution") == []


# ═══ the retry worker wires it in, and does not disturb running work ═══


def test_the_retry_worker_runs_the_recovery_sweep_for_every_organisation(
    api, worker, store, dao, clinical_conn, vcf_root, engine
):
    org, sub_id, exc_id = failed_on_pending_qc(api, worker, store, dao, clinical_conn, vcf_root, engine)
    dao.record_qc(admin_session(dao, org), org["sample_id"], "passed")
    resolve(dao, org, exc_id)
    clinical_conn.commit()
    retry_worker = exception_retry_worker.ExceptionRetryWorker(dao, store)
    retry_worker._process_all_orgs()
    assert store.get_submission(sub_id).status == "queued"


def test_the_retry_worker_opens_the_store_without_touching_rows_that_are_running(store_path, monkeypatch):
    """Its own SubmissionStore must not mark another worker's running rows interrupted."""
    import sys
    import types

    from api.submission_store import SubmissionStore

    seen = {}
    real = SubmissionStore

    def spy(path, **kwargs):
        seen.update(kwargs)
        return real(path, **kwargs)

    fake_psycopg = types.SimpleNamespace(connect=lambda dsn, **kwargs: object())
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setenv("CLINICAL_DSN", "postgresql://example.invalid/none")
    monkeypatch.setenv("GEPER_SUBMISSION_STORE_PATH", str(store_path))
    monkeypatch.setattr("api.submission_store.SubmissionStore", spy)
    monkeypatch.setattr("clinical.data_access.DataAccess", lambda connection: object())
    monkeypatch.setattr(exception_retry_worker.ExceptionRetryWorker, "run_loop", lambda self, poll_interval=30: None)
    exception_retry_worker.main()
    assert seen.get("reconcile_on_open") is False


def test_the_retry_worker_pass_also_runs_the_retry_scheduler_it_never_reached_before(
    api, worker, store, dao, clinical_conn, org_a, vcf_path, engine, monkeypatch
):
    """`_process_all_orgs` used to fail on every pass (a query against a column and an attribute that do not exist), so
    no transient failure was ever retried by the process meant to retry it."""
    sub_id = _h._make_transient_failure(api, worker, store, clinical_conn, org_a, vcf_path, engine, monkeypatch)
    _h._make_retry_due(clinical_conn, org_a["order_id"])
    clinical_conn.commit()
    exception_retry_worker.ExceptionRetryWorker(dao, store)._process_all_orgs()
    assert store.get_submission(sub_id).status == "queued"
    ((_r, _c, status, attempts, _n, _created),) = _h.schedule_rows(clinical_conn, org_a["order_id"])
    assert (status, attempts) == ("open", 1)
