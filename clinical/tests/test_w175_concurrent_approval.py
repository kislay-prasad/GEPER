"""
clinical/tests/test_w175_concurrent_approval.py
───────────────────────────────────────────────

TWO SIMULTANEOUS APPROVALS OF ONE REPORT, AND A CLAIM RECORDED WHILE ONE IS BEING SEALED.

THE QUESTION. `_approve_report` read the report's state with a plain SELECT, then (after hashing) wrote an UPDATE
whose WHERE named only the report and organisation. Under READ COMMITTED two requests could both read
'under_review'. The database trigger `enforce_content_immutability` makes approval facts write-once, so the
loser's UPDATE was refused -- but as a RAW DATABASE ERROR (P0001), not as the domain's "cannot approve", and only
because the two attempts happened to carry different `approved_at` values. This file pins what must be true:

  * exactly ONE of N simultaneous approvals succeeds, and every other one is the domain's own ValueError;
  * the report's seal (approver, time, hash) is the winner's and nobody else's, and one `report_approved` audit
    success row exists;
  * a claim cannot be appended to a report that is approved (or being approved): the content hash covers the
    claims, so a claim that arrives after the seal reads as TAMPERING to `verify_report_integrity`;
  * another organisation's approver learns nothing and changes nothing.

REAL POSTGRESQL, REAL CONNECTIONS, REAL THREADS. A rendezvous inside `_compute_report_content_hash` forces both
approvals to have read the state before either writes -- the interleaving a lock-free read-then-write allows --
and gives up after a few seconds, so a correct implementation (where the second request cannot even reach that
point because it is waiting for the first) is not made to deadlock by the test.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from clinical.data_access import BcryptHasher, DataAccess
from clinical.tests import test_w162_registration_and_consent as _w162
from clinical.tests.test_w162_registration_and_consent import _account
from clinical.tests.test_w168_review_workflow import _audit, _rows, _sealed, concur, under_review

client = _w162.client
clinical_dsn = _w162.clinical_dsn
conn = _w162.conn
dao = _w162.dao
reader = _w162.reader
reset_the_login_throttle = _w162.reset_the_login_throttle
world = _w162.world


def _connect():
    import os

    import psycopg

    return psycopg.connect(os.environ["CLINICAL_DSN"], connect_timeout=10)


def _claims(report_id):
    return _rows('SELECT claim_type FROM reviewer_claims WHERE report_id = %s ORDER BY "timestamp"', (report_id,))


def _approvals_audited(report_id):
    return [
        row
        for row in _rows(
            "SELECT user_id, org_id FROM audit_log WHERE action = 'report_approved' AND outcome = 'success' "
            "AND resource_id = %s",
            (str(report_id),),
        )
    ]


def _run(attempts, rendezvous=None):
    """Run each (label, session, fn) on its OWN connection, released together. -> {label: ('ok', result) or
    (ExceptionName, message)}. With `rendezvous`, each approval waits (up to 3s) for the others after it has READ
    the report and before it writes."""
    start = threading.Barrier(len(attempts))
    meet = threading.Barrier(rendezvous) if rendezvous else None
    out = {}
    lock = threading.Lock()

    class _Hooked(DataAccess):
        def _compute_report_content_hash(self, session, report_id):
            if meet is not None:
                try:
                    meet.wait(timeout=3)
                except threading.BrokenBarrierError:
                    pass
            return super()._compute_report_content_hash(session, report_id)

    def worker(label, session, fn):
        with _connect() as connection:
            access = _Hooked(connection, password_hasher=BcryptHasher(rounds=4))
            start.wait()
            try:
                outcome = ("ok", fn(access, session))
            except Exception as exc:  # noqa: BLE001 -- the type is what the test asserts on
                outcome = (type(exc).__name__, str(exc))
            with lock:
                out[label] = outcome

    pool = [threading.Thread(target=worker, args=a) for a in attempts]
    [t.start() for t in pool]
    [t.join(90) for t in pool]
    assert len(out) == len(attempts), f"a worker did not finish: {out}"
    return out


class TestSimultaneousApprovals:
    @pytest.mark.parametrize("threads", [2, 6])
    def test_exactly_one_succeeds_and_every_other_is_the_domains_refusal(
        self, client, world, reader, tmp_path, threads
    ):
        flow = under_review(client, world, reader, tmp_path)
        report = uuid.UUID(flow["report_id"])
        approver = world["a"]["approver"]
        claims_before = _claims(report)

        out = _run(
            [(i, approver, lambda dao_, s, r=report: dao_.approve_report(s, r)) for i in range(threads)],
            rendezvous=threads,
        )

        kinds = sorted(kind for kind, _ in out.values())
        assert kinds == ["ValueError"] * (threads - 1) + ["ok"], out
        assert all("cannot approve" in msg for kind, msg in out.values() if kind == "ValueError"), out
        state, approver_id, content_hash = _sealed(report)
        assert state == "approved" and approver_id == world["a"]["approver_id"] and content_hash
        assert len(_approvals_audited(report)) == 1
        assert _claims(report) == claims_before, "an approval attempt must not touch the claims"
        assert reader.verify_report_integrity(world["a"]["admin"], report) is True

    def test_two_different_approvers_the_recorded_approver_is_the_winner(
        self, client, world, reader, dao, conn, tmp_path
    ):
        a = world["a"]
        second_id, second = _account(dao, conn, a["org_id"], "approver2@a.example", ("Approver",))
        flow = under_review(client, world, reader, tmp_path)
        report = uuid.UUID(flow["report_id"])

        out = _run(
            [
                ("first", a["approver"], lambda d, s: d.approve_report(s, report)),
                ("second", second, lambda d, s: d.approve_report(s, report)),
            ],
            rendezvous=2,
        )

        winners = [label for label, (kind, _) in out.items() if kind == "ok"]
        assert len(winners) == 1 and {k for k, _ in out.values()} == {"ok", "ValueError"}, out
        _state, approver_id, _hash = _sealed(report)
        assert approver_id == {"first": a["approver_id"], "second": second_id}[winners[0]]
        audited = _approvals_audited(report)
        assert audited == [(approver_id, a["org_id"])]

    def test_another_organisations_approver_cannot_join_the_race_and_changes_nothing(
        self, client, world, reader, tmp_path
    ):
        flow = under_review(client, world, reader, tmp_path)
        report = uuid.UUID(flow["report_id"])
        out = _run(
            [
                ("own", world["a"]["approver"], lambda d, s: d.approve_report(s, report)),
                ("other_org", world["b"]["approver"], lambda d, s: d.approve_report(s, report)),
            ],
            rendezvous=2,
        )
        assert out["own"][0] == "ok"
        assert out["other_org"][0] == "NotFoundError", out
        assert _sealed(report)[1] == world["a"]["approver_id"]
        assert _approvals_audited(report) == [(world["a"]["approver_id"], world["a"]["org_id"])]

    def test_a_stale_second_approval_is_refused_and_the_seal_is_unchanged(self, client, world, reader, tmp_path):
        flow = under_review(client, world, reader, tmp_path)
        report = uuid.UUID(flow["report_id"])
        reader_session = world["a"]["approver"]
        reader.approve_report(reader_session, report)
        sealed = _sealed(report)
        approved_at = _rows("SELECT approved_at FROM reports WHERE id = %s", (report,))
        with pytest.raises(ValueError, match="cannot approve"):
            reader.approve_report(reader_session, report)
        assert _sealed(report) == sealed
        assert _rows("SELECT approved_at FROM reports WHERE id = %s", (report,)) == approved_at
        assert len(_approvals_audited(report)) == 1

    def test_a_second_submission_is_refused_and_the_state_stays_under_review(self, client, world, reader, tmp_path):
        flow = under_review(client, world, reader, tmp_path)
        report = uuid.UUID(flow["report_id"])
        with pytest.raises(ValueError):
            reader.submit_for_review(world["a"]["interpreter"], report)
        assert _sealed(report)[0] == "under_review"

    def test_simultaneous_submissions_move_the_report_once(self, client, world, reader, tmp_path):
        from clinical.tests.test_w168_review_workflow import draft_report

        flow = draft_report(client, world, reader, tmp_path)
        assert concur(client, world, flow).status_code == 201
        report = uuid.UUID(flow["report_id"])
        out = _run([(i, world["a"]["interpreter"], lambda d, s: d.submit_for_review(s, report)) for i in range(4)])
        assert sorted(kind for kind, _ in out.values()) == ["ValueError"] * 3 + ["ok"], out
        assert _sealed(report)[0] == "under_review"
        assert len(_audit("report_submitted_for_review", "success")) == 1


class TestAClaimCannotBeAppendedToASealedReport:
    def test_a_claim_after_approval_is_refused_and_integrity_still_holds(self, client, world, reader, tmp_path):
        flow = under_review(client, world, reader, tmp_path)
        report = uuid.UUID(flow["report_id"])
        reader.approve_report(world["a"]["approver"], report)
        claims = _claims(report)
        interpretation = uuid.UUID(flow["interpretation_id"])
        with pytest.raises(ValueError, match="approved"):
            reader.record_sole_signatory_claim(world["a"]["interpreter"], interpretation, report, "after the fact")
        with pytest.raises(ValueError, match="approved"):
            reader.record_disagreement_claim(
                world["a"]["interpreter"], interpretation, report, "1-1-A-C", "Pathogenic", "after the fact"
            )
        assert _claims(report) == claims
        assert reader.verify_report_integrity(world["a"]["admin"], report) is True

    def test_a_claim_in_flight_while_the_report_is_being_sealed_cannot_slip_past_the_hash(
        self, client, world, reader, tmp_path
    ):
        """The approval has hashed the claims; before it commits, another request tries to add one. It must wait for
        the seal and then be refused -- NOT be inserted after the hash was taken."""
        flow = under_review(client, world, reader, tmp_path)
        report = uuid.UUID(flow["report_id"])
        interpretation = uuid.UUID(flow["interpretation_id"])
        sealing = threading.Event()
        out = {}

        class _Slow(DataAccess):
            def _compute_report_content_hash(self, session, report_id):
                digest = super()._compute_report_content_hash(session, report_id)
                sealing.set()
                time.sleep(2.0)  # the window between "hash taken" and "committed"
                return digest

        def approve():
            with _connect() as connection:
                access = _Slow(connection, password_hasher=BcryptHasher(rounds=4))
                access.approve_report(world["a"]["approver"], report)

        def claim():
            sealing.wait(10)
            with _connect() as connection:
                access = DataAccess(connection, password_hasher=BcryptHasher(rounds=4))
                try:
                    access.record_sole_signatory_claim(world["a"]["interpreter"], interpretation, report, "late")
                    out["claim"] = "inserted"
                except ValueError as exc:
                    out["claim"] = f"refused: {exc}"

        pool = [threading.Thread(target=approve), threading.Thread(target=claim)]
        [t.start() for t in pool]
        [t.join(60) for t in pool]

        assert out["claim"].startswith("refused"), out
        assert len(_claims(report)) == 1, "only the claim made before approval exists"
        assert reader.verify_report_integrity(world["a"]["admin"], report) is True


class TestTheLocksWorkUnderTheApplicationsRestrictedRole:
    """`clinical_app` holds only COLUMN-level UPDATE on reports (schema.sql). FOR UPDATE / FOR SHARE need an UPDATE
    privilege on the table, and this proves a column grant is enough -- otherwise the production role would fail
    every approval and every claim while the superuser the rest of the suite connects as sailed through."""

    def _as_app(self):
        connection = _connect()
        connection.execute("SET ROLE clinical_app")
        return connection

    def test_claim_submission_and_approval_all_run_as_clinical_app(self, client, world, reader, tmp_path):
        from clinical.tests.test_w168_review_workflow import draft_report

        flow = draft_report(client, world, reader, tmp_path)
        report = uuid.UUID(flow["report_id"])
        interpretation = uuid.UUID(flow["interpretation_id"])
        a = world["a"]
        with self._as_app() as connection:
            access = DataAccess(connection, password_hasher=BcryptHasher(rounds=4))
            access.record_sole_signatory_claim(a["interpreter"], interpretation, report, "as the application role")
            access.submit_for_review(a["interpreter"], report)
            access.approve_report(a["approver"], report)
            with pytest.raises(ValueError, match="cannot approve"):
                access.approve_report(a["approver"], report)
            with pytest.raises(ValueError, match="approved"):
                access.record_sole_signatory_claim(a["interpreter"], interpretation, report, "too late")
        assert _sealed(report)[0] == "approved"
        assert len(_claims(report)) == 1
