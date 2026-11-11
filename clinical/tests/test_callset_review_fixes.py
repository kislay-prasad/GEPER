# ruff: noqa: F811
"""
Final-review corrections for the CNV/SV call-set work (H-1, M-1 .. M-6, replay), against the REAL schema as the REAL
`clinical_app` / `clinical_retention` roles.

CONCURRENCY TESTS ARE DETERMINISTIC. None sleeps for a guessed duration. A "conflicting operation in progress" is made by
leaving a transaction OPEN (the DAO's own `_in_transaction` flag makes its decorators pass through, so the act runs
and the transaction is not committed); the second operation is then started in a thread and the test waits on a
CONDITION -- `pg_stat_activity` reporting that backend as waiting on a lock -- before releasing the first. The outcome
is then a function of the lock order, not of timing.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest

from clinical.data_access import (
    AuthorizationError,
    CallSetConflictError,
    CallSetInputError,
    DataAccess,
    NotFoundError,
)
from clinical.retention import RetentionPrincipal
from clinical.tests.test_callset_dao import (  # noqa: F401  (fixtures and helpers re-used)
    FAR,
    T0,
    World,
    _fresh_dao,
    app_conn,
    approve_caller,
    audits,
    build_org,
    dao,
    events,
    new_run,
    release,
    ret_conn,
    row,
    same_sample_run,
    state,
    su,
    submit,
    system_principal,
    tamper,
    validated,
    vcf_text,
    world,
)

psycopg = pytest.importorskip("psycopg")


# ─── deterministic concurrency helpers ───────────────────────────────────────


def wait_until_blocked(observer, pid: int, timeout: float = 30.0) -> None:
    """Wait for a CONDITION (backend `pid` is waiting on a lock), polling; never a fixed sleep the outcome depends on."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = observer.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()
        observer.rollback()
        if found and found[0] == "Lock":
            return
        time.sleep(0.02)
    raise AssertionError(f"backend {pid} never blocked on a lock")


def run_in_thread(fn):
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 -- handed back to the test
            box["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    return thread, box


def open_transaction_dao():
    """A DAO whose acts run and then LEAVE THE TRANSACTION OPEN (decorators pass through), plus its connection."""
    connection, local = _fresh_dao()
    local._in_transaction = True
    return connection, local


@pytest.fixture()
def purgeable(su, dao, world):
    """A validated call set on a released run whose retention has long expired, and the org's system principal."""
    cs = validated(su, dao, world)
    release(su, world, world.run, datetime(2030, 1, 1, tzinfo=timezone.utc))
    system_principal(dao, world.org)
    return cs


# ═══ H-1: clinical eligibility at submission, validation and map-read ════════


def _qc(su, w, run, status):
    with su.cursor() as cur:
        cur.execute("UPDATE samples SET qc_status = %s WHERE sample_id = %s", (status, run.sample))
    su.commit()


def _order_state(su, run, new_state):
    with su.cursor() as cur:
        cur.execute(
            "UPDATE orders SET state = %s WHERE order_id = (SELECT order_id FROM samples WHERE sample_id = %s)",
            (new_state, run.sample),
        )
    su.commit()


def _withdraw_consent(su, run):
    with su.cursor() as cur:
        cur.execute(
            "UPDATE consents SET withdrawn_at = now(), withdrawn_by = recorded_by WHERE consent_id = "
            "(SELECT consent_id FROM orders WHERE order_id = (SELECT order_id FROM samples WHERE sample_id = %s))",
            (run.sample,),
        )
    su.commit()


def _restore_consent(su, run):
    """Un-withdrawing is exactly what the consent guard forbids under a call set, so this is an owner-level edit."""
    tamper(
        su,
        "UPDATE consents SET withdrawn_at = NULL, withdrawn_by = NULL WHERE consent_id = "
        "(SELECT consent_id FROM orders WHERE order_id = (SELECT order_id FROM samples WHERE sample_id = %s))",
        (run.sample,),
        table="consents",
        trigger="trg_callset_identity_consents",
    )


def _small_variant_analysis(su, w, run, assembly):
    with su.cursor() as cur:
        cur.execute(
            "INSERT INTO interpretations (org_id, vcf_id, run_document, submission_key, created_at, created_by, retention_days) "
            "VALUES (%s, %s, %s::jsonb, %s, %s, %s, 1825)",
            (w.org, run.vcf, json.dumps({"assembly": assembly, "variants": []}), uuid.uuid4().hex, T0, w.lab1.user_id),
        )
    su.commit()


class TestSubmissionRefusesWhatIsNotClinicallyEligible:
    def test_a_qc_failed_sample_is_refused_and_nothing_is_stored(self, su, dao, world):
        _qc(su, world, world.run, "failed")
        with pytest.raises(CallSetConflictError, match="cannot accept this call set"):
            submit(dao, world, world.run)
        assert row(su, "SELECT count(*) FROM call_sets") == [(0,)] and row(
            su, "SELECT count(*) FROM call_set_events"
        ) == [(0,)]
        _qc(su, world, world.run, "pending")
        with pytest.raises(CallSetConflictError, match="cannot accept this call set"):
            submit(dao, world, world.run)
        _qc(su, world, world.run, "passed")
        submit(dao, world, world.run)  # positive twin: the same request is accepted once QC has passed
        assert row(su, "SELECT count(*) FROM call_sets") == [(1,)]

    def test_a_cancelled_order_is_refused(self, su, dao, world):
        _order_state(su, world.run, "cancelled")
        with pytest.raises(CallSetConflictError, match="cannot accept this call set"):
            submit(dao, world, world.run)
        assert row(su, "SELECT count(*) FROM call_sets") == [(0,)]
        _order_state(su, world.run, "in_progress")
        submit(dao, world, world.run)

    def test_withdrawn_or_missing_consent_is_refused(self, su, dao, world):
        _withdraw_consent(su, world.run)
        with pytest.raises(CallSetConflictError, match="cannot accept this call set"):
            submit(dao, world, world.run)
        assert row(su, "SELECT count(*) FROM call_sets") == [(0,)]
        _restore_consent(su, world.run)
        submit(dao, world, world.run)

    def test_a_small_variant_analysis_on_another_build_is_refused_naming_both(self, su, dao, world):
        _small_variant_analysis(su, world, world.run, "GRCh37")
        with pytest.raises(CallSetConflictError, match="cannot accept this call set"):
            submit(dao, world, world.run)
        assert row(su, "SELECT count(*) FROM call_sets") == [(0,)]

    def test_a_small_variant_analysis_on_the_same_build_does_not_block(self, su, dao, world):
        _small_variant_analysis(su, world, world.run, "GRCh38")
        submit(dao, world, world.run)

    def test_a_successful_parse_alone_is_not_eligibility(self, su, dao, world):
        """The file is perfect and the run, sample and order identities are right; QC alone decides."""
        data = vcf_text(world.run.sample, n=3)
        _qc(su, world, world.run, "failed")
        with pytest.raises(CallSetConflictError):
            submit(dao, world, world.run, data=data)


class TestValidationRechecksEligibility:
    @pytest.mark.parametrize(
        "break_it, fix_it, message",
        [
            (lambda su, w: _qc(su, w, w.run, "failed"), lambda su, w: _qc(su, w, w.run, "passed"), "QC"),
            (
                lambda su, w: _order_state(su, w.run, "cancelled"),
                lambda su, w: _order_state(su, w.run, "in_progress"),
                "cancelled",
            ),
            (lambda su, w: _withdraw_consent(su, w.run), lambda su, w: _restore_consent(su, w.run), "consent"),
            (
                lambda su, w: _small_variant_analysis(su, w, w.run, "GRCh37"),
                None,
                "small-variant",
            ),
        ],
        ids=["qc", "cancelled-order", "consent", "small-variant-assembly"],
    )
    def test_each_condition_changing_after_submission_blocks_validation(
        self, su, dao, world, break_it, fix_it, message
    ):
        cs = submit(dao, world, world.run)
        break_it(su, world)
        with pytest.raises(CallSetConflictError, match=message):
            dao.validate_call_set(world.interp1, cs)
        assert state(su, cs) == "pending_validation" and events(su, cs) == ["submitted"]
        if fix_it is not None:  # positive twin
            fix_it(su, world)
            dao.validate_call_set(world.interp1, cs)
            assert state(su, cs) == "validated"


class TestTheMapNeverDrawsIneligibleEvidence:
    def _on_map(self, dao, w):
        return [r["call_set_id"] for r in dao.list_patient_validated_call_sets(w.reader, w.run.patient)]

    def test_each_condition_removes_validated_evidence_from_the_map_and_restoring_it_returns_it(self, su, dao, world):
        cs = validated(su, dao, world)
        assert self._on_map(dao, world) == [cs]
        for break_it, fix_it in (
            (lambda: _qc(su, world, world.run, "failed"), lambda: _qc(su, world, world.run, "passed")),
            (lambda: _order_state(su, world.run, "cancelled"), lambda: _order_state(su, world.run, "in_progress")),
            (lambda: _withdraw_consent(su, world.run), lambda: _restore_consent(su, world.run)),
        ):
            break_it()
            assert self._on_map(dao, world) == [], "QC-failed / cancelled / unconsented evidence must never be eligible"
            fix_it()
            assert self._on_map(dao, world) == [cs]

    def test_a_small_variant_analysis_on_another_build_hides_the_evidence(self, su, dao, world):
        validated(su, dao, world)
        assert len(self._on_map(dao, world)) == 1
        _small_variant_analysis(su, world, world.run, "GRCh37")
        assert self._on_map(dao, world) == []

    def test_a_sample_that_is_no_longer_the_runs_sample_hides_the_evidence(self, su, dao, world):
        cs = validated(su, dao, world)
        other = new_run(su, dao, world, patient=world.run.patient)
        tamper(
            su,
            "UPDATE sequencing_runs SET sample_id = %s WHERE id = %s",
            (other.sample, world.run.id),
            table="sequencing_runs",
            trigger="trg_callset_identity_runs",
        )
        assert self._on_map(dao, world) == [], "the validated set must not silently follow the run to another sample"
        tamper(
            su,
            "UPDATE sequencing_runs SET sample_id = %s WHERE id = %s",
            (world.run.sample, world.run.id),
            table="sequencing_runs",
            trigger="trg_callset_identity_runs",
        )
        assert self._on_map(dao, world) == [cs]


# ═══ M-6: identity cannot be repointed under a call set; validation holds the rows ═══


class TestIdentityIsImmutableUnderACallSet:
    @pytest.mark.parametrize(
        "sql, trigger_hint",
        [
            (
                "UPDATE sequencing_runs SET sample_id = (SELECT sample_id FROM samples WHERE sample_id <> %(sample)s LIMIT 1) WHERE id = %(run)s",
                "sequencing_runs",
            ),
            (
                "UPDATE samples SET order_id = (SELECT order_id FROM orders WHERE order_id <> (SELECT order_id FROM samples WHERE sample_id = %(sample)s) LIMIT 1) WHERE sample_id = %(sample)s",
                "samples",
            ),
            (
                "UPDATE orders SET patient_id = (SELECT patient_id FROM patients WHERE patient_id <> %(patient)s LIMIT 1) WHERE order_id = (SELECT order_id FROM samples WHERE sample_id = %(sample)s)",
                "orders",
            ),
            (
                "UPDATE orders SET test_id = (SELECT test_id FROM tests WHERE test_id <> %(test)s LIMIT 1) WHERE order_id = (SELECT order_id FROM samples WHERE sample_id = %(sample)s)",
                "orders",
            ),
            ("UPDATE tests SET assembly = 'GRCh37' WHERE test_id = %(test)s", "tests"),
        ],
        ids=["run-sample", "sample-order", "order-patient", "order-test", "test-assembly"],
    )
    def test_the_application_role_cannot_repoint_any_link_of_the_chain(
        self, su, dao, world, app_conn, sql, trigger_hint
    ):
        # a second patient/order/sample/test so every repoint has a real target
        other = new_run(su, dao, world, assembly="GRCh37")
        cs = submit(dao, world, world.run)
        params = {
            "sample": world.run.sample,
            "run": world.run.id,
            "patient": world.run.patient,
            "test": world.tests["GRCh38"],
        }
        with app_conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException, match=f"callset_identity: {trigger_hint}"):
                cur.execute(sql, params)
        app_conn.rollback()
        assert other.id != world.run.id and state(su, cs) == "pending_validation"

    def test_the_same_edits_are_allowed_where_no_call_set_stands_on_them(self, su, dao, world, app_conn):
        other = new_run(su, dao, world, assembly="GRCh37")  # no call set on this chain
        with app_conn.cursor() as cur:
            cur.execute("UPDATE tests SET assembly = 'GRCh38' WHERE test_id = %s", (world.tests["GRCh37"],))
            cur.execute("UPDATE sequencing_runs SET sample_id = sample_id WHERE id = %s", (other.id,))
        app_conn.commit()

    def test_ordinary_updates_the_application_makes_are_unaffected(self, su, dao, world, app_conn):
        submit(dao, world, world.run)
        with app_conn.cursor() as cur:
            cur.execute(
                "UPDATE samples SET qc_status = 'passed', qc_reason = 'x' WHERE sample_id = %s", (world.run.sample,)
            )
            cur.execute(
                "UPDATE orders SET state = 'in_progress' WHERE order_id = (SELECT order_id FROM samples WHERE sample_id = %s)",
                (world.run.sample,),
            )
            cur.execute("UPDATE tests SET status = 'retired' WHERE test_id = %s", (world.tests["GRCh38"],))
        app_conn.commit()

    def test_validation_holds_the_chain_rows_so_an_identity_change_waits_for_it(self, su, dao, world):
        """`FOR SHARE` on run/sample/order/test: a concurrent write to any of them blocks until validation's transaction ends."""
        connection, local = open_transaction_dao()
        writer = psycopg.connect(os.environ["CLINICAL_TEST_DSN"])
        try:
            local._callset_chain_facts(world.interp1, world.run.id, lock=True)  # what validate_call_set does, held open
            writer.execute("SET ROLE clinical_app")
            writer.commit()
            pid = writer.info.backend_pid
            thread, box = run_in_thread(
                lambda: (
                    writer.execute("UPDATE tests SET name = 'renamed' WHERE test_id = %s", (world.tests["GRCh38"],)),
                    writer.commit(),
                )
            )
            wait_until_blocked(su, pid)
            assert thread.is_alive(), "the writer must be waiting, not finished"
            connection.rollback()  # validation's transaction ends
            thread.join(timeout=30)
            assert "error" not in box and row(
                su, "SELECT name FROM tests WHERE test_id = %s", (world.tests["GRCh38"],)
            ) == [("renamed",)]
        finally:
            writer.close()
            connection.close()


# ═══ M-1: one lock per call set; lifecycle and purge cannot interleave ═══════


class TestPurgeAndLifecycleAreSerialised:
    def test_an_event_arriving_while_the_purge_is_open_waits_and_is_then_refused(
        self, su, dao, world, purgeable, ret_conn
    ):
        cs = purgeable
        RetentionPrincipal(ret_conn).purge_expired(world.org, "call_set", now=FAR)  # redacted, NOT committed: lock held
        connection, local = _fresh_dao()
        try:
            pid = connection.info.backend_pid
            thread, box = run_in_thread(lambda: local.request_call_set_retraction(world.interp1, cs, "late request"))
            wait_until_blocked(su, pid)
            assert thread.is_alive()
            ret_conn.commit()
            thread.join(timeout=30)
        finally:
            connection.close()
        assert isinstance(box.get("error"), CallSetConflictError), box
        assert row(
            su, "SELECT count(*) FROM call_set_events WHERE call_set_id = %s AND tombstoned_at IS NULL", (cs,)
        ) == [(0,)]
        assert events(su, cs) == ["submitted", "validated"], "the refused event left no trace"
        assert row(su, "SELECT tombstoned_at IS NOT NULL, source_vcf IS NULL FROM call_sets WHERE id = %s", (cs,)) == [
            (True, True)
        ]

    def test_a_purge_arriving_while_an_event_is_open_waits_and_then_tombstones_that_event_too(
        self, su, dao, world, purgeable, ret_conn
    ):
        cs = purgeable
        connection, local = open_transaction_dao()
        try:
            local.request_call_set_retraction(world.interp1, cs, "in flight")  # inserted, NOT committed: lock held
            pid = ret_conn.info.backend_pid
            thread, box = run_in_thread(
                lambda: (RetentionPrincipal(ret_conn).purge_expired(world.org, "call_set", now=FAR), ret_conn.commit())
            )
            wait_until_blocked(su, pid)
            assert thread.is_alive()
            connection.commit()
            thread.join(timeout=30)
        finally:
            connection.close()
        assert "error" not in box, box
        assert events(su, cs) == ["submitted", "validated", "retraction_requested"]
        assert row(
            su, "SELECT bool_and(tombstoned_at IS NOT NULL) FROM call_set_events WHERE call_set_id = %s", (cs,)
        ) == [(True,)], "an event that committed before the purge took its lock must be swept up by it"
        assert row(
            su, "SELECT tombstoned_at IS NOT NULL, content_redacted_at IS NOT NULL FROM call_sets WHERE id = %s", (cs,)
        ) == [(True, True)]

    def test_a_replacement_in_flight_makes_the_purge_of_its_predecessor_wait_and_then_skip_it(
        self, su, dao, world, purgeable, ret_conn
    ):
        predecessor = purgeable
        connection, local = open_transaction_dao()
        try:
            successor = submit(
                local, world, world.run, data=vcf_text(world.run.sample, n=2), supersedes=predecessor
            )  # open
            pid = ret_conn.info.backend_pid
            thread, box = run_in_thread(
                lambda: (RetentionPrincipal(ret_conn).purge_expired(world.org, "call_set", now=FAR), ret_conn.commit())
            )
            wait_until_blocked(su, pid)
            connection.commit()
            thread.join(timeout=30)
        finally:
            connection.close()
        assert "error" not in box, box
        # the predecessor now has a LIVE successor: eligibility, re-checked under the lock, no longer holds
        assert row(
            su, "SELECT source_vcf IS NOT NULL, tombstoned_at IS NULL FROM call_sets WHERE id = %s", (predecessor,)
        ) == [(True, True)]
        assert row(su, "SELECT tombstoned_at IS NULL FROM call_sets WHERE id = %s", (successor,)) == [(True,)]

    def test_a_validation_in_flight_is_swept_up_by_the_purge_that_waited_for_it(
        self, su, dao, world, purgeable, ret_conn
    ):
        """Validation of the successor in flight, purge of the predecessor racing it: the predecessor is superseded
        OR purged, never validated-and-redacted at once."""
        predecessor = purgeable
        successor = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2), supersedes=predecessor)
        connection, local = open_transaction_dao()
        try:
            local.validate_call_set(world.interp2, successor)  # supersedes the predecessor, NOT committed
            pid = ret_conn.info.backend_pid
            thread, box = run_in_thread(
                lambda: (RetentionPrincipal(ret_conn).purge_expired(world.org, "call_set", now=FAR), ret_conn.commit())
            )
            wait_until_blocked(su, pid)
            connection.commit()
            thread.join(timeout=30)
        finally:
            connection.close()
        assert "error" not in box, box
        assert state(su, predecessor) == "superseded"
        assert row(
            su, "SELECT tombstoned_at IS NULL, source_vcf IS NOT NULL FROM call_sets WHERE id = %s", (predecessor,)
        ) == [(True, True)]
        assert events(su, successor) == ["submitted", "validated"]
        assert row(
            su, "SELECT bool_and(tombstoned_at IS NOT NULL) FROM call_set_events WHERE call_set_id = %s", (successor,)
        ) == [(True,)], "the purge waited for the validation and then redacted the successor with its validated event"

    def test_withdrawal_in_flight_blocks_validation_until_it_ends_then_validation_is_refused(self, su, dao, world):
        cs = submit(dao, world, world.run)
        connection, local = open_transaction_dao()
        v_conn, v_dao = _fresh_dao()
        try:
            local.withdraw_call_set(world.lab1, cs, "wrong file")  # NOT committed
            pid = v_conn.info.backend_pid
            thread, box = run_in_thread(lambda: v_dao.validate_call_set(world.interp1, cs))
            wait_until_blocked(su, pid)
            connection.commit()
            thread.join(timeout=30)
        finally:
            connection.close()
            v_conn.close()
        assert isinstance(box.get("error"), CallSetConflictError), box
        assert events(su, cs) == ["submitted", "withdrawn"]


class TestBatchPurgeIsAtomic:
    def _two_purgeable(self, su, dao, world):
        first = validated(su, dao, world)
        second_run = new_run(su, dao, world, vcf_days=1825)
        second = validated(su, dao, world, run=second_run)
        release(su, world, world.run, datetime(2030, 1, 1, tzinfo=timezone.utc))
        release(su, world, second_run, datetime(2030, 1, 1, tzinfo=timezone.utc))
        system_principal(dao, world.org)
        return first, second

    def test_a_failure_on_the_second_call_set_rolls_back_the_first_too(self, su, dao, world, ret_conn, monkeypatch):
        first, second = self._two_purgeable(su, dao, world)
        calls = []
        real = RetentionPrincipal._write_purge_audit_entry

        def fail_on_second(self, *args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise RuntimeError("injected failure on the second audit row")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(RetentionPrincipal, "_write_purge_audit_entry", fail_on_second)
        with pytest.raises(RuntimeError):
            RetentionPrincipal(ret_conn).purge_expired(world.org, "call_set", now=FAR)
        ret_conn.rollback()
        assert len(calls) == 2, "the first set really was redacted inside the transaction before the failure"
        for cs in (first, second):
            assert row(
                su,
                "SELECT source_vcf IS NOT NULL, parsed_document IS NOT NULL, tombstoned_at IS NULL FROM call_sets WHERE id = %s",
                (cs,),
            ) == [(True, True, True)]
            assert row(
                su,
                "SELECT bool_and(tombstoned_at IS NULL), count(*) FROM call_set_events WHERE call_set_id = %s",
                (cs,),
            ) == [(True, 2)]
        assert audits(su, "retention_purge") == []

    def test_a_successful_batch_commits_all_and_nothing_less(self, su, dao, world, ret_conn):
        first, second = self._two_purgeable(su, dao, world)
        result = RetentionPrincipal(ret_conn).purge_expired(world.org, "call_set", now=FAR)
        ret_conn.commit()
        assert sorted(result.tombstoned_ids, key=str) == sorted([first, second], key=str)
        assert sorted(r[2] for r in audits(su, "retention_purge")) == sorted([str(first), str(second)])
        assert row(su, "SELECT count(*) FROM call_sets WHERE source_vcf IS NULL AND parsed_document IS NULL") == [(2,)]


# ═══ M-2: Lab technicians see status and non-genomic outcomes only ═══════════


class TestLabTechnicianAccess:
    def test_a_technician_sees_status_of_their_own_submissions_with_no_genomic_content(self, su, dao, world):
        cs = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=3))
        dao.reject_call_set(world.interp1, cs, "wrong sample ordering")
        status = dao.get_call_set_status(world.lab1, cs)
        assert status["state"] == "rejected"
        assert status["counts"]["records"] == 3 and status["counts"]["detected"] == 3
        assert [(e["event_type"], e["reason"]) for e in status["events"]] == [
            ("submitted", None),
            ("rejected", "wrong sample ordering"),
        ]
        assert set(status) == {
            "id",
            "sequencing_run_id",
            "record_class",
            "assembly",
            "caller_name",
            "caller_version",
            "created_at",
            "created_by",
            "tombstoned_at",
            "content_redacted_at",
            "state",
            "counts",
            "resubmission_of",
            "events",
        }
        flat = json.dumps(status, default=str)
        for forbidden in ("breakpoints", "source_vcf", "parsed_document", "SVTYPE", "chrom", "line_no"):
            assert forbidden not in flat, forbidden

    def test_a_technician_cannot_read_documents_bytes_or_other_technicians_submissions(self, su, dao, world):
        mine = submit(dao, world, world.run, session=world.lab1)
        other_run = new_run(su, dao, world, patient=world.run.patient)
        theirs = submit(dao, world, other_run, session=world.lab2)
        for tech in (world.lab1, world.lab2):
            with pytest.raises(AuthorizationError):
                dao.get_call_set_for_review(tech, mine)
            with pytest.raises(AuthorizationError):
                dao.list_patient_validated_call_sets(tech, world.run.patient)
        with pytest.raises(NotFoundError) as other:
            dao.get_call_set_status(world.lab1, theirs)
        with pytest.raises(NotFoundError) as missing:
            dao.get_call_set_status(world.lab1, uuid.uuid4())
        assert [x["id"] for x in dao.list_call_sets_for_run(world.lab1, world.run.id)] == [mine]
        assert [x["id"] for x in dao.list_call_sets_for_run(world.lab2, other_run.id)] == [theirs]
        assert dao.list_call_sets_for_run(world.lab1, other_run.id) == []
        assert other.type is missing.type

    def test_the_technician_cannot_get_a_document_even_for_a_call_set_they_just_submitted(self, su, dao, world):
        cs = submit(dao, world, world.run)
        with pytest.raises(AuthorizationError):
            dao.get_call_set_for_review(world.lab1, cs)
        assert [a[0] for a in audits(su, "call_set_read")] == ["denied"]

    def test_interpreters_and_approvers_keep_their_review_access_and_it_carries_no_bytes(self, su, dao, world):
        cs = validated(su, dao, world)
        for who in (world.interp2, world.appr1):
            review = dao.get_call_set_for_review(who, cs)
            assert review["document"]["counts"]["records"] == 1 and "source_vcf" not in review
            assert len(dao.list_call_sets_for_run(who, world.run.id)) == 1
            assert dao.get_call_set_status(who, cs)["state"] == "validated"

    def test_no_http_route_exposes_call_set_detail_and_the_map_refuses_a_technician(self):
        from clinical.app import app

        paths = set(app.openapi()["paths"])
        offenders = sorted(p for p in paths if "call-set" in p or "call_set" in p or "callset" in p)
        assert offenders == [], offenders
        genome = sorted(p for p in paths if "/genome" in p)
        assert genome and all(p.startswith(("/clinic/patients/", "/portal/me/genome")) for p in genome)


# ═══ M-3: audit rows: design vocabulary, no details, atomic with the act ═════


class TestAuditMatchesTheApprovedDesignAndIsAtomic:
    def test_every_verb_writes_one_design_named_row_with_no_details(self, su, dao, world):
        actions = (
            "caller_proposed",
            "caller_approved",
            "caller_retired",
            "call_set_validated",
            "call_set_retraction_requested",
            "call_set_retraction_declined",
            "call_set_retracted",
            "call_set_rejected",
            "call_set_withdrawn",
            "call_set_submitted",
        )
        before = {a: len(audits(su, a)) for a in actions}  # the `world` fixture already approved one caller
        dao.propose_caller_version(world.admin, "gridss", "2.0", "sv", "basis text")
        dao.approve_caller_version(world.appr1, "gridss", "2.0", "sv", "basis text")
        dao.retire_caller_version(world.appr2, "gridss", "2.0", "sv", "no longer used")
        cs = submit(dao, world, world.run)
        dao.validate_call_set(world.interp1, cs)
        dao.request_call_set_retraction(world.interp2, cs, "check")
        dao.decline_call_set_retraction(world.appr1, cs, "fine")
        dao.retract_call_set(world.appr1, cs, "defect")
        rej = submit(dao, world, new_run(su, dao, world, patient=world.run.patient))
        dao.reject_call_set(world.interp1, rej, "bad")
        wd = submit(dao, world, new_run(su, dao, world, patient=world.run.patient))
        dao.withdraw_call_set(world.lab1, wd, "oops")
        expected = {
            "caller_proposed": ("caller_approval", world.admin),
            "caller_approved": ("caller_approval", world.appr1),
            "caller_retired": ("caller_approval", world.appr2),
            "call_set_validated": ("call_set", world.interp1),
            "call_set_retraction_requested": ("call_set", world.interp2),
            "call_set_retraction_declined": ("call_set", world.appr1),
            "call_set_retracted": ("call_set", world.appr1),
            "call_set_rejected": ("call_set", world.interp1),
            "call_set_withdrawn": ("call_set", world.lab1),
        }
        for action, (resource_type, session) in expected.items():
            rows = audits(su, action)
            assert len(rows) == before[action] + 1, action
            r = rows[-1]
            assert r[0] == "success" and r[1] == resource_type, action
            assert r[3] in ({}, None), f"{action} must carry no details, got {r[3]}"
            actor = row(
                su, "SELECT user_id, org_id FROM audit_log WHERE action = %s ORDER BY log_id DESC LIMIT 1", (action,)
            )
            assert actor == [(session.user_id, world.org)], action
        submitted = audits(su, "call_set_submitted")
        assert len(submitted) == before["call_set_submitted"] + 3
        assert all(s[0] == "success" and s[3] in ({}, None) for s in submitted)
        dao.list_call_sets_for_run(world.interp1, world.run.id)
        dao.get_call_set_status(world.interp1, cs)
        dao.get_call_set_for_review(world.interp1, cs)
        assert [a[0] for a in audits(su, "call_set_read")] == ["success"] * 3
        assert all(a[3] in ({}, None) for a in audits(su, "call_set_read"))
        assert audits(su, "caller_version_proposed") == [] and audits(su, "genome_callset_read") == []

    def test_the_success_row_commits_in_the_same_transaction_as_the_act(self, su, dao, world):
        cs = submit(dao, world, world.run)
        dao.validate_call_set(world.interp1, cs)
        same = row(
            su,
            "SELECT a.xmin::text = c.xmin::text FROM audit_log a, call_sets c "
            "WHERE a.action = 'call_set_submitted' AND a.outcome = 'success' AND c.id = %s",
            (cs,),
        )
        assert same == [(True,)], "submit: audit row and call_sets row were written by different transactions"
        same = row(
            su,
            "SELECT a.xmin::text = e.xmin::text FROM audit_log a, call_set_events e "
            "WHERE a.action = 'call_set_validated' AND a.outcome = 'success' AND e.call_set_id = %s AND e.event_type = 'validated'",
            (cs,),
        )
        assert same == [(True,)], "validate: audit row and event were written by different transactions"

    def test_if_the_success_audit_row_cannot_be_written_the_act_does_not_happen(self, su, dao, world, monkeypatch):
        real = DataAccess._write_audit_entry

        def refuse_success(self, action, resource_type, resource_id, outcome, *a, **k):
            if outcome == "success":
                raise RuntimeError("injected: audit write failed")
            return real(self, action, resource_type, resource_id, outcome, *a, **k)

        monkeypatch.setattr(DataAccess, "_write_audit_entry", refuse_success)
        with pytest.raises(RuntimeError):
            submit(dao, world, world.run)
        assert row(su, "SELECT count(*) FROM call_sets") == [(0,)] and row(
            su, "SELECT count(*) FROM call_set_events"
        ) == [(0,)]
        monkeypatch.undo()
        cs = submit(dao, world, world.run)
        monkeypatch.setattr(DataAccess, "_write_audit_entry", refuse_success)
        with pytest.raises(RuntimeError):
            dao.validate_call_set(world.interp1, cs)
        assert events(su, cs) == ["submitted"], "a validation whose audit row failed must not exist"

    def test_a_refusal_rolls_the_act_back_and_is_itself_recorded(self, su, dao, world):
        cs = submit(dao, world, world.run, session=world.lab_interp)
        with pytest.raises(AuthorizationError):
            dao.validate_call_set(world.lab_interp, cs)
        with pytest.raises(CallSetConflictError):
            dao.decline_call_set_retraction(world.appr1, cs, "none open")
        assert [a[0] for a in audits(su, "call_set_validated")] == ["denied"]
        assert [a[0] for a in audits(su, "call_set_retraction_declined")] == ["error"]
        assert events(su, cs) == ["submitted"]

    def test_historical_audit_rows_are_never_rewritten(self, su, dao, world):
        submit(dao, world, world.run)
        before = row(su, 'SELECT log_id, action, details::text, "timestamp" FROM audit_log ORDER BY log_id')
        dao.list_call_sets_for_run(world.interp1, world.run.id)
        after = row(su, 'SELECT log_id, action, details::text, "timestamp" FROM audit_log ORDER BY log_id')
        assert after[: len(before)] == before


# ═══ M-5: parser versioning ═══════════════════════════════════════════════════


class TestParserVersioning:
    def _v2(self, monkeypatch):
        import clinical.genome_analysis as ga

        calls = []

        def parse_v2(text, **kwargs):
            calls.append("v2")
            return ga.parse_structural_vcf(text, **kwargs)

        monkeypatch.setitem(ga.PARSER_RULESETS, "structural_vcf/2", parse_v2)
        monkeypatch.setattr(ga, "CALLSET_PARSER_VERSION", "structural_vcf/2")
        return calls

    def test_a_parser_bump_neither_strands_pending_call_sets_nor_touches_their_evidence(
        self, su, dao, world, monkeypatch
    ):
        pending = submit(dao, world, world.run)
        before = row(
            su,
            "SELECT parser_version, source_content_hash, parsed_document::text FROM call_sets WHERE id = %s",
            (pending,),
        )
        calls = self._v2(monkeypatch)
        newer = submit(dao, world, new_run(su, dao, world, patient=world.run.patient))
        assert row(su, "SELECT parser_version FROM call_sets WHERE id = %s", (newer,)) == [("structural_vcf/2",)]
        assert calls == ["v2"], "new submissions are parsed by the CURRENT rule set"
        dao.validate_call_set(world.interp1, pending)  # recorded under /1, still validatable
        assert state(su, pending) == "validated" and calls == ["v2"], "the /1 call set was re-derived with /1, not /2"
        assert (
            row(
                su,
                "SELECT parser_version, source_content_hash, parsed_document::text FROM call_sets WHERE id = %s",
                (pending,),
            )
            == before
        )
        dao.validate_call_set(world.interp1, newer)
        assert calls == ["v2", "v2"], "validating the /2 call set re-derives it with /2"

    def test_an_unavailable_rule_set_is_a_refusal_never_a_silent_pass_and_changes_nothing(
        self, su, dao, world, monkeypatch
    ):
        pending = submit(dao, world, world.run)
        import clinical.genome_analysis as ga

        monkeypatch.delitem(ga.PARSER_RULESETS, "structural_vcf/1")
        monkeypatch.setitem(ga.PARSER_RULESETS, "structural_vcf/9", ga.parse_structural_vcf)
        with pytest.raises(CallSetConflictError, match="no longer available"):
            dao.validate_call_set(world.interp1, pending)
        assert state(su, pending) == "pending_validation" and events(su, pending) == ["submitted"]

    def test_a_validated_call_set_is_never_re_derived_or_re_versioned(self, su, dao, world, monkeypatch):
        cs = validated(su, dao, world)
        before = row(su, "SELECT parser_version, parsed_document::text FROM call_sets WHERE id = %s", (cs,))
        self._v2(monkeypatch)
        assert [r["call_set_id"] for r in dao.list_patient_validated_call_sets(world.reader, world.run.patient)] == [cs]
        assert row(su, "SELECT parser_version, parsed_document::text FROM call_sets WHERE id = %s", (cs,)) == before
        with pytest.raises(psycopg.Error):  # and the database refuses any attempt to rewrite it
            with su.cursor() as cur:
                cur.execute("UPDATE call_sets SET parser_version = 'structural_vcf/2' WHERE id = %s", (cs,))
        su.rollback()


# ═══ M-9: CNV through the DAO, extreme and invalid values ═════════════════════


def cnv_text(sample, lines):
    return (
        "##fileformat=VCFv4.2\n##contig=<ID=1,length=248956422>\n"
        f"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t{sample}\n" + "".join(lines)
    ).encode()


class TestCnvThroughTheDao:
    @pytest.fixture()
    def cnv_world(self, su, dao, world):
        approve_caller(dao, world, record_class="cnv")
        return world

    def test_a_cnv_call_set_with_an_extreme_quality_survives_storage_verification_and_the_map(self, su, dao, cnv_world):
        w = cnv_world
        line = "1\t1000\tc1\tN\t<DUP>\t1.5e22\tPASS\tSVTYPE=DUP;END=5000;CN=3;CIPOS=-10,10\tGT:CN\t0/1:3\n"
        cs = submit(dao, w, w.run, record_class="cnv", data=cnv_text(w.run.sample, [line]))
        dao.validate_call_set(w.interp1, cs)  # re-parses the stored bytes and compares with the jsonb-stored document
        (shown,) = dao.list_patient_validated_call_sets(w.reader, w.run.patient)
        (record,) = shown["call_set"].cnvs
        assert shown["record_class"] == "cnv" and record.copy_number == 3.0 and record.quality == 1.5e22

    def test_invalid_values_are_recorded_as_invalid_outcomes_and_never_drawn(self, su, dao, cnv_world):
        w = cnv_world
        good = "1\t1000\tgood\tN\t<DEL>\t60\tPASS\tSVTYPE=DEL;END=5000;CN=1\tGT:CN\t0/1:1\n"
        bad = [
            "1\t2000\tneg\tN\t<DEL>\t60\tPASS\tSVTYPE=DEL;END=6000;CN=-1\tGT:CN\t0/1:-1\n",
            "1\t9000\tback\tN\t<DEL>\t60\tPASS\tSVTYPE=DEL;END=100;CN=1\tGT:CN\t0/1:1\n",
            "1\tabc\tpos\tN\t<DEL>\t60\tPASS\tSVTYPE=DEL;END=7000;CN=1\tGT:CN\t0/1:1\n",
            "1\t3000\tnanq\tN\t<DEL>\tnan\tPASS\tSVTYPE=DEL;END=8000;CN=1\tGT:CN\t0/1:1\n",
            "1\t999999999\tfar\tN\t<DEL>\t60\tPASS\tSVTYPE=DEL;END=999999999;CN=1\tGT:CN\t0/1:1\n",
        ]
        cs = submit(dao, w, w.run, record_class="cnv", data=cnv_text(w.run.sample, [good, *bad]))
        counts = row(su, "SELECT parsed_document->'counts' FROM call_sets WHERE id = %s", (cs,))[0][0]
        assert counts["records"] == 6 and counts["detected"] == 1 and counts["invalid"] == 5, counts
        dao.validate_call_set(w.interp1, cs)
        (shown,) = dao.list_patient_validated_call_sets(w.reader, w.run.patient)
        assert [r.source_id for r in shown["call_set"].cnvs] == ["good"]
        assert len(shown["call_set"].rejected) == 5

    def test_a_file_that_is_not_a_cnv_vcf_at_all_is_refused_whole(self, su, dao, cnv_world):
        w = cnv_world
        with pytest.raises(CallSetInputError):
            submit(dao, w, w.run, record_class="cnv", data=b"this is not a vcf\n")
        assert row(su, "SELECT count(*) FROM call_sets") == [(0,)]


# ═══ M-4: the derived-document limit is a clean refusal, not a database CHECK failure ═══


class TestDerivedDocumentLimit:
    def test_a_file_inside_the_byte_and_record_limits_can_still_exceed_the_stored_document_limit(
        self, su, dao, world, monkeypatch
    ):
        data = vcf_text(world.run.sample, n=5)
        monkeypatch.setattr(DataAccess, "CALLSET_DB_MAX_DOCUMENT_BYTES", 500)
        with pytest.raises(CallSetInputError, match="stored-document size limit"):
            submit(dao, world, world.run, data=data)
        assert row(su, "SELECT count(*) FROM call_sets") == [(0,)] and row(
            su, "SELECT count(*) FROM call_set_events"
        ) == [(0,)]
        monkeypatch.undo()
        submit(dao, world, world.run, data=data)  # twin: the same file under the real limit is accepted
        assert row(su, "SELECT count(*) FROM call_sets") == [(1,)]


class TestTheDatabaseTakesTheLockEvenWhenNoPurgeCodeDoes:
    def test_a_raw_redaction_waits_for_a_lifecycle_act_in_flight(self, su, dao, world, purgeable, ret_conn):
        """The lock is also in the call_sets trigger, so a purge written some other way (a script, a future path) cannot
        redact under an event that has not committed."""
        cs = purgeable
        system = row(su, "SELECT user_id FROM users WHERE org_id = %s AND is_system_account", (world.org,))[0][0]
        connection, local = open_transaction_dao()
        try:
            local.request_call_set_retraction(world.interp1, cs, "in flight")  # NOT committed: lock held
            pid = ret_conn.info.backend_pid

            def raw_purge():
                ret_conn.execute("SELECT set_config('clinical.actor_id', %s, true)", (str(system),))
                ret_conn.execute(
                    "UPDATE call_sets SET tombstoned_at = now(), tombstoned_by = %s, source_vcf = NULL, parsed_document = NULL, "
                    "content_redacted_at = now(), content_redacted_by = %s WHERE org_id = %s AND id = %s",
                    (system, system, world.org, cs),
                )
                ret_conn.execute(
                    'INSERT INTO audit_log (org_id, user_id, "timestamp", actor_role, action, resource_type, resource_id, outcome, details) '
                    "VALUES (%s, %s, now(), 'System', 'retention_purge', 'call_set', %s, 'success', '{}'::jsonb)",
                    (world.org, system, str(cs)),
                )
                ret_conn.commit()

            thread, box = run_in_thread(raw_purge)
            wait_until_blocked(su, pid)
            assert thread.is_alive(), "the raw redaction must wait for the in-flight event"
            connection.rollback()  # the lifecycle act is abandoned; the redaction may now proceed
            thread.join(timeout=30)
        finally:
            connection.close()
        assert "error" not in box, box
        assert row(su, "SELECT content_redacted_at IS NOT NULL FROM call_sets WHERE id = %s", (cs,)) == [(True,)]
        assert events(su, cs) == ["submitted", "validated"], "the abandoned event left no trace"
