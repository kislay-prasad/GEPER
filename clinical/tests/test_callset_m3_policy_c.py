# ruff: noqa: F811
"""
M3, Policy C (approved): a call set is eligible only if BOTH hold now --

  (1) the consent the ORDER was placed under (`orders.consent_id`) exists, belongs to the order's patient and organisation,
      is for the order's scope and is not withdrawn; and
  (2) the patient's LATEST consent for that scope is not withdrawn.

Either failing holds the call set back (`consent_withdrawn`, no new reason or status); a newer consent never revives an
order whose own consent was withdrawn. Run against the REAL schema as the REAL `clinical_app` role. Each check has a
mutant (see the wave report): removing (1) or (2) fails the tests named in `ORDER_CHECK` / `LATEST_CHECK`.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from datetime import date

import pytest

from clinical.data_access import CallSetConflictError, DataAccess
from clinical.tests.test_callset_dao import (  # noqa: F401  (fixtures and helpers re-used)
    _fresh_dao,
    app_conn,
    approve_caller,
    build_org,
    dao,
    new_run,
    row,
    state,
    su,
    submit,
    tamper,
    validated,
    vcf_text,
    world,
)

psycopg = pytest.importorskip("psycopg")

HELD = {"eligible": [], "excluded": {"consent_withdrawn": 1}}


@pytest.fixture(autouse=True)
def _clean_cache():
    DataAccess._CALLSET_VERIFIED_CACHE.clear()
    yield
    DataAccess._CALLSET_VERIFIED_CACHE.clear()


def _evidence(dao, w):
    return dao.patient_call_set_evidence(w.reader, w.run.patient)


def _order_consent(su, run):
    return row(
        su,
        "SELECT consent_id FROM orders WHERE order_id = (SELECT order_id FROM samples WHERE sample_id = %s)",
        (run.sample,),
    )[0][0]


def _latest_consent(dao, w, scope="testing"):
    """A NEWER consent for the same patient and scope (so it is the 'latest applicable' one)."""
    return dao.record_consent(w.reader, w.run.patient, scope)


@contextmanager
def _order_points_at(su, run, target):
    """Owner-level: point the order at ANY consent id (the foreign key and the identity guard are lifted, then restored)."""
    original = _order_consent(su, run)
    where = "order_id = (SELECT order_id FROM samples WHERE sample_id = %s)"
    su.execute("ALTER TABLE public.orders DROP CONSTRAINT orders_consent_fk")
    su.commit()
    try:
        tamper(
            su,
            f"UPDATE orders SET consent_id = %s WHERE {where}",
            (target, run.sample),
            table="orders",
            trigger="trg_callset_identity_orders",
        )
        yield original
    finally:
        tamper(
            su,
            f"UPDATE orders SET consent_id = %s WHERE {where}",
            (original, run.sample),
            table="orders",
            trigger="trg_callset_identity_orders",
        )
        su.execute(
            "ALTER TABLE public.orders ADD CONSTRAINT orders_consent_fk "
            "FOREIGN KEY (consent_id, org_id) REFERENCES consents (consent_id, org_id)"
        )
        su.commit()


# ═══ the four combinations (tests 1-4, 10) ════════════════════════════════════


class TestBothConsentsMustBeActive:
    def test_both_active_is_eligible(self, su, dao, world):  # 1
        cs = validated(su, dao, world)
        _latest_consent(dao, world)  # the latest is a DIFFERENT, active consent from the order's
        assert [e["call_set_id"] for e in _evidence(dao, world)["eligible"]] == [cs]
        assert dao.get_call_set_for_review(world.interp1, cs)["document"] is not None

    def test_order_consent_withdrawn_latest_active_is_held_back(self, su, dao, world):  # 2   [ORDER_CHECK]
        cs = validated(su, dao, world)
        dao.withdraw_consent(world.reader, _order_consent(su, world.run))
        _latest_consent(dao, world)
        assert _evidence(dao, world) == HELD
        review = dao.get_call_set_for_review(world.interp1, cs)
        assert review["document"] is None and review["document_withheld"] == "consent_withdrawn"

    def test_order_consent_active_latest_withdrawn_is_held_back(self, su, dao, world):  # 3   [LATEST_CHECK]
        cs = validated(su, dao, world)
        dao.withdraw_consent(world.reader, _latest_consent(dao, world))
        assert _evidence(dao, world) == HELD
        assert dao.get_call_set_for_review(world.interp1, cs)["document_withheld"] == "consent_withdrawn"

    def test_both_withdrawn_is_held_back(self, su, dao, world):  # 4
        validated(su, dao, world)
        dao.withdraw_consent(world.reader, _order_consent(su, world.run))
        dao.withdraw_consent(world.reader, _latest_consent(dao, world))
        assert _evidence(dao, world) == HELD

    def test_a_newer_active_consent_cannot_revive_a_withdrawn_order_consent(self, su, dao, world):  # 10   [ORDER_CHECK]
        cs = validated(su, dao, world)
        dao.withdraw_consent(world.reader, _order_consent(su, world.run))
        assert _evidence(dao, world) == HELD
        for _ in range(3):
            _latest_consent(dao, world)
            assert _evidence(dao, world) == HELD, "re-consent is a new consent, not the order's"
        assert state(su, cs) == "validated", "hide-only: nothing is erased or retracted"
        assert row(
            su, "SELECT source_vcf IS NOT NULL, parsed_document IS NOT NULL FROM call_sets WHERE id = %s", (cs,)
        ) == [(True, True)]

    def test_withdrawing_the_latest_then_recording_a_newer_one_restores_when_the_order_consent_is_active(
        self, su, dao, world
    ):
        """The one restoring path that remains: the order's own consent was never withdrawn."""
        cs = validated(su, dao, world)
        dao.withdraw_consent(world.reader, _latest_consent(dao, world))
        assert _evidence(dao, world) == HELD
        _latest_consent(dao, world)
        assert [e["call_set_id"] for e in _evidence(dao, world)["eligible"]] == [cs]


# ═══ missing / inconsistent order consent fails closed (tests 5-8) ═════════════


class TestAnInconsistentOrderConsentFailsClosed:
    def test_a_missing_order_consent_fails_closed(self, su, dao, world):  # 5   [ORDER_CHECK]
        cs = validated(su, dao, world)
        with _order_points_at(su, world.run, uuid.uuid4()):  # a consent id that does not exist
            assert _evidence(dao, world) == HELD
            assert dao.get_call_set_for_review(world.interp1, cs)["document_withheld"] == "consent_withdrawn"
        assert [e["call_set_id"] for e in _evidence(dao, world)["eligible"]] == [cs], "twin: restored"

    def test_a_consent_of_another_patient_fails_closed(self, su, dao, world):  # 6   [ORDER_CHECK]
        validated(su, dao, world)
        stranger = dao.create_patient(world.reader, "Other", date(1991, 2, 2), "F")
        theirs = dao.record_consent(world.reader, stranger, "testing")
        with _order_points_at(su, world.run, theirs):
            assert _evidence(dao, world) == HELD
        assert len(_evidence(dao, world)["eligible"]) == 1

    def test_a_consent_of_another_organisation_fails_closed(self, su, dao, world):  # 7   [ORDER_CHECK]
        validated(su, dao, world)
        other = build_org(su, dao, "z")
        their_patient = dao.create_patient(other.reader, "Elsewhere", date(1992, 3, 3), "M")
        theirs = dao.record_consent(other.reader, their_patient, "testing")
        with _order_points_at(su, world.run, theirs):
            assert _evidence(dao, world) == HELD
        assert len(_evidence(dao, world)["eligible"]) == 1

    def test_a_consent_of_the_wrong_scope_fails_closed(self, su, dao, world):  # 8   [ORDER_CHECK]
        validated(su, dao, world)
        wrong = dao.record_consent(world.reader, world.run.patient, "some-other-scope")
        with _order_points_at(su, world.run, wrong):
            assert _evidence(dao, world) == HELD
        assert len(_evidence(dao, world)["eligible"]) == 1

    def test_no_consent_at_all_for_the_scope_fails_closed(self, su, dao, world):
        validated(su, dao, world)
        # the order says a scope no consent covers: the latest-consent half is NULL and must read as "no"
        tamper(
            su,
            "UPDATE orders SET required_scope = 'uncovered' WHERE order_id = (SELECT order_id FROM samples WHERE sample_id = %s)",
            (world.run.sample,),
            table="orders",
            trigger="trg_callset_identity_orders",
        )
        assert _evidence(dao, world) == HELD


# ═══ supersession cannot revive (test 9) ═══════════════════════════════════════


class TestSupersessionCannotReviveAWithdrawnConsent:
    def test_pointer_manipulation_does_not_restore_authorisation(self, su, dao, world, app_conn):  # 9
        validated(su, dao, world)
        older = _order_consent(su, world.run)
        newer = _latest_consent(dao, world)
        dao.withdraw_consent(world.reader, older)
        assert _evidence(dao, world) == HELD
        # as the application role: re-pointing, un-withdrawing and re-timing are all refused or ineffective
        for sql, params in (
            ("UPDATE consents SET superseded_by = %s WHERE consent_id = %s", (newer, older)),
            ("UPDATE consents SET superseded_by = %s WHERE consent_id = %s", (older, newer)),
            ("UPDATE consents SET withdrawn_at = NULL, withdrawn_by = NULL WHERE consent_id = %s", (older,)),
            ("UPDATE consents SET recorded_at = recorded_at + interval '1 day' WHERE consent_id = %s", (older,)),
        ):
            with app_conn.cursor() as cur:
                try:
                    cur.execute(sql, params)
                except psycopg.Error:
                    pass
            app_conn.rollback()
            assert _evidence(dao, world) == HELD, sql
        # and with the guard bypassed at owner level the map still ignores the pointers
        tamper(
            su,
            "UPDATE consents SET superseded_by = %s WHERE consent_id = %s",
            (newer, older),
            table="consents",
            trigger="trg_callset_identity_consents",
        )
        tamper(
            su,
            "UPDATE consents SET superseded_by = %s WHERE consent_id = %s",
            (older, newer),
            table="consents",
            trigger="trg_callset_identity_consents",
        )
        assert _evidence(dao, world) == HELD


# ═══ the same rule at submit, validate, resubmit (tests 11, 12) ═════════════════


@pytest.fixture(params=["order_withdrawn_latest_active", "order_active_latest_withdrawn", "both_withdrawn"])
def broken(request, su, dao):
    """Make the consent state `request.param` for a world; returns a function that does it (so a test chooses WHEN)."""

    def apply(w):
        if request.param == "order_withdrawn_latest_active":
            dao.withdraw_consent(w.reader, _order_consent(su, w.run))
            _latest_consent(dao, w)
        elif request.param == "order_active_latest_withdrawn":
            dao.withdraw_consent(w.reader, _latest_consent(dao, w))
        else:
            dao.withdraw_consent(w.reader, _order_consent(su, w.run))
            dao.withdraw_consent(w.reader, _latest_consent(dao, w))

    return apply


class TestTheSameRuleAtEveryEntryPoint:
    def test_submission_is_refused(self, su, dao, world, broken):  # 11
        broken(world)
        with pytest.raises(CallSetConflictError, match="cannot accept this call set"):  # generic by design
            submit(dao, world, world.run)
        assert row(su, "SELECT count(*) FROM call_sets WHERE org_id = %s", (world.org,)) == [(0,)]

    def test_validation_is_refused(self, su, dao, world, broken):  # 11
        cs = submit(dao, world, world.run)
        broken(world)
        with pytest.raises(CallSetConflictError, match="consent"):
            dao.validate_call_set(world.interp1, cs)
        assert state(su, cs) == "pending_validation"

    def test_resubmission_is_refused(self, su, dao, world, broken):  # 12
        old = submit(dao, world, world.run)
        dao.reject_call_set(world.interp1, old, "sheet wrong")
        broken(world)
        with pytest.raises(CallSetConflictError, match="cannot accept this call set|consent"):
            dao.resubmit_call_set(world.lab1, old, "sheet corrected")
        assert row(su, "SELECT count(*) FROM call_sets WHERE org_id = %s", (world.org,)) == [(1,)]

    def test_a_replacement_submission_is_refused(self, su, dao, world, broken):
        first = validated(su, dao, world)
        broken(world)
        with pytest.raises(CallSetConflictError, match="cannot accept this call set|consent"):
            submit(dao, world, world.run, supersedes=first, data=vcf_text(world.run.sample, n=2))

    def test_the_reviewer_document_is_withheld_for_every_reviewer_role(self, su, dao, world, broken):
        cs = validated(su, dao, world)
        broken(world)
        for reviewer in (world.interp1, world.appr1):
            review = dao.get_call_set_for_review(reviewer, cs)
            assert review["document"] is None and review["document_withheld"] == "consent_withdrawn"
            assert review["state"] == "validated" and review["lineage"][0]["id"] == cs  # metadata and lineage remain


# ═══ map reads and the warm cache (tests 13, 14) ═══════════════════════════════


class TestMapReadsAndTheCache:
    def test_every_map_read_path_agrees(self, su, dao, world, broken):  # 13
        from clinical.genome_endpoints import DaoCallSetSource

        validated(su, dao, world)
        broken(world)
        source = DaoCallSetSource(dao)
        assert list(source.call_sets_for(world.reader, world.run.patient)) == []
        assert source.excluded_for(world.reader, world.run.patient) == {"consent_withdrawn": 1}
        assert dao.list_patient_validated_call_sets(world.reader, world.run.patient) == []
        assert _evidence(dao, world) == HELD

    def test_a_warm_cache_cannot_bypass_a_withdrawal_of_either_consent(self, su, dao, world, broken):  # 14
        cs = validated(su, dao, world)
        assert [e["call_set_id"] for e in _evidence(dao, world)["eligible"]] == [cs]
        assert len(DataAccess._CALLSET_VERIFIED_CACHE) == 1, "warm"
        broken(world)
        assert len(DataAccess._CALLSET_VERIFIED_CACHE) == 1, "still warm: the cache holds verified bytes, not consent"
        assert _evidence(dao, world) == HELD
        assert dao.list_patient_validated_call_sets(world.reader, world.run.patient) == []


# ═══ concurrency (test 15) ═════════════════════════════════════════════════════


class TestWithdrawalAndMapAccessUnderTheTransactionModel:
    def test_an_uncommitted_withdrawal_is_not_seen_and_a_committed_one_is_seen_at_once(self, su, dao, world):  # 15
        cs = validated(su, dao, world)
        order_consent = _order_consent(su, world.run)
        _latest_consent(dao, world)
        su.execute(
            "UPDATE consents SET withdrawn_at = now(), withdrawn_by = recorded_by WHERE consent_id = %s",
            (order_consent,),
        )
        # not committed: READ COMMITTED readers still see the consent as active
        assert [e["call_set_id"] for e in _evidence(dao, world)["eligible"]] == [cs]
        su.commit()
        assert _evidence(dao, world) == HELD

    def test_readers_racing_a_withdrawal_see_before_or_after_never_an_error_and_never_go_back(self, su, dao, world):
        cs = validated(su, dao, world)
        order_consent = _order_consent(su, world.run)
        readers = [_fresh_dao() for _ in range(3)]
        start, results, errors = threading.Event(), {}, []

        def read_loop(index, local):
            seen = []
            start.wait()
            try:
                for _ in range(12):
                    seen.append(bool(local.patient_call_set_evidence(world.reader, world.run.patient)["eligible"]))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            results[index] = seen

        threads = [threading.Thread(target=read_loop, args=(i, d)) for i, (_c, d) in enumerate(readers)]
        try:
            for t in threads:
                t.start()
            start.set()
            dao.withdraw_consent(world.reader, order_consent)
            for t in threads:
                t.join(timeout=120)
        finally:
            for c, _d in readers:
                c.close()
        assert not errors, errors
        for seen in results.values():
            assert seen == sorted(seen, reverse=True), f"eligible must never reappear once withheld: {seen}"
        assert _evidence(dao, world) == HELD
        assert state(su, cs) == "validated"


# ═══ the LATEST-consent lookup is scoped to this patient and this scope, and is deterministic on a tie ═══════
#
# Review 5 found that removing `k.patient_id = o.patient_id`, or `k.scope = o.required_scope`, or the `consent_id`
# tie-break from the latest-consent lookup survived every test. The first two would let someone else's newer ACTIVE
# consent mask this patient's withdrawn latest consent and expose evidence.


class TestTheLatestConsentIsThisPatientsAndThisScopes:
    def _withdrawn_latest(self, su, dao, world):
        cs = validated(su, dao, world)
        dao.withdraw_consent(world.reader, _latest_consent(dao, world))
        assert _evidence(dao, world) == HELD
        return cs

    def test_a_newer_active_consent_of_another_patient_cannot_mask_a_withdrawn_latest(
        self, su, dao, world
    ):  # [LATEST_PATIENT]
        self._withdrawn_latest(su, dao, world)
        other = dao.create_patient(world.reader, "Other", date(1991, 2, 2), "F")
        dao.record_consent(world.reader, other, "testing")  # newer, active, same organisation and scope
        assert _evidence(dao, world) == HELD, "another patient's consent must not decide this patient's evidence"

    def test_a_newer_withdrawn_consent_of_another_patient_does_not_hide_this_patients_evidence(
        self, su, dao, world
    ):  # [LATEST_PATIENT]
        cs = validated(su, dao, world)
        other = dao.create_patient(world.reader, "Other", date(1991, 2, 2), "F")
        dao.withdraw_consent(world.reader, dao.record_consent(world.reader, other, "testing"))
        assert [e["call_set_id"] for e in _evidence(dao, world)["eligible"]] == [cs]

    def test_a_newer_active_consent_of_another_scope_cannot_mask_a_withdrawn_latest(
        self, su, dao, world
    ):  # [LATEST_SCOPE]
        self._withdrawn_latest(su, dao, world)
        dao.record_consent(world.reader, world.run.patient, "some-other-scope")  # newer, active, same patient
        assert _evidence(dao, world) == HELD, "another scope's consent must not decide this scope's evidence"

    def test_a_newer_withdrawn_consent_of_another_scope_does_not_hide_the_required_scopes_evidence(
        self, su, dao, world
    ):  # [LATEST_SCOPE]
        cs = validated(su, dao, world)
        dao.withdraw_consent(world.reader, dao.record_consent(world.reader, world.run.patient, "some-other-scope"))
        assert [e["call_set_id"] for e in _evidence(dao, world)["eligible"]] == [cs]


class TestTheLatestConsentIsDeterministicOnATie:
    """Two consents of one patient and scope with the SAME recorded_at: the higher consent_id is the latest, whatever
    the insertion order. Rows are inserted at owner level so that both the ids and the timestamps are chosen."""

    @pytest.mark.parametrize("higher_inserted_first", [True, False])
    @pytest.mark.parametrize("higher_is_withdrawn", [True, False])
    def test_the_higher_consent_id_decides(
        self, su, dao, world, higher_inserted_first, higher_is_withdrawn
    ):  # [LATEST_TIEBREAK]
        cs = validated(su, dao, world)
        low, high = sorted([uuid.uuid4(), uuid.uuid4()])
        patient, reader = world.run.patient, world.reader.user_id
        tied_at = row(su, "SELECT max(recorded_at) + interval '1 day' FROM consents WHERE org_id = %s", (world.org,))[
            0
        ][0]
        plan = [(high, higher_is_withdrawn), (low, not higher_is_withdrawn)]
        if not higher_inserted_first:
            plan.reverse()
        for consent_id, withdrawn in plan:
            su.execute(
                "INSERT INTO consents (consent_id, patient_id, org_id, scope, recorded_by, recorded_at, withdrawn_at, withdrawn_by) "
                "VALUES (%s, %s, %s, 'testing', %s, %s, %s, %s)",
                (
                    consent_id,
                    patient,
                    world.org,
                    reader,
                    tied_at,
                    tied_at if withdrawn else None,
                    reader if withdrawn else None,
                ),
            )
        su.commit()
        assert row(
            su, "SELECT count(DISTINCT recorded_at) FROM consents WHERE consent_id = ANY(%s)", ([low, high],)
        ) == [(1,)]
        eligible = [e["call_set_id"] for e in _evidence(dao, world)["eligible"]]
        assert eligible == ([] if higher_is_withdrawn else [cs])
