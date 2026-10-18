"""
clinical/tests/test_w176_signoff_state.py
─────────────────────────────────────────

GET /review/reports/{report_id}/signoff-state -- WHERE A REPORT'S SIGN-OFF STANDS, READ-ONLY.

Stage 1 of recovery from an interrupted sign-off. The three steps of a sign-off (claim, submission, approval)
are separate commits, so a failure part-way leaves a report that the claim, the state or the approval each
describe differently. This route lets an authorised operator SEE that, and nothing more. It resumes nothing,
finalises nothing and writes nothing.

WHAT IS PINNED
  * it is served to Interpreter, Approver, Auditor and Administrator, and to nobody else (uniform 403, the role
    gate running BEFORE the lookup); anonymous and patient sessions get the staff 401;
  * another organisation's report, an absent one and a malformed id are the same 404;
  * every state is distinguishable: no claim, a claim recorded, under review, approved, released, retracted;
  * claims are listed with id, type, actor and time ONLY -- never a reason, variant, classification or evidence,
    and the response carries no report content and no hash VALUE (only whether one exists);
  * it changes nothing: the report row, the claims and the approval facts are byte-for-byte what they were, and
    it writes no audit row (in particular not `report_integrity_verified`, which `verify_report_integrity` would);
  * the report, the claims and the retraction flag come from ONE SQL statement (one snapshot).
"""

from __future__ import annotations

import uuid

import pytest

from clinical.admin_endpoints import ADMIN_FORBIDDEN, ADMIN_NOT_FOUND
from clinical.tests import test_w162_registration_and_consent as _w162
from clinical.tests.test_w162_registration_and_consent import MALFORMED, NOWHERE
from clinical.tests.test_w168_review_workflow import (
    ALL,
    _cookie,
    _disagree,
    _post,
    _rows,
    approved,
    concur,
    draft_report,
    under_review,
)
from clinical.tests.test_w174_human_tool_read_api import PATIENT_COOKIE, _exec, _get, _identify, _patient_cookie

client = _w162.client
clinical_dsn = _w162.clinical_dsn
conn = _w162.conn
dao = _w162.dao
reader = _w162.reader
reset_the_login_throttle = _w162.reset_the_login_throttle
world = _w162.world

READERS = ("interpreter", "approver", "auditor", "admin")
NOT_READERS = tuple(r for r in ALL if r not in READERS)
TOP_LEVEL = {
    "report_id",
    "interpretation_id",
    "state",
    "is_retracted",
    "approver_id",
    "approved_at",
    "has_content_hash",
    "claims",
}
CLAIM_KEYS = {"claim_id", "claim_type", "actor_id", "recorded_at"}
REASON = "SECRET-CLAIM-REASON-w176 do not echo"


def _url(flow):
    return f"/review/reports/{flow['report_id']}/signoff-state"


def _state(client, world, flow, who="auditor", tag="a"):
    response = _get(client, _cookie(client, world, who, tag), _url(flow))
    assert response.status_code == 200, response.text
    return response.json()


def _snapshot(report_id):
    """Everything this route must leave alone: the report row, every claim, and the audit actions it could add."""
    return (
        _rows("SELECT state, approver_id, approved_at, content_hash FROM reports WHERE id = %s", (report_id,)),
        _rows(
            'SELECT id, claim_type, actor_id, "timestamp", reason, variant_key, classification '
            'FROM reviewer_claims WHERE report_id = %s ORDER BY "timestamp", id',
            (report_id,),
        ),
        _rows(
            "SELECT count(*) FROM audit_log WHERE action IN "
            "('signoff_state_read', 'report_integrity_verified', 'record_sole_signatory', "
            "'report_submitted_for_review', 'report_approved', 'record_disagreement')"
        ),
    )


class TestWhoMayReadIt:
    @pytest.mark.parametrize("who", READERS)
    def test_each_reading_role_gets_the_state(self, client, world, reader, tmp_path, who):
        flow = draft_report(client, world, reader, tmp_path)
        body = _state(client, world, flow, who)
        assert set(body) == TOP_LEVEL
        assert body["report_id"] == flow["report_id"] and body["interpretation_id"] == flow["interpretation_id"]

    @pytest.mark.parametrize("who", NOT_READERS)
    def test_everyone_else_on_the_staff_is_refused_uniformly(self, client, world, reader, tmp_path, who):
        flow = draft_report(client, world, reader, tmp_path)
        response = _get(client, _cookie(client, world, who), _url(flow))
        assert response.status_code == 403 and response.json() == {"detail": ADMIN_FORBIDDEN}

    def test_anonymous_and_patient_sessions_get_the_staff_401(self, client, world, reader, tmp_path):
        flow = draft_report(client, world, reader, tmp_path)
        baseline = _get(client, {}, "/exceptions/worklist")
        anonymous = _get(client, {}, _url(flow))
        assert anonymous.status_code == baseline.status_code == 401 and anonymous.json() == baseline.json()
        patient = _patient_cookie(reader, world, client)
        as_patient = _get(client, {PATIENT_COOKIE: patient}, _url(flow))
        assert as_patient.status_code == 401 and as_patient.json() == baseline.json()


class TestOrganisationIsolation:
    def test_another_organisations_report_is_the_same_404_as_an_absent_or_malformed_one(
        self, client, world, reader, tmp_path
    ):
        flow = draft_report(client, world, reader, tmp_path, tag="a")
        foreign = _cookie(client, world, "interpreter", tag="b")
        cross = _get(client, foreign, _url(flow))
        nowhere = _get(client, foreign, f"/review/reports/{NOWHERE}/signoff-state")
        malformed = _get(client, foreign, f"/review/reports/{MALFORMED}/signoff-state")
        assert (
            (cross.status_code, cross.json())
            == (nowhere.status_code, nowhere.json())
            == (malformed.status_code, malformed.json())
            == (404, {"detail": ADMIN_NOT_FOUND})
        )

    def test_the_role_gate_runs_before_the_lookup(self, client, world, reader, tmp_path):
        flow = draft_report(client, world, reader, tmp_path, tag="a")
        stranger = _cookie(client, world, "nobody", tag="b")
        real = _get(client, stranger, _url(flow))
        absent = _get(client, stranger, f"/review/reports/{NOWHERE}/signoff-state")
        assert (
            (real.status_code, real.json()) == (absent.status_code, absent.json()) == (403, {"detail": ADMIN_FORBIDDEN})
        )

    def test_an_organisations_claims_never_appear_in_anothers_report(self, client, world, reader, tmp_path):
        flow_a = under_review(client, world, reader, tmp_path, tag="a")
        flow_b = draft_report(client, world, reader, tmp_path, tag="b")
        assert _state(client, world, flow_b, tag="b")["claims"] == []
        assert len(_state(client, world, flow_a, tag="a")["claims"]) == 1


class TestEveryStateIsDistinguishable:
    def test_a_draft_with_no_claim(self, client, world, reader, tmp_path):
        body = _state(client, world, draft_report(client, world, reader, tmp_path))
        assert body["state"] == "draft" and body["claims"] == []
        assert body["approver_id"] is None and body["approved_at"] is None and body["has_content_hash"] is False
        assert body["is_retracted"] is False

    def test_a_draft_with_a_claim_recorded(self, client, world, reader, tmp_path):
        flow = draft_report(client, world, reader, tmp_path)
        assert concur(client, world, flow, reason=REASON).status_code == 201
        body = _state(client, world, flow)
        assert body["state"] == "draft" and body["has_content_hash"] is False
        assert len(body["claims"]) == 1
        claim = body["claims"][0]
        assert set(claim) == CLAIM_KEYS and claim["claim_type"] == "sole_signatory"
        assert claim["actor_id"] == str(world["a"]["interpreter_id"])
        assert claim["claim_id"] and claim["recorded_at"]

    def test_under_review(self, client, world, reader, tmp_path):
        body = _state(client, world, under_review(client, world, reader, tmp_path))
        assert body["state"] == "under_review" and len(body["claims"]) == 1
        assert body["approver_id"] is None and body["has_content_hash"] is False

    def test_approved_carries_the_approver_the_time_and_the_existence_of_a_hash(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path)
        body = _state(client, world, flow)
        assert body["state"] == "approved" and body["has_content_hash"] is True
        assert body["approver_id"] == str(world["a"]["approver_id"]) and body["approved_at"]
        assert body["is_retracted"] is False

    def test_released(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path)
        released = _post(client, _cookie(client, world, "approver"), f"/review/reports/{flow['report_id']}/release")
        assert released.status_code == 201, released.text
        body = _state(client, world, flow)
        assert body["state"] == "released" and body["has_content_hash"] is True and body["approver_id"]

    def test_retracted_is_flagged_and_the_state_is_not_moved(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path)
        _identify(reader, world, "approver")
        retracted = _post(
            client,
            _cookie(client, world, "approver"),
            f"/admin/reports/{flow['report_id']}/retraction",
            {"reason": "wrong sample"},
        )
        assert retracted.status_code == 201, retracted.text
        body = _state(client, world, flow)
        assert body["is_retracted"] is True and body["state"] == "approved"

    def test_claims_of_more_than_one_kind_are_listed_oldest_first(self, client, world, reader, tmp_path):
        flow = draft_report(client, world, reader, tmp_path)
        assert concur(client, world, flow).status_code == 201
        assert _disagree(client, _cookie(client, world, "interpreter"), flow).status_code == 201
        claims = _state(client, world, flow)["claims"]
        assert [c["claim_type"] for c in claims] == ["sole_signatory", "disagree"]
        assert [c["recorded_at"] for c in claims] == sorted(c["recorded_at"] for c in claims)


class TestWhatItDoesNotReveal:
    def test_no_reason_variant_classification_hash_or_content_is_returned(self, client, world, reader, tmp_path):
        flow = draft_report(client, world, reader, tmp_path)
        assert concur(client, world, flow, reason=REASON).status_code == 201
        assert _disagree(client, _cookie(client, world, "interpreter"), flow).status_code == 201
        done = _post(client, _cookie(client, world, "interpreter"), f"/review/reports/{flow['report_id']}/submission")
        assert done.status_code == 204
        assert (
            _post(
                client, _cookie(client, world, "approver"), f"/review/reports/{flow['report_id']}/approval"
            ).status_code
            == 204
        )
        response = _get(client, _cookie(client, world, "auditor"), _url(flow))
        body = response.json()
        stored_hash = _rows("SELECT content_hash FROM reports WHERE id = %s", (flow["report_id"],))[0][0]
        text = response.text
        assert set(body) == TOP_LEVEL and all(set(c) == CLAIM_KEYS for c in body["claims"])
        assert "SECRET-CLAIM-REASON" not in text
        assert stored_hash and stored_hash not in text
        # (`has_content_hash` is the one allowed key containing "hash"; the key set was asserted exactly above.)
        for forbidden in ("reason", "variant", "classification", "evidence", "run_document", "patient"):
            assert forbidden not in text, forbidden


class TestATombstonedClaim:
    """
    A claim the retention principal has tombstoned (`tombstoned_at` / `tombstoned_by`, the same UPDATE
    `clinical/retention.py` issues when it purges an interpretation) is STILL LISTED, and listed EXACTLY as before:
    the response has no tombstone flag. That is the documented design, not an omission --
    `get_reviewer_claims` and `_compute_report_content_hash` also count every claim row, tombstoned or not, so this
    route describes the same set the report's hash was taken over. The test pins that, and that reading it neither
    changes the claim nor the report nor exposes the reason or the hash.
    """

    def _tombstone(self, claim_id, by):
        # What the retention principal's grant allows: those two columns, set together (claim_tombstone_complete).
        _exec("UPDATE reviewer_claims SET tombstoned_at = now(), tombstoned_by = %s WHERE id = %s", (by, claim_id))

    def _everything(self, report_id):
        return (
            _rows("SELECT state, approver_id, approved_at, content_hash FROM reports WHERE id = %s", (report_id,)),
            _rows(
                'SELECT id, claim_type, actor_id, "timestamp", reason, tombstoned_at, tombstoned_by '
                'FROM reviewer_claims WHERE report_id = %s ORDER BY "timestamp", id',
                (report_id,),
            ),
        )

    @pytest.mark.parametrize("stage", ["under_review", "approved"])
    def test_it_is_listed_unmarked_and_nothing_changes(self, client, world, reader, tmp_path, stage):
        flow = (under_review if stage == "under_review" else approved)(client, world, reader, tmp_path)
        report = uuid.UUID(flow["report_id"])
        before_body = _state(client, world, flow)
        assert len(before_body["claims"]) == 1
        claim_id = before_body["claims"][0]["claim_id"]

        self._tombstone(claim_id, world["a"]["admin_id"])
        tombstoned = self._everything(report)
        assert tombstoned[1][0][5] is not None and tombstoned[1][0][6] == world["a"]["admin_id"], (
            "setup: not tombstoned"
        )

        cookie = _cookie(client, world, "auditor")
        response = _get(client, cookie, _url(flow))
        assert response.status_code == 200, response.text
        body = response.json()

        # still included, and represented exactly as it was before it was tombstoned
        assert [c["claim_id"] for c in body["claims"]] == [claim_id]
        assert body["claims"] == before_body["claims"]
        assert set(body["claims"][0]) == CLAIM_KEYS and set(body) == TOP_LEVEL
        assert "tombstone" not in response.text.lower()

        # no reason, no hash value
        stored_hash = _rows("SELECT content_hash FROM reports WHERE id = %s", (report,))[0][0]
        assert "reason" not in response.text and "I read the draft and concur" not in response.text
        assert (stored_hash is None) == (stage == "under_review")
        assert body["has_content_hash"] is (stage == "approved")
        if stored_hash:
            assert stored_hash not in response.text

        # and reading it changed neither the claim (tombstone columns included) nor the report
        for _ in range(2):
            assert _get(client, cookie, _url(flow)).status_code == 200
        assert self._everything(report) == tombstoned


class TestItChangesNothing:
    def test_reading_leaves_the_report_the_claims_and_the_audit_log_as_they_were(self, client, world, reader, tmp_path):
        flow = approved(client, world, reader, tmp_path)
        cookie = _cookie(client, world, "auditor")  # log in first: a login is itself audited
        report = uuid.UUID(flow["report_id"])
        before = _snapshot(report)
        for _ in range(3):
            assert _get(client, cookie, _url(flow)).status_code == 200
        assert _snapshot(report) == before
        assert reader.verify_report_integrity(world["a"]["admin"], report) is True  # still intact afterwards

    def test_a_refused_or_missing_read_changes_nothing_either(self, client, world, reader, tmp_path):
        flow = under_review(client, world, reader, tmp_path)
        report = uuid.UUID(flow["report_id"])
        refused = _cookie(client, world, "orderer")
        foreign = _cookie(client, world, "auditor", tag="b")
        before = _snapshot(report)
        assert _get(client, refused, _url(flow)).status_code == 403
        assert _get(client, foreign, _url(flow)).status_code == 404
        assert _get(client, {}, _url(flow)).status_code == 401
        assert _snapshot(report) == before

    def test_the_dao_method_is_a_read_that_writes_no_audit_row_of_its_own(self, client, world, reader, tmp_path):
        flow = under_review(client, world, reader, tmp_path)
        report = uuid.UUID(flow["report_id"])
        before = _rows("SELECT count(*) FROM audit_log")[0][0]
        result = reader.read_signoff_state(world["a"]["auditor"], report)
        assert result["state"] == "under_review"
        assert _rows("SELECT count(*) FROM audit_log")[0][0] == before
        assert reader.read_signoff_state(world["a"]["auditor"], uuid.uuid4()) is None

    def test_the_report_the_claims_and_the_retraction_flag_come_from_one_statement(self, world, reader, monkeypatch):
        """One SELECT is one snapshot under READ COMMITTED; two would each see their own."""
        seen = []
        original = type(reader)._query

        def spy(self, sql, params=()):
            seen.append(sql)
            return original(self, sql, params)

        monkeypatch.setattr(type(reader), "_query", spy)
        reader.read_signoff_state(world["a"]["auditor"], uuid.uuid4())
        touching_state = [
            sql for sql in seen if "FROM reports" in sql and "reviewer_claims" in sql and "report_retractions" in sql
        ]
        assert len(touching_state) == 1, seen
        assert not [
            sql
            for sql in seen
            if sql not in touching_state and ("reviewer_claims" in sql or "report_retractions" in sql)
        ]
