# ruff: noqa: F811
"""
Third correction wave for the CNV/SV call-set work, against the REAL schema as the REAL `clinical_app` /
`clinical_retention` roles:

  H-A  terminal-state replay and the safe resubmission path
  M-A  FOR SHARE locking proved THROUGH `submit_call_set` / `validate_call_set` / `resubmit_call_set`
  M-B  the map re-verifies stored bytes against the stored document and fails closed
  M-C  orders.required_scope, orders.consent_id and the meaning of a consent are fixed under a call set
  M-D  the migration verifies all five identity triggers
  M-E  a submission's refusal says nothing about the run's state

Concurrency tests wait on a CONDITION (a backend waiting on a lock), never a guessed sleep; see
test_callset_review_fixes.py for the technique.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import shutil
import tempfile
import uuid

import pytest
from psycopg.types.json import Jsonb

from clinical import migrate
from clinical.data_access import (
    AuthorizationError,
    CallSetConflictError,
    CallSetInputError,
    DataAccess,
    NotFoundError,
)
from clinical.genome_analysis import CALLSET_PARSER_VERSION, parse_structural_vcf
from clinical.tests.migration_db import connect, scratch_database
from clinical.tests.test_callset_dao import (  # noqa: F401  (fixtures and helpers re-used)
    T0,
    _fresh_dao,
    app_conn,
    approve_caller,
    audits,
    build_org,
    dao,
    events,
    new_run,
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
from clinical.tests.test_callset_review_fixes import (  # noqa: F401
    _order_state,
    _qc,
    _restore_consent,
    _small_variant_analysis,
    _withdraw_consent,
    open_transaction_dao,
    purgeable,
    ret_conn,
    run_in_thread,
    wait_until_blocked,
)

psycopg = pytest.importorskip("psycopg")
RUN_REFUSAL = DataAccess.CALLSET_RUN_REFUSAL


# ═══ H-A: replay by state, and the safe resubmission path ═════════════════════


class TestReplayByState:
    def _terminal(self, su, dao, world, how):
        data = vcf_text(world.run.sample)
        cs = submit(dao, world, world.run, data=data)
        if how == "rejected":
            dao.reject_call_set(world.interp1, cs, "wrong sample ordering")
        elif how == "withdrawn":
            dao.withdraw_call_set(world.lab1, cs, "wrong file")
        elif how == "retracted":
            dao.validate_call_set(world.interp1, cs)
            dao.retract_call_set(world.appr1, cs, "data error")
        elif how == "superseded":
            dao.validate_call_set(world.interp1, cs)
            newer = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2), supersedes=cs)
            dao.validate_call_set(world.interp2, newer)
        assert state(su, cs) == how
        return cs, data

    @pytest.mark.parametrize("how", ["rejected", "withdrawn", "retracted", "superseded"])
    def test_a_replay_of_a_terminal_call_set_is_never_a_successful_return(self, su, dao, world, how):
        cs, data = self._terminal(su, dao, world, how)
        before = row(su, "SELECT count(*) FROM call_sets")
        with pytest.raises(CallSetConflictError) as refused:
            submit(dao, world, world.run, data=data)
        assert how in str(refused.value), "the submitter is told the state, not handed an id"
        assert row(su, "SELECT count(*) FROM call_sets") == before and events(su, cs).count("submitted") == 1

    @pytest.mark.parametrize("how", ["rejected", "withdrawn"])
    def test_for_a_rejected_or_withdrawn_file_the_message_points_at_resubmission(self, su, dao, world, how):
        _cs, data = self._terminal(su, dao, world, how)
        with pytest.raises(CallSetConflictError, match="resubmit_call_set"):
            submit(dao, world, world.run, data=data)

    def test_a_retracted_or_superseded_file_is_not_offered_for_resubmission(self, su, dao, world):
        _cs, data = self._terminal(su, dao, world, "retracted")
        with pytest.raises(CallSetConflictError) as refused:
            submit(dao, world, world.run, data=data)
        assert "resubmit_call_set" not in str(refused.value) and "cannot be resubmitted unchanged" in str(refused.value)

    def test_another_technician_learns_nothing_about_the_state_of_someone_elses_submission(self, su, dao, world):
        _cs, data = self._terminal(su, dao, world, "rejected")
        with pytest.raises(CallSetConflictError) as refused:
            submit(dao, world, world.run, data=data, session=world.lab2)
        assert str(refused.value) == DataAccess.CALLSET_CONFLICT_REFUSAL and "rejected" not in str(refused.value)

    def test_a_replay_of_a_live_call_set_is_still_idempotent(self, su, dao, world):
        data = vcf_text(world.run.sample)
        first = submit(dao, world, world.run, data=data)
        assert submit(dao, world, world.run, data=data) == first
        dao.validate_call_set(world.interp1, first)
        assert submit(dao, world, world.run, data=data) == first and events(su, first) == ["submitted", "validated"]


class TestResubmission:
    @pytest.mark.parametrize("how", ["rejected", "withdrawn"])
    def test_a_rejected_or_withdrawn_call_set_can_be_resubmitted_as_a_NEW_call_set(
        self, su, dao, world, how, monkeypatch
    ):
        data = vcf_text(world.run.sample, n=2)
        old = submit(dao, world, world.run, data=data)
        if how == "rejected":
            dao.reject_call_set(world.interp1, old, "wrong sample ordering")
        else:
            dao.withdraw_call_set(world.lab1, old, "wrong file")
        # the CURRENT parser and a fresh retention snapshot are used, so a parser bump is picked up
        import clinical.genome_analysis as ga

        monkeypatch.setitem(ga.PARSER_RULESETS, "structural_vcf/2", ga.parse_structural_vcf)
        monkeypatch.setattr(ga, "CALLSET_PARSER_VERSION", "structural_vcf/2")
        new = dao.resubmit_call_set(world.lab1, old, "the sample sheet was corrected")
        assert new != old and state(su, new) == "pending_validation" and state(su, old) == how
        assert events(su, new) == ["submitted"], "no lifecycle is carried over"
        (r,) = row(
            su,
            "SELECT n.resubmission_of, n.parser_version, o.parser_version, n.source_content_hash = o.source_content_hash, "
            "n.created_by, n.retention_days FROM call_sets n, call_sets o WHERE n.id = %s AND o.id = %s",
            (new, old),
        )
        assert r == (old, "structural_vcf/2", CALLSET_PARSER_VERSION, True, world.lab1.user_id, 1825)
        assert row(su, "SELECT reason FROM call_set_events WHERE call_set_id = %s", (new,)) == [
            ("the sample sheet was corrected",)
        ]
        dao.validate_call_set(
            world.interp2, new
        )  # its own, fresh validation, re-derived with the rule set it was recorded under
        assert state(su, new) == "validated"
        monkeypatch.undo()
        (a,) = [x for x in audits(su, "call_set_resubmitted") if x[0] == "success"]
        assert a[1] == "call_set" and a[2] == str(new) and a[3] in ({}, None)

    def test_every_check_is_made_again_on_resubmission(self, su, dao, world):
        old = submit(dao, world, world.run)
        dao.withdraw_call_set(world.lab1, old, "oops")
        _qc(su, world, world.run, "failed")
        with pytest.raises(CallSetConflictError) as refused:
            dao.resubmit_call_set(world.lab1, old, "retry")
        assert str(refused.value) == RUN_REFUSAL
        _qc(su, world, world.run, "passed")
        _withdraw_consent(su, world.run)
        with pytest.raises(CallSetConflictError, match="cannot accept"):
            dao.resubmit_call_set(world.lab1, old, "retry")
        _restore_consent(su, world.run)
        assert state(su, dao.resubmit_call_set(world.lab1, old, "retry")) == "pending_validation"

    def test_only_a_rejected_or_withdrawn_call_set_of_the_submitter_can_be_resubmitted(self, su, dao, world):
        pending = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=1))
        with pytest.raises(CallSetConflictError, match="pending_validation"):
            dao.resubmit_call_set(world.lab1, pending, "x")
        dao.validate_call_set(world.interp1, pending)
        with pytest.raises(CallSetConflictError, match="validated"):
            dao.resubmit_call_set(world.lab1, pending, "x")
        dao.retract_call_set(world.appr1, pending, "defect")
        with pytest.raises(CallSetConflictError, match="retracted"):
            dao.resubmit_call_set(world.lab1, pending, "x")
        rejected = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2))
        dao.reject_call_set(world.interp1, rejected, "bad")
        with pytest.raises(NotFoundError):
            dao.resubmit_call_set(world.lab2, rejected, "not mine")
        for role_holder in (world.interp1, world.appr1, world.reader):
            with pytest.raises(AuthorizationError):
                dao.resubmit_call_set(role_holder, rejected, "x")
        with pytest.raises(CallSetInputError):
            dao.resubmit_call_set(world.lab1, rejected, "   ")

    def test_duplicate_protection_holds_while_a_resubmission_is_live(self, su, dao, world):
        old = submit(dao, world, world.run)
        dao.withdraw_call_set(world.lab1, old, "oops")
        new = dao.resubmit_call_set(world.lab1, old, "again")
        with pytest.raises(CallSetConflictError, match="conflicts"):
            dao.resubmit_call_set(world.lab1, old, "twice")  # a live one exists
        data = vcf_text(world.run.sample)
        assert submit(dao, world, world.run, data=data) == new, "and a plain replay returns the live one"
        dao.reject_call_set(world.interp1, new, "still wrong")
        again = dao.resubmit_call_set(world.lab1, new, "third attempt")  # the chain continues, one live at a time
        assert state(su, again) == "pending_validation"
        assert row(su, "SELECT count(*) FROM call_sets WHERE resubmission_of IS NOT NULL") == [(2,)]

    def test_the_cross_run_and_wrong_run_rules_are_untouched(self, su, dao, world):
        data = vcf_text(world.run.sample)
        other = same_sample_run(su, world, world.run)
        old = submit(dao, world, world.run, data=data)
        dao.withdraw_call_set(world.lab1, old, "wrong run")
        with pytest.raises(CallSetConflictError, match="conflicts with an existing"):
            submit(dao, world, other, data=data)  # withdrawal alone does not free the bytes for another run
        again = dao.resubmit_call_set(world.lab1, old, "right run after all")
        dao.validate_call_set(world.interp1, again)
        dao.retract_call_set(world.appr1, again, "placed on the wrong run", wrong_run_placement=True)
        moved = submit(dao, world, other, data=data)  # only a wrong-run retraction frees them, as a NEW call set
        assert state(su, moved) == "pending_validation"

    def test_a_redacted_call_set_cannot_be_resubmitted(self, su, dao, world, ret_conn):
        from datetime import datetime, timezone

        from clinical.retention import RetentionPrincipal

        old = submit(dao, world, world.run)
        dao.withdraw_call_set(world.lab1, old, "x")
        _release_and_system_principal(su, dao, world, datetime(2030, 1, 1, tzinfo=timezone.utc))
        RetentionPrincipal(ret_conn).purge_expired(world.org, "call_set", now=datetime(2060, 1, 1, tzinfo=timezone.utc))
        ret_conn.commit()
        assert row(su, "SELECT source_vcf IS NULL FROM call_sets WHERE id = %s", (old,)) == [(True,)]
        with pytest.raises(CallSetConflictError, match="retained out"):
            dao.resubmit_call_set(world.lab1, old, "x")

    def test_two_concurrent_resubmissions_of_one_call_set_make_exactly_one(self, su, dao, world):
        import threading

        old = submit(dao, world, world.run)
        dao.withdraw_call_set(world.lab1, old, "oops")
        outcomes, barrier = [], threading.Barrier(2)

        def attempt():
            connection, local = _fresh_dao()
            try:
                barrier.wait(timeout=10)
                outcomes.append(("ok", local.resubmit_call_set(world.lab1, old, "retry")))
            except CallSetConflictError as exc:
                outcomes.append(("conflict", str(exc)))
            finally:
                connection.close()

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        [t.start() for t in threads]
        [t.join(timeout=30) for t in threads]
        assert sorted(o[0] for o in outcomes) == ["conflict", "ok"], outcomes
        assert row(su, "SELECT count(*) FROM call_sets WHERE resubmission_of = %s", (old,)) == [(1,)]


def _release_and_system_principal(su, dao, world, when):
    from clinical.tests.test_callset_dao import release

    release(su, world, world.run, when)
    system_principal(dao, world.org)


# ── the same rules, reached by direct SQL as the application role ──


def _raw_insert(connection, w, run, data, *, resubmission_of=None, user=None, commit=False):
    user = user or w.lab1
    doc = parse_structural_vcf(data.decode(), assembly="GRCh38", record_class="sv").to_document()
    cid = uuid.uuid4()
    with connection.cursor() as cur:
        cur.execute("SELECT set_config('clinical.actor_id', %s, true)", (str(user.user_id),))
        cur.execute(
            "INSERT INTO call_sets (org_id,id,sequencing_run_id,record_class,assembly,caller_name,caller_version,"
            "caller_approved_at_submission,vcf_sample_name,source_filename,source_vcf,source_content_hash,parser_version,"
            "parsed_document,resubmission_of,created_at,created_by,retention_days) "
            "VALUES (%s,%s,%s,'sv','GRCh38','caller-x','1.0',false,%s,'x.vcf',%s,%s,%s,%s,%s,now(),%s,1825)",
            (
                w.org,
                cid,
                run.id,
                run.sample,
                data,
                hashlib.sha256(data).hexdigest(),
                CALLSET_PARSER_VERSION,
                Jsonb(doc),
                resubmission_of,
                user.user_id,
            ),
        )
        cur.execute(
            "INSERT INTO call_set_events (org_id,call_set_id,event_type,actor_id,actor_role,occurred_at) "
            "VALUES (%s,%s,'submitted',%s,'Lab technician',now())",
            (w.org, cid, user.user_id),
        )
    if commit:
        connection.commit()
    return cid


class TestTheDatabaseEnforcesTheSameRunRule:
    def test_without_the_unique_index_a_live_duplicate_is_still_refused(self, su, dao, world, app_conn):
        data = vcf_text(world.run.sample)
        submit(dao, world, world.run, data=data)
        with pytest.raises(psycopg.errors.RaiseException, match="already live on this sequencing run"):
            _raw_insert(app_conn, world, world.run, data)
        app_conn.rollback()
        assert row(su, "SELECT count(*) FROM call_sets") == [(1,)]

    def test_a_non_live_duplicate_needs_an_explicit_and_valid_resubmission_reference(self, su, dao, world, app_conn):
        data = vcf_text(world.run.sample)
        old = submit(dao, world, world.run, data=data)
        dao.reject_call_set(world.interp1, old, "bad")
        with pytest.raises(psycopg.errors.RaiseException, match="only as a resubmission"):
            _raw_insert(app_conn, world, world.run, data)
        app_conn.rollback()
        elsewhere = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=3))
        dao.reject_call_set(world.interp1, elsewhere, "bad")
        with pytest.raises(psycopg.errors.RaiseException, match="must name a rejected or withdrawn"):
            _raw_insert(app_conn, world, world.run, data, resubmission_of=elsewhere)  # a different file
        app_conn.rollback()
        new = _raw_insert(app_conn, world, world.run, data, resubmission_of=old, commit=True)
        assert state(su, new) == "pending_validation"

    def test_a_resubmission_cannot_name_a_pending_validated_or_retracted_call_set(self, su, dao, world, app_conn):
        data = vcf_text(world.run.sample)
        live = submit(dao, world, world.run, data=data)
        with pytest.raises(psycopg.errors.RaiseException):
            _raw_insert(app_conn, world, world.run, data, resubmission_of=live)
        app_conn.rollback()
        dao.validate_call_set(world.interp1, live)
        dao.retract_call_set(world.appr1, live, "defect")
        with pytest.raises(psycopg.errors.RaiseException, match="must name a rejected or withdrawn"):
            _raw_insert(app_conn, world, world.run, data, resubmission_of=live)
        app_conn.rollback()

    def test_two_racing_direct_inserts_of_one_file_leave_exactly_one(self, su, dao, world):
        data = vcf_text(world.run.sample)
        first_conn, _ = _fresh_dao()
        second_conn, _ = _fresh_dao()
        try:
            _raw_insert(first_conn, world, world.run, data)  # inserted, NOT committed: holds the content lock
            pid = second_conn.info.backend_pid
            thread, box = run_in_thread(
                lambda: _raw_insert(second_conn, world, world.run, data, user=world.lab2, commit=True)
            )
            wait_until_blocked(su, pid)
            assert thread.is_alive()
            first_conn.commit()
            thread.join(timeout=30)
        finally:
            first_conn.close()
            second_conn.close()
        assert isinstance(box.get("error"), psycopg.errors.RaiseException), box
        assert row(su, "SELECT count(*) FROM call_sets") == [(1,)]


# ═══ M-A: FOR SHARE, proved through the production methods ════════════════════

WRITERS = {
    "tests": "UPDATE tests SET name = 'renamed' WHERE test_id = %(test)s",
    "samples": "UPDATE samples SET qc_reason = 'x' WHERE sample_id = %(sample)s",
    "orders": "UPDATE orders SET priority = 'urgent' WHERE order_id = (SELECT order_id FROM samples WHERE sample_id = %(sample)s)",
    "sequencing_runs": "UPDATE sequencing_runs SET created_by = created_by WHERE id = %(run)s",
}


class TestForShareThroughTheProductionMethods:
    @pytest.mark.parametrize("table", list(WRITERS))
    def test_validate_call_set_locks_every_row_of_the_chain(self, su, dao, world, table):
        cs = submit(dao, world, world.run)
        connection, local = open_transaction_dao()
        try:
            local.validate_call_set(world.interp1, cs)  # the real method; its transaction is left open
            self._writer_waits(su, world, table, connection)
        finally:
            connection.close()

    @pytest.mark.parametrize("table", list(WRITERS))
    def test_submit_call_set_locks_every_row_of_the_chain(self, su, dao, world, table):
        connection, local = open_transaction_dao()
        try:
            submit(local, world, world.run)
            self._writer_waits(su, world, table, connection)
        finally:
            connection.close()

    @pytest.mark.parametrize("table", ["tests", "samples"])
    def test_resubmit_call_set_locks_the_chain_too(self, su, dao, world, table):
        old = submit(dao, world, world.run)
        dao.withdraw_call_set(world.lab1, old, "x")
        connection, local = open_transaction_dao()
        try:
            local.resubmit_call_set(world.lab1, old, "again")
            self._writer_waits(su, world, table, connection)
        finally:
            connection.close()

    @staticmethod
    def _writer_waits(su, world, table, holder_connection):
        params = {"test": world.tests["GRCh38"], "sample": world.run.sample, "run": world.run.id}
        writer = psycopg.connect(os.environ["CLINICAL_TEST_DSN"])
        try:
            writer.execute("SET ROLE clinical_app")
            writer.commit()
            pid = writer.info.backend_pid
            thread, box = run_in_thread(lambda: (writer.execute(WRITERS[table], params), writer.commit()))
            wait_until_blocked(su, pid)
            assert thread.is_alive(), f"a write to {table} must wait for the production method's lock"
            holder_connection.rollback()
            thread.join(timeout=30)
            assert "error" not in box, box
        finally:
            writer.close()

    def test_without_the_method_holding_the_transaction_the_same_writes_do_not_wait(self, su, dao, world):
        """Twin: after the production method has COMMITTED, the identical writer is not blocked at all."""
        cs = submit(dao, world, world.run)
        dao.validate_call_set(world.interp1, cs)
        writer = psycopg.connect(os.environ["CLINICAL_TEST_DSN"])
        try:
            writer.execute("SET ROLE clinical_app")
            writer.execute(WRITERS["tests"], {"test": world.tests["GRCh38"]})
            writer.commit()
        finally:
            writer.close()


# ═══ M-B: the map re-verifies the stored evidence and fails closed ════════════


class TestMapFailsClosedOnIntegrity:
    def _shown(self, dao, w):
        return [r["call_set_id"] for r in dao.list_patient_validated_call_sets(w.reader, w.run.patient)]

    def test_a_document_that_does_not_match_its_bytes_is_never_returned_and_is_audited(self, su, dao, world, app_conn):
        DataAccess._CALLSET_VERIFIED_CACHE.clear()
        good = validated(su, dao, world)
        other = new_run(su, dao, world, patient=world.run.patient)
        bytes_ = vcf_text(other.sample)
        fake_src = vcf_text(other.sample, base=777777)
        fake_doc = parse_structural_vcf(fake_src.decode(), assembly="GRCh38", record_class="sv").to_document()
        cid = uuid.uuid4()
        user = world.lab1
        with app_conn.cursor() as cur:  # the application role, directly, with a document that satisfies the models
            cur.execute("SELECT set_config('clinical.actor_id', %s, true)", (str(user.user_id),))
            cur.execute(
                "INSERT INTO call_sets (org_id,id,sequencing_run_id,record_class,assembly,caller_name,caller_version,"
                "caller_approved_at_submission,vcf_sample_name,source_filename,source_vcf,source_content_hash,parser_version,"
                "parsed_document,created_at,created_by,retention_days) "
                "VALUES (%s,%s,%s,'sv','GRCh38','caller-x','1.0',false,%s,'x.vcf',%s,%s,%s,%s,now(),%s,1825)",
                (
                    world.org,
                    cid,
                    other.id,
                    other.sample,
                    bytes_,
                    hashlib.sha256(bytes_).hexdigest(),
                    CALLSET_PARSER_VERSION,
                    Jsonb(fake_doc),
                    user.user_id,
                ),
            )
            cur.execute(
                "INSERT INTO call_set_events (org_id,call_set_id,event_type,actor_id,actor_role,occurred_at) VALUES (%s,%s,'submitted',%s,'Lab technician',now())",
                (world.org, cid, user.user_id),
            )
        app_conn.commit()
        approval = row(
            su, "SELECT id FROM caller_approvals WHERE org_id = %s AND event_type = 'approved'", (world.org,)
        )[0][0]
        with app_conn.cursor() as cur:
            cur.execute("SELECT set_config('clinical.actor_id', %s, true)", (str(world.interp1.user_id),))
            cur.execute(
                "INSERT INTO call_set_events (org_id,call_set_id,event_type,actor_id,actor_role,occurred_at,checks,caller_approval_id) "
                "VALUES (%s,%s,'validated',%s,'Interpreter',now(),%s,%s)",
                (
                    world.org,
                    cid,
                    world.interp1.user_id,
                    Jsonb({"sample_identity": True, "assembly": True, "caller": True}),
                    approval,
                ),
            )
        app_conn.commit()
        assert state(su, cid) == "validated", "the database accepts it: it cannot compare bytes with a document"
        assert self._shown(dao, world) == [good], "the map must not draw coordinates the bytes do not contain"
        incidents = audits(su, "call_set_integrity_failure")
        assert [(a[0], a[1], a[2], a[3]) for a in incidents] == [("error", "call_set", str(cid), {})]

    def test_each_kind_of_mismatch_is_excluded_and_a_twin_proves_the_good_one_shows(self, su, dao, world):
        DataAccess._CALLSET_VERIFIED_CACHE.clear()
        cs = validated(su, dao, world)
        assert self._shown(dao, world) == [cs]
        tamper(
            su,
            "UPDATE call_sets SET parsed_document = jsonb_set(parsed_document, '{records,0,status}', '\"uncertain\"') WHERE id = %s",
            (cs,),
        )
        assert self._shown(dao, world) == [], "document edited after the fact"
        tamper(
            su,
            "UPDATE call_sets SET parsed_document = jsonb_set(parsed_document, '{records,0,status}', '\"detected\"') WHERE id = %s",
            (cs,),
        )
        assert self._shown(dao, world) == [cs], "restored: verified again, not remembered as bad"
        new_bytes = vcf_text(world.run.sample, n=1, base=555555)
        tamper(
            su,
            "UPDATE call_sets SET source_vcf = %s, source_content_hash = %s WHERE id = %s",
            (new_bytes, hashlib.sha256(new_bytes).hexdigest(), cs),
        )
        assert self._shown(dao, world) == [], "bytes replaced consistently with their hash, document now disagrees"
        assert (
            len(audits(su, "call_set_integrity_failure")) == 1
        )  # reported once per call set per hour, not on every read

    def test_bytes_and_hash_cannot_disagree_in_the_database_and_the_map_check_refuses_them_anyway(self, su, dao, world):
        cs = validated(su, dao, world)
        with pytest.raises(psycopg.errors.CheckViolation):
            tamper(su, "UPDATE call_sets SET source_content_hash = %s WHERE id = %s", ("0" * 64, cs))
        su.rollback()
        source, doc, digest, version = row(
            su,
            "SELECT source_vcf, parsed_document, source_content_hash, parser_version FROM call_sets WHERE id = %s",
            (cs,),
        )[0]
        DataAccess._CALLSET_VERIFIED_CACHE.clear()
        assert DataAccess._callset_evidence_intact(cs, bytes(source), digest, version, doc) is True
        assert DataAccess._callset_evidence_intact(cs, bytes(source), "0" * 64, version, doc) is False
        assert DataAccess._callset_evidence_intact(cs, bytes(source) + b"x", digest, version, doc) is False
        assert DataAccess._callset_evidence_intact(cs, b"", digest, version, doc) is False
        assert DataAccess._callset_evidence_intact(cs, None, digest, version, doc) is False

    def test_an_unavailable_or_unknown_rule_set_fails_closed(self, su, dao, world, monkeypatch):
        DataAccess._CALLSET_VERIFIED_CACHE.clear()
        cs = validated(su, dao, world)
        import clinical.genome_analysis as ga

        monkeypatch.delitem(ga.PARSER_RULESETS, CALLSET_PARSER_VERSION)
        monkeypatch.setitem(ga.PARSER_RULESETS, "structural_vcf/9", ga.parse_structural_vcf)
        assert self._shown(dao, world) == []
        monkeypatch.undo()
        DataAccess._CALLSET_VERIFIED_CACHE.clear()
        assert self._shown(dao, world) == [cs]
        tamper(su, "UPDATE call_sets SET parser_version = 'structural_vcf/0' WHERE id = %s", (cs,))
        assert self._shown(dao, world) == []

    def test_a_verified_call_set_is_not_re_parsed_on_every_read_but_a_changed_document_is_re_verified(
        self, su, dao, world, monkeypatch
    ):
        DataAccess._CALLSET_VERIFIED_CACHE.clear()
        cs = validated(su, dao, world)
        import clinical.genome_analysis as ga

        calls = []
        real = ga.verify_document_for_version
        monkeypatch.setattr(ga, "verify_document_for_version", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
        assert self._shown(dao, world) == [cs] and len(calls) == 1
        assert self._shown(dao, world) == [cs] and len(calls) == 1, "second read served from the verified cache"
        tamper(
            su,
            "UPDATE call_sets SET parsed_document = jsonb_set(parsed_document, '{records,0,status}', '\"uncertain\"') WHERE id = %s",
            (cs,),
        )
        assert self._shown(dao, world) == [] and len(calls) == 2, "a different document is never served from the cache"


# ═══ M-C: the consent relationship is fixed under a call set ══════════════════


def _second_consent(dao, world):
    return dao.record_consent(world.reader, world.run.patient, "testing")


class TestConsentRelationshipIsProtected:
    @pytest.mark.parametrize(
        "sql",
        [
            "UPDATE orders SET required_scope = 'other-scope' WHERE org_id = %(org)s",
            "UPDATE orders SET consent_id = %(other_consent)s WHERE org_id = %(org)s",
            "UPDATE consents SET scope = 'other-scope' WHERE consent_id = %(consent)s",
            "UPDATE consents SET patient_id = %(other_patient)s WHERE consent_id = %(consent)s",
            "UPDATE consents SET recorded_at = recorded_at + interval '1 day' WHERE consent_id = %(consent)s",
        ],
        ids=["order-scope", "order-consent", "consent-scope", "consent-patient", "consent-recorded-at"],
    )
    def test_the_application_role_cannot_repoint_the_consent_relationship(self, su, dao, world, app_conn, sql):
        validated(su, dao, world)
        other_patient = new_run(su, dao, world).patient  # a second patient, for the repoint target
        params = {
            "org": world.org,
            "consent": row(
                su,
                "SELECT consent_id FROM orders WHERE order_id = (SELECT order_id FROM samples WHERE sample_id = %s)",
                (world.run.sample,),
            )[0][0],
            "other_consent": _second_consent(dao, world),
            "other_patient": other_patient,
        }
        with app_conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException, match="callset_identity"):
                cur.execute(sql, params)
        app_conn.rollback()

    def test_a_withdrawn_consent_cannot_be_revived_or_a_supersession_undone(self, su, dao, world, app_conn):
        validated(su, dao, world)
        consent = row(
            su,
            "SELECT consent_id FROM orders WHERE order_id = (SELECT order_id FROM samples WHERE sample_id = %s)",
            (world.run.sample,),
        )[0][0]
        _withdraw_consent(su, world.run)  # recording a withdrawal is allowed (NULL -> value)
        with app_conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException, match="callset_identity"):
                cur.execute(
                    "UPDATE consents SET withdrawn_at = NULL, withdrawn_by = NULL WHERE consent_id = %s", (consent,)
                )
        app_conn.rollback()
        newer = dao.record_consent(
            world.reader, world.run.patient, "testing", supersedes_id=consent
        )  # NULL -> value allowed
        assert row(su, "SELECT superseded_by FROM consents WHERE consent_id = %s", (consent,)) == [(newer,)]
        with app_conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException, match="callset_identity"):
                cur.execute("UPDATE consents SET superseded_by = NULL WHERE consent_id = %s", (consent,))
        app_conn.rollback()

    def test_the_scope_swap_that_used_to_bring_evidence_back_is_now_refused(self, su, dao, world, app_conn):
        cs = validated(su, dao, world)
        dao.record_consent(world.reader, world.run.patient, "other-scope")
        _withdraw_consent(su, world.run)
        assert dao.list_patient_validated_call_sets(world.reader, world.run.patient) == []
        with app_conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException, match="callset_identity"):
                cur.execute("UPDATE orders SET required_scope = 'other-scope' WHERE org_id = %s", (world.org,))
        app_conn.rollback()
        assert (
            dao.list_patient_validated_call_sets(world.reader, world.run.patient) == [] and state(su, cs) == "validated"
        )

    def test_recording_consent_still_works_and_other_patients_are_unaffected(self, su, dao, world, app_conn):
        validated(su, dao, world)
        free = new_run(su, dao, world)  # a patient with NO call set
        free_consent = row(su, "SELECT consent_id FROM consents WHERE patient_id = %s", (free.patient,))[0][0]
        with app_conn.cursor() as cur:
            cur.execute("UPDATE consents SET scope = 'renamed' WHERE consent_id = %s", (free_consent,))
            cur.execute(
                "UPDATE orders SET required_scope = 'renamed' WHERE order_id = (SELECT order_id FROM samples WHERE sample_id = %s)",
                (free.sample,),
            )
        app_conn.commit()
        assert dao.record_consent(world.reader, world.run.patient, "extra") is not None


# ═══ M-D: the migration verifies the five identity triggers ═══════════════════

MIGRATIONS = pathlib.Path(migrate.__file__).resolve().parent / "migrations"


def _trigger_assertion():
    text = (MIGRATIONS / "0010_call_sets.sql").read_text(encoding="utf-8")
    (line,) = [
        m
        for m in re.findall(r"^--\s*MIGRATION-ASSERT:\s*(.+?)\s*$", text, re.M)
        if "trg_callset_identity_consents" in m
    ]
    return line


def _holds(connection, expression):
    with connection.cursor() as cur:
        cur.execute(f"SELECT ({expression})")
        value = cur.fetchone()[0]
    connection.rollback()
    return value


class TestMigrationVerifiesTheIdentityTriggers:
    def test_the_assertion_is_true_on_a_correct_database_for_the_application_role_too(self):
        from clinical.bootstrap import apply_schema

        with scratch_database("w3trg") as dsn, connect(dsn) as c:
            apply_schema(c)
            c.commit()
            assert _holds(c, _trigger_assertion()) is True
            with c.cursor() as cur:
                cur.execute("SET ROLE clinical_app")
                cur.execute(f"SELECT ({_trigger_assertion()})")
                assert cur.fetchone()[0] is True, "/readyz evaluates these as clinical_app"
            c.rollback()

    @pytest.mark.parametrize(
        "damage",
        [
            "DROP TRIGGER trg_callset_identity_tests ON public.tests",
            "DROP TRIGGER trg_callset_identity_consents ON public.consents",
            "ALTER TABLE public.orders DISABLE TRIGGER trg_callset_identity_orders",
            "DROP TRIGGER trg_callset_identity_orders ON public.orders; CREATE TRIGGER trg_callset_identity_orders BEFORE UPDATE OF patient_id, test_id, org_id "
            "ON public.orders FOR EACH ROW EXECUTE FUNCTION public.callset_identity_guard()",
            "DROP TRIGGER trg_callset_identity_runs ON public.sequencing_runs; CREATE TRIGGER trg_callset_identity_runs AFTER UPDATE OF sample_id, org_id "
            "ON public.sequencing_runs FOR EACH ROW EXECUTE FUNCTION public.callset_identity_guard()",
            "DROP TRIGGER trg_callset_identity_samples ON public.samples; CREATE TRIGGER trg_callset_identity_samples BEFORE UPDATE OF order_id, org_id "
            "ON public.samples FOR EACH STATEMENT EXECUTE FUNCTION public.callset_identity_guard()",
            "CREATE TRIGGER trg_extra BEFORE UPDATE OF status ON public.tests FOR EACH ROW EXECUTE FUNCTION public.callset_identity_guard()",
        ],
        ids=[
            "tests-dropped",
            "consents-dropped",
            "orders-disabled",
            "orders-narrowed",
            "runs-after",
            "samples-statement-level",
            "extra-trigger",
        ],
    )
    def test_each_kind_of_damage_makes_the_assertion_false(self, damage):
        from clinical.bootstrap import apply_schema

        with scratch_database("w3trg2") as dsn, connect(dsn) as c:
            apply_schema(c)
            c.commit()
            assert _holds(c, _trigger_assertion()) is True
            with c.cursor() as cur:
                cur.execute(damage)
            c.commit()
            assert _holds(c, _trigger_assertion()) is False, damage

    def test_a_database_missing_the_triggers_fails_verification_and_a_correct_one_passes(self):
        """`migrate()` applies; the post-conditions are evaluated by `inspect_state` -- what `migrate --verify` (run by the
        deploy after `--migrate`) and `/readyz` call. So the proof is that verification FAILS without the triggers."""
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="w3mig_"))
        try:
            for f in MIGRATIONS.glob("*.sql"):
                shutil.copy(f, tmp / f.name)
            text = (tmp / "0010_call_sets.sql").read_text(encoding="utf-8")
            a = text.index("CREATE TRIGGER trg_callset_identity_runs")
            b = text.index("-- ─── GRANTS (same pattern")
            (tmp / "0010_call_sets.sql").write_text(text[:a] + text[b:], encoding="utf-8")
            with scratch_database("w3mig") as dsn, connect(dsn) as c:
                migrate.migrate(c, migrate.discover(tmp))
                c.commit()
                bad = migrate.inspect_state(c, migrate.discover(tmp))
                assert not bad.ok, "verification must fail when the identity triggers are missing"
            with scratch_database("w3mig2") as dsn, connect(dsn) as c:
                migrate.migrate(c)
                c.commit()
                good = migrate.inspect_state(c)
                assert good.ok, (good.reason, good.detail)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ═══ M-E: a refusal says nothing about the run ════════════════════════════════


class TestSubmissionRefusalsAreGeneric:
    def _causes(self, su, world):
        return {
            "qc": (lambda: _qc(su, world, world.run, "failed"), lambda: _qc(su, world, world.run, "passed"), {}),
            "cancelled": (
                lambda: _order_state(su, world.run, "cancelled"),
                lambda: _order_state(su, world.run, "in_progress"),
                {},
            ),
            "consent": (lambda: _withdraw_consent(su, world.run), lambda: _restore_consent(su, world.run), {}),
            "analysis-build": (lambda: _small_variant_analysis(su, world, world.run, "GRCh37"), None, {}),
        }

    def test_every_cause_gives_the_identical_message_and_names_no_state(self, su, dao, world):
        messages = {}
        for name, (break_it, fix_it, _kw) in self._causes(su, world).items():
            break_it()
            with pytest.raises(CallSetConflictError) as refused:
                submit(dao, world, world.run, session=world.lab2)
            messages[name] = str(refused.value)
            if fix_it:
                fix_it()
        stranger = str(uuid.uuid4())
        with pytest.raises(CallSetConflictError) as refused:  # a sample that is not this run's
            submit(dao, world, world.run, sample=stranger, data=vcf_text(stranger), session=world.lab2)
        messages["sample"] = str(refused.value)
        with pytest.raises(CallSetConflictError) as refused:  # a build that is not the ordered test's
            submit(
                dao, world, world.run, assembly="GRCh37", data=vcf_text(world.run.sample, "GRCh37"), session=world.lab2
            )
        messages["assembly"] = str(refused.value)
        assert set(messages.values()) == {RUN_REFUSAL}, messages
        for word in ("QC", "consent", "cancel", "GRCh", "assembly", "analysis", "order", "sample has"):
            assert word.lower() not in RUN_REFUSAL.lower(), word

    def test_a_file_error_comes_first_so_an_ineligible_run_looks_exactly_like_an_eligible_one(self, su, dao, world):
        bad = b"not a vcf at all\n"
        with pytest.raises(CallSetInputError) as eligible:
            submit(dao, world, world.run, data=bad, session=world.lab2)
        _qc(su, world, world.run, "failed")
        _withdraw_consent(su, world.run)
        with pytest.raises(CallSetInputError) as ineligible:
            submit(dao, world, world.run, data=bad, session=world.lab2)
        assert str(eligible.value) == str(ineligible.value)
        ok = vcf_text(world.run.sample)
        with pytest.raises(CallSetConflictError) as refused:
            submit(dao, world, world.run, data=ok, session=world.lab2)
        assert str(refused.value) == RUN_REFUSAL

    def test_a_run_that_is_not_in_the_organisation_is_the_same_not_found_as_before(self, su, dao, world):
        other = build_org(su, dao, "z")
        with pytest.raises(NotFoundError):
            submit(dao, other, world.run, session=other.lab1)

    def test_validation_by_an_interpreter_still_names_the_failed_gate(self, su, dao, world):
        """Documented asymmetry: the person reviewing the evidence is told why; the person submitting is not."""
        cs = submit(dao, world, world.run)
        _qc(su, world, world.run, "failed")
        with pytest.raises(CallSetConflictError, match="QC"):
            dao.validate_call_set(world.interp1, cs)
