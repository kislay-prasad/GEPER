# ruff: noqa: F811
"""
Fourth correction wave (third independent review), against the REAL schema as the REAL `clinical_app` /
`clinical_retention` roles:

  H-1  lineage survives replacement and resubmission; ONE active validated call set per run, class and assembly
  H-2  a withdrawn consent cannot be revived through supersession or mutation
  M-1  reviewers see lineage and earlier decisions
  M-4  withdrawn consent withholds the genomic document from reviewers (nothing erased)
  M-5  the map says WHICH validated evidence it is holding back, and why
  M-6  identical bytes cannot be accepted as both evidence classes
  L-1..L-6  NUL input, audit growth, lock ordering, ordering by the database, audit vocabulary, generic messages

Concurrency tests wait on a CONDITION (a backend waiting on a lock), never a guessed sleep.
"""

from __future__ import annotations

import ast
import datetime
import hashlib
import pathlib
import threading
import uuid

import pytest
from psycopg.types.json import Jsonb

from clinical import data_access as da_module
from clinical.data_access import (
    BcryptHasher,
    CallSetConflictError,
    CallSetInputError,
    DataAccess,
    NotFoundError,
)
from clinical.genome_analysis import CALLSET_PARSER_VERSION, parse_structural_vcf
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
    tamper,
    validated,
    vcf_text,
    world,
)
from clinical.tests.test_callset_review_fixes import (  # noqa: F401
    _order_state,
    _qc,
    _small_variant_analysis,
    _withdraw_consent,
    open_transaction_dao,
    run_in_thread,
    wait_until_blocked,
)

psycopg = pytest.importorskip("psycopg")


def _withdraw_all_consents(su, w):
    """Every consent of the organisation's patients (the exclusion fixture records a second one for the same patient)."""
    su.execute("UPDATE consents SET withdrawn_at = now(), withdrawn_by = recorded_by WHERE org_id = %s", (w.org,))
    su.commit()


def _drawn(dao, w):
    return [r["call_set_id"] for r in dao.list_patient_validated_call_sets(w.reader, w.run.patient)]


def _cnv_text(sample, base=1000):
    return (
        "##fileformat=VCFv4.2\n##contig=<ID=1,length=248956422>\n"
        f"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t{sample}\n"
        f"1\t{base}\tc1\tN\t<DEL>\t60\tPASS\tSVTYPE=DEL;END={base + 4000};CN=1\tGT:CN\t0/1:1\n"
    ).encode()


# ═══ H-1: lineage, and ONE active validated call set per run / class / assembly ═══


class TestLineageSurvives:
    def test_a_resubmitted_replacement_keeps_its_replacement_link_and_still_replaces(self, su, dao, world):
        first = validated(su, dao, world, data=vcf_text(world.run.sample, n=1))
        replacement = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2), supersedes=first)
        dao.reject_call_set(world.interp1, replacement, "bad")
        again = dao.resubmit_call_set(world.lab1, replacement, "again")
        assert row(su, "SELECT supersedes_id, resubmission_of FROM call_sets WHERE id = %s", (again,)) == [
            (first, replacement)
        ]
        dao.validate_call_set(world.interp2, again)
        assert (state(su, first), state(su, again)) == ("superseded", "validated")
        assert _drawn(dao, world) == [again], "exactly one call set is drawn for the run"

    def test_when_the_predecessor_is_no_longer_validated_the_link_is_not_carried(self, su, dao, world):
        first = validated(su, dao, world, data=vcf_text(world.run.sample, n=1))
        replacement = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2), supersedes=first)
        dao.reject_call_set(world.interp1, replacement, "bad")
        dao.retract_call_set(world.appr1, first, "defect")
        again = dao.resubmit_call_set(world.lab1, replacement, "again")
        assert row(su, "SELECT supersedes_id, resubmission_of FROM call_sets WHERE id = %s", (again,)) == [
            (None, replacement)
        ]

    def test_a_rejected_replacement_does_not_block_the_next_one_but_a_live_one_does(self, su, dao, world):
        first = validated(su, dao, world, data=vcf_text(world.run.sample, n=1))
        one = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2), supersedes=first)
        dao.reject_call_set(world.interp1, one, "bad")
        two = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=3), supersedes=first)
        with pytest.raises(CallSetConflictError, match="replacement in progress or in force"):
            submit(dao, world, world.run, data=vcf_text(world.run.sample, n=4), supersedes=first)
        assert state(su, two) == "pending_validation"


class TestOneActiveValidatedCallSet:
    def test_a_second_validated_call_set_for_the_same_run_class_and_assembly_is_refused(self, su, dao, world):
        first = validated(su, dao, world, data=vcf_text(world.run.sample, n=1))
        second = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2))  # not a replacement
        with pytest.raises(CallSetConflictError, match="already validated.*replace it explicitly"):
            dao.validate_call_set(world.interp2, second)
        assert state(su, second) == "pending_validation" and events(su, second) == ["submitted"]
        assert _drawn(dao, world) == [first]
        dao.retract_call_set(
            world.appr1, first, "defect"
        )  # twin: once the first is out of force the second may be validated
        dao.validate_call_set(world.interp2, second)
        assert _drawn(dao, world) == [second]

    def test_another_class_another_run_are_not_blocked(self, su, dao, world):
        approve_caller(dao, world, record_class="cnv")
        validated(su, dao, world, data=vcf_text(world.run.sample, n=1))
        cnv = submit(dao, world, world.run, data=_cnv_text(world.run.sample), record_class="cnv")
        dao.validate_call_set(world.interp2, cnv)
        other = new_run(su, dao, world, patient=world.run.patient)
        elsewhere = submit(dao, world, other, data=vcf_text(other.sample, n=2))
        dao.validate_call_set(world.interp2, elsewhere)
        assert len(_drawn(dao, world)) == 3

    def test_the_database_refuses_it_for_a_direct_validated_event_too(self, su, dao, world, app_conn):
        validated(su, dao, world, data=vcf_text(world.run.sample, n=1))
        second = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2))
        approval = row(
            su, "SELECT id FROM caller_approvals WHERE org_id = %s AND event_type = 'approved'", (world.org,)
        )[0][0]
        with app_conn.cursor() as cur:
            cur.execute("SELECT set_config('clinical.actor_id', %s, true)", (str(world.interp2.user_id),))
            with pytest.raises(psycopg.errors.RaiseException, match="already validated"):
                cur.execute(
                    "INSERT INTO call_set_events (org_id, call_set_id, event_type, actor_id, actor_role, occurred_at, checks, caller_approval_id) "
                    "VALUES (%s, %s, 'validated', %s, 'Interpreter', now(), %s, %s)",
                    (
                        world.org,
                        second,
                        world.interp2.user_id,
                        Jsonb({"sample_identity": True, "assembly": True, "caller": True}),
                        approval,
                    ),
                )
        app_conn.rollback()

    def test_two_validations_racing_for_one_run_leave_exactly_one(self, su, dao, world):
        a = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=1))
        b = submit(dao, world, world.run, data=vcf_text(world.run.sample, n=2))
        connection, local = open_transaction_dao()
        v_conn, v_dao = _fresh_dao()
        try:
            local.validate_call_set(world.interp1, a)  # validated, NOT committed: holds the run's lock
            thread, box = run_in_thread(lambda: v_dao.validate_call_set(world.interp2, b))
            wait_until_blocked(su, v_conn.info.backend_pid)
            assert thread.is_alive()
            connection.commit()
            thread.join(timeout=30)
        finally:
            connection.close()
            v_conn.close()
        assert isinstance(box.get("error"), CallSetConflictError), box
        assert (state(su, a), state(su, b)) == ("validated", "pending_validation")


# ═══ H-2: a withdrawn consent stays withdrawn ═════════════════════════════════


class TestWithdrawnConsentCannotBeRevived:
    def _scene(self, su, dao, world):
        cs = validated(su, dao, world)
        patient = world.run.patient
        older = row(su, "SELECT consent_id FROM consents WHERE org_id = %s", (world.org,))[0][0]
        newer = dao.record_consent(world.reader, patient, "testing")
        su.execute(
            "UPDATE consents SET withdrawn_at = now(), withdrawn_by = recorded_by WHERE consent_id = %s", (newer,)
        )
        su.commit()
        assert _drawn(dao, world) == []
        return cs, patient, older, newer

    def test_superseding_the_withdrawn_consent_with_an_older_active_one_is_refused(self, su, dao, world, app_conn):
        _cs, _patient, older, newer = self._scene(su, dao, world)
        with app_conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException, match="callset_identity"):
                cur.execute("UPDATE consents SET superseded_by = %s WHERE consent_id = %s", (older, newer))
        app_conn.rollback()
        assert _drawn(dao, world) == []

    def test_a_target_of_another_scope_or_patient_is_refused_too(self, su, dao, world, app_conn):
        _cs, patient, _older, newer = self._scene(su, dao, world)
        elsewhere = dao.record_consent(world.reader, patient, "some-other-scope")
        with app_conn.cursor() as cur:
            with pytest.raises(psycopg.errors.RaiseException, match="callset_identity"):
                cur.execute("UPDATE consents SET superseded_by = %s WHERE consent_id = %s", (elsewhere, newer))
        app_conn.rollback()

    def test_even_if_the_trigger_is_bypassed_at_owner_level_the_map_ignores_supersession_pointers(self, su, dao, world):
        _cs, patient, older, newer = self._scene(su, dao, world)
        tamper(
            su,
            "UPDATE consents SET superseded_by = %s WHERE consent_id = %s",
            (older, newer),
            table="consents",
            trigger="trg_callset_identity_consents",
        )
        evidence = dao.patient_call_set_evidence(world.reader, patient)
        assert evidence["eligible"] == [] and evidence["excluded"] == {"consent_withdrawn": 1}

    def test_a_genuinely_newer_consent_is_a_new_consent_and_does_restore_the_evidence(self, su, dao, world):
        cs, patient, _older, newer = self._scene(su, dao, world)
        dao.record_consent(
            world.reader, patient, "testing", supersedes_id=newer
        )  # re-consent: recorded LATER, NULL -> value
        assert _drawn(dao, world) == [cs], (
            "documented: re-consent restores; it is a new consent, not the old one revived"
        )


# ═══ M-1: reviewers see lineage ════════════════════════════════════════════════


class TestReviewersSeeLineage:
    def test_a_resubmission_shows_what_it_resubmits_and_who_rejected_it_and_why(self, su, dao, world):
        old = submit(dao, world, world.run)
        dao.reject_call_set(world.interp1, old, "wrong sample ordering")
        new = dao.resubmit_call_set(world.lab1, old, "sheet corrected")
        for reviewer in (world.interp2, world.appr1):
            review = dao.get_call_set_for_review(reviewer, new)
            assert review["resubmission_of"] == old and review["document"] is not None
            ids = [x["id"] for x in review["lineage"]]
            assert ids == [old, new]
            assert [x["is_this_call_set"] for x in review["lineage"]] == [False, True]
            (decision,) = review["lineage"][0]["prior_decisions"]
            assert (decision["event_type"], decision["actor_role"], decision["reason"]) == (
                "rejected",
                "Interpreter",
                "wrong sample ordering",
            )
            assert review["lineage"][1]["resubmission_of"] == old and review["lineage"][1]["prior_decisions"] == []
            status = dao.get_call_set_status(reviewer, new)
            assert [x["id"] for x in status["lineage"]] == [old, new] and status["resubmission_of"] == old

    def test_lineage_includes_the_same_file_submitted_under_another_class_and_a_withdrawal(self, su, dao, world):
        first = submit(dao, world, world.run)
        dao.withdraw_call_set(world.lab1, first, "wrong file")
        second = dao.resubmit_call_set(world.lab1, first, "it was the right file")
        lineage = dao.get_call_set_for_review(world.interp1, second)["lineage"]
        assert [(x["id"], x["state"]) for x in lineage] == [(first, "withdrawn"), (second, "pending_validation")]
        assert lineage[0]["prior_decisions"][0]["event_type"] == "withdrawn"

    def test_a_technician_sees_only_the_link_of_their_own_submission_never_the_history(self, su, dao, world):
        old = submit(dao, world, world.run)
        dao.reject_call_set(world.interp1, old, "wrong sample ordering")
        new = dao.resubmit_call_set(world.lab1, old, "sheet corrected")
        status = dao.get_call_set_status(world.lab1, new)
        assert status["resubmission_of"] == old and "lineage" not in status
        assert [x["resubmission_of"] for x in dao.list_call_sets_for_run(world.lab1, world.run.id)] == [None, old]
        with pytest.raises(Exception, match="requires one of the roles"):
            dao.get_call_set_for_review(world.lab1, new)
        with pytest.raises(NotFoundError):
            dao.get_call_set_status(world.lab2, new)

    def test_another_organisation_sees_nothing(self, su, dao, world):
        old = submit(dao, world, world.run)
        other = build_org(su, dao, "z")
        with pytest.raises(NotFoundError):
            dao.get_call_set_for_review(other.interp1, old)


# ═══ M-4: withdrawn consent withholds the document from reviewers ═══════════════


class TestWithdrawnConsentWithholdsTheDocument:
    def test_reviewers_get_status_but_no_document_and_nothing_is_erased(self, su, dao, world):
        cs = validated(su, dao, world)
        assert dao.get_call_set_for_review(world.interp1, cs)["document"] is not None
        _withdraw_consent(su, world.run)
        for reviewer in (world.interp1, world.appr1):
            review = dao.get_call_set_for_review(reviewer, cs)
            assert review["document"] is None and review["document_withheld"] == "consent_withdrawn"
            assert review["state"] == "validated" and [e["event_type"] for e in review["events"]] == [
                "submitted",
                "validated",
            ]
            assert review["lineage"][0]["id"] == cs
        assert row(
            su,
            "SELECT source_vcf IS NOT NULL, parsed_document IS NOT NULL, tombstoned_at IS NULL FROM call_sets WHERE id = %s",
            (cs,),
        ) == [(True, True, True)], "hide-only: the record is retained, not erased"
        dao.record_consent(world.reader, world.run.patient, "testing")
        # Policy C (M3): the consent the ORDER was placed under is the one withdrawn here, and a later consent does not
        # revive it, so the document stays withheld. (The restoring case is in test_callset_m3_policy_c.py.)
        again = dao.get_call_set_for_review(world.interp1, cs)
        assert again["document"] is None and again["document_withheld"] == "consent_withdrawn"

    def test_a_pending_call_set_is_withheld_too_and_validation_is_refused(self, su, dao, world):
        cs = submit(dao, world, world.run)
        _withdraw_consent(su, world.run)
        assert dao.get_call_set_for_review(world.interp1, cs)["document"] is None
        with pytest.raises(CallSetConflictError, match="consent"):
            dao.validate_call_set(world.interp1, cs)


# ═══ M-5: held back is not the same as never attached ═══════════════════════════


class TestHeldBackEvidenceIsReported:
    def _evidence(self, dao, w):
        return dao.patient_call_set_evidence(w.reader, w.run.patient)

    def test_never_attached_is_empty_in_both_parts(self, su, dao, world):
        assert self._evidence(dao, world) == {"eligible": [], "excluded": {}}
        submit(dao, world, world.run)  # pending: not validated, so neither drawn nor "held back"
        assert self._evidence(dao, world) == {"eligible": [], "excluded": {}}

    @pytest.mark.parametrize(
        "reason, make_it",
        [
            ("consent_withdrawn", lambda su, w: _withdraw_all_consents(su, w)),
            ("sample_qc_not_passed", lambda su, w: _qc(su, w, w.run, "failed")),
            ("order_cancelled", lambda su, w: _order_state(su, w.run, "cancelled")),
            (
                "sample_identity_changed",
                lambda su, w: tamper(
                    su,
                    "UPDATE sequencing_runs SET sample_id = %s WHERE id = %s",
                    (
                        w.other_sample,
                        w.run.id,
                    ),
                    table="sequencing_runs",
                    trigger="trg_callset_identity_runs",
                ),
            ),
            (
                "assembly_mismatch",
                lambda su, w: tamper(
                    su,
                    "UPDATE tests SET assembly = 'GRCh37' WHERE test_id = %s",
                    (w.tests["GRCh38"],),
                    table="tests",
                    trigger="trg_callset_identity_tests",
                ),
            ),
            ("small_variant_build_mismatch", lambda su, w: _small_variant_analysis(su, w, w.run, "GRCh37")),
            (
                "integrity_failure",
                lambda su, w: tamper(
                    su,
                    "UPDATE call_sets SET parsed_document = jsonb_set(parsed_document, '{records,0,status}', '\"uncertain\"') WHERE sequencing_run_id = %s",
                    (w.run.id,),
                ),
            ),
        ],
    )
    def test_each_exclusion_is_counted_under_its_own_reason(self, su, dao, world, reason, make_it):
        DataAccess._CALLSET_VERIFIED_CACHE.clear()
        world.other_sample = new_run(su, dao, world, patient=world.run.patient).sample  # a repoint target, SAME patient
        validated(su, dao, world)
        assert len(self._evidence(dao, world)["eligible"]) == 1
        make_it(su, world)
        evidence = self._evidence(dao, world)
        assert evidence["eligible"] == [] and evidence["excluded"] == {reason: 1}
        assert reason in DataAccess.CALLSET_EXCLUSION_REASONS

    def test_the_real_source_reports_what_it_holds_back_in_the_same_single_read(self, su, dao, world):
        from clinical.genome_endpoints import DaoCallSetSource, _call_sets_note

        validated(su, dao, world)
        _withdraw_consent(su, world.run)
        source = DaoCallSetSource(dao)
        assert list(source.call_sets_for(world.reader, world.run.patient)) == []
        held = source.excluded_for(world.reader, world.run.patient)
        assert held == {"consent_withdrawn": 1}
        assert "NOT shown" in _call_sets_note([], held) and "is attached to this patient" not in _call_sets_note(
            [], held
        )
        assert "is attached to this patient" in _call_sets_note([], {})

    def test_other_patients_and_other_organisations_do_not_leak_into_the_counts(self, su, dao, world):
        validated(su, dao, world)
        _withdraw_consent(su, world.run)
        stranger = new_run(su, dao, world)
        assert dao.patient_call_set_evidence(world.reader, stranger.patient) == {"eligible": [], "excluded": {}}
        other = build_org(su, dao, "y")
        with pytest.raises(NotFoundError):
            dao.patient_call_set_evidence(other.reader, world.run.patient)


# ═══ M-6: identical bytes are identical bytes, whatever class they are declared as ═══


class TestNoCrossClassDuplicates:
    def test_the_same_bytes_cannot_be_accepted_as_both_cnv_and_sv_for_one_run(self, su, dao, world):
        approve_caller(dao, world, record_class="cnv")
        data = vcf_text(world.run.sample)
        submit(dao, world, world.run, data=data)
        with pytest.raises(CallSetConflictError, match="conflicts with an existing"):
            submit(dao, world, world.run, data=data, record_class="cnv")
        assert row(su, "SELECT count(*) FROM call_sets") == [(1,)]

    def test_it_is_still_refused_after_the_first_is_rejected_and_resubmission_keeps_the_class(self, su, dao, world):
        approve_caller(dao, world, record_class="cnv")
        data = vcf_text(world.run.sample)
        first = submit(dao, world, world.run, data=data)
        dao.reject_call_set(world.interp1, first, "bad")
        with pytest.raises(CallSetConflictError):
            submit(dao, world, world.run, data=data, record_class="cnv")
        again = dao.resubmit_call_set(world.lab1, first, "retry")
        assert row(su, "SELECT record_class FROM call_sets WHERE id = %s", (again,)) == [("sv",)]

    def test_the_database_refuses_it_for_direct_inserts_and_a_resubmission_cannot_change_class(
        self, su, dao, world, app_conn
    ):
        data = vcf_text(world.run.sample)
        first = submit(dao, world, world.run, data=data)
        doc = parse_structural_vcf(data.decode(), assembly="GRCh38", record_class="cnv").to_document()

        def raw(resubmission_of=None):
            with app_conn.cursor() as cur:
                cur.execute("SELECT set_config('clinical.actor_id', %s, true)", (str(world.lab1.user_id),))
                cur.execute(
                    "INSERT INTO call_sets (org_id,id,sequencing_run_id,record_class,assembly,caller_name,caller_version,"
                    "caller_approved_at_submission,vcf_sample_name,source_filename,source_vcf,source_content_hash,parser_version,"
                    "parsed_document,resubmission_of,created_at,created_by,retention_days) "
                    "VALUES (%s,%s,%s,'cnv','GRCh38','caller-x','1.0',false,%s,'x.vcf',%s,%s,%s,%s,%s,now(),%s,1825)",
                    (
                        world.org,
                        uuid.uuid4(),
                        world.run.id,
                        world.run.sample,
                        data,
                        hashlib.sha256(data).hexdigest(),
                        CALLSET_PARSER_VERSION,
                        Jsonb(doc),
                        resubmission_of,
                        world.lab1.user_id,
                    ),
                )

        with pytest.raises(psycopg.errors.RaiseException, match="already live"):
            raw()
        app_conn.rollback()
        dao.reject_call_set(world.interp1, first, "bad")
        with pytest.raises(psycopg.errors.RaiseException, match="only as a resubmission"):
            raw()
        app_conn.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="must name a rejected or withdrawn"):
            raw(resubmission_of=first)  # same file, but the prior is an SV call set and this one says CNV
        app_conn.rollback()


# ═══ L-1 .. L-6 ═══════════════════════════════════════════════════════════════


class _ManualClock:
    def __init__(self):
        self.t = datetime.datetime(2026, 11, 11, 12, 0, tzinfo=datetime.timezone.utc)

    def now(self):
        return self.t


class TestSmallerHardening:
    def test_l1_a_nul_character_is_a_clean_input_error_and_nothing_is_stored(self, su, dao, world):
        data = vcf_text(world.run.sample).replace(b"sv0", b"sv\x000")
        with pytest.raises(CallSetInputError, match="NUL"):
            submit(dao, world, world.run, data=data)
        assert row(su, "SELECT count(*) FROM call_sets") == [(0,)]

    def test_l2_an_integrity_incident_is_audited_once_an_hour_not_on_every_read(self, su, dao, world, app_conn):
        DataAccess._CALLSET_VERIFIED_CACHE.clear()
        cs = validated(su, dao, world)
        tamper(
            su,
            "UPDATE call_sets SET parsed_document = jsonb_set(parsed_document, '{records,0,status}', '\"uncertain\"') WHERE id = %s",
            (cs,),
        )
        clock = _ManualClock()
        timed = DataAccess(app_conn, clock=clock, password_hasher=BcryptHasher(rounds=4))
        for _ in range(3):
            timed.list_patient_validated_call_sets(world.reader, world.run.patient)
        assert len(audits(su, "call_set_integrity_failure")) == 1
        clock.t += datetime.timedelta(hours=2)
        timed.list_patient_validated_call_sets(world.reader, world.run.patient)
        assert len(audits(su, "call_set_integrity_failure")) == 2, "a persistent fault is still re-reported, hourly"

    def test_l3_a_dao_resubmission_and_a_direct_insert_for_the_same_prior_no_longer_deadlock(
        self, su, dao, world, app_conn
    ):
        data = vcf_text(world.run.sample)
        prior = submit(dao, world, world.run, data=data)
        dao.withdraw_call_set(world.lab1, prior, "x")
        reached, go, outcome = threading.Event(), threading.Event(), {}
        b_conn, b_dao = _fresh_dao()
        real = DataAccess._callset_small_variant_assemblies

        def paused(self, session, run_ids):
            reached.set()
            go.wait(timeout=60)
            return real(self, session, run_ids)

        def run_b():
            try:
                outcome["b"] = b_dao.resubmit_call_set(world.lab1, prior, "again")
            except BaseException as exc:  # noqa: BLE001
                outcome["b_error"] = exc

        a_conn, _ = _fresh_dao()
        doc = parse_structural_vcf(data.decode(), assembly="GRCh38", record_class="sv").to_document()

        def run_a():
            try:
                with a_conn.cursor() as cur:
                    cur.execute("SELECT set_config('clinical.actor_id', %s, true)", (str(world.lab1.user_id),))
                    cur.execute(
                        "INSERT INTO call_sets (org_id,id,sequencing_run_id,record_class,assembly,caller_name,caller_version,"
                        "caller_approved_at_submission,vcf_sample_name,source_filename,source_vcf,source_content_hash,parser_version,"
                        "parsed_document,resubmission_of,created_at,created_by,retention_days) "
                        "VALUES (%s,%s,%s,'sv','GRCh38','caller-x','1.0',false,%s,'x.vcf',%s,%s,%s,%s,%s,now(),%s,1825)",
                        (
                            world.org,
                            uuid.uuid4(),
                            world.run.id,
                            world.run.sample,
                            data,
                            hashlib.sha256(data).hexdigest(),
                            CALLSET_PARSER_VERSION,
                            Jsonb(doc),
                            prior,
                            world.lab1.user_id,
                        ),
                    )
                a_conn.commit()
                outcome["a"] = "inserted"
            except BaseException as exc:  # noqa: BLE001
                outcome["a_error"] = exc

        DataAccess._callset_small_variant_assemblies = paused
        try:
            tb = threading.Thread(target=run_b)
            tb.start()
            assert reached.wait(30), "the DAO resubmission never reached its pause"  # B holds the prior's call-set lock
            ta = threading.Thread(target=run_a)
            ta.start()
            wait_until_blocked(su, a_conn.info.backend_pid)  # A waits for the PRIOR's lock; it holds no content lock
            go.set()
            tb.join(60)
            ta.join(60)
        finally:
            DataAccess._callset_small_variant_assemblies = real
            b_conn.close()
            a_conn.close()
        assert "b" in outcome, outcome  # the DAO resubmission completed
        error = outcome.get("a_error")
        assert isinstance(error, psycopg.errors.RaiseException) and "already live" in str(error), outcome
        assert not isinstance(error, psycopg.errors.DeadlockDetected)

    def test_l3_a_deadlock_that_does_get_through_is_a_retryable_conflict_not_a_raw_error(self, su, dao, world):
        class FakeDeadlock(Exception):
            sqlstate = "40P01"

        def boom(sql, params):
            raise FakeDeadlock("deadlock detected")

        dao._execute = boom
        try:
            with pytest.raises(CallSetConflictError, match="retry"):
                dao._callset_execute("SELECT 1", ())
        finally:
            del dao._execute

    def test_l4_latest_means_the_databases_order_not_an_application_timestamp(self, su, dao, world):
        data = vcf_text(world.run.sample)
        other = same_sample_run(su, world, world.run)
        x = submit(dao, world, world.run, data=data)
        dao.reject_call_set(world.interp1, x, "bad")
        y = dao.resubmit_call_set(world.lab1, x, "again")
        dao.validate_call_set(world.interp1, y)
        dao.retract_call_set(world.appr1, y, "placed on the wrong run", wrong_run_placement=True)
        tamper(
            su, "UPDATE call_sets SET created_at = created_at - interval '10 years' WHERE id = %s", (y,)
        )  # backdated
        moved = submit(dao, world, other, data=data)
        assert state(su, moved) == "pending_validation", "y is the latest by sequence however its created_at reads"

    def test_l5_the_call_set_audit_vocabulary_is_exactly_this(self):
        tree = ast.parse(pathlib.Path(da_module.__file__).read_text(encoding="utf-8"))
        actions = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "auditable":
                for kw in node.keywords:
                    if (
                        kw.arg == "action"
                        and isinstance(kw.value, ast.Constant)
                        and str(kw.value.value).startswith(("call_set", "caller_"))
                    ):
                        actions.add(kw.value.value)
            if isinstance(node, ast.Constant) and node.value == "call_set_integrity_failure":
                actions.add(node.value)
        assert actions == {
            "caller_proposed",
            "caller_approved",
            "caller_retired",
            "caller_versions_read",
            "call_set_submitted",
            "call_set_resubmitted",  # added in wave 3: the one new verb (see docs, section 7)
            "call_set_validated",
            "call_set_rejected",
            "call_set_withdrawn",
            "call_set_retraction_requested",
            "call_set_retraction_declined",
            "call_set_retracted",
            "call_set_read",
            "call_set_integrity_failure",
        }

    def test_l6_the_cross_run_refusal_does_not_confirm_the_bytes_exist_elsewhere(self, su, dao, world):
        data = vcf_text(world.run.sample)
        other = same_sample_run(su, world, world.run)
        submit(dao, world, world.run, data=data)
        with pytest.raises(CallSetConflictError) as refused:
            submit(dao, world, other, data=data, session=world.lab2)
        assert str(refused.value) == DataAccess.CALLSET_CONFLICT_REFUSAL and "run" not in str(refused.value).lower()
